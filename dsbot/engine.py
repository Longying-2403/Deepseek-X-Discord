"""Scheduling engine: buffer messages → decide when to act → call the model → carry out the actions.

Two paths share the same conversation history:
- Called (@mention, reply, alias): run a round right away; it doesn't use the proactive quota, only the per-person-per-minute limit applies.
- Proactive: the channel has proactive chat on, and there are new messages + (a lull, or it waited too long) + quota left + a long enough gap + not over budget.

Every round appends user (this batch of new messages) + assistant (the model's raw output) to the history and never rewrites old content,
so the next request always starts with the previous request + output, which maximizes DeepSeek's prefix-cache hits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, AsyncContextManager, Awaitable, Callable, Protocol

from .actions import dump_actions, parse_actions
from .config import AppConfig
from .files import classify, max_bytes_for, read_file
from .images import MAX_RAW_BYTES, filename_for, to_data_uri, upload_ready
from .ledger import Ledger
from .links import site_name
from .llm import LLMError
from .models import (
    Action,
    ChannelInfo,
    ChannelPlace,
    Completion,
    DocBlock,
    FileAttachment,
    FileText,
    ImageAttachment,
    IncomingMessage,
    LinkRef,
    LinkResult,
    Picture,
    SearchResult,
    Usage,
)
from .prompts import (
    DEFAULT_PERSONA,
    IMAGE_BLOCKED,
    IMAGE_BUDGET,
    IMAGE_COOLDOWN,
    IMAGE_FAILED,
    IMAGE_LIMIT,
    IMAGE_NONE,
    IMAGE_OFF,
    PERSONA_DONE,
    PERSONA_MAX_CHARS,
    PERSONA_NO_ASKER,
    PERSONA_NO_HISTORY,
    PERSONA_NO_TEXT,
    PERSONA_NOT_HERE,
    PERSONA_NOT_OWNER,
    PERSONA_RESET,
    PERSONA_TOO_LONG,
    PERSONA_UNCHANGED,
    PERSONA_UNDONE,
    SUMMARY_SYSTEM,
    build_system_prompt,
    clean_persona,
)
from .quota import QuotaBucket, SlidingWindow, TurnTracker
from .search import today_text
from .render import (
    Gallery,
    assistant_text,
    content_tokens,
    history_lines,
    last_listed,
    neutralize,
    own_text,
    render_channel_list,
    render_recall,
    render_round,
    render_search,
    render_tool_result,
)
from .settings import Settings, View
from .store import Conversation, Store
from .textutil import one_line, split_for_discord, typing_delay

log = logging.getLogger(__name__)

MAX_MESSAGES_PER_ROUND = 4  # at most this many messages are sent per round
SID_MAP_LIMIT = 4000
BAD_REQUEST = {400, 413, 422}  # errors where retrying won't help: the batch itself is the problem
ERROR_BACKOFF = 60.0  # after an ordinary error, no proactive checks in this channel for this long
SEND_FAIL_LIMIT = 3  # pause the channel after this many rounds in a row that couldn't send (e.g. no permission)
SEND_FAIL_BACKOFF = 1800.0
HOUSEKEEP_EVERY = 600.0
IDLE_EVICT = 3600.0  # channels idle this long drop their in-memory history (reloaded from the database when needed)
EMBED_WAIT = 2.0  # if the calling message has a link whose preview hasn't arrived yet, wait at most this long before replying
AUTO_READ_WINDOW = 120.0  # if the same person posted a file and calls it within this long, read that file right away
RECENT_FILES_KEEP = 30  # how many recent messages with files each channel remembers (the model can ask to read them)
MAX_READ_TARGETS = 3
MAX_DOC_IMAGES = 4  # at most this many scanned-PDF page images per round
PASTE_NAME = "message.txt"  # the file Discord turns overly long messages into
PASTE_MAX_BYTES = 1_000_000
MAX_PASTE_PREVIEWS = 3
KEEP_RECENT_TURNS = 12  # when compacting memory, the last 6 rounds (12 turns) stay untouched
DOWNLOAD_TIMEOUT = 60.0  # max wait for downloading one attachment
FORCE_COMPACT_TOKENS = 100_000  # if the context is over twice the budget and over this too, compact without waiting for enough old history
# actions after which the model is asked again. read / search / recall / persona are only allowed when it's called; image also works in proactive chat
TOOL_KINDS = ("read", "search", "recall", "persona", "image")
SEND_KINDS = ("sticker", "meme", "pick")  # what's sent is a picture: it counts as "speaking up", just like talking
RECENT_LINKS_KEEP = 30  # how many recent messages with links each channel remembers (the model can ask to open them with read)
MAX_RECALLS = 2
OWNER_EXTRA_TOKENS = 4000  # the round where dsowner calls it: it may rewrite the whole persona (up to 4000 characters), so allow more output
MAX_OUTPUT_CAP = 8000
INTERACTIONS_PER_PERSON = 3  # remember each person's last few conversations with it (to carry recent talk across channels)


class Transport(Protocol):
    def channel_info(self, channel_id: int) -> ChannelInfo: ...

    async def send(self, channel_id: int, text: str, *, reply_to: int | None, ping: bool) -> int | None: ...

    async def react(self, channel_id: int, message_id: int, emoji: str) -> bool: ...

    async def send_sticker(self, channel_id: int, name: str, *, reply_to: int | None, ping: bool) -> int | None: ...

    async def send_file(
        self, channel_id: int, data: bytes, filename: str, *, reply_to: int | None, ping: bool
    ) -> int | None: ...

    def typing(self, channel_id: int) -> AsyncContextManager[Any]: ...

    def locate(self, channel_id: int) -> ChannelPlace | None: ...


class Searcher(Protocol):
    async def search(self, query: str, *, today: str, max_chars: int, user_id: str | None = None) -> SearchResult: ...


class LinkOpener(Protocol):
    def mode(self) -> str: ...  # all: both posts on X and web pages can be opened; x: only posts on X

    def readable(self, url: str) -> bool: ...

    async def read(self, url: str, *, max_chars: int, image_side: int | None = None) -> LinkResult: ...


class PictureSource(Protocol):
    async def find(self, query: str) -> list[Picture]: ...


class MemeStore(Protocol):
    def names(self, guild_id: int) -> list[str]: ...

    def find(self, guild_id: int, wanted: str) -> Any: ...  # an object with .name when found, None otherwise

    def load(self, meme: Any) -> tuple[bytes, str] | None: ...  # (content, file name)

    def counts(self, guild_id: int) -> tuple[int, int]: ...


class LLM(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: str = "off",
        json_mode: bool = False,
        max_tokens: int | None = None,
        user_id: str | None = None,
    ) -> Completion: ...


def att_key(att: FileAttachment) -> tuple[str, int]:
    return (att.filename, att.size)


@dataclass
class FileEntry:
    """A recent message with files in the channel."""

    message: IncomingMessage
    read_turns: dict[tuple[str, int], int] = field(default_factory=dict)  # attachments read into the conversation → id of the turn holding the content
    failed: set[tuple[str, int]] = field(default_factory=set)  # attachments known to be unreadable: not read automatically again, only retried when the model asks

    def unread(self) -> list[FileAttachment]:
        """Attachments not yet fully read into the conversation and not known to be unreadable (automatic reads only read these)."""
        return [a for a in self.message.files if att_key(a) not in self.read_turns and att_key(a) not in self.failed]


@dataclass
class _Planned:
    anchor: int  # which message the content block is placed after
    message_id: int  # the message the file is in
    att: FileAttachment
    max_chars: int
    preview: bool = False
    entry: FileEntry | None = None
    partial: bool = False  # not enough budget left this round to read all of it (or some page images were left out): can be read in full later


@dataclass
class LinkEntry:
    """A recent message with links in the channel."""

    message: IncomingMessage
    read_turns: dict[str, int] = field(default_factory=dict)  # links opened into the conversation → id of the turn holding the content
    failed: set[str] = field(default_factory=set)  # links known not to open: not opened automatically again, only retried when the model asks
    titles: dict[str, str] = field(default_factory=dict)  # the titles got when opening them (so later mentions use the same name)

    def unread(self) -> list[LinkRef]:
        return [link for link in self.message.links if link.url not in self.read_turns and link.url not in self.failed]


@dataclass
class _LinkPlan:
    anchor: int  # which message the content block is placed after
    entry: LinkEntry
    link: LinkRef
    partial: bool = False  # not enough characters left this round to read all of it (or some images were left out): can be opened in full later


@dataclass
class _Budget:
    """How much can still be read in a round (including the second, file-reading step)."""

    files: int
    chars: int
    images: int
    allow_images: bool
    links: int = 0  # how many more links can be opened


def _add_detail(detail: str, note: str) -> str:
    return f"{detail}; {note}" if detail else note


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def _block_without_images(block: DocBlock) -> DocBlock:
    if not block.result.images:
        return block
    note = "the images couldn't be sent to you" if block.source == "link" else "the page images couldn't be sent to you"
    return replace(block, result=replace(block.result, images=[], detail=_add_detail(block.result.detail, note)))


def _without_images(docs: dict[int, list[DocBlock]]) -> dict[int, list[DocBlock]]:
    return {k: [_block_without_images(b) for b in v] for k, v in docs.items()}


def _sticker_label(name: str) -> str:
    """How a sticker (or meme) name is written in the prompt (the model writes it back that way, so it's matched that way too)."""
    return neutralize(one_line(name, 30))


def _norm_channel(name: str) -> str:
    return "".join(name.lower().lstrip("#＃").split())


def _ago(seconds: float) -> str:
    """Always ends in "ago": render.own_text recognizes the "(… chatted with you in #x 5 minutes ago:" heading by that."""
    if seconds < 120:
        return "a moment ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    hours = f"{seconds / 3600:.0f}"
    return f"{hours} hour ago" if hours == "1" else f"{hours} hours ago"


def _pick_lines(lines: list[str], about: str | None, budget: int) -> list[str]:
    """Which lines to pick when looking through history: with keywords, the lines that mention them (plus one line on each side), otherwise the latest ones; at most budget characters in total."""
    missed = False
    if about:
        words = [w for w in about.lower().split() if w]
        hits = [i for i, line in enumerate(lines) if any(w in line.lower() for w in words)]
        if hits:
            keep: set[int] = set()
            size = 0
            for i in reversed(hits):  # newest first
                for j in (i - 1, i, i + 1):
                    if 0 <= j < len(lines) and j not in keep:
                        keep.add(j)
                        size += len(lines[j]) + 1
                if size > budget:
                    break
            lines = [lines[k] for k in sorted(keep)]
        else:
            missed = True
    out: list[str] = []
    size = 0
    for line in reversed(lines):
        if size + len(line) + 1 > budget:
            break
        out.append(line)
        size += len(line) + 1
    out.reverse()
    if missed and out:
        out.insert(0, f'(Nothing mentioning "{one_line(about or "", 40)}" was found; here are the most recent messages)')
    return out


def plan_compaction(contents: list[Any], *, force: bool = False) -> int:
    """How many of the oldest turns to delete when compacting memory (an even number: they're deleted in pairs).

    Deletes about half by estimated token count, but the last 6 rounds stay untouched. If the old part is still under half
    (say a big file was just read), it doesn't compact yet and waits until that file is old too, so it doesn't compact every
    few rounds and invalidate the cache each time. force is for when the context really is too long.
    """
    n = len(contents) - len(contents) % 2
    keep = 2 if force else KEEP_RECENT_TURNS
    eligible = n - keep
    eligible -= eligible % 2
    if eligible < 2:
        return 0
    weights = [content_tokens(c) for c in contents[:n]]
    total = sum(weights) or 1
    if not force and sum(weights[:eligible]) < total * 0.5:
        return 0
    removed = acc = 0
    for i in range(0, eligible, 2):
        acc += weights[i] + weights[i + 1]
        removed = i + 2
        if acc >= total / 2:
            break
    return removed


@dataclass
class Interaction:
    """One conversation someone had with it in some channel (a few lines of plain text)."""

    channel_id: int
    at: float
    seq: int  # recording order (a slow round is recorded later and sorts later, so starting early doesn't make it look "already carried over")
    lines: list[str]


@dataclass
class ChannelState:
    channel_id: int
    guild_id: int
    parent_id: int | None
    bucket: QuotaBucket
    category_id: int | None = None
    pending: list[IncomingMessage] = field(default_factory=list)
    inflight: list[IncomingMessage] = field(default_factory=list)  # the batch being processed right now
    omitted: int = 0
    first_human_at: float | None = None  # when the first human message in the buffer arrived
    last_activity_at: float = 0.0  # last time someone spoke or typed
    last_eval_at: float = 0.0
    last_speak_at: float = 0.0
    mention_due: bool = False
    scheduled: bool = False
    error_until: float = 0.0
    send_failures: int = 0
    generation: int = 0  # +1 on every "clear memory"; a round that's halfway through uses it to give up writing
    turns: TurnTracker = field(default_factory=TurnTracker)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    conversation: Conversation | None = None
    sid_by_msg: dict[int, int] = field(default_factory=dict)
    msg_by_sid: dict[int, int] = field(default_factory=dict)
    recent_files: dict[int, FileEntry] = field(default_factory=dict)  # message_id -> message with files
    recent_links: dict[int, LinkEntry] = field(default_factory=dict)  # message_id -> message with links
    last_image_at: float = 0.0  # when it last looked for images online (the same channel has to wait a while before the next search)
    media_failures: int = 0  # how many times in a row pictures couldn't be sent (most likely the "Attach Files" permission is missing)
    media_blocked_until: float = 0.0  # sending pictures kept failing: no pictures until then (text works as usual)
    gallery: list[Picture] = field(default_factory=list)  # the candidate pictures found this round (pick chooses from these)
    digest_seen: dict[int, int] = field(default_factory=dict)  # person -> seq of their latest talk elsewhere that was already carried into this channel
    listed: tuple[str, ...] | None = None  # the "channels you can look through" list already written into this channel's log
    last_decision: str = ""


class Engine:
    def __init__(
        self,
        cfg: AppConfig,
        settings: Settings,
        store: Store,
        llm: LLM,
        ledger: Ledger,
        transport: Transport,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: random.Random | None = None,
        image_loader: Callable[[ImageAttachment, int], Awaitable[str | None]] | None = None,
        file_reader: Callable[[FileAttachment, int], Awaitable[FileText]] | None = None,
        searcher: Searcher | None = None,
        owner_id: int | None = None,
        link_reader: LinkOpener | None = None,
        pictures: PictureSource | None = None,
        memes: MemeStore | None = None,
    ):
        self.cfg = cfg
        self.link_reader = link_reader  # opens links (posts on X through FxTwitter, web pages through Tavily)
        self.pictures = pictures  # finds images online (needs Tavily)
        self.memes = memes  # the meme library
        self.owner_id = owner_id  # OWNER_ID: marked 〔dsowner〕 in the chat log, and the only person who can change the persona in chat
        self.ec = cfg.engine
        self.settings = settings
        self.store = store
        self.llm = llm
        self.ledger = ledger
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.image_loader = image_loader or self._load_image
        self.file_reader = file_reader or self._read_file
        self._file_slots = asyncio.Semaphore(2)  # parse at most 2 files at the same time
        self.tz = cfg.bot.timezone
        self.pricing = cfg.pricing
        self.states: dict[int, ChannelState] = {}
        self.limiter = SlidingWindow()
        self.paused_until = 0.0  # global pause on insufficient balance / bad key
        self._last_housekeep = 0.0
        self._tasks: set[asyncio.Task[Any]] = set()
        self.searcher = searcher
        self.interactions: dict[tuple[int, int], deque[Interaction]] = {}  # (server, person) -> their last few conversations with it
        self._interaction_seq = 0

    # ------------------------------------------------------------------ input

    def state(
        self, channel_id: int, guild_id: int, parent_id: int | None = None, category_id: int | None = None
    ) -> ChannelState:
        st = self.states.get(channel_id)
        if st is None:
            view = self.settings.view(guild_id, channel_id, parent_id, category_id)
            st = ChannelState(channel_id, guild_id, parent_id, QuotaBucket(view.quota_capacity), category_id)
            self.states[channel_id] = st
            return st
        if st.parent_id is None and parent_id is not None:
            st.parent_id = parent_id
        if category_id is not None or parent_id is None:
            st.category_id = category_id  # the channel may have moved to another category (a thread keeps the old value when its category can't be read)
        return st

    def _is_owner(self, m: IncomingMessage) -> bool:
        return self.owner_id is not None and m.author_id == self.owner_id and not m.is_bot

    def view(self, st: ChannelState) -> View:
        return self.settings.view(st.guild_id, st.channel_id, st.parent_id, st.category_id)

    def enabled(self, guild_id: int, channel_id: int, parent_id: int | None, category_id: int | None) -> bool:
        return bool(
            self.settings.get(
                "enabled", guild_id=guild_id, channel_id=channel_id, parent_id=parent_id, category_id=category_id
            )
        )

    async def handle_message(self, msg: IncomingMessage) -> None:
        now = self.clock()
        msg.received_at = now
        msg.from_owner = self._is_owner(msg)
        if not self.enabled(msg.guild_id, msg.channel_id, msg.parent_id, msg.category_id):
            # not in an allowed area: ignore it completely, and drop anything buffered before too
            st = self.states.get(msg.channel_id)
            if st is not None and st.pending:
                st.pending.clear()
                st.first_human_at = None
            return
        st = self.state(msg.channel_id, msg.guild_id, msg.parent_id, msg.category_id)
        view = self.view(st)

        st.pending.append(msg)
        if msg.files:
            st.recent_files[msg.message_id] = FileEntry(msg)
        if msg.links:
            st.recent_links[msg.message_id] = LinkEntry(msg)
        self._prune(st, now)

        if msg.is_bot:
            return  # other bots: context only; they don't count as turns and never trigger anything
        st.last_activity_at = now
        if st.first_human_at is None:
            st.first_human_at = now
        if st.turns.is_new_turn(msg.author_id, now, self.ec.turn_merge_seconds):
            st.bucket.on_turn(view.refill_turns, view.quota_capacity)

        if msg.addressed:
            if self.limiter.allow((msg.guild_id, msg.author_id), view.mention_rate_limit, 60.0, now):
                msg.trigger = True
                st.mention_due = True
                self._kick(st)
            else:
                log.info("Channel %s: %s is calling it too often; not responding right away this time", st.channel_id, msg.author_name)

    def handle_typing(self, channel_id: int) -> None:
        st = self.states.get(channel_id)
        if st is not None:
            st.last_activity_at = max(st.last_activity_at, self.clock())

    def find_message(self, channel_id: int, message_id: int) -> IncomingMessage | None:
        """A message that hasn't been handled yet (buffered or being processed)."""
        st = self.states.get(channel_id)
        if st is None:
            return None
        for m in (*st.pending, *st.inflight):
            if m.message_id == message_id:
                return m
        return None

    def refresh_message(self, updated: IncomingMessage) -> bool:
        """A message was edited, or Discord added the link preview: update the copy that hasn't been handled yet. Whether it called the bot stays the same."""
        m = self.find_message(updated.channel_id, updated.message_id)
        if m is None:
            return False
        m.text = updated.text
        m.images = updated.images
        m.files = updated.files
        m.stickers = updated.stickers
        m.links = updated.links  # once the preview arrives, the links get titles
        m.awaiting_embeds = updated.awaiting_embeds
        st = self.states.get(m.channel_id)
        if st is not None and m.links and m.message_id not in st.recent_links:
            st.recent_links[m.message_id] = LinkEntry(m)
        return True

    def _prune(self, st: ChannelState, now: float) -> None:
        """Drop messages that are too old from the buffer, plus anything over the limit."""
        cutoff = now - self.ec.pending_max_age_seconds
        fresh = [m for m in st.pending if m.received_at >= cutoff]
        overflow = len(fresh) - self.ec.max_pending_messages
        if overflow > 0:
            st.omitted += overflow
            fresh = fresh[overflow:]
        if len(fresh) != len(st.pending):
            st.pending = fresh
            st.first_human_at = next((m.received_at for m in st.pending if not m.is_bot), None)
        for mid in [mid for mid, e in st.recent_files.items() if e.message.received_at < cutoff]:
            del st.recent_files[mid]
        while len(st.recent_files) > RECENT_FILES_KEEP:
            del st.recent_files[next(iter(st.recent_files))]
        for mid in [mid for mid, e in st.recent_links.items() if e.message.received_at < cutoff]:
            del st.recent_links[mid]
        while len(st.recent_links) > RECENT_LINKS_KEEP:
            del st.recent_links[next(iter(st.recent_links))]

    # ------------------------------------------------------------------ scheduling

    async def tick(self) -> None:
        """Called from outside every few seconds; checks which channels should run a round."""
        now = self.clock()
        for st in list(self.states.values()):
            if st.scheduled or st.lock.locked() or not st.pending:
                continue
            if st.mention_due or self._proactive_ready(st, now):
                self._kick(st)
        if now - self._last_housekeep >= HOUSEKEEP_EVERY:
            self._last_housekeep = now
            self._housekeep(now)

    def _housekeep(self, now: float) -> None:
        self.limiter.prune(60.0, now)
        ttl = self.ec.share_recent_hours * 3600
        for key, log_ in list(self.interactions.items()):
            while log_ and now - log_[0].at > ttl:
                log_.popleft()
            if not log_:
                del self.interactions[key]
        for st in self.states.values():
            # this person has nothing left elsewhere to carry over, so "how far we got" isn't needed; newer talks get bigger seqs and are still carried
            for person in [p for p in st.digest_seen if (st.guild_id, p) not in self.interactions]:
                del st.digest_seen[person]
        for st in self.states.values():
            idle = now - max(st.last_activity_at, st.last_eval_at)
            if st.conversation is not None and not st.lock.locked() and not st.scheduled and idle > IDLE_EVICT:
                st.conversation = None

    def _kick(self, st: ChannelState) -> None:
        if st.scheduled or st.lock.locked():
            return  # the round in progress checks again when it finishes
        st.scheduled = True
        task = asyncio.get_running_loop().create_task(self._run(st))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for every round in progress to finish (used in tests and on shutdown)."""
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    def _over_budget(self, st: ChannelState, view: View, now: float) -> tuple[bool, bool]:
        guild = self.ledger.guild_spent(st.guild_id, now)
        total = self.ledger.total_spent(now)
        g_budget = view.daily_budget_usd
        t_budget = self.settings.get("global_daily_budget_usd")  # the global cap only honors the global setting
        over = guild >= g_budget or total >= t_budget
        mult = self.ec.hard_stop_multiplier
        hard = guild >= g_budget * mult or total >= t_budget * mult
        return over, hard

    def _proactive_ready(self, st: ChannelState, now: float) -> bool:
        view = self.view(st)
        if not view.enabled or not view.proactive or st.first_human_at is None:
            return False
        if now < self.paused_until or now < st.error_until:
            return False
        if not st.bucket.ready(view.quota_capacity):
            return False  # no quota: don't even check, to save tokens
        if now - st.last_speak_at < view.min_speak_gap:
            return False
        if now - st.last_eval_at < view.min_eval_interval:
            return False
        if self._over_budget(st, view, now)[0]:
            return False
        quiet = now - st.last_activity_at >= view.quiet_seconds
        waited = now - st.first_human_at >= view.max_wait_seconds
        return quiet or waited

    async def _run(self, st: ChannelState) -> None:
        async with st.lock:
            st.scheduled = False
            st.mention_due = False
            self._prune(st, self.clock())
            if not self.view(st).enabled:
                st.pending.clear()  # an area an admin just turned off: drop the buffered messages too
                st.first_human_at = None
            if st.pending and (any(m.trigger for m in st.pending) or self._proactive_ready(st, self.clock())):
                batch = list(st.pending)
                st.inflight = batch
                try:
                    await self._round(st, batch)
                except Exception:
                    log.exception("Channel %s: error while handling a round", st.channel_id)
                    # don't let the same batch cost money again on every tick: cool down first, and don't auto-retry the mentions either
                    now = self.clock()
                    st.last_eval_at = now
                    st.error_until = now + ERROR_BACKOFF
                    for m in batch:
                        m.trigger = False
                finally:
                    st.inflight = []
                    st.gallery = []  # candidate pictures can only be picked within the round they were found in
        if st.mention_due and st.pending:
            self._kick(st)

    # ------------------------------------------------------------------ one round

    def _conversation(self, st: ChannelState) -> Conversation:
        if st.conversation is None:
            st.conversation = self.store.load_conversation(st.channel_id, st.guild_id)
            st.listed = last_listed(st.conversation.turns)
            for sid, mid in self.store.load_msgmap(st.channel_id).items():
                st.msg_by_sid.setdefault(sid, mid)
                st.sid_by_msg.setdefault(mid, sid)
        return st.conversation

    def _number(self, st: ChannelState, conv: Conversation, shown: list[IncomingMessage]) -> list[tuple[int, IncomingMessage]]:
        numbered = []
        for m in shown:
            sid = conv.next_short_id
            conv.next_short_id += 1
            st.sid_by_msg[m.message_id] = sid
            st.msg_by_sid[sid] = m.message_id
            numbered.append((sid, m))
        if len(st.msg_by_sid) > SID_MAP_LIMIT:
            for sid in sorted(st.msg_by_sid)[: len(st.msg_by_sid) - SID_MAP_LIMIT + 500]:
                mid = st.msg_by_sid.pop(sid)
                if st.sid_by_msg.get(mid) == sid:
                    del st.sid_by_msg[mid]
        return numbered

    def _select(self, batch: list[IncomingMessage]) -> list[IncomingMessage]:
        """The latest N messages, plus older ones that call it (a mention must not get pushed out by lots of later messages)."""
        limit = self.ec.max_batch_messages
        recent = batch[-limit:]
        older = batch[: max(0, len(batch) - limit)]
        return [m for m in older if m.trigger] + recent

    async def _round(self, st: ChannelState, batch: list[IncomingMessage]) -> None:
        now = self.clock()
        view = self.view(st)
        generation = st.generation
        triggers = [m for m in batch if m.trigger]
        kind = "mention" if triggers else "proactive"

        over, hard = self._over_budget(st, view, now)
        if kind == "proactive" and (over or now < self.paused_until):
            return
        if kind == "mention" and (hard or now < self.paused_until):
            log.warning("Channel %s: %s, not responding to mentions", st.channel_id, "paused" if now < self.paused_until else "over budget")
            for m in triggers:
                m.trigger = False
                await self._safe_react(st, m.message_id, "⚠️")
            return

        if any(m.trigger and m.awaiting_embeds and now - m.received_at < EMBED_WAIT for m in batch):
            # previews of GIF / image links usually arrive a second or two later (the message-update event adds the image to the same object)
            await self.sleep(EMBED_WAIT)
            now = self.clock()

        conv = self._conversation(st)
        shown = self._select(batch)
        omitted = st.omitted + len(batch) - len(shown)
        numbered = self._number(st, conv, shown)
        shown_ids = {id(m) for m in shown}
        targets = [st.sid_by_msg[m.message_id] for m in triggers if id(m) in shown_ids]
        images = await self._images(shown, triggers) if view.vision else {}
        budget = _Budget(
            self.ec.max_files_per_round,
            self.ec.file_round_max_chars,
            MAX_DOC_IMAGES,
            bool(view.vision),
            links=max(0, self.ec.max_links_per_round),
        )
        docs, planned, opened = await self._round_content(st, view, kind, shown, triggers, budget, now)
        if st.generation != generation:
            # memory was cleared while files were being read: scrap this round, keep the mention, and redo it right away with the fresh memory
            st.mention_due = st.mention_due or any(m.trigger for m in batch)
            return
        info = self.transport.channel_info(st.channel_id)
        system = self._system_prompt(st, view, conv, info)
        context, digest_marks, listed = self._shared_context(st, view, triggers, now) if kind == "mention" else ([], {}, None)

        def build(with_images: bool) -> tuple[Any, list[dict[str, Any]]]:
            content = render_round(
                numbered,
                sid_by_msg=st.sid_by_msg,
                kind=kind,
                targets=targets,
                now=now,
                tz=self.tz,
                omitted=omitted,
                images=images if with_images else {},
                docs=docs if with_images else _without_images(docs),
                max_chars=self.ec.max_chars_per_message,
                context=context,
            )
            return content, self._messages(system, conv, content)

        has_images = bool(images) or any(b.result.images for blocks in docs.values() for b in blocks)
        thinking = view.mention_thinking if kind == "mention" else view.proactive_thinking
        base_tokens = self.ec.max_output_tokens if thinking == "off" else self.ec.max_thinking_tokens
        max_tokens = min(MAX_OUTPUT_CAP, base_tokens + OWNER_EXTRA_TOKENS) if any(m.from_owner for m in triggers) else None
        started = self.clock()
        try:
            user_content, completion, cost, row = await self._ask_with_fallback(
                st, kind, build, has_images, thinking, generation, max_tokens=max_tokens
            )
        except LLMError as exc:
            if exc.status in BAD_REQUEST:
                st.digest_seen.update(digest_marks)  # this batch was rejected: don't carry those lines over again either, or it would keep getting rejected
            await self._on_error(st, kind, exc, now, batch, targets, conv, info)
            return

        # finding images online also works in proactive chat (posting pictures is everyday group-chat stuff); the other look-it-up actions are only for when it's called.
        # When it can't search in proactive chat (cooling down, no searches left), treat it as not having asked, and don't ask the model again for it
        extra = ("image",) if kind == "proactive" and self._image_ready(st, view, now) else ()
        actions, stored = self._interpret(st, completion, kind, targets, allow_tools=kind == "mention", extra_tools=extra)
        if st.generation != generation:
            log.info("Channel %s: memory was cleared during this round; discarding the result", st.channel_id)
            self.store.set_outcome(row, "discarded")
            self._consume(st, batch, now)
            return

        # write this round into the history first (append-only), then consume the buffer
        self.store.append_round(conv, user_content, stored, [(sid, m.message_id) for sid, m in numbered], now=now)
        self._consume(st, batch, now)
        self._mark_read(conv, planned)
        self._mark_links(conv, opened)
        st.digest_seen.update(digest_marks)
        if listed is not None:
            st.listed = listed

        tools = [a for a in actions if a.kind in TOOL_KINDS]
        if tools:
            # the model wants to read files / open links / search the web / find images / look through other channels / change the persona first:
            # do that, then ask it again (the second step can't use these, except that if the first step didn't change the persona, the second
            # step may change it once, e.g. after reading a character card)
            self.store.set_outcome(row, "+".join(sorted({a.kind for a in tools})))
            st.last_decision = "called; looking things up first" if kind == "mention" else "wants to send a picture; looking for one first"
            allow_persona = any(m.from_owner for m in triggers) and any(a.kind == "read" for a in tools)
            step = await self._tool_step(
                st, conv, tools, targets, view, info, system, generation, budget, numbered, allow_persona, max_tokens, kind=kind
            )
            if step is None:
                return
            completion, cost, row, actions = step
            late = [a for a in actions if a.kind == "persona"]
            if late:
                self.store.set_outcome(row, "persona")
                step = await self._tool_step(
                    st, conv, late, targets, view, info, system, generation, budget, numbered, False, max_tokens
                )
                if step is None:
                    return
                completion, cost, row, actions = step

        actions, dropped = self._check_stickers(st, actions, info)
        actions, dropped_media = self._check_media(st, view, actions)
        if (dropped or dropped_media) and kind == "mention" and targets and not any(
            a.kind in ("reply", "react", *SEND_KINDS) for a in actions
        ):
            actions.append(Action("react", targets[-1], emoji="👍"))  # the sticker or meme it wanted to send can't be sent: when called, it still owes some response
        # sending a sticker, a meme or a found picture counts as "speaking up", just like talking: it uses quota and counts toward the gap between messages
        decided_reply = any((a.kind == "reply" and a.messages) or a.kind in SEND_KINDS for a in actions)
        decided_react = any(a.kind == "react" for a in actions)
        outcome = "reply" if decided_reply else ("react" if decided_react else "pass")
        if st.generation == generation:
            self._remember_interactions(st, numbered, actions, kind, targets, triggers)
        reacted = False
        failed_media: set[str] = set()
        try:
            spoke, reacted, failed_media = await self._execute(st, actions, kind, targets, elapsed=self.clock() - started)
        finally:
            # quota is spent based on the model's decision, not on whether sending worked, so a channel without permission isn't checked forever
            if decided_reply:
                st.last_speak_at = self.clock()
            if kind == "proactive":
                if decided_reply:
                    st.bucket.spend(1.0)
                elif decided_react:
                    st.bucket.spend(self.ec.react_cost)
            self.store.set_outcome(row, outcome)
            st.last_decision = self._describe(kind, outcome, actions, completion)
        decided_text = any(a.kind == "reply" and a.messages for a in actions)
        uploads = {"meme", "pick"}  # the two kinds that need the "Attach Files" permission
        self._note_media(st, bool(failed_media & uploads), any(a.kind in uploads for a in actions))
        if failed_media and not spoke and not reacted and kind == "mention" and targets:
            # the picture it wanted to send didn't go through (most likely the "Attach Files" permission is missing) and it said nothing else this round: when called, at least react
            mid = st.msg_by_sid.get(targets[-1])
            if mid is not None:
                await self._safe_react(st, mid, "👍")
        if decided_text and not spoke:
            # only failures to send text count: when it's just pictures that don't go through, text still works, so proactive chat doesn't need pausing
            st.send_failures += 1
            if st.send_failures >= SEND_FAIL_LIMIT:
                st.error_until = self.clock() + SEND_FAIL_BACKOFF
                log.warning("Channel %s: couldn't send messages for %d rounds in a row (missing permission?); pausing proactive chat for 30 minutes", st.channel_id, st.send_failures)
        elif spoke:
            st.send_failures = 0

        u = completion.usage
        log.info(
            "[%s #%s] %s → %s | in %d (hit %d) out %d | $%.5f | %s",
            info.guild_name,
            info.channel_name,
            "mention" if kind == "mention" else "proactive",
            outcome,
            u.prompt_tokens,
            u.hit,
            u.output,
            cost,
            one_line(completion.reasoning or "", 160),
        )
        if u.prompt_tokens > view.context_budget_tokens and st.generation == generation:
            # Normally it waits until old history makes up half before compacting (fewer cache misses). If it's really too long
            # (e.g. several big files read in a row), it doesn't wait: the threshold is at least 100k tokens, so even with a very
            # small budget a file that was just read isn't compacted away and then read all over again
            limit = max(2 * view.context_budget_tokens, FORCE_COMPACT_TOKENS)
            await self._compact(st, conv, info, force=u.prompt_tokens > limit)

    def _messages(self, system: str, conv: Conversation, content: Any) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        msgs += [{"role": t.role, "content": t.content} for t in conv.turns]
        msgs.append({"role": "user", "content": content})
        return msgs

    async def _ask_with_fallback(
        self,
        st: ChannelState,
        kind: str,
        build: Callable[[bool], tuple[Any, list[dict[str, Any]]]],
        has_images: bool,
        thinking: str,
        generation: int,
        *,
        max_tokens: int | None = None,
    ) -> tuple[Any, Completion, float, int]:
        """Call the model; if a request with images is rejected (400 or similar), retry once without them. Returns (user content actually sent, result, cost, usage row)."""
        content, messages = build(True)
        try:
            completion, cost, row = await self._ask(st, kind, messages, thinking, generation, max_tokens)
        except LLMError as exc:
            if exc.status not in BAD_REQUEST or not has_images or st.generation != generation:
                raise
            log.warning("Channel %s: request with images was rejected (%s); retrying without the images", st.channel_id, exc)
            content, messages = build(False)
            completion, cost, row = await self._ask(st, kind, messages, thinking, generation, max_tokens)
        return content, completion, cost, row

    def _interpret(
        self,
        st: ChannelState,
        completion: Completion,
        kind: str,
        targets: list[int],
        *,
        allow_tools: bool,
        extra_tools: tuple[str, ...] = (),
    ) -> tuple[list[Action], str]:
        """Parse the model output; returns (actions, content to write into the history)."""
        actions, ok = parse_actions(completion.content)
        if not ok:
            log.warning("Channel %s: model output isn't valid JSON: %s", st.channel_id, one_line(completion.content, 200))
            text = completion.content.strip()
            if kind == "mention" and text and not text.startswith(("{", "[")):
                actions = [Action("reply", targets[-1] if targets else None, [text[:1800]])]
            return actions, dump_actions(actions)
        tools = [a for a in actions if a.kind in TOOL_KINDS]
        if not tools:
            return actions, completion.content
        if not allow_tools:
            tools = [a for a in tools if a.kind in extra_tools]
        if tools:
            # when it wants to look things up, only these actions count this time; the history records only them, matching what actually happens
            picked = (
                [a for a in tools if a.kind == "read"][:MAX_READ_TARGETS]
                + [a for a in tools if a.kind == "search"][: max(1, self.ec.max_searches_per_round)]
                + [a for a in tools if a.kind == "recall"][:MAX_RECALLS]
                + [a for a in tools if a.kind == "image"][:1]
            )
            if not picked:
                # a persona change has to come on its own: next to reading files it waits (the step after reading can still change it), and next to
                # searching or looking through history it's dropped, so it's never based on unread content, web pages or what others wrote in other channels
                picked = [a for a in tools if a.kind == "persona"][:1]
            return picked, dump_actions(picked)
        actions = [a for a in actions if a.kind not in TOOL_KINDS]  # not called, or it already looked things up once
        return actions, dump_actions(actions)

    async def _tool_step(
        self,
        st: ChannelState,
        conv: Conversation,
        tools: list[Action],
        targets: list[int],
        view: View,
        info: ChannelInfo,
        system: str,
        generation: int,
        budget: _Budget,
        numbered: list[tuple[int, IncomingMessage]],
        allow_persona: bool = False,
        max_tokens: int | None = None,
        *,
        kind: str = "mention",
    ) -> tuple[Completion, float, int, list[Action]] | None:
        now = self.clock()
        reads = [a for a in tools if a.kind == "read"]
        plan, link_plan, file_notes, wanted = self._plan_reads(st, view, reads)
        sections: list[str] = []
        gallery: Gallery | None = None

        async def work() -> tuple[list[tuple[int, DocBlock, _Planned, FileText]], list[tuple[_LinkPlan, DocBlock, FileText]]]:
            nonlocal gallery
            results = await self._read_planned(st, plan, budget)
            opened = await self._read_links(st, link_plan, budget, now)
            for action in tools:
                if action.kind == "search":
                    sections.append(await self._search_section(st, view, action, now))
                elif action.kind == "recall":
                    sections.append(self._recall_section(st, view, action, info))
                elif action.kind == "persona":
                    sections.append(await self._persona_section(st, action, numbered, targets))
                elif action.kind == "image":
                    note, found = await self._image_section(st, view, action, now)
                    if found:
                        gallery = Gallery(action.query or "", [(p.description, p.uri) for p in found])
                    elif note:
                        sections.append(note)
            return results, opened

        if kind == "mention":
            async with self.transport.typing(st.channel_id):
                results, opened = await work()
        else:
            results, opened = await work()  # finding images in proactive chat: it hasn't decided whether to post yet, so no "typing" indicator
        if st.generation != generation:
            return None  # memory was cleared while looking things up
        blocks = [block for _, block, _, _ in results] + [block for _, block, _ in opened]
        if not reads:
            header = None
        elif wanted == {"link"}:
            header = "(The links you asked to open:)"
        elif wanted == {"file", "link"}:
            header = "(The files and links you asked to read:)"
        else:
            header = "(The files you asked to read:)"

        def build(with_images: bool) -> tuple[Any, list[dict[str, Any]]]:
            used = blocks if with_images else [_block_without_images(b) for b in blocks]
            notes: list[str] = []
            if gallery is not None and not with_images:
                st.gallery = []  # the candidates couldn't be shown to it: nothing can be sent this round
                notes.append("(The pictures found couldn't be shown to you, so no picture this time.)")
            content = render_tool_result(
                used,
                file_notes,
                sections,
                notes,
                targets=targets,
                files=bool(reads),
                header=header,
                gallery=gallery if with_images else None,
                kind=kind,
            )
            return content, self._messages(system, conv, content)

        has_images = any(b.result.images for b in blocks) or gallery is not None
        thinking = view.mention_thinking if kind == "mention" else view.proactive_thinking
        try:
            content, completion, cost, row = await self._ask_with_fallback(
                st, kind, build, has_images, thinking, generation, max_tokens=max_tokens
            )
        except LLMError as exc:
            await self._on_error(st, kind, exc, now, [], targets, conv, info)
            return None
        extra = ("persona",) if allow_persona else ()
        actions, stored = self._interpret(st, completion, kind, targets, allow_tools=False, extra_tools=extra)
        if st.generation != generation:
            self.store.set_outcome(row, "discarded")
            return None
        self.store.append_round(conv, content, stored, [], now=now)
        self._mark_read(conv, [(item, result) for _, _, item, result in results])
        self._mark_links(conv, opened)
        return completion, cost, row, actions

    def _plan_reads(
        self, st: ChannelState, view: View, reads: list[Action]
    ) -> tuple[list[_Planned], list[_LinkPlan], list[str], set[str]]:
        """The files and links a read action asks for. Returns (files to read, links to open, notes for the model, whether it's files or links being asked for)."""
        plan: list[_Planned] = []
        link_plan: list[_LinkPlan] = []
        notes: list[str] = []
        wanted: set[str] = set()
        files_off_noted = links_off_noted = False
        mode = self._links_mode(view)
        for action in reads:
            mid = st.msg_by_sid.get(action.to) if action.to is not None else None
            fentry = st.recent_files.get(mid) if mid is not None else None
            lentry = st.recent_links.get(mid) if mid is not None else None
            has_files = fentry is not None and bool(fentry.message.files)
            has_links = lentry is not None and bool(lentry.message.links)
            if not has_files and not has_links:
                notes.append(f"(#{action.to} has no readable file or link, or it's too old to get anymore)")
                continue
            if has_files:
                wanted.add("file")
            if has_links:
                wanted.add("link")
            if has_files and not view.files:
                if not files_off_noted:
                    notes.append("(Reading files is turned off, so the file can't be read)")
                    files_off_noted = True
            elif has_files:
                assert fentry is not None
                for att in fentry.message.files:
                    if att_key(att) in fentry.read_turns:
                        name = neutralize(one_line(att.filename, 60))
                        notes.append(f'(The file "{name}" from #{action.to} was already shown to you earlier; scroll up to find it)')
                    else:
                        plan.append(_Planned(mid, mid, att, self._file_chars(), entry=fentry))
            if not has_links:
                continue
            assert lentry is not None
            if mode == "off":
                if not links_off_noted:
                    notes.append("(Opening links is turned off, so for links you only see Discord's preview)")
                    links_off_noted = True
                continue
            assert self.link_reader is not None
            for link in lentry.message.links:
                name = neutralize(one_line(lentry.titles.get(link.url) or link.title or site_name(link.url), 40))
                if link.url in lentry.read_turns:
                    notes.append(f'(The link "{name}" from #{action.to} was already shown to you earlier; scroll up to find it)')
                elif not self.link_reader.readable(link.url):
                    notes.append(
                        f'(The link "{name}" from #{action.to} can\'t be opened: only posts on X can be opened here; for other sites you only see Discord\'s preview)'
                    )
                else:
                    link_plan.append(_LinkPlan(mid, lentry, link))
        return plan, link_plan, notes, wanted

    # ------------------------------------------------------------------ web search

    def _can_search(self, view: View) -> bool:
        return (
            bool(view.web_search)
            and self.searcher is not None
            and view.daily_search_limit > 0
            and self.ec.max_searches_per_round > 0
        )

    def _today(self, now: float) -> str:
        return today_text(now, self.tz)

    async def _search_section(self, st: ChannelState, view: View, action: Action, now: float) -> str:
        query = action.query or ""
        shown = neutralize(one_line(query, 60))
        if not self._can_search(view):
            return f'(You wanted to search for "{shown}", but web search isn\'t available here. Answer from what you already know, and say so if you\'re not sure.)'
        limit = int(view.daily_search_limit)
        day = self.ledger.day(now)
        if self.store.count_usage(day, guild_id=st.guild_id, kind="search") >= limit:
            return f'(You wanted to search for "{shown}", but this server has used up today\'s searches (limit: {limit}). Answer from what you already know, and say so if you\'re not sure.)'
        if self._over_budget(st, view, now)[0]:
            return f'(You wanted to search for "{shown}", but today\'s budget is used up. Answer from what you already know, and say so if you\'re not sure.)'
        assert self.searcher is not None
        # record a row first to claim a slot, then search: even if other channels search at the same time, together they can't go over the limit
        row = self.store.record_usage(ts=now, day=day, guild_id=st.guild_id, channel_id=st.channel_id, kind="search", outcome="pending")
        try:
            result = await self.searcher.search(
                query, today=self._today(now), max_chars=self.ec.search_max_chars, user_id=f"guild-{st.guild_id}"
            )
        except asyncio.CancelledError:
            self._finish_search(st, row, Usage(), "error")  # cancelled on shutdown: don't leave a row stuck at "pending"
            raise
        except Exception as exc:  # SearchError, and unexpected errors too: either way this search just didn't work
            log.warning('Channel %s: search for "%s" failed: %s', st.channel_id, one_line(query, 60), exc)
            self._finish_search(st, row, getattr(exc, "usage", None) or Usage(), "error")
            return f'(The search for "{shown}" failed. Answer from what you already know, and say so if you\'re not sure.)'
        self._finish_search(st, row, result.usage, "search")
        log.info('Channel %s: searched for "%s" (%s, %d sources)', st.channel_id, one_line(query, 60), result.provider, len(result.sources))
        return render_search(query, result.summary, result.sources, self.ec.search_max_chars)

    def _finish_search(self, st: ChannelState, row: int, usage: Usage, outcome: str) -> None:
        now = self.clock()
        cost = self.pricing.cost(usage, now)
        self.ledger.add(st.guild_id, cost, now)
        self.store.update_usage(row, outcome=outcome, hit=usage.hit, miss=usage.miss, output=usage.output, cost=cost)

    # ------------------------------------------------------------------ links

    def _links_mode(self, view: View) -> str:
        """What it can do with links: all (posts on X and web pages) / x (only posts on X; Tavily isn't set up) / off."""
        if not view.links or self.link_reader is None:
            return "off"
        return self.link_reader.mode()

    async def _round_content(
        self,
        st: ChannelState,
        view: View,
        kind: str,
        shown: list[IncomingMessage],
        triggers: list[IncomingMessage],
        budget: _Budget,
        now: float,
    ) -> tuple[dict[int, list[DocBlock]], list[tuple[_Planned, FileText]], list[tuple[_LinkPlan, DocBlock, FileText]]]:
        """The file and link contents attached directly this round. Shows "typing" when it's called (reading files and opening links takes a few seconds)."""

        async def gather() -> tuple[
            dict[int, list[DocBlock]], list[tuple[_Planned, FileText]], list[tuple[_LinkPlan, DocBlock, FileText]]
        ]:
            docs: dict[int, list[DocBlock]] = {}
            planned: list[tuple[_Planned, FileText]] = []
            if view.files:
                docs, planned = await self._round_docs(st, shown, triggers, budget)
            opened: list[tuple[_LinkPlan, DocBlock, FileText]] = []
            if kind == "mention":
                opened = await self._round_links(st, view, shown, triggers, budget, now)
                for item, block, _ in opened:
                    docs.setdefault(item.anchor, []).append(block)
            return docs, planned, opened

        if kind == "mention":
            async with self.transport.typing(st.channel_id):
                return await gather()
        return await gather()

    def _auto_links(
        self, st: ChannelState, shown: list[IncomingMessage], triggers: list[IncomingMessage]
    ) -> list[tuple[LinkEntry, int]]:
        """Links opened right away without the model asking: those in the message calling it, in the message replied to when calling it, and ones the same person posted just before (within two minutes)."""
        shown_ids = {m.message_id for m in shown}
        picked: dict[int, tuple[LinkEntry, int]] = {}
        for t in triggers:
            candidates = [st.recent_links.get(t.message_id)]
            if t.reply_to_id is not None:
                candidates.append(st.recent_links.get(t.reply_to_id))
            for entry in reversed(list(st.recent_links.values())):
                m = entry.message
                if m.author_id == t.author_id and 0 <= t.received_at - m.received_at <= AUTO_READ_WINDOW:
                    candidates.append(entry)
            for entry in candidates:
                if entry is None or not entry.unread():
                    continue
                mid = entry.message.message_id
                if mid not in picked:
                    picked[mid] = (entry, mid if mid in shown_ids else t.message_id)
        return list(picked.values())

    async def _round_links(
        self,
        st: ChannelState,
        view: View,
        shown: list[IncomingMessage],
        triggers: list[IncomingMessage],
        budget: _Budget,
        now: float,
    ) -> list[tuple[_LinkPlan, DocBlock, FileText]]:
        if not triggers or self._links_mode(view) == "off":
            return []
        assert self.link_reader is not None
        plan = [
            _LinkPlan(anchor, entry, link)
            for entry, anchor in self._auto_links(st, shown, triggers)
            for link in entry.unread()
            if self.link_reader.readable(link.url)  # without Tavily, ordinary web pages just get their preview; no need to mention it
        ]
        return await self._read_links(st, plan, budget, now)

    async def _read_links(
        self, st: ChannelState, plan: list[_LinkPlan], budget: _Budget, now: float
    ) -> list[tuple[_LinkPlan, DocBlock, FileText]]:
        """Open these links (all at once, a few seconds at most). For any that don't fit the per-round slots, the character budget or the daily limit, tell the model they weren't opened."""
        if not plan:
            return []
        assert self.link_reader is not None
        day = self.ledger.day(now)
        limit = max(0, int(self.ec.daily_link_limit))
        used = self.store.count_usage(day, guild_id=st.guild_id, kind="link")
        slots: list[tuple[_LinkPlan, FileText | None, int, int]] = []  # (link, why it wasn't opened, usage row, max characters to read)
        for item in plan:
            if budget.links <= 0:
                n = self.ec.max_links_per_round
                why = f"at most {n} link{'s' if n != 1 else ''} can be opened at a time, so this one wasn't"
                slots.append((item, FileText(error=why, transient=True), 0, 0))
            elif budget.chars < 500:
                slots.append((item, FileText(error="too much has been read this round already, so this one wasn't opened", transient=True), 0, 0))
            elif used >= limit:
                why = f"this server has used up today's link opens (limit: {limit})"
                slots.append((item, FileText(error=why, transient=True), 0, 0))
            else:
                cap = max(1, min(self.ec.link_max_chars, budget.chars))
                budget.chars -= cap
                budget.links -= 1
                used += 1
                # record a row first to claim a slot, then open: even if other channels open links at the same time, together they can't go over the limit
                row = self.store.record_usage(
                    ts=now, day=day, guild_id=st.guild_id, channel_id=st.channel_id, kind="link", outcome="pending"
                )
                slots.append((item, None, row, cap))

        async def open_one(item: _LinkPlan, cap: int) -> LinkResult:
            assert self.link_reader is not None
            side = self.ec.image_max_side if budget.allow_images and not item.link.preview_image else None
            return await self.link_reader.read(item.link.url, max_chars=cap, image_side=side)

        jobs = [(i, item, cap) for i, (item, skipped, _, cap) in enumerate(slots) if skipped is None]
        try:
            fetched = await asyncio.gather(*(open_one(item, cap) for _, item, cap in jobs), return_exceptions=True)
        except asyncio.CancelledError:
            for item, skipped, row, _ in slots:
                if skipped is None:
                    self.store.update_usage(row, outcome="error")  # cancelled on shutdown: don't leave a row stuck at "pending"
            raise
        results: dict[int, LinkResult] = {}
        for (i, item, _), got in zip(jobs, fetched):
            if isinstance(got, BaseException):
                if not isinstance(got, Exception):
                    raise got
                log.warning("Channel %s: error opening a link on %s: %r", st.channel_id, site_name(item.link.url), got)
                got = LinkResult("", FileText(error="something went wrong", transient=True))
            results[i] = got

        out: list[tuple[_LinkPlan, DocBlock, FileText]] = []
        for i, (item, skipped, row, cap) in enumerate(slots):
            title = item.link.title or ""
            if skipped is not None:
                result = skipped
            else:
                got = results[i]
                result = got.result
                title = got.title or title
                self.store.update_usage(row, outcome="error" if result.error else "link")
                budget.chars += cap - len(result.text)  # characters were reserved up to the cap; give back what wasn't used
                result, trimmed = self._fit_images(result, budget, link=True)
                # a page longer than the per-link cap, of which only the beginning could be read, doesn't count as unfinished; only running out of this round's allowance does
                item.partial = (result.truncated and cap < self.ec.link_max_chars) or trimmed
                log.info(
                    "Channel %s: opened a link on %s%s",
                    st.channel_id,
                    site_name(item.link.url),
                    f", but it failed: {result.error}" if result.error else f" ({len(result.text)} characters)",
                )
            name = title or item.entry.titles.get(item.link.url) or site_name(item.link.url) or "link"
            if not result.error:
                item.entry.titles[item.link.url] = name
            block = DocBlock(self._label(st, item.entry.message.message_id), name, result, source="link")
            out.append((item, block, result))
        return out

    def _mark_links(self, conv: Conversation, opened: list[tuple[_LinkPlan, DocBlock, FileText]]) -> None:
        """Mark opened links as "the content is in the conversation" (undone when memory compaction deletes that turn); links known not to open aren't opened automatically again."""
        if not conv.turns or not opened:
            return
        turn_id = conv.turns[-2].id if len(conv.turns) >= 2 else conv.turns[-1].id
        for item, _, result in opened:
            url = item.link.url
            if item.partial:
                continue  # only part of it was read this time: open it in full the next time it's asked for
            if result.error:
                if not result.transient:
                    item.entry.failed.add(url)
            else:
                item.entry.read_turns[url] = turn_id
                item.entry.failed.discard(url)

    # ------------------------------------------------------------------ pictures

    def _memes_on(self, view: View) -> bool:
        return bool(view.images) and self.memes is not None

    def _can_find_images(self, view: View) -> bool:
        return bool(view.images) and self.pictures is not None and int(view.daily_image_limit) > 0

    def _image_ready(self, st: ChannelState, view: View, now: float) -> bool:
        """Whether it can look for images right now: it's allowed to, this channel isn't cooling down, and there are searches left today."""
        if not self._can_find_images(view) or now < st.media_blocked_until:
            return False
        if st.last_image_at and now - st.last_image_at < self.ec.image_cooldown_seconds:
            return False
        return self.store.count_usage(self.ledger.day(now), guild_id=st.guild_id, kind="image") < int(view.daily_image_limit)

    async def _image_section(self, st: ChannelState, view: View, action: Action, now: float) -> tuple[str | None, list[Picture]]:
        """Look for images online. When found, returns (None, candidates), and the candidates are also kept in st.gallery (pick chooses from there); otherwise (note, [])."""
        query = action.query or ""
        shown = neutralize(one_line(query, 60))
        if not self._can_find_images(view):
            return IMAGE_OFF.format(query=shown), []
        if now < st.media_blocked_until:  # even if found they couldn't be sent: don't waste the credits
            return IMAGE_BLOCKED.format(query=shown), []
        limit = int(view.daily_image_limit)
        day = self.ledger.day(now)
        if self.store.count_usage(day, guild_id=st.guild_id, kind="image") >= limit:
            return IMAGE_LIMIT.format(query=shown, limit=limit), []
        if st.last_image_at and now - st.last_image_at < self.ec.image_cooldown_seconds:
            return IMAGE_COOLDOWN.format(query=shown), []
        if self._over_budget(st, view, now)[0]:
            return IMAGE_BUDGET.format(query=shown), []
        assert self.pictures is not None
        st.last_image_at = now
        # record a row first to claim a slot, then search
        row = self.store.record_usage(ts=now, day=day, guild_id=st.guild_id, channel_id=st.channel_id, kind="image", outcome="pending")
        try:
            found = await self.pictures.find(query)
        except asyncio.CancelledError:
            self.store.update_usage(row, outcome="error")
            raise
        except Exception as exc:  # TavilyError, and unexpected errors too
            log.warning('Channel %s: image search for "%s" failed: %s', st.channel_id, one_line(query, 60), exc)
            self.store.update_usage(row, outcome="error")
            return IMAGE_FAILED.format(query=shown), []
        self.store.update_usage(row, outcome="image")
        found = found[: max(1, self.ec.image_candidates)]
        if not found:
            return IMAGE_NONE.format(query=shown), []
        st.gallery = list(found)
        log.info('Channel %s: image search for "%s", showing it %d candidates', st.channel_id, one_line(query, 60), len(found))
        return None, st.gallery

    def _find_meme(self, st: ChannelState, view: View, wanted: str) -> Any:
        """Find a meme by name: first against the names in the library, then against how they're written in the prompt (marker symbols neutralized, shortened)."""
        if not self._memes_on(view) or not wanted:
            return None
        assert self.memes is not None
        found = self.memes.find(st.guild_id, wanted)
        if found is not None:
            return found
        labels = {_sticker_label(name): name for name in self.memes.names(st.guild_id)}
        real = labels.get(wanted.strip())
        if real is None:
            folded = {label.casefold(): name for label, name in labels.items()}
            real = folded.get(wanted.strip().casefold())
        return self.memes.find(st.guild_id, real) if real is not None else None

    def _note_media(self, st: ChannelState, failed: bool, tried: bool) -> None:
        """Pictures failed to send two rounds in a row (most likely the "Attach Files" permission is missing): no pictures in this channel for 30 minutes, so every round isn't wasted on them."""
        if not tried:
            return
        if not failed:
            st.media_failures = 0
            return
        st.media_failures += 1
        if st.media_failures >= 2:
            st.media_failures = 0
            st.media_blocked_until = self.clock() + SEND_FAIL_BACKOFF
            log.warning('Channel %s: pictures keep failing to send (missing the "Attach Files" permission?); no pictures for 30 minutes', st.channel_id)

    def _check_media(self, st: ChannelState, view: View, actions: list[Action]) -> tuple[list[Action], bool]:
        """A meme has to really be in the library; a pick needs an image search this round and a matching number. Anything that doesn't match is skipped. Returns (actions, whether anything was skipped)."""
        if not any(a.kind in ("meme", "pick") for a in actions):
            return actions, False
        if self.clock() < st.media_blocked_until:
            log.info("Channel %s: pictures haven't been getting through lately; not sending this one", st.channel_id)
            out = [a for a in actions if a.kind not in ("meme", "pick")]
            return out, True
        out: list[Action] = []
        for a in actions:
            if a.kind == "meme":
                meme = self._find_meme(st, view, a.name or "")
                if meme is None:
                    log.info('Channel %s: no meme called "%s" in the library; skipping', st.channel_id, one_line(a.name or "", 40))
                    continue
                a = replace(a, name=meme.name)
            elif a.kind == "pick":
                if not st.gallery or a.image is None or not 1 <= a.image <= len(st.gallery):
                    log.info("Channel %s: picture %s isn't among the ones found this round; skipping", st.channel_id, a.image)
                    continue
            out.append(a)
        return out, len(out) < len(actions)

    async def _send_media(self, st: ChannelState, action: Action, reply_to: int | None, ping: bool) -> int | None:
        """Send a sticker / meme / found picture. Returns None when it can't be sent."""
        try:
            if action.kind == "sticker":
                return await self.transport.send_sticker(st.channel_id, action.name or "", reply_to=reply_to, ping=ping)
            if action.kind == "meme":
                meme = self.memes.find(st.guild_id, action.name or "") if self.memes is not None else None
                # read the file in a worker thread: when the NAS's disk cache is cold, reading an animated GIF of a few MB takes a moment too
                loaded = await asyncio.to_thread(self.memes.load, meme) if meme is not None and self.memes is not None else None
                if loaded is None:
                    log.info('Channel %s: couldn\'t read the meme "%s" (maybe it was just deleted)', st.channel_id, action.name)
                    return None
                data, filename = loaded
                return await self.transport.send_file(st.channel_id, data, filename, reply_to=reply_to, ping=ping)
            if action.kind == "pick" and action.image is not None and 1 <= action.image <= len(st.gallery):
                picture = st.gallery[action.image - 1]
                # transparent ones get a white background (the same as what it saw) and oversized ones are shrunk; done in a worker thread, since big pictures take a second or two
                ready = await asyncio.to_thread(upload_ready, picture.data, self.ec.image_max_bytes)
                if ready is None:
                    log.info("Channel %s: found picture %d can't be sent (too big or broken)", st.channel_id, action.image)
                    return None
                data, mime = ready
                sent = await self.transport.send_file(st.channel_id, data, filename_for(mime), reply_to=reply_to, ping=ping)
                if sent is not None:
                    log.info("Channel %s: sent found picture %d (%s)", st.channel_id, action.image, picture.host)
                return sent
        except Exception:
            log.exception("Channel %s: error sending a picture", st.channel_id)
        return None

    # ------------------------------------------------------------------ cross-channel memory

    def shares_out(self, channel_id: int, place: ChannelPlace) -> bool:
        """Whether this channel's content may be shown to other channels on the same server: sharing is on, and a private channel must also have it turned on for itself."""
        view = self.settings.view(place.guild_id, channel_id, place.parent_id, place.category_id)
        if not view.enabled or not view.share_memory:
            return False
        if place.public:
            return True
        explicit = self.settings.overrides("channel", channel_id).get("share_memory")
        if explicit is None and place.parent_id is not None and not place.private_thread:
            explicit = self.settings.overrides("channel", place.parent_id).get("share_memory")
        return explicit is True

    def shared_sources(self, guild_id: int, channel_id: int) -> list[tuple[int, ChannelPlace]]:
        """The other channels whose history this channel can look through: regular channels first, then threads, each sorted by name."""
        out = []
        for cid in self.store.conversation_channels(guild_id):
            if cid == channel_id:
                continue
            place = self.transport.locate(cid)
            if place is not None and place.guild_id == guild_id and self.shares_out(cid, place):
                out.append((cid, place))
        out.sort(key=lambda item: (item[1].parent_id is not None, item[1].name, item[0]))
        return out

    def _display(self, place: ChannelPlace) -> str:
        """The channel name shown to the model. Threads are written as 'thread "title" (in #parent)', so a thread named like a channel can't pose as it."""
        name = neutralize(one_line(place.name, 40))
        if place.parent_id is None:
            return f"#{name}"
        parent = self.transport.locate(place.parent_id)
        where = f" (in #{neutralize(one_line(parent.name, 30))})" if parent is not None else ""
        return f'thread "{name}"{where}'

    def _find_source(
        self, sources: list[tuple[int, ChannelPlace]], wanted: str
    ) -> tuple[int, ChannelPlace] | list[tuple[int, ChannelPlace]] | None:
        """Find the channel to look through by name. Returns it when found; a list of candidates when same-named threads can't be told apart; None when nothing matches."""
        raw = " ".join(wanted.split()).strip()
        key = _norm_channel(wanted).rstrip("…")
        if not key:
            return None

        def names(place: ChannelPlace) -> set[str]:
            return {_norm_channel(place.name), _norm_channel(self._display(place)), _norm_channel(neutralize(place.name))}

        tests = (
            lambda p: raw in (p.name, self._display(p), neutralize(p.name)) or raw.lstrip("#＃") == p.name,  # exact match
            lambda p: key in names(p),
            lambda p: any(n.startswith(key) for n in names(p)),  # long names in the list were cut short (ending in "…")
            lambda p: any(key in n or (len(n) >= 2 and n in key) for n in names(p)),  # e.g. "games channel" finding "games"
        )
        for test in tests:
            found = [s for s in sources if test(s[1])]
            channels = [s for s in found if s[1].parent_id is None]
            if channels:
                return channels[0]  # channels are created by admins, so on a name clash the channel wins
            if len(found) == 1:
                return found[0]
            if found:
                return found  # several threads match: don't guess, so a same-named thread someone made can't pose as the real one
        return None

    def _system_prompt(self, st: ChannelState, view: View, conv: Conversation, info: ChannelInfo) -> str:
        # the channel list doesn't go here: it changes (new channels, archived threads), and in the system prompt every change would invalidate the whole cache.
        # Emoji and stickers rarely change, so they go here (the lists are sorted, so as long as they don't change the cache isn't affected)
        emojis = info.emojis[: max(0, self.ec.emoji_list_limit)]
        stickers = [_sticker_label(n) for n in info.stickers[: max(0, self.ec.sticker_list_limit)]]
        meme_names = self.memes.names(st.guild_id) if self._memes_on(view) and self.memes is not None else []
        memes = [_sticker_label(n) for n in meme_names[: max(0, self.ec.meme_list_limit)]]
        return build_system_prompt(
            guild=neutralize(one_line(info.guild_name, 60)),
            channel=neutralize(one_line(info.channel_name, 60)),
            name=neutralize(one_line(info.bot_name, 40)),
            persona=view.persona,
            summary=conv.summary,
            search=self._can_search(view),
            share=bool(view.share_memory),
            emojis=emojis,
            stickers=stickers,
            more_emojis=len(info.emojis) - len(emojis),
            more_stickers=len(info.stickers) - len(stickers),
            memes=memes,
            more_memes=len(meme_names) - len(memes),
            images=self._can_find_images(view),
            links=self._links_mode(view),
        )

    def _check_stickers(self, st: ChannelState, actions: list[Action], info: ChannelInfo) -> tuple[list[Action], bool]:
        """A sticker is only sent when its name matches one of the server's stickers (swapped for the real name); otherwise it's just skipped, not treated as "couldn't send". Returns (actions, whether anything was skipped)."""
        if not any(a.kind == "sticker" for a in actions):
            return actions, False
        names: dict[str, str] = {}
        for real in info.stickers:
            for key in (real, _sticker_label(real), real.casefold(), _sticker_label(real).casefold()):
                names.setdefault(key, real)
        out: list[Action] = []
        for a in actions:
            if a.kind == "sticker":
                wanted = (a.name or "").strip()
                real = names.get(wanted) or names.get(wanted.casefold())
                if real is None:
                    log.info('Channel %s: no sticker called "%s"; skipping', st.channel_id, one_line(wanted, 40))
                    continue
                a = replace(a, name=real)
            out.append(a)
        return out, len(out) < len(actions)

    def _persona_target(self, st: ChannelState, scope: str | None) -> tuple[str, int, str] | str:
        """Which level's persona to change: (scope, id, how to describe it); when it can't be changed, the reason (a note for the model).

        No scope: the one currently used here (the one the model sees and edits). "channel": only this channel.
        "server": the whole server; but when the persona here comes from a more specific level (the channel, the parent channel, the category), nothing
        is changed, so that one isn't copied to the whole server.
        """
        ids = {"guild_id": st.guild_id, "channel_id": st.channel_id, "parent_id": st.parent_id, "category_id": st.category_id}
        origin = self.settings.origin("persona", **ids)
        if scope == "channel":
            return "channel", st.channel_id, "this channel"
        if origin is None or origin[0] in ("guild", "global"):
            return "guild", st.guild_id, "whole server"
        where = {
            ("channel", st.channel_id): "this channel",
            ("channel", st.parent_id): "the parent channel",
            ("category", st.category_id): "the category",
        }.get(origin, "this channel")
        if scope == "server":
            return PERSONA_NOT_HERE.format(origin=where)
        return origin[0], origin[1], where

    def _inherited_persona(self, st: ChannelState, scope: str, scope_id: int) -> str:
        """The persona used when this level has none of its own (the same "level above" as in /ds persona)."""
        if scope == "guild":
            value = self.settings.get("persona")
        elif scope == "category":
            value = self.settings.get("persona", guild_id=st.guild_id)
        elif scope_id == st.channel_id:  # this channel: the thread's parent channel → category → server → global
            value = self.settings.get("persona", guild_id=st.guild_id, channel_id=st.parent_id, category_id=st.category_id)
        else:  # the channel the thread is in: category → server → global
            value = self.settings.get("persona", guild_id=st.guild_id, category_id=st.category_id)
        return clean_persona(value or "") or DEFAULT_PERSONA

    async def _persona_section(
        self, st: ChannelState, action: Action, numbered: list[tuple[int, IncomingMessage]], targets: list[int]
    ) -> str:
        """Change the persona: only accepted from a message this round in which dsowner in person calls it. The result (changed or not) goes to the model, which then replies."""
        by_sid = dict(numbered)
        to = action.to
        if to is None:
            # no message given: if dsowner called it exactly once this round, it's that one
            owners = [sid for sid in targets if sid in by_sid and self._is_owner(by_sid[sid]) and by_sid[sid].addressed]
            if len(owners) != 1:
                return PERSONA_NO_ASKER
            to = owners[0]
        asker = by_sid.get(to)
        if asker is None or to not in targets or not self._is_owner(asker) or not asker.addressed:
            log.warning("Channel %s: someone wanted it to change its persona, but #%s isn't a message in which dsowner called it this round; not changed", st.channel_id, to)
            return PERSONA_NOT_OWNER.format(to=to)

        if action.undo:
            chain = [("channel", st.channel_id), ("guild", st.guild_id)]
            if st.parent_id is not None:
                chain.append(("channel", st.parent_id))
            if st.category_id is not None:
                chain.append(("category", st.category_id))
            last = self.store.last_persona_change(chain)
            if last is None:
                return PERSONA_NO_HISTORY
            self.store.mark_persona_reverted(last["id"])  # an undone change isn't undone twice: saying "change it back" again goes one more step back
            scope, scope_id = last["scope"], last["scope_id"]
            where = {"guild": "whole server", "category": "the category"}.get(
                scope, "this channel" if scope_id == st.channel_id else "the parent channel"
            )
            restored = last["old"]
            note = PERSONA_UNDONE.format(to=to, where=where)
        else:
            if action.text is None:
                return PERSONA_NO_TEXT
            text = clean_persona(action.text)
            if len(text) > PERSONA_MAX_CHARS:
                return PERSONA_TOO_LONG.format(limit=PERSONA_MAX_CHARS)
            target = self._persona_target(st, action.scope)
            if isinstance(target, str):
                return target
            scope, scope_id, where = target
            if not text or text == self._inherited_persona(st, scope, scope_id):
                # same as the level above: don't store it separately, so later changes to the level above apply here too
                restored = None
                what = "restored the default persona (whole server)" if scope == "guild" else f"set {where} back to using the persona from the level above"
                note = PERSONA_RESET.format(to=to, what=what)
            else:
                restored = text
                note = PERSONA_DONE.format(to=to, where=where)

        old = self.settings.overrides(scope, scope_id).get("persona")
        if restored == old and not action.undo:
            return PERSONA_UNCHANGED  # already like that: nothing written, recorded or marked
        if restored is None:
            self.settings.unset(scope, scope_id, "persona")
        else:
            self.settings.set(scope, scope_id, "persona", restored)
        # every change is recorded in the database (in ./data, so it survives rebuilding the container), so "change it back" can undo it
        self.store.add_persona_change(
            ts=self.clock(),
            scope=scope,
            scope_id=scope_id,
            old=old,
            new=restored,
            by_user=asker.author_id,
            via="undo" if action.undo else "chat",
            request=one_line(asker.text, 300),
        )
        log.info(
            "Channel %s: dsowner %s in chat (%s). Request: %s\n  Before: %s\n  Now: %s",
            st.channel_id,
            "undid the last persona change" if action.undo else "changed the persona",
            where,
            one_line(asker.text, 200),
            old if old else "(none of its own; inherited from the level above)",
            restored if restored else "(none of its own; inherits from the level above)",
        )
        await self._safe_react(st, asker.message_id, "📝")  # so dsowner can see at a glance that the persona really changed
        return note

    def _recall_section(self, st: ChannelState, view: View, action: Action, info: ChannelInfo) -> str:
        shown = neutralize(one_line(action.channel or "", 40))
        if not view.share_memory:
            return f'(You wanted to look through the history of "{shown}", but memory sharing is off here, so other channels can\'t be seen.)'
        sources = self.shared_sources(st.guild_id, st.channel_id)
        match = self._find_source(sources, action.channel or "")
        if match is None:
            names = ", ".join(self._display(p) for _, p in sources[:10]) or "none"
            return f'(No channel named "{shown}" whose history you can look through. Available: {names})'
        if isinstance(match, list):
            names = ", ".join(self._display(p) for _, p in match[:10])
            return f'(Several match "{shown}": {names}. Can\'t tell which one is meant, so nothing was looked up.)'
        channel_id, place = match
        other = self.states.get(channel_id)
        conv = other.conversation if other is not None and other.conversation is not None else None
        if conv is None:
            conv = self.store.load_conversation(channel_id, st.guild_id)
        lines = history_lines([(t.role, t.content) for t in conv.turns], info.bot_name)
        lines = _pick_lines(lines, action.query, self.ec.recall_max_chars)
        return render_recall(self._display(place), conv.summary, lines, action.query)

    def _shared_context(
        self, st: ChannelState, view: View, triggers: list[IncomingMessage], now: float
    ) -> tuple[list[str], dict[int, int], tuple[str, ...] | None]:
        """Cross-channel content placed before this round when it's called: the list of channels it can look through (only
        when the list changed), and what the people calling it recently talked about with it in other channels.

        Returns (text lines, seqs of the conversations carried over, the list written this time).
        """
        if not view.share_memory:
            return [], {}, None
        lines: list[str] = []
        listed: tuple[str, ...] | None = None
        sources = self.shared_sources(st.guild_id, st.channel_id)
        names = tuple(self._display(p) for _, p in sources[: self.ec.share_list_limit])
        if names != st.listed:
            listed = names
            lines += render_channel_list(list(names), len(sources) - len(names))
        ttl = self.ec.share_recent_hours * 3600
        marks: dict[int, int] = {}
        allowed = {cid: p for cid, p in sources}
        for m in triggers:
            if m.is_bot or m.author_id in marks:
                continue
            since = st.digest_seen.get(m.author_id, 0)
            fresh = [
                item
                for item in self.interactions.get((st.guild_id, m.author_id), ())
                if item.channel_id in allowed and item.seq > since and now - item.at <= ttl
            ]
            if not fresh:
                continue
            marks[m.author_id] = max(item.seq for item in fresh)  # only the last two are carried; older ones are skipped
            for item in fresh[-2:]:
                where = self._display(allowed[item.channel_id])
                block = [f"({neutralize(one_line(m.author_name, 32))} chatted with you in {where} {_ago(now - item.at)}:"]
                block += ["  " + neutralize(line) for line in item.lines]
                block[-1] += ")"
                lines += block
        return lines, marks, listed

    def _remember_interactions(
        self,
        st: ChannelState,
        numbered: list[tuple[int, IncomingMessage]],
        actions: list[Action],
        kind: str,
        targets: list[int],
        triggers: list[IncomingMessage],
    ) -> None:
        """Record who said what to it this round and how it replied (to carry recent talk across channels)."""
        by_sid = {sid: m for sid, m in numbered}
        talks: dict[int, tuple[IncomingMessage, list[str]]] = {}
        for action in actions:
            if action.kind != "reply" or not action.messages:
                continue
            to = action.to if action.to is not None else (targets[-1] if kind == "mention" and targets else None)
            m = by_sid.get(to) if to is not None else None
            if m is not None and not m.is_bot:
                talks.setdefault(id(m), (m, []))[1].extend(action.messages)
        for m in triggers:
            talks.setdefault(id(m), (m, []))
        for m, replies in talks.values():
            said = one_line(m.text, 150) if m.text.strip() else "(sent an image or file)"
            lines = [f"{one_line(m.author_name, 32)}: {said}"] + [f"You: {one_line(r, 150)}" for r in replies[:3]]
            key = (st.guild_id, m.author_id)
            self._interaction_seq += 1
            self.interactions.setdefault(key, deque(maxlen=INTERACTIONS_PER_PERSON)).append(
                Interaction(st.channel_id, self.clock(), self._interaction_seq, lines)
            )

    # ------------------------------------------------------------------ files

    def _label(self, st: ChannelState, message_id: int) -> str:
        sid = st.sid_by_msg.get(message_id)
        return f"#{sid}" if sid is not None else "an earlier message"

    def _auto_entries(
        self, st: ChannelState, shown: list[IncomingMessage], triggers: list[IncomingMessage]
    ) -> list[tuple[FileEntry, int]]:
        """Files read right away, without the model asking: the ones in the calling message, in the message it replied to,
        and the ones the same person just posted (within two minutes).

        Returns [(message with the files, message the content block goes after)]. Attachments already read into the conversation aren't read again.
        """
        shown_ids = {m.message_id for m in shown}
        picked: dict[int, tuple[FileEntry, int]] = {}
        for t in triggers:
            candidates = [st.recent_files.get(t.message_id)]
            if t.reply_to_id is not None:
                candidates.append(st.recent_files.get(t.reply_to_id))
            for entry in reversed(list(st.recent_files.values())):
                m = entry.message
                if m.author_id == t.author_id and 0 <= t.received_at - m.received_at <= AUTO_READ_WINDOW:
                    candidates.append(entry)
            for entry in candidates:
                if entry is None or not entry.unread():
                    continue
                mid = entry.message.message_id
                if mid not in picked:
                    picked[mid] = (entry, mid if mid in shown_ids else t.message_id)
        return list(picked.values())

    async def _round_docs(
        self, st: ChannelState, shown: list[IncomingMessage], triggers: list[IncomingMessage], budget: _Budget
    ) -> tuple[dict[int, list[DocBlock]], list[tuple[_Planned, FileText]]]:
        plan: list[_Planned] = []
        auto = self._auto_entries(st, shown, triggers)
        for entry, anchor in auto:
            for att in entry.unread():
                plan.append(_Planned(anchor, entry.message.message_id, att, self._file_chars(), entry=entry))
        # Discord turns overly long messages into message.txt: if it isn't read directly, still show the model the beginning, like a normal long message
        auto_ids = {entry.message.message_id for entry, _ in auto}
        previews = 0
        for m in shown:
            if m.message_id in auto_ids:
                continue
            for att in m.files:
                if att.filename == PASTE_NAME and att.size <= PASTE_MAX_BYTES and previews < MAX_PASTE_PREVIEWS:
                    plan.append(_Planned(m.message_id, m.message_id, att, self.ec.max_chars_per_message, preview=True))
                    previews += 1
        docs: dict[int, list[DocBlock]] = {}
        done: list[tuple[_Planned, FileText]] = []
        for anchor, block, item, result in await self._read_planned(st, plan, budget):
            docs.setdefault(anchor, []).append(block)
            done.append((item, result))
        return docs, done

    def _file_chars(self) -> int:
        """Max characters to read from one file (never more than a whole round allows, or a big file could never be read in full and would be re-read every time)."""
        return max(1, min(self.ec.file_max_chars, self.ec.file_round_max_chars))

    def _precheck(self, att: FileAttachment) -> FileText | None:
        """Files known to be unreadable without downloading them (unsupported format, too big); they don't use up the per-round file limit."""
        kind, reason = classify(att.filename, att.content_type)
        if kind == "unsupported":
            return FileText(error=reason)
        limit = max_bytes_for(kind, self.ec.file_max_bytes)
        if att.size and att.size > limit:
            return FileText(error=f"file too large ({_size(att.size)}); the limit is {_size(limit)}")
        return None

    def _fit_images(self, result: FileText, budget: _Budget, *, link: bool = False) -> tuple[FileText, bool]:
        """Returns (result, whether images were left out because this round already had too many). link: images from an opened link (worded differently)."""
        if not result.images:
            return result, False
        what, unit = ("images", "image") if link else ("page images", "page")
        if not budget.allow_images:
            return replace(result, images=[], detail=_add_detail(result.detail, f"vision is off, so no {what} were attached")), False
        keep = result.images[: max(0, budget.images)]
        budget.images -= len(keep)
        if len(keep) == len(result.images):
            return result, False
        note = (
            f"too many images this round, only {len(keep)} {unit}{'s' if len(keep) != 1 else ''} attached"
            if keep
            else f"too many images this round, so no {what} were attached"
        )
        return replace(result, images=keep, detail=_add_detail(result.detail, note)), True

    async def _read_planned(
        self, st: ChannelState, plan: list[_Planned], budget: _Budget
    ) -> list[tuple[int, DocBlock, _Planned, FileText]]:
        out: list[tuple[int, DocBlock, _Planned, FileText]] = []
        for item in plan:
            result = self._precheck(item.att)
            if result is None:
                if not item.preview and budget.files <= 0:
                    n = self.ec.max_files_per_round
                    result = FileText(error=f"at most {n} file{'s' if n != 1 else ''} can be read at once, so this one wasn't read", transient=True)
                elif budget.chars < 500:
                    result = FileText(error="too much has already been read this round, so this one wasn't read", transient=True)
                else:
                    cap = min(item.max_chars, budget.chars)
                    result = await self.file_reader(item.att, cap)
                    if not item.preview:
                        budget.files -= 1
                    budget.chars -= len(result.text)
                    result, trimmed = self._fit_images(result, budget)
                    # a short file that fit isn't partial; neither is one longer than the per-file limit, which can only ever be read up to that limit
                    item.partial = (result.truncated and cap < item.max_chars) or trimmed
            block = DocBlock(self._label(st, item.message_id), item.att.filename, result)
            out.append((item.anchor, block, item, result))
        return out

    def _mark_read(self, conv: Conversation, done: list[tuple[_Planned, FileText]]) -> None:
        """Mark attachments read in full as "already in the conversation" (remembering the turn, undone when compaction deletes it);
        record the ones known to be unreadable (unsupported format, broken file) so they aren't read automatically again.
        Temporary failures and partial reads are read again next time."""
        if not conv.turns:
            return
        turn_id = conv.turns[-2].id if len(conv.turns) >= 2 else conv.turns[-1].id
        for item, result in done:
            if item.entry is None or item.preview:
                continue
            key = att_key(item.att)
            if result.error:
                if not result.transient:
                    item.entry.failed.add(key)
            elif not item.partial:
                item.entry.read_turns[key] = turn_id
                item.entry.failed.discard(key)

    async def _read_file(self, att: FileAttachment, max_chars: int) -> FileText:
        kind, reason = classify(att.filename, att.content_type)
        if kind == "unsupported":
            return FileText(error=reason)
        limit = max_bytes_for(kind, self.ec.file_max_bytes)
        if att.size and att.size > limit:
            return FileText(error=f"file too large ({_size(att.size)}); the limit is {_size(limit)}")
        if att.fetch is None:
            return FileText(error="couldn't get this file")
        try:
            raw = await asyncio.wait_for(att.fetch(), DOWNLOAD_TIMEOUT)
        except Exception as exc:
            log.info("Downloading file %s failed: %r", att.filename, exc)
            return FileText(error="download failed (the file may have been deleted)", transient=True)
        if len(raw) > limit:
            return FileText(error=f"file too large ({_size(len(raw))}); the limit is {_size(limit)}")
        async with self._file_slots:
            return await read_file(raw, att.filename, att.content_type, max_chars, self.ec.file_timeout_seconds)

    def _consume(self, st: ChannelState, batch: list[IncomingMessage], now: float) -> None:
        consumed = {id(m) for m in batch}
        st.pending = [m for m in st.pending if id(m) not in consumed]
        st.omitted = 0
        st.first_human_at = next((m.received_at for m in st.pending if not m.is_bot), None)
        st.last_eval_at = now
        st.error_until = 0.0

    async def _ask(
        self,
        st: ChannelState,
        kind: str,
        messages: list[dict[str, Any]],
        thinking: str,
        generation: int,
        max_tokens: int | None = None,
    ) -> tuple[Completion, float, int]:
        """Call the model; show "typing…" when called, and retry once with thinking off on an empty reply (or when the reasoning used up all the tokens)."""
        if kind != "mention":
            return await self._call(st, kind, messages, thinking, max_tokens)
        async with self.transport.typing(st.channel_id):
            completion, cost, row = await self._call(st, kind, messages, thinking, max_tokens)
            empty = not completion.content.strip() or (
                completion.finish_reason == "length" and not parse_actions(completion.content)[1]
            )
            if empty and st.generation == generation:  # if memory was just cleared, don't spend money again
                self.store.set_outcome(row, "empty")
                completion, cost, row = await self._call(st, kind, messages, "off", max_tokens)
        return completion, cost, row

    async def _call(
        self, st: ChannelState, kind: str, messages: list[dict[str, Any]], thinking: str, max_tokens: int | None = None
    ) -> tuple[Completion, float, int]:
        if max_tokens is None:
            max_tokens = self.ec.max_output_tokens if thinking == "off" else self.ec.max_thinking_tokens
        completion = await self.llm.complete(
            messages, thinking=thinking, json_mode=True, max_tokens=max_tokens, user_id=f"guild-{st.guild_id}"
        )
        now = self.clock()
        cost = self.pricing.cost(completion.usage, now)
        self.ledger.add(st.guild_id, cost, now)
        u = completion.usage
        # record the spend right away, so the database and the in-memory ledger agree whatever goes wrong later
        row = self.store.record_usage(
            ts=now,
            day=self.ledger.day(now),
            guild_id=st.guild_id,
            channel_id=st.channel_id,
            kind=kind,
            outcome="pending",
            hit=u.hit,
            miss=u.miss,
            output=u.output,
            reasoning=u.reasoning,
            cost=cost,
        )
        return completion, cost, row

    async def _on_error(
        self,
        st: ChannelState,
        kind: str,
        exc: LLMError,
        now: float,
        batch: list[IncomingMessage],
        targets: list[int],
        conv: Conversation,
        info: ChannelInfo,
    ) -> None:
        log.warning("Channel %s: DeepSeek call failed: %s", st.channel_id, exc)
        st.last_eval_at = now
        for m in batch:
            m.trigger = False  # only clear this batch's mentions; mentions that came in during the call are handled as usual
        if exc.status == 402:
            self.paused_until = now + 600
            log.error("DeepSeek balance is insufficient; pausing for 10 minutes. Top up at platform.deepseek.com.")
        elif exc.status == 401:
            self.paused_until = now + 3600
            log.error("DeepSeek API key is invalid; pausing for 1 hour. Check DEEPSEEK_API_KEY in .env.")
        elif exc.status in BAD_REQUEST:
            # the batch itself was rejected (e.g. content moderation): drop it so the whole channel doesn't get stuck
            log.error("Channel %s: DeepSeek rejected this batch of messages; dropped it: %s", st.channel_id, exc)
            self._consume(st, batch, now)
            text = str(exc).lower()
            too_big = exc.status == 413 or any(word in text for word in ("context", "length", "token", "too large", "too many"))
            if too_big and st.conversation is conv:
                await self._compact(st, conv, info, force=True)
        else:
            st.error_until = now + ERROR_BACKOFF
        self.store.record_usage(
            ts=now,
            day=self.ledger.day(now),
            guild_id=st.guild_id,
            channel_id=st.channel_id,
            kind=kind,
            outcome="error",
        )
        st.last_decision = f"error: {exc}"
        if kind == "mention" and targets:
            mid = st.msg_by_sid.get(targets[-1])
            if mid is not None:
                await self._safe_react(st, mid, "⚠️")

    async def _safe_react(self, st: ChannelState, message_id: int, emoji: str) -> bool:
        try:
            return await self.transport.react(st.channel_id, message_id, emoji)
        except Exception:
            log.exception("Channel %s: error while adding a reaction", st.channel_id)
            return False

    async def _execute(
        self, st: ChannelState, actions: list[Action], kind: str, targets: list[int], elapsed: float
    ) -> tuple[bool, bool, set[str]]:
        """Returns (whether anything was sent, whether it reacted, which kinds of pictures failed to send: sticker / meme / pick)."""
        spoke = reacted = False
        failed: set[str] = set()
        sent_count = 0
        pinged: set[int] = set()  # ping the same message only once (e.g. when it both replies and sends a sticker)
        for action in actions:
            if action.kind == "react":
                mid = st.msg_by_sid.get(action.to) if action.to is not None else None
                if mid is not None and action.emoji:
                    reacted = await self._safe_react(st, mid, action.emoji) or reacted
                continue

            to = action.to
            if to is None and kind == "mention" and targets:
                to = targets[-1]
            reply_mid = st.msg_by_sid.get(to) if to is not None else None
            ping = kind == "mention" and to in targets and reply_mid not in pinged
            if ping and reply_mid is not None:
                pinged.add(reply_mid)
            if action.kind in SEND_KINDS:
                if sent_count >= MAX_MESSAGES_PER_ROUND or (action.kind != "pick" and not action.name):
                    continue
                delay = self.ec.typing_min_seconds
                if sent_count == 0:
                    delay = max(0.5, delay - elapsed)
                async with self.transport.typing(st.channel_id):
                    await self.sleep(delay)
                sent = await self._send_media(st, action, reply_mid, ping)
                if sent is not None:
                    spoke = True
                else:
                    failed.add(action.kind)
                sent_count += 1
                continue
            for i, text in enumerate(action.messages):
                if sent_count >= MAX_MESSAGES_PER_ROUND:
                    break
                delay = typing_delay(
                    text,
                    self.ec.typing_chars_per_second,
                    self.ec.typing_min_seconds,
                    self.ec.typing_max_seconds,
                    self.rng,
                )
                if sent_count == 0:
                    delay = max(0.5, delay - elapsed)  # the model's generation time counts as "typing" too
                async with self.transport.typing(st.channel_id):
                    await self.sleep(delay)
                for j, chunk in enumerate(split_for_discord(text)):
                    first = i == 0 and j == 0
                    try:
                        sent = await self.transport.send(
                            st.channel_id, chunk, reply_to=reply_mid if first else None, ping=ping and first
                        )
                    except Exception:
                        log.exception("Channel %s: error while sending a message", st.channel_id)
                        sent = None
                    if sent is not None:
                        spoke = True
                sent_count += 1
        return spoke, reacted, failed

    async def _images(
        self, shown: list[IncomingMessage], triggers: list[IncomingMessage]
    ) -> dict[int, list[tuple[str, str]]]:
        out: dict[int, list[tuple[str, str]]] = {}
        budget = self.ec.max_images_per_round
        trigger_ids = {m.message_id for m in triggers}
        # first the images, GIFs and stickers people posted (most recent first); link-preview images only if there are slots left
        for previews in (False, True):
            for m in reversed(shown):
                for att in m.images:
                    if (att.kind == "preview") != previews:
                        continue
                    if budget <= 0:
                        return out
                    wanted = m.message_id in trigger_ids  # images from the person calling it get a closer look; the rest just a rough one
                    side = self.ec.image_max_side if wanted else self.ec.low_detail_max_side
                    uri = await self.image_loader(att, side)
                    if uri is None:
                        continue
                    budget -= 1
                    out.setdefault(m.message_id, []).append((uri, "auto" if wanted else "low"))
        return out

    async def _load_image(self, att: ImageAttachment, max_side: int) -> str | None:
        if att.fetch is None or att.size > MAX_RAW_BYTES:
            return None
        try:
            raw = await asyncio.wait_for(att.fetch(), DOWNLOAD_TIMEOUT)
        except Exception as exc:
            log.info("Downloading image failed: %r", exc)
            return None
        return await asyncio.to_thread(to_data_uri, raw, max_side)

    async def _compact(self, st: ChannelState, conv: Conversation, info: ChannelInfo, *, force: bool = False) -> None:
        """Compact the oldest part of the conversation into a summary. Only this one request misses the cache."""
        remove = plan_compaction([t.content for t in conv.turns], force=force)
        if remove < 2:
            return
        generation = st.generation
        lines = []
        for t in conv.turns[:remove]:
            if t.role == "user":
                # recent talk from other channels, looked-up history and search results stay out of the summary: once those channels are cleared or stop sharing, this channel shouldn't keep remembering them
                lines.append(own_text(t.content))
            else:
                raw = t.content if isinstance(t.content, str) else json.dumps(t.content, ensure_ascii=False)
                said = assistant_text(raw, info.bot_name)
                if said:
                    lines.append(said)
        prompt = [
            {"role": "system", "content": SUMMARY_SYSTEM.format(name=info.bot_name, limit=self.ec.summary_max_chars)},
            {
                "role": "user",
                "content": f"【Previous summary】\n{conv.summary or '(none)'}\n\n【Chat history】\n" + "\n".join(lines),
            },
        ]
        now = self.clock()
        try:
            completion = await self.llm.complete(
                prompt, thinking="off", json_mode=False, max_tokens=2000, user_id=f"guild-{st.guild_id}"
            )
        except LLMError as exc:
            log.warning("Channel %s: compacting into a summary failed: %s", st.channel_id, exc)
            return
        cost = self.pricing.cost(completion.usage, now)
        self.ledger.add(st.guild_id, cost, now)
        self.store.record_usage(
            ts=now,
            day=self.ledger.day(now),
            guild_id=st.guild_id,
            channel_id=st.channel_id,
            kind="summary",
            outcome="summary",
            hit=completion.usage.hit,
            miss=completion.usage.miss,
            output=completion.usage.output,
            cost=cost,
        )
        summary = completion.content.strip()
        if not summary or st.generation != generation:
            return
        last_removed = conv.turns[remove - 1].id
        self.store.compact(conv, summary[: self.ec.summary_max_chars * 2], remove)
        st.listed = last_listed(conv.turns)  # the list may have been in the compacted part; if so, it's written again the next time it's called
        for entry in st.recent_files.values():
            # file contents were compacted into the summary with the old history, so they must be readable again when asked about
            entry.read_turns = {k: v for k, v in entry.read_turns.items() if v > last_removed}
        for link_entry in st.recent_links.values():
            link_entry.read_turns = {k: v for k, v in link_entry.read_turns.items() if v > last_removed}
        log.info("Channel %s: compacted %d old turns into a summary", st.channel_id, remove)

    @staticmethod
    def _describe(kind: str, outcome: str, actions: list[Action], completion: Completion) -> str:
        if outcome == "reply":
            said = next((a.messages[0] for a in actions if a.kind == "reply" and a.messages), "")
            media = next((a for a in actions if a.kind in SEND_KINDS), None)
            if said or media is None:
                text = f"said: {one_line(said, 40)}"
            elif media.kind == "pick":
                text = "sent a picture found online"
            else:
                text = f"sent {'a sticker' if media.kind == 'sticker' else 'a meme'}: {one_line(media.name or '', 40)}"
        elif outcome == "react":
            text = "reacted " + " ".join(a.emoji or "" for a in actions if a.kind == "react")
        else:
            text = "stayed quiet"
        prefix = "called" if kind == "mention" else "proactive check"
        reason = one_line(completion.reasoning or "", 80)
        return f"{prefix}, {text}" + (f" (thoughts: {reason})" if reason else "")

    # ------------------------------------------------------------------ for the commands

    def reset_channel(self, channel_id: int) -> None:
        self.store.reset_conversation(channel_id)
        st = self.states.get(channel_id)
        if st is not None:
            st.generation += 1
            st.conversation = None
            for entry in st.recent_files.values():
                entry.read_turns.clear()  # what was read got cleared along with the memory
                entry.failed.clear()
            for link_entry in st.recent_links.values():
                link_entry.read_turns.clear()
                link_entry.failed.clear()
            st.digest_seen.clear()
            st.listed = None
        for key, log_ in list(self.interactions.items()):
            # this channel's memory was cleared, so other channels shouldn't "remember" what was said here either
            kept = [item for item in log_ if item.channel_id != channel_id]
            if kept:
                self.interactions[key] = deque(kept, maxlen=INTERACTIONS_PER_PERSON)
            else:
                del self.interactions[key]

    def status(
        self, guild_id: int, channel_id: int, parent_id: int | None, category_id: int | None = None
    ) -> dict[str, Any]:
        now = self.clock()
        st = self.states.get(channel_id)
        view = self.settings.view(guild_id, channel_id, parent_id, category_id)
        if st is not None:
            st.bucket.ready(view.quota_capacity)  # clamp to the current cap first
        return {
            "tokens": st.bucket.tokens if st else float(view.quota_capacity),
            "turns_since_refill": st.bucket.turns_since_refill if st else 0,
            "pending": len(st.pending) if st else 0,
            "last_speak_ago": (now - st.last_speak_at) if st and st.last_speak_at else None,
            "last_decision": st.last_decision if st else "",
            "guild_spent": self.ledger.guild_spent(guild_id, now),
            "total_spent": self.ledger.total_spent(now),
            "paused": now < self.paused_until,
            "send_blocked": bool(st and st.send_failures >= SEND_FAIL_LIMIT and now < st.error_until),
            "day": self.ledger.day(now),
            "searches": self.store.count_usage(self.ledger.day(now), guild_id=guild_id, kind="search"),
            "can_search": self._can_search(view),
            "images_found": self.store.count_usage(self.ledger.day(now), guild_id=guild_id, kind="image"),
            "links_opened": self.store.count_usage(self.ledger.day(now), guild_id=guild_id, kind="link"),
            "can_find_images": self._can_find_images(view),
            "memes_on": self._memes_on(view),
            "meme_counts": self.memes.counts(guild_id) if self.memes is not None else (0, 0),
            "links_mode": self._links_mode(view),
            "media_blocked_for": max(0.0, st.media_blocked_until - now) if st else 0.0,
        }
