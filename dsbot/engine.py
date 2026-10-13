"""调度引擎：缓冲消息 → 判断时机 → 调用模型 → 执行动作。

两条路径共用同一份对话历史：
- 被叫到（@、回复、别名）：立刻跑一轮，不消耗主动额度，只受“每人每分钟”限速。
- 主动：频道开了主动发言，并且 有新消息 + (冷场 或 等太久) + 有额度 + 间隔够长 + 没超预算 时跑一轮。

每一轮固定往历史里追加 user（这批新消息）+ assistant（模型原样输出），从不改写旧内容，
所以下一轮的请求总是以上一轮的请求 + 输出开头，最大化 DeepSeek 的前缀缓存命中。
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

MAX_MESSAGES_PER_ROUND = 4  # 一轮最多发几条消息
SID_MAP_LIMIT = 4000
BAD_REQUEST = {400, 413, 422}  # 重试也没用的错误：这批消息本身有问题
ERROR_BACKOFF = 60.0  # 普通错误后，这个频道多久内不主动判断
SEND_FAIL_LIMIT = 3  # 连续几轮发不出去（比如没权限）就暂停这个频道
SEND_FAIL_BACKOFF = 1800.0
HOUSEKEEP_EVERY = 600.0
IDLE_EVICT = 3600.0  # 闲置这么久的频道，把内存里的对话记录先放掉（需要时再从数据库读）
EMBED_WAIT = 2.0  # 叫它的消息带链接、预览还没到时，最多等这么久再回
AUTO_READ_WINDOW = 120.0  # 同一个人发了文件、这么久之内又叫它，就直接读那个文件
RECENT_FILES_KEEP = 30  # 每个频道记住最近多少条带文件的消息（模型可以用 read 要求读）
MAX_READ_TARGETS = 3
MAX_DOC_IMAGES = 4  # 一轮里扫描版 PDF 最多附几张页面图
PASTE_NAME = "message.txt"  # Discord 把太长的消息自动转成的文件
PASTE_MAX_BYTES = 1_000_000
MAX_PASTE_PREVIEWS = 3
KEEP_RECENT_TURNS = 12  # 压缩记忆时，最近 6 轮（12 条）不动
DOWNLOAD_TIMEOUT = 60.0  # 下载一个附件最多等多久
FORCE_COMPACT_TOKENS = 100_000  # 上下文超过预算两倍、也超过这么多时，不等旧记录攒够就压缩
# 用完要再问一次模型的动作。read / search / recall / persona 只在被叫到时能用；image 主动聊天时也能用
TOOL_KINDS = ("read", "search", "recall", "persona", "image")
SEND_KINDS = ("sticker", "meme", "pick")  # 发出去的是一张图：和说话一样算“开口了”
RECENT_LINKS_KEEP = 30  # 每个频道记住最近多少条带链接的消息（模型可以用 read 要求打开）
MAX_RECALLS = 2
OWNER_EXTRA_TOKENS = 4000  # dsowner 叫它的那一轮：可能要整段重写人设（最多 4000 字），输出上限多给一些
MAX_OUTPUT_CAP = 8000
INTERACTIONS_PER_PERSON = 3  # 每个人记住最近几次跟它的对话（跨频道带近况用）


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
    def mode(self) -> str: ...  # all：推文和网页都能打开；x：只能打开推文

    def readable(self, url: str) -> bool: ...

    async def read(self, url: str, *, max_chars: int, image_side: int | None = None) -> LinkResult: ...


class PictureSource(Protocol):
    async def find(self, query: str) -> list[Picture]: ...


class MemeStore(Protocol):
    def names(self, guild_id: int) -> list[str]: ...

    def find(self, guild_id: int, wanted: str) -> Any: ...  # 找到返回有 .name 的对象，找不到返回 None

    def load(self, meme: Any) -> tuple[bytes, str] | None: ...  # (内容, 文件名)

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
    """频道里最近一条带文件的消息。"""

    message: IncomingMessage
    read_turns: dict[tuple[str, int], int] = field(default_factory=dict)  # 读进对话的附件 → 内容所在那条记录的 id
    failed: set[tuple[str, int]] = field(default_factory=set)  # 确定读不了的附件：不再自动读，模型要求时才再试

    def unread(self) -> list[FileAttachment]:
        """还没完整读进对话、也不是确定读不了的附件（自动读只读这些）。"""
        return [a for a in self.message.files if att_key(a) not in self.read_turns and att_key(a) not in self.failed]


@dataclass
class _Planned:
    anchor: int  # 内容块放在哪条消息后面
    message_id: int  # 文件所在的消息
    att: FileAttachment
    max_chars: int
    preview: bool = False
    entry: FileEntry | None = None
    partial: bool = False  # 这一轮剩下的额度不够，没读完（或者少附了页面图）：之后还能再完整读


@dataclass
class LinkEntry:
    """频道里最近一条带链接的消息。"""

    message: IncomingMessage
    read_turns: dict[str, int] = field(default_factory=dict)  # 打开过、内容在对话里的链接 → 那条记录的 id
    failed: set[str] = field(default_factory=set)  # 确定打不开的：不再自动打开，模型要求时才再试
    titles: dict[str, str] = field(default_factory=dict)  # 打开时拿到的标题（再提到时用同一个名字）

    def unread(self) -> list[LinkRef]:
        return [link for link in self.message.links if link.url not in self.read_turns and link.url not in self.failed]


@dataclass
class _LinkPlan:
    anchor: int  # 内容块放在哪条消息后面
    entry: LinkEntry
    link: LinkRef
    partial: bool = False  # 这一轮剩下的字数不够，没读完（或者少附了图）：之后还能再完整打开


@dataclass
class _Budget:
    """一轮（包括看文件的第二步）里还能读多少。"""

    files: int
    chars: int
    images: int
    allow_images: bool
    links: int = 0  # 还能打开几个链接


def _add_detail(detail: str, note: str) -> str:
    return f"{detail}；{note}" if detail else note


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def _block_without_images(block: DocBlock) -> DocBlock:
    if not block.result.images:
        return block
    note = "图没能发给你" if block.source == "链接" else "页面图没能发给你"
    return replace(block, result=replace(block.result, images=[], detail=_add_detail(block.result.detail, note)))


def _without_images(docs: dict[int, list[DocBlock]]) -> dict[int, list[DocBlock]]:
    return {k: [_block_without_images(b) for b in v] for k, v in docs.items()}


def _sticker_label(name: str) -> str:
    """贴图名在提示词里的写法（模型照着这个写回来，所以核对时也按这个比）。"""
    return neutralize(one_line(name, 30))


def _norm_channel(name: str) -> str:
    return "".join(name.lower().lstrip("#＃").split())


def _ago(seconds: float) -> str:
    if seconds < 120:
        return "刚才"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{seconds / 3600:.0f} 小时前"


def _pick_lines(lines: list[str], about: str | None, budget: int) -> list[str]:
    """翻记录时挑哪些行：有关键词就挑提到它的（连同前后一行），否则挑最近的；总长不超过 budget。"""
    missed = False
    if about:
        words = [w for w in about.lower().split() if w]
        hits = [i for i, line in enumerate(lines) if any(w in line.lower() for w in words)]
        if hits:
            keep: set[int] = set()
            size = 0
            for i in reversed(hits):  # 新的优先
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
        out.insert(0, f"（没找到提到「{one_line(about or '', 40)}」的内容，下面是最近的记录）")
    return out


def plan_compaction(contents: list[Any], *, force: bool = False) -> int:
    """压缩记忆时要删掉最早的几条（偶数，成对删）。

    按估算的 token 数删掉大约一半，但最近 6 轮不动；旧的部分还不到一半时（比如刚读了一个大文件）先不压，
    等那个文件变旧了再一起压，免得每隔几轮就压一次、每次都让缓存失效。force 用于上下文真的太长的时候。
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
    """某个人在某个频道跟它的一次对话（几行纯文本）。"""

    channel_id: int
    at: float
    seq: int  # 记下的先后（慢的轮次后记，就排在后面，不会因为开始得早被当成“已经带过”）
    lines: list[str]


@dataclass
class ChannelState:
    channel_id: int
    guild_id: int
    parent_id: int | None
    bucket: QuotaBucket
    category_id: int | None = None
    pending: list[IncomingMessage] = field(default_factory=list)
    inflight: list[IncomingMessage] = field(default_factory=list)  # 正在处理的这一批
    omitted: int = 0
    first_human_at: float | None = None  # 缓冲区里第一条真人消息到达的时间
    last_activity_at: float = 0.0  # 最近一次有人说话或打字
    last_eval_at: float = 0.0
    last_speak_at: float = 0.0
    mention_due: bool = False
    scheduled: bool = False
    error_until: float = 0.0
    send_failures: int = 0
    generation: int = 0  # 每次“清空记忆”加一，跑到一半的轮次据此放弃写入
    turns: TurnTracker = field(default_factory=TurnTracker)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    conversation: Conversation | None = None
    sid_by_msg: dict[int, int] = field(default_factory=dict)
    msg_by_sid: dict[int, int] = field(default_factory=dict)
    recent_files: dict[int, FileEntry] = field(default_factory=dict)  # message_id -> 带文件的消息
    recent_links: dict[int, LinkEntry] = field(default_factory=dict)  # message_id -> 带链接的消息
    last_image_at: float = 0.0  # 上次上网找图的时间（同一个频道隔一会儿才能再找）
    media_failures: int = 0  # 连着几次图发不出去（多半是缺“附加文件”权限）
    media_blocked_until: float = 0.0  # 发图连着失败：这之前先不发图（文字照常）
    gallery: list[Picture] = field(default_factory=list)  # 这一轮找到的候选图（pick 从这里挑）
    digest_seen: dict[int, int] = field(default_factory=dict)  # 人 -> 已经带进这个频道的、他在别处最近一次对话的序号
    listed: tuple[str, ...] | None = None  # 已经写进这个频道记录里的“能翻记录的频道”名单
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
        self.link_reader = link_reader  # 打开链接（推文走 FxTwitter，网页走 Tavily）
        self.pictures = pictures  # 上网找图（要 Tavily）
        self.memes = memes  # 表情包库
        self.owner_id = owner_id  # OWNER_ID：聊天记录里标〔dsowner〕，只有他能在聊天里改人设
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
        self._file_slots = asyncio.Semaphore(2)  # 同时最多解析 2 个文件
        self.tz = cfg.bot.timezone
        self.pricing = cfg.pricing
        self.states: dict[int, ChannelState] = {}
        self.limiter = SlidingWindow()
        self.paused_until = 0.0  # 余额不足 / key 错误时全局暂停
        self._last_housekeep = 0.0
        self._tasks: set[asyncio.Task[Any]] = set()
        self.searcher = searcher
        self.interactions: dict[tuple[int, int], deque[Interaction]] = {}  # (服务器, 人) -> 最近跟它的几次对话
        self._interaction_seq = 0

    # ------------------------------------------------------------------ 输入

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
            st.category_id = category_id  # 频道可能被挪到别的类别（帖子的类别取不到时保留旧值）
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
            # 不在允许的区域：完全不看，之前攒下的也一起丢掉
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
            return  # 别的 bot：只当上下文，不算轮次、不触发
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
                log.info("频道 %s：%s 点名太频繁，这次不立刻回应", st.channel_id, msg.author_name)

    def handle_typing(self, channel_id: int) -> None:
        st = self.states.get(channel_id)
        if st is not None:
            st.last_activity_at = max(st.last_activity_at, self.clock())

    def find_message(self, channel_id: int, message_id: int) -> IncomingMessage | None:
        """还没处理完（缓冲中或正在处理）的消息。"""
        st = self.states.get(channel_id)
        if st is None:
            return None
        for m in (*st.pending, *st.inflight):
            if m.message_id == message_id:
                return m
        return None

    def refresh_message(self, updated: IncomingMessage) -> bool:
        """消息被编辑、或 Discord 补上了链接预览：更新还没处理的那条。点名状态保持不变。"""
        m = self.find_message(updated.channel_id, updated.message_id)
        if m is None:
            return False
        m.text = updated.text
        m.images = updated.images
        m.files = updated.files
        m.stickers = updated.stickers
        m.links = updated.links  # 预览到了之后，链接多了标题
        m.awaiting_embeds = updated.awaiting_embeds
        st = self.states.get(m.channel_id)
        if st is not None and m.links and m.message_id not in st.recent_links:
            st.recent_links[m.message_id] = LinkEntry(m)
        return True

    def _prune(self, st: ChannelState, now: float) -> None:
        """丢掉缓冲区里太旧的消息，以及超出上限的部分。"""
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

    # ------------------------------------------------------------------ 调度

    async def tick(self) -> None:
        """由外部每隔几秒调用一次，检查哪些频道该跑一轮。"""
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
            # 这个人在别处已经没有可带的对话了，记录“带到哪了”也没用了；以后新的对话序号更大，照样会带
            for person in [p for p in st.digest_seen if (st.guild_id, p) not in self.interactions]:
                del st.digest_seen[person]
        for st in self.states.values():
            idle = now - max(st.last_activity_at, st.last_eval_at)
            if st.conversation is not None and not st.lock.locked() and not st.scheduled and idle > IDLE_EVICT:
                st.conversation = None

    def _kick(self, st: ChannelState) -> None:
        if st.scheduled or st.lock.locked():
            return  # 正在跑的那一轮结束后会再检查
        st.scheduled = True
        task = asyncio.get_running_loop().create_task(self._run(st))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """等所有进行中的轮次结束（测试和关闭时用）。"""
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    def _over_budget(self, st: ChannelState, view: View, now: float) -> tuple[bool, bool]:
        guild = self.ledger.guild_spent(st.guild_id, now)
        total = self.ledger.total_spent(now)
        g_budget = view.daily_budget_usd
        t_budget = self.settings.get("global_daily_budget_usd")  # 全局上限只认全局设置
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
            return False  # 没额度时连判断都不做，省 token
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
                st.pending.clear()  # 刚被管理员关掉的区域：攒着的消息也不处理了
                st.first_human_at = None
            if st.pending and (any(m.trigger for m in st.pending) or self._proactive_ready(st, self.clock())):
                batch = list(st.pending)
                st.inflight = batch
                try:
                    await self._round(st, batch)
                except Exception:
                    log.exception("频道 %s 处理一轮时出错", st.channel_id)
                    # 不能让同一批消息每次 tick 都再花一次钱：先冷却，点名也不再自动重试
                    now = self.clock()
                    st.last_eval_at = now
                    st.error_until = now + ERROR_BACKOFF
                    for m in batch:
                        m.trigger = False
                finally:
                    st.inflight = []
                    st.gallery = []  # 找到的候选图只在这一轮里能挑
        if st.mention_due and st.pending:
            self._kick(st)

    # ------------------------------------------------------------------ 一轮

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
        """最近的 N 条，再加上更早但在叫它的消息（点名不能因为后面消息太多被挤掉）。"""
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
            log.warning("频道 %s：%s，不回应点名", st.channel_id, "暂停中" if now < self.paused_until else "已超预算")
            for m in triggers:
                m.trigger = False
                await self._safe_react(st, m.message_id, "⚠️")
            return

        if any(m.trigger and m.awaiting_embeds and now - m.received_at < EMBED_WAIT for m in batch):
            # GIF / 图片链接的预览图通常晚一两秒才到（消息更新事件会把图补进同一个对象里）
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
            # 读文件的时候记忆被清空了：这一轮作废，点名留着，马上用新的记忆重来
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
                st.digest_seen.update(digest_marks)  # 这批内容被拒了：带过来的那几句也别再带，免得一直被拒
            await self._on_error(st, kind, exc, now, batch, targets, conv, info)
            return

        # 主动聊天时也能上网找图（发图是群友的日常）；其他要查资料的动作只在被叫到时用。
        # 主动聊天时找不了（冷却中、次数用完）就当它没说要找图，不为它多问一次模型
        extra = ("image",) if kind == "proactive" and self._image_ready(st, view, now) else ()
        actions, stored = self._interpret(st, completion, kind, targets, allow_tools=kind == "mention", extra_tools=extra)
        if st.generation != generation:
            log.info("频道 %s：这一轮进行中记忆被清空，丢弃结果", st.channel_id)
            self.store.set_outcome(row, "discarded")
            self._consume(st, batch, now)
            return

        # 先把这一轮写进历史（只追加），再消费缓冲区
        self.store.append_round(conv, user_content, stored, [(sid, m.message_id) for sid, m in numbered], now=now)
        self._consume(st, batch, now)
        self._mark_read(conv, planned)
        self._mark_links(conv, opened)
        st.digest_seen.update(digest_marks)
        if listed is not None:
            st.listed = listed

        tools = [a for a in actions if a.kind in TOOL_KINDS]
        if tools:
            # 模型要先看文件 / 打开链接 / 上网搜 / 找图 / 翻别的频道 / 改人设：办完再问它一次（第二步不能再用这些，
            # 只有第一步没改人设时，第二步还能改一次人设，比如看完角色卡再照着改）
            self.store.set_outcome(row, "+".join(sorted({a.kind for a in tools})))
            st.last_decision = "被点名，先去查资料" if kind == "mention" else "想发图，先去找"
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
            actions.append(Action("react", targets[-1], emoji="👍"))  # 想发的贴图、表情包发不了：被叫到了总得有点回应
        # 发贴图、表情包、找到的图和说话一样算“开口了”：扣额度、计发言间隔
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
            # 额度按模型的决定扣，不按发没发成功扣：没权限的频道也不会无限判断
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
        uploads = {"meme", "pick"}  # 要“附加文件”权限的那两种
        self._note_media(st, bool(failed_media & uploads), any(a.kind in uploads for a in actions))
        if failed_media and not spoke and not reacted and kind == "mention" and targets:
            # 想发的图发不出去（多半是缺“附加文件”权限），这一轮也没说别的：被叫到了至少回个表情
            mid = st.msg_by_sid.get(targets[-1])
            if mid is not None:
                await self._safe_react(st, mid, "👍")
        if decided_text and not spoke:
            # 只算发文字失败：只是图发不出去时，文字照样能发，主动发言不用停
            st.send_failures += 1
            if st.send_failures >= SEND_FAIL_LIMIT:
                st.error_until = self.clock() + SEND_FAIL_BACKOFF
                log.warning("频道 %s：连续 %d 轮发不出消息（缺权限？），暂停主动发言 30 分钟", st.channel_id, st.send_failures)
        elif spoke:
            st.send_failures = 0

        u = completion.usage
        log.info(
            "[%s #%s] %s → %s | 输入 %d（命中 %d）输出 %d | $%.5f | %s",
            info.guild_name,
            info.channel_name,
            "点名" if kind == "mention" else "主动",
            outcome,
            u.prompt_tokens,
            u.hit,
            u.output,
            cost,
            one_line(completion.reasoning or "", 160),
        )
        if u.prompt_tokens > view.context_budget_tokens and st.generation == generation:
            # 平时等旧记录攒够一半再压（少让缓存失效）。实在太长（比如连着读了好几个大文件）就不等了：
            # 门槛至少 10 万 token，预算设得很小时也不会刚读完文件就压掉、接着又重读
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
        """调用模型；带图的请求被拒（400 之类）时去掉图片再试一次。返回 (实际发出的 user 内容, 结果, 花费, 用量记录)。"""
        content, messages = build(True)
        try:
            completion, cost, row = await self._ask(st, kind, messages, thinking, generation, max_tokens)
        except LLMError as exc:
            if exc.status not in BAD_REQUEST or not has_images or st.generation != generation:
                raise
            log.warning("频道 %s：带图片的请求被拒（%s），去掉图片再试一次", st.channel_id, exc)
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
        """解析模型输出，返回 (动作, 写进历史的内容)。"""
        actions, ok = parse_actions(completion.content)
        if not ok:
            log.warning("频道 %s：模型输出不是合法 JSON：%s", st.channel_id, one_line(completion.content, 200))
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
            # 要查资料时这一次只算这些动作；历史里也只记它们，和实际做的事一致
            picked = (
                [a for a in tools if a.kind == "read"][:MAX_READ_TARGETS]
                + [a for a in tools if a.kind == "search"][: max(1, self.ec.max_searches_per_round)]
                + [a for a in tools if a.kind == "recall"][:MAX_RECALLS]
                + [a for a in tools if a.kind == "image"][:1]
            )
            if not picked:
                # 改人设要单独来：和看文件放在一起时先不改（看完文件那一步还能改），和搜索、翻记录一起时不改，
                # 免得照着没看过的内容、或者网页、别的频道里别人写的东西去改
                picked = [a for a in tools if a.kind == "persona"][:1]
            return picked, dump_actions(picked)
        actions = [a for a in actions if a.kind not in TOOL_KINDS]  # 没被叫到、或者已经查过一次了
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
            results, opened = await work()  # 主动聊天时找图：还没决定发不发，先不显示“正在输入”
        if st.generation != generation:
            return None  # 查资料的时候记忆被清空了
        blocks = [block for _, block, _, _ in results] + [block for _, block, _ in opened]
        if not reads:
            header = None
        elif wanted == {"链接"}:
            header = "（你要打开的链接：）"
        elif wanted == {"文件", "链接"}:
            header = "（你要看的文件和链接：）"
        else:
            header = "（你要看的文件：）"

        def build(with_images: bool) -> tuple[Any, list[dict[str, Any]]]:
            used = blocks if with_images else [_block_without_images(b) for b in blocks]
            notes: list[str] = []
            if gallery is not None and not with_images:
                st.gallery = []  # 候选图没能给它看：这一轮就不能发了
                notes.append("（找到的图没能给你看，这次不发图。）")
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
        """read 动作要看的文件和链接。返回 (要读的文件, 要打开的链接, 给模型的说明, 要看的是文件还是链接)。"""
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
                notes.append(f"（#{action.to} 里没有能读的文件或链接，或者太久已经拿不到了）")
                continue
            if has_files:
                wanted.add("文件")
            if has_links:
                wanted.add("链接")
            if has_files and not view.files:
                if not files_off_noted:
                    notes.append("（读文件的功能被关掉了，看不了文件）")
                    files_off_noted = True
            elif has_files:
                assert fentry is not None
                for att in fentry.message.files:
                    if att_key(att) in fentry.read_turns:
                        name = neutralize(one_line(att.filename, 60))
                        notes.append(f"（#{action.to} 的文件「{name}」前面已经给你看过了，往上翻就有）")
                    else:
                        plan.append(_Planned(mid, mid, att, self._file_chars(), entry=fentry))
            if not has_links:
                continue
            assert lentry is not None
            if mode == "off":
                if not links_off_noted:
                    notes.append("（打开链接的功能被关掉了，链接只看得到 Discord 的预览）")
                    links_off_noted = True
                continue
            assert self.link_reader is not None
            for link in lentry.message.links:
                name = neutralize(one_line(lentry.titles.get(link.url) or link.title or site_name(link.url), 40))
                if link.url in lentry.read_turns:
                    notes.append(f"（#{action.to} 的链接「{name}」前面已经给你看过了，往上翻就有）")
                elif not self.link_reader.readable(link.url):
                    notes.append(f"（#{action.to} 的链接「{name}」打不开：这里只能打开 X 的推文，别的网站只看得到 Discord 的预览）")
                else:
                    link_plan.append(_LinkPlan(mid, lentry, link))
        return plan, link_plan, notes, wanted

    # ------------------------------------------------------------------ 上网搜

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
            return f"（想搜「{shown}」，但这里不能上网搜。凭你已有的知识回答，拿不准就直说。）"
        limit = int(view.daily_search_limit)
        day = self.ledger.day(now)
        if self.store.count_usage(day, guild_id=st.guild_id, kind="search") >= limit:
            return f"（想搜「{shown}」，但今天这个服务器的搜索次数用完了（{limit} 次）。凭你已有的知识回答，拿不准就直说。）"
        if self._over_budget(st, view, now)[0]:
            return f"（想搜「{shown}」，但今天的预算用完了。凭你已有的知识回答，拿不准就直说。）"
        assert self.searcher is not None
        # 先记一笔占住名额，再去搜：别的频道同时在搜时，也不会一起超过上限
        row = self.store.record_usage(ts=now, day=day, guild_id=st.guild_id, channel_id=st.channel_id, kind="search", outcome="pending")
        try:
            result = await self.searcher.search(
                query, today=self._today(now), max_chars=self.ec.search_max_chars, user_id=f"guild-{st.guild_id}"
            )
        except asyncio.CancelledError:
            self._finish_search(st, row, Usage(), "error")  # 关机时被取消：别留下一笔一直“进行中”的记录
            raise
        except Exception as exc:  # SearchError，以及意料之外的错误：都只算这次没搜成
            log.warning("频道 %s：搜索「%s」失败：%s", st.channel_id, one_line(query, 60), exc)
            self._finish_search(st, row, getattr(exc, "usage", None) or Usage(), "error")
            return f"（搜「{shown}」失败了。凭你已有的知识回答，拿不准就直说。）"
        self._finish_search(st, row, result.usage, "search")
        log.info("频道 %s：搜了「%s」（%s，%d 个来源）", st.channel_id, one_line(query, 60), result.provider, len(result.sources))
        return render_search(query, result.summary, result.sources, self.ec.search_max_chars)

    def _finish_search(self, st: ChannelState, row: int, usage: Usage, outcome: str) -> None:
        now = self.clock()
        cost = self.pricing.cost(usage, now)
        self.ledger.add(st.guild_id, cost, now)
        self.store.update_usage(row, outcome=outcome, hit=usage.hit, miss=usage.miss, output=usage.output, cost=cost)

    # ------------------------------------------------------------------ 链接

    def _links_mode(self, view: View) -> str:
        """打开链接的能力：all（推文和网页）/ x（只有推文，没配置 Tavily）/ off。"""
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
        """这一轮直接附上的文件和链接内容。被叫到时显示“正在输入”（读文件、打开链接要几秒）。"""

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
        """不用模型开口就直接打开的链接：叫它的那条消息里的、叫它时回复的那条里的、同一个人刚刚（两分钟内）发的。"""
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
            if self.link_reader.readable(link.url)  # 没配置 Tavily 时，一般网页就只看预览，不用提
        ]
        return await self._read_links(st, plan, budget, now)

    async def _read_links(
        self, st: ChannelState, plan: list[_LinkPlan], budget: _Budget, now: float
    ) -> list[tuple[_LinkPlan, DocBlock, FileText]]:
        """打开这些链接（同时打开，最多几秒）。名额、字数、每日次数不够的，告诉模型没打开。"""
        if not plan:
            return []
        assert self.link_reader is not None
        day = self.ledger.day(now)
        limit = max(0, int(self.ec.daily_link_limit))
        used = self.store.count_usage(day, guild_id=st.guild_id, kind="link")
        slots: list[tuple[_LinkPlan, FileText | None, int, int]] = []  # (链接, 没打开的原因, 记账的行, 最多读多少字)
        for item in plan:
            if budget.links <= 0:
                why = f"一次最多打开 {self.ec.max_links_per_round} 个链接，这个没打开"
                slots.append((item, FileText(error=why, transient=True), 0, 0))
            elif budget.chars < 500:
                slots.append((item, FileText(error="这一轮读的内容已经太多了，这个没打开", transient=True), 0, 0))
            elif used >= limit:
                why = f"今天这个服务器打开链接的次数用完了（{limit} 个）"
                slots.append((item, FileText(error=why, transient=True), 0, 0))
            else:
                cap = max(1, min(self.ec.link_max_chars, budget.chars))
                budget.chars -= cap
                budget.links -= 1
                used += 1
                # 先记一笔占住名额，再去打开：别的频道同时在打开时，也不会一起超过上限
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
                    self.store.update_usage(row, outcome="error")  # 关机时被取消：别留下一笔一直“进行中”的记录
            raise
        results: dict[int, LinkResult] = {}
        for (i, item, _), got in zip(jobs, fetched):
            if isinstance(got, BaseException):
                if not isinstance(got, Exception):
                    raise got
                log.warning("频道 %s：打开链接 %s 出错：%r", st.channel_id, site_name(item.link.url), got)
                got = LinkResult("", FileText(error="打不开（出错了）", transient=True))
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
                budget.chars += cap - len(result.text)  # 先按上限占的字数，没用完的还回去
                result, trimmed = self._fit_images(result, budget, link=True)
                # 网页本身比单个链接的上限还长、只能读前面的，不算没读完；是这一轮的额度不够才算
                item.partial = (result.truncated and cap < self.ec.link_max_chars) or trimmed
                log.info(
                    "频道 %s：打开链接 %s%s",
                    st.channel_id,
                    site_name(item.link.url),
                    f"失败：{result.error}" if result.error else f"（{len(result.text)} 字）",
                )
            name = title or item.entry.titles.get(item.link.url) or site_name(item.link.url) or "链接"
            if not result.error:
                item.entry.titles[item.link.url] = name
            block = DocBlock(self._label(st, item.entry.message.message_id), name, result, source="链接")
            out.append((item, block, result))
        return out

    def _mark_links(self, conv: Conversation, opened: list[tuple[_LinkPlan, DocBlock, FileText]]) -> None:
        """打开过的链接记成“内容在对话里了”（压缩记忆删掉那条记录时再取消）；确定打不开的以后不再自动打开。"""
        if not conv.turns or not opened:
            return
        turn_id = conv.turns[-2].id if len(conv.turns) >= 2 else conv.turns[-1].id
        for item, _, result in opened:
            url = item.link.url
            if item.partial:
                continue  # 这次只读了一部分：下次被要求时再完整打开
            if result.error:
                if not result.transient:
                    item.entry.failed.add(url)
            else:
                item.entry.read_turns[url] = turn_id
                item.entry.failed.discard(url)

    # ------------------------------------------------------------------ 发图

    def _memes_on(self, view: View) -> bool:
        return bool(view.images) and self.memes is not None

    def _can_find_images(self, view: View) -> bool:
        return bool(view.images) and self.pictures is not None and int(view.daily_image_limit) > 0

    def _image_ready(self, st: ChannelState, view: View, now: float) -> bool:
        """现在就能找图：能找、这个频道没在冷却、今天还有次数。"""
        if not self._can_find_images(view) or now < st.media_blocked_until:
            return False
        if st.last_image_at and now - st.last_image_at < self.ec.image_cooldown_seconds:
            return False
        return self.store.count_usage(self.ledger.day(now), guild_id=st.guild_id, kind="image") < int(view.daily_image_limit)

    async def _image_section(self, st: ChannelState, view: View, action: Action, now: float) -> tuple[str | None, list[Picture]]:
        """上网找图。找到了返回 (None, 候选图)，候选图也记在 st.gallery 里（pick 从这里挑）；没找成返回 (说明, [])。"""
        query = action.query or ""
        shown = neutralize(one_line(query, 60))
        if not self._can_find_images(view):
            return IMAGE_OFF.format(query=shown), []
        if now < st.media_blocked_until:  # 找到了也发不出去：别白花额度
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
        # 先记一笔占住名额，再去找
        row = self.store.record_usage(ts=now, day=day, guild_id=st.guild_id, channel_id=st.channel_id, kind="image", outcome="pending")
        try:
            found = await self.pictures.find(query)
        except asyncio.CancelledError:
            self.store.update_usage(row, outcome="error")
            raise
        except Exception as exc:  # TavilyError，以及意料之外的错误
            log.warning("频道 %s：找图「%s」失败：%s", st.channel_id, one_line(query, 60), exc)
            self.store.update_usage(row, outcome="error")
            return IMAGE_FAILED.format(query=shown), []
        self.store.update_usage(row, outcome="image")
        found = found[: max(1, self.ec.image_candidates)]
        if not found:
            return IMAGE_NONE.format(query=shown), []
        st.gallery = list(found)
        log.info("频道 %s：找图「%s」，给它看 %d 张候选", st.channel_id, one_line(query, 60), len(found))
        return None, st.gallery

    def _find_meme(self, st: ChannelState, view: View, wanted: str) -> Any:
        """按名字找表情包：先按库里的名字比，再按提示词里列出来的写法（中和过标记符号、截短过）比。"""
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
        """图连着两轮发不出去（多半是缺“附加文件”权限）：这个频道 30 分钟内先不发图，免得每轮都白费。"""
        if not tried:
            return
        if not failed:
            st.media_failures = 0
            return
        st.media_failures += 1
        if st.media_failures >= 2:
            st.media_failures = 0
            st.media_blocked_until = self.clock() + SEND_FAIL_BACKOFF
            log.warning("频道 %s：连着发不出图（缺“附加文件”权限？），30 分钟内先不发图", st.channel_id)

    def _check_media(self, st: ChannelState, view: View, actions: list[Action]) -> tuple[list[Action], bool]:
        """表情包要库里真有；pick 要这一轮真的找过图、编号也对得上。对不上的跳过。返回 (动作, 有没有跳过)。"""
        if not any(a.kind in ("meme", "pick") for a in actions):
            return actions, False
        if self.clock() < st.media_blocked_until:
            log.info("频道 %s：最近发不出图，这次的图先不发", st.channel_id)
            out = [a for a in actions if a.kind not in ("meme", "pick")]
            return out, True
        out: list[Action] = []
        for a in actions:
            if a.kind == "meme":
                meme = self._find_meme(st, view, a.name or "")
                if meme is None:
                    log.info("频道 %s：表情包库里没有「%s」，跳过", st.channel_id, one_line(a.name or "", 40))
                    continue
                a = replace(a, name=meme.name)
            elif a.kind == "pick":
                if not st.gallery or a.image is None or not 1 <= a.image <= len(st.gallery):
                    log.info("频道 %s：要发的图 %s 不在这一轮找到的图里，跳过", st.channel_id, a.image)
                    continue
            out.append(a)
        return out, len(out) < len(actions)

    async def _send_media(self, st: ChannelState, action: Action, reply_to: int | None, ping: bool) -> int | None:
        """发贴图 / 表情包 / 找到的图。发不出去返回 None。"""
        try:
            if action.kind == "sticker":
                return await self.transport.send_sticker(st.channel_id, action.name or "", reply_to=reply_to, ping=ping)
            if action.kind == "meme":
                meme = self.memes.find(st.guild_id, action.name or "") if self.memes is not None else None
                # 读文件放到线程里：NAS 的硬盘没缓存时，读一张几 MB 的动图也要一会儿
                loaded = await asyncio.to_thread(self.memes.load, meme) if meme is not None and self.memes is not None else None
                if loaded is None:
                    log.info("频道 %s：表情包「%s」读不出来（可能刚被删了）", st.channel_id, action.name)
                    return None
                data, filename = loaded
                return await self.transport.send_file(st.channel_id, data, filename, reply_to=reply_to, ping=ping)
            if action.kind == "pick" and action.image is not None and 1 <= action.image <= len(st.gallery):
                picture = st.gallery[action.image - 1]
                # 透明的铺白底（和它看到的一样）、太大的缩小；放到线程里做，大图要一两秒
                ready = await asyncio.to_thread(upload_ready, picture.data, self.ec.image_max_bytes)
                if ready is None:
                    log.info("频道 %s：找到的图%d 没法发（太大或者坏了）", st.channel_id, action.image)
                    return None
                data, mime = ready
                sent = await self.transport.send_file(st.channel_id, data, filename_for(mime), reply_to=reply_to, ping=ping)
                if sent is not None:
                    log.info("频道 %s：发了找到的图%d（%s）", st.channel_id, action.image, picture.host)
                return sent
        except Exception:
            log.exception("频道 %s：发图出错", st.channel_id)
        return None

    # ------------------------------------------------------------------ 跨频道记忆

    def shares_out(self, channel_id: int, place: ChannelPlace) -> bool:
        """这个频道的内容能不能给同服务器别的频道看：开了共享；私密频道还要在这个频道上单独开。"""
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
        """这个频道可以翻记录的别的频道：普通频道在前，帖子在后，各自按名字排。"""
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
        """给模型看的频道名。帖子写成“帖子「标题」（在 #所在频道）”，群友起个和频道一样的帖子名也冒充不了频道。"""
        name = neutralize(one_line(place.name, 40))
        if place.parent_id is None:
            return f"#{name}"
        parent = self.transport.locate(place.parent_id)
        where = f"（在 #{neutralize(one_line(parent.name, 30))}）" if parent is not None else ""
        return f"帖子「{name}」{where}"

    def _find_source(
        self, sources: list[tuple[int, ChannelPlace]], wanted: str
    ) -> tuple[int, ChannelPlace] | list[tuple[int, ChannelPlace]] | None:
        """按名字找要翻的频道。找到返回它；同名的帖子分不出是哪个时返回候选列表；找不到返回 None。"""
        raw = " ".join(wanted.split()).strip()
        key = _norm_channel(wanted).rstrip("…")
        if not key:
            return None

        def names(place: ChannelPlace) -> set[str]:
            return {_norm_channel(place.name), _norm_channel(self._display(place)), _norm_channel(neutralize(place.name))}

        tests = (
            lambda p: raw in (p.name, self._display(p), neutralize(p.name)) or raw.lstrip("#＃") == p.name,  # 原样一致
            lambda p: key in names(p),
            lambda p: any(n.startswith(key) for n in names(p)),  # 名单里的长名字被截断过（结尾是“…”）
            lambda p: any(key in n or (len(n) >= 2 and n in key) for n in names(p)),  # “游戏频道”找“游戏”之类
        )
        for test in tests:
            found = [s for s in sources if test(s[1])]
            channels = [s for s in found if s[1].parent_id is None]
            if channels:
                return channels[0]  # 频道是管理员建的，同名时优先频道
            if len(found) == 1:
                return found[0]
            if found:
                return found  # 好几个帖子都对得上：不猜，免得被群友起的同名帖子冒充
        return None

    def _system_prompt(self, st: ChannelState, view: View, conv: Conversation, info: ChannelInfo) -> str:
        # 频道名单不放这里：它会变（新频道、帖子归档），放在 system prompt 里每变一次整段缓存都失效。
        # 表情、贴图很少变，放这里（名单已经排好序，不变就不影响缓存）
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
        """贴图名对上服务器里的贴图才发（换成真实名字）；对不上的直接跳过，不当成“发不出消息”。返回 (动作, 有没有跳过)。"""
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
                    log.info("频道 %s：没有叫「%s」的贴图，跳过", st.channel_id, one_line(wanted, 40))
                    continue
                a = replace(a, name=real)
            out.append(a)
        return out, len(out) < len(actions)

    def _persona_target(self, st: ChannelState, scope: str | None) -> tuple[str, int, str] | str:
        """改哪一级的人设：(范围, id, 给人看的说法)；改不了时返回原因（给模型看的提示）。

        不写 scope：改这里现在用的那份（模型看到、照着改的就是它）。"channel"：只改这个频道。
        "server"：全服务器；但这里的人设来自更具体的一级（频道、所在频道、类别）时不改，免得把那一份抄到全服务器去。
        """
        ids = {"guild_id": st.guild_id, "channel_id": st.channel_id, "parent_id": st.parent_id, "category_id": st.category_id}
        origin = self.settings.origin("persona", **ids)
        if scope == "channel":
            return "channel", st.channel_id, "这个频道"
        if origin is None or origin[0] in ("guild", "global"):
            return "guild", st.guild_id, "全服务器"
        where = {
            ("channel", st.channel_id): "这个频道",
            ("channel", st.parent_id): "所在频道",
            ("category", st.category_id): "所在类别",
        }.get(origin, "这个频道")
        if scope == "server":
            return PERSONA_NOT_HERE.format(origin=where)
        return origin[0], origin[1], where

    def _inherited_persona(self, st: ChannelState, scope: str, scope_id: int) -> str:
        """这一级不单独设置时会用的人设（和 /ds 人设 的“上一级”一致）。"""
        if scope == "guild":
            value = self.settings.get("persona")
        elif scope == "category":
            value = self.settings.get("persona", guild_id=st.guild_id)
        elif scope_id == st.channel_id:  # 这个频道：帖子所在频道 → 类别 → 服务器 → 全局
            value = self.settings.get("persona", guild_id=st.guild_id, channel_id=st.parent_id, category_id=st.category_id)
        else:  # 帖子所在的频道：类别 → 服务器 → 全局
            value = self.settings.get("persona", guild_id=st.guild_id, category_id=st.category_id)
        return clean_persona(value or "") or DEFAULT_PERSONA

    async def _persona_section(
        self, st: ChannelState, action: Action, numbered: list[tuple[int, IncomingMessage]], targets: list[int]
    ) -> str:
        """改人设：只认这一轮里 dsowner 本人叫它的那条消息。改完（或没改成）的结果告诉模型，它再回话。"""
        by_sid = dict(numbered)
        to = action.to
        if to is None:
            # 没写是哪条：这一轮 dsowner 只叫了它一次，就是那条
            owners = [sid for sid in targets if sid in by_sid and self._is_owner(by_sid[sid]) and by_sid[sid].addressed]
            if len(owners) != 1:
                return PERSONA_NO_ASKER
            to = owners[0]
        asker = by_sid.get(to)
        if asker is None or to not in targets or not self._is_owner(asker) or not asker.addressed:
            log.warning("频道 %s：有人想让它改人设，但 #%s 不是 dsowner 这一轮叫它的消息，没改", st.channel_id, to)
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
            self.store.mark_persona_reverted(last["id"])  # 撤过的不再撤：再说一次“改回去”就再往前撤一次
            scope, scope_id = last["scope"], last["scope_id"]
            where = {"guild": "全服务器", "category": "所在类别"}.get(
                scope, "这个频道" if scope_id == st.channel_id else "所在频道"
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
                # 和上一级一样就不单独存，以后上一级改了这里也会跟着变
                restored = None
                what = "恢复了默认人设（全服务器）" if scope == "guild" else f"让{where}改回沿用上一级的人设"
                note = PERSONA_RESET.format(to=to, what=what)
            else:
                restored = text
                note = PERSONA_DONE.format(to=to, where=where)

        old = self.settings.overrides(scope, scope_id).get("persona")
        if restored == old and not action.undo:
            return PERSONA_UNCHANGED  # 本来就是这样：不写、不记、不打记号
        if restored is None:
            self.settings.unset(scope, scope_id, "persona")
        else:
            self.settings.set(scope, scope_id, "persona", restored)
        # 每次改动都记进数据库（./data 里，重建容器也还在），说“改回去”就能撤销
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
            "频道 %s：dsowner 在聊天里%s人设（%s）。要求：%s\n  原来：%s\n  现在：%s",
            st.channel_id,
            "撤销了上一次修改的" if action.undo else "改了",
            where,
            one_line(asker.text, 200),
            old if old else "（没单独设置，沿用上一级）",
            restored if restored else "（不单独设置，沿用上一级）",
        )
        await self._safe_react(st, asker.message_id, "📝")  # 让 dsowner 一眼看到人设真的改了
        return note

    def _recall_section(self, st: ChannelState, view: View, action: Action, info: ChannelInfo) -> str:
        shown = neutralize(one_line(action.channel or "", 40))
        if not view.share_memory:
            return f"（想翻「{shown}」的记录，但这里没开记忆共享，看不了别的频道。）"
        sources = self.shared_sources(st.guild_id, st.channel_id)
        match = self._find_source(sources, action.channel or "")
        if match is None:
            names = "、".join(self._display(p) for _, p in sources[:10]) or "没有"
            return f"（找不到能翻记录的频道「{shown}」。能翻的：{names}）"
        if isinstance(match, list):
            names = "、".join(self._display(p) for _, p in match[:10])
            return f"（叫「{shown}」的有好几个：{names}。分不出是哪个，没翻。）"
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
        """被叫到时放在这一轮前面的跨频道内容：能翻记录的频道名单（变了才写），以及叫它的人最近在别的频道跟它聊过的话。

        返回 (文字行, 带进来的对话序号, 这次写进去的名单)。
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
            marks[m.author_id] = max(item.seq for item in fresh)  # 只带最近两次；更早的就不带了
            for item in fresh[-2:]:
                where = self._display(allowed[item.channel_id])
                block = [f"（{neutralize(one_line(m.author_name, 32))} {_ago(now - item.at)}在 {where} 跟你聊过："]
                block += ["  " + neutralize(line) for line in item.lines]
                block[-1] += "）"
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
        """记下这一轮谁跟它说了什么、它怎么回的（跨频道带近况用）。"""
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
            said = one_line(m.text, 150) if m.text.strip() else "（发了图片或文件）"
            lines = [f"{one_line(m.author_name, 32)}：{said}"] + [f"你：{one_line(r, 150)}" for r in replies[:3]]
            key = (st.guild_id, m.author_id)
            self._interaction_seq += 1
            self.interactions.setdefault(key, deque(maxlen=INTERACTIONS_PER_PERSON)).append(
                Interaction(st.channel_id, self.clock(), self._interaction_seq, lines)
            )

    # ------------------------------------------------------------------ 文件

    def _label(self, st: ChannelState, message_id: int) -> str:
        sid = st.sid_by_msg.get(message_id)
        return f"#{sid}" if sid is not None else "之前"

    def _auto_entries(
        self, st: ChannelState, shown: list[IncomingMessage], triggers: list[IncomingMessage]
    ) -> list[tuple[FileEntry, int]]:
        """不用模型开口就直接读的文件：叫它的那条消息里的、叫它时回复的那条里的、同一个人刚刚（两分钟内）发的。

        返回 [(文件所在的消息, 内容块放在哪条消息后面)]。已经读进对话的附件不再读。
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
        # Discord 把太长的消息自动转成 message.txt：没被直接读的，也给模型看个开头，就像普通的长消息
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
        """单个文件最多读多少字（不超过一轮的总量，否则大文件永远读不完、每次都要重读）。"""
        return max(1, min(self.ec.file_max_chars, self.ec.file_round_max_chars))

    def _precheck(self, att: FileAttachment) -> FileText | None:
        """不用下载就知道读不了的（格式不支持、太大），不占“一次最多读几个文件”的名额。"""
        kind, reason = classify(att.filename, att.content_type)
        if kind == "unsupported":
            return FileText(error=reason)
        limit = max_bytes_for(kind, self.ec.file_max_bytes)
        if att.size and att.size > limit:
            return FileText(error=f"文件太大（{_size(att.size)}），最多读 {_size(limit)}")
        return None

    def _fit_images(self, result: FileText, budget: _Budget, *, link: bool = False) -> tuple[FileText, bool]:
        """返回 (结果, 是不是因为这一轮图太多少附了页面图)。link：打开的链接里的图（说法不一样）。"""
        if not result.images:
            return result, False
        what, unit = ("图", "张") if link else ("页面图", "页")
        if not budget.allow_images:
            return replace(result, images=[], detail=_add_detail(result.detail, f"看图功能关着，没附{what}")), False
        keep = result.images[: max(0, budget.images)]
        budget.images -= len(keep)
        if len(keep) == len(result.images):
            return result, False
        note = f"这一轮图太多，只附了 {len(keep)} {unit}" if keep else f"这一轮图太多，{what}没附上"
        return replace(result, images=keep, detail=_add_detail(result.detail, note)), True

    async def _read_planned(
        self, st: ChannelState, plan: list[_Planned], budget: _Budget
    ) -> list[tuple[int, DocBlock, _Planned, FileText]]:
        out: list[tuple[int, DocBlock, _Planned, FileText]] = []
        for item in plan:
            result = self._precheck(item.att)
            if result is None:
                if not item.preview and budget.files <= 0:
                    result = FileText(error=f"一次最多读 {self.ec.max_files_per_round} 个文件，这个没读", transient=True)
                elif budget.chars < 500:
                    result = FileText(error="这一轮读的内容已经太多了，这个没读", transient=True)
                else:
                    cap = min(item.max_chars, budget.chars)
                    result = await self.file_reader(item.att, cap)
                    if not item.preview:
                        budget.files -= 1
                    budget.chars -= len(result.text)
                    result, trimmed = self._fit_images(result, budget)
                    # 文件本身就短、额度够读完的，不算没读完；文件比单个文件的上限还长、只能读前面的，也不算
                    item.partial = (result.truncated and cap < item.max_chars) or trimmed
            block = DocBlock(self._label(st, item.message_id), item.att.filename, result)
            out.append((item.anchor, block, item, result))
        return out

    def _mark_read(self, conv: Conversation, done: list[tuple[_Planned, FileText]]) -> None:
        """完整读到的附件记成“已经在对话里了”（记下是哪条记录，压缩记忆删掉那条时再取消）；
        确定读不了的（格式不支持、坏文件）记下来，以后不再自动读。暂时的失败和只读了一部分的，下次还会读。"""
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
            return FileText(error=f"文件太大（{_size(att.size)}），最多读 {_size(limit)}")
        if att.fetch is None:
            return FileText(error="拿不到这个文件")
        try:
            raw = await asyncio.wait_for(att.fetch(), DOWNLOAD_TIMEOUT)
        except Exception as exc:
            log.info("下载文件 %s 失败：%r", att.filename, exc)
            return FileText(error="下载失败（文件可能已经被删了）", transient=True)
        if len(raw) > limit:
            return FileText(error=f"文件太大（{_size(len(raw))}），最多读 {_size(limit)}")
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
        """调用模型；被点名时显示“正在输入”，空回复（或推理把 token 用光）时关掉思考重试一次。"""
        if kind != "mention":
            return await self._call(st, kind, messages, thinking, max_tokens)
        async with self.transport.typing(st.channel_id):
            completion, cost, row = await self._call(st, kind, messages, thinking, max_tokens)
            empty = not completion.content.strip() or (
                completion.finish_reason == "length" and not parse_actions(completion.content)[1]
            )
            if empty and st.generation == generation:  # 记忆刚被清空就别再花一次钱了
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
        # 花了钱就立刻记账，后面出什么错数据库和内存账本都一致
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
        log.warning("频道 %s：DeepSeek 调用失败：%s", st.channel_id, exc)
        st.last_eval_at = now
        for m in batch:
            m.trigger = False  # 只清这一批的点名；调用期间新来的点名照常处理
        if exc.status == 402:
            self.paused_until = now + 600
            log.error("DeepSeek 余额不足，暂停 10 分钟。请到 platform.deepseek.com 充值。")
        elif exc.status == 401:
            self.paused_until = now + 3600
            log.error("DeepSeek API key 无效，暂停 1 小时。请检查 .env 里的 DEEPSEEK_API_KEY。")
        elif exc.status in BAD_REQUEST:
            # 这批消息本身被拒（比如内容审核）：丢掉它，别卡住整个频道
            log.error("频道 %s：这批消息被 DeepSeek 拒绝，已丢弃：%s", st.channel_id, exc)
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
        st.last_decision = f"出错：{exc}"
        if kind == "mention" and targets:
            mid = st.msg_by_sid.get(targets[-1])
            if mid is not None:
                await self._safe_react(st, mid, "⚠️")

    async def _safe_react(self, st: ChannelState, message_id: int, emoji: str) -> bool:
        try:
            return await self.transport.react(st.channel_id, message_id, emoji)
        except Exception:
            log.exception("频道 %s：加表情出错", st.channel_id)
            return False

    async def _execute(
        self, st: ChannelState, actions: list[Action], kind: str, targets: list[int], elapsed: float
    ) -> tuple[bool, bool, set[str]]:
        """返回 (发出去了什么, 加了表情, 没发出去的图是哪几种：sticker / meme / pick)。"""
        spoke = reacted = False
        failed: set[str] = set()
        sent_count = 0
        pinged: set[int] = set()  # 同一条消息只提醒一次（比如又回话又发贴图）
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
                    delay = max(0.5, delay - elapsed)  # 模型生成的时间也算在“打字”里
                async with self.transport.typing(st.channel_id):
                    await self.sleep(delay)
                for j, chunk in enumerate(split_for_discord(text)):
                    first = i == 0 and j == 0
                    try:
                        sent = await self.transport.send(
                            st.channel_id, chunk, reply_to=reply_mid if first else None, ping=ping and first
                        )
                    except Exception:
                        log.exception("频道 %s：发消息出错", st.channel_id)
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
        # 先看群友发的图、GIF、贴图（优先最近的），名额还有剩才看链接预览的图
        for previews in (False, True):
            for m in reversed(shown):
                for att in m.images:
                    if (att.kind == "preview") != previews:
                        continue
                    if budget <= 0:
                        return out
                    wanted = m.message_id in trigger_ids  # 叫它的人发的图看清楚点，其他图看个大概
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
            log.info("下载图片失败：%r", exc)
            return None
        return await asyncio.to_thread(to_data_uri, raw, max_side)

    async def _compact(self, st: ChannelState, conv: Conversation, info: ChannelInfo, *, force: bool = False) -> None:
        """把最早的一部分对话压成摘要。只在这一次会让缓存 miss。"""
        remove = plan_compaction([t.content for t in conv.turns], force=force)
        if remove < 2:
            return
        generation = st.generation
        lines = []
        for t in conv.turns[:remove]:
            if t.role == "user":
                # 别的频道带来的近况、翻到的记录、搜索结果不进摘要：那些频道清空或不再共享后，这里也不该一直记着
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
                "content": f"【旧摘要】\n{conv.summary or '（无）'}\n\n【聊天记录】\n" + "\n".join(lines),
            },
        ]
        now = self.clock()
        try:
            completion = await self.llm.complete(
                prompt, thinking="off", json_mode=False, max_tokens=2000, user_id=f"guild-{st.guild_id}"
            )
        except LLMError as exc:
            log.warning("频道 %s：压缩摘要失败：%s", st.channel_id, exc)
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
        st.listed = last_listed(conv.turns)  # 名单可能在被压掉的那部分里，那样下次被叫到时重新写
        for entry in st.recent_files.values():
            # 文件内容随旧记录一起压成了摘要，被问到时要能重新读
            entry.read_turns = {k: v for k, v in entry.read_turns.items() if v > last_removed}
        for link_entry in st.recent_links.values():
            link_entry.read_turns = {k: v for k, v in link_entry.read_turns.items() if v > last_removed}
        log.info("频道 %s：已把 %d 条旧记录压成摘要", st.channel_id, remove)

    @staticmethod
    def _describe(kind: str, outcome: str, actions: list[Action], completion: Completion) -> str:
        if outcome == "reply":
            said = next((a.messages[0] for a in actions if a.kind == "reply" and a.messages), "")
            media = next((a for a in actions if a.kind in SEND_KINDS), None)
            if said or media is None:
                text = f"说了：{one_line(said, 40)}"
            elif media.kind == "pick":
                text = "发了一张网上找的图"
            else:
                text = f"发了{'贴图' if media.kind == 'sticker' else '表情包'}：{one_line(media.name or '', 40)}"
        elif outcome == "react":
            text = "加了表情 " + " ".join(a.emoji or "" for a in actions if a.kind == "react")
        else:
            text = "没说话"
        prefix = "被点名" if kind == "mention" else "主动判断"
        reason = one_line(completion.reasoning or "", 80)
        return f"{prefix}，{text}" + (f"（想法：{reason}）" if reason else "")

    # ------------------------------------------------------------------ 给命令用

    def reset_channel(self, channel_id: int) -> None:
        self.store.reset_conversation(channel_id)
        st = self.states.get(channel_id)
        if st is not None:
            st.generation += 1
            st.conversation = None
            for entry in st.recent_files.values():
                entry.read_turns.clear()  # 读过的内容随记忆一起清掉了
                entry.failed.clear()
            for link_entry in st.recent_links.values():
                link_entry.read_turns.clear()
                link_entry.failed.clear()
            st.digest_seen.clear()
            st.listed = None
        for key, log_ in list(self.interactions.items()):
            # 清空了这个频道的记忆，别的频道也不该再“想起”这里聊过的事
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
            st.bucket.ready(view.quota_capacity)  # 先按当前上限截一下
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
