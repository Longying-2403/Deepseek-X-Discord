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
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, AsyncContextManager, Awaitable, Callable, Iterable, Protocol, Sequence

from .actions import dump_actions, parse_actions
from .config import AppConfig
from .files import classify, max_bytes_for, read_file
from .images import MAX_RAW_BYTES, filename_for, to_data_uri, upload_ready
from .ledger import Ledger
from .links import site_name
from .llm import LLMError
from .memory import CardStore, MemoryStore, clean_card, clean_memory, parse_review
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
    CARD_QUOTED,
    DEFAULT_PERSONA,
    DESKTOP_NO_APP,
    DESKTOP_NOT_OWNER,
    DESKTOP_OFF,
    DESKTOP_RESULT,
    DESKTOP_URL_NOT_HIS,
    DM_LIMIT_TEXT,
    EDIT_NOTE,
    EDITED_MARK,
    IMAGE_BLOCKED,
    IMAGE_BUDGET,
    IMAGE_COOLDOWN,
    IMAGE_FAILED,
    IMAGE_LIMIT,
    IMAGE_NONE,
    IMAGE_OFF,
    MUSIC_IDLE,
    MUSIC_NOW,
    MUSIC_OFF,
    MUSIC_RESULT,
    MUSIC_UNANSWERED,
    PERSONA_CARD,
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
    TOOL_FAILED,
    WHO_NONE,
    WHO_OFF,
    build_review_prompt,
    build_system_prompt,
    clean_persona,
)
from .mentions import link, merge_tiers, name_forms
from .music import pick_link
from .quota import QuotaBucket, SlidingWindow, TurnTracker
from .search import today_text
from .render import (
    Gallery,
    assistant_text,
    card_texts,
    content_tokens,
    deleted_line,
    history_lines,
    last_listed,
    last_music_line,
    message_block,
    message_line,
    neutralize,
    neutralize_record,
    own_text,
    public_lines,
    render_cards,
    render_channel_list,
    render_line,
    render_recall,
    render_review_input,
    render_round,
    render_search,
    render_tool_result,
    render_who,
    review_bot_lines,
    review_text,
    rewrite_message,
)
from .settings import Settings, View
from .store import Conversation, Store, Turn
from .textutil import DISCORD_LIMIT, Said, one_line, quoted_cards, quotes_card, split_for_discord, touches_card, typing_delay

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
DM_SETTLE = 3.0  # 私信：对方停下这么久（没再发消息）才回，连着发的几条一起回
DM_TYPING_HOLD = 6.0  # 私信：发完消息后又显示“正在输入”，这么久之内当他还在打字
DM_MAX_WAIT = 15.0  # 私信：最多等这么久就回（一直在打字也回）
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


def _queue_view(action: Action) -> bool:
    """看播放队列：查资料那一步里放在最后办，看到的是这一轮都办完之后的队列（它回话时说的也是那时候的）。"""
    return action.kind == "music" and action.op == "queue"


# 用完要再问一次模型的动作。read / search / recall / persona / who / music 只在被叫到时能用；image 主动聊天时也能用
TOOL_KINDS = ("read", "search", "recall", "persona", "image", "who", "music", "desktop")
MEMORY_KINDS = ("card", "memory")  # 改人设卡、长期记忆：和回话放在一起，不用再问一次模型
MAX_WHO = 2
MAX_REVIEW_CHUNKS = 4  # 一次整理最多分几段问模型（聊天记录太长时）
MAX_REVIEW_CARDS = 3  # 一次整理最多改几张人设卡（每张都要整段重写，太多了输出会很长）
MAX_REVIEW_MEMORY_OPS = 10
MAX_REVIEW_FAILURES = 3  # 同一批聊天连着整理失败几次就放弃
DELETED_KEEP = 1000  # 每个频道记住最近多少条被删掉的消息
PEOPLE_KEEP = 300  # 每个频道记住最近多少个说过话、被 @ 过的人（它 @ 人时按名字找）
MAX_PINGS_PER_ROUND = 3  # 它一次回话（一轮，可能分几条发）最多真的提醒几个人
PING_COOLDOWN = 300.0  # 同一个人这么久之内最多被它提醒一次（之后的 @ 只显示，不发通知）
GUILD_PINGS = 10  # 每个服务器 10 分钟内它最多提醒几次（免得被人叫着到处 @ 人）
GUILD_PING_WINDOW = 600.0
RECENT_EDIT_TURNS = 8  # 最近这么多条记录里的消息被编辑：直接改记录；更早的只在下一轮附一句（改旧记录会让后面的缓存全失效）
MEMBER_SEARCHES_PER_MINUTE = 20  # 按名字在服务器成员里找人，全部服务器加起来每分钟最多几次
REVIEW_TIMEOUT_SCALE = 2.0  # 整理记忆在后台跑、没人在等：最多等两倍的 timeout_seconds
REVIEW_MAX_TOKENS = 6000
_SID_LINE = re.compile(r"^#(\d{1,9}) ")


def _member_blocks(lines: list[str]) -> list[str]:
    """整理记忆的聊天里群友自己说的：一条消息连同续行合成一段（跨行的片段也算他们说过的）。"""
    out: list[str] = []
    member = False  # 上一行是群友的消息（或者它的续行）
    for line in lines:
        if _SID_LINE.match(line):
            out.append(line)
            member = True
        elif member and line.startswith("  "):
            out[-1] += "\n" + line
        else:
            member = False
    return out


_MUSIC_RESULT_HEAD = MUSIC_RESULT.split("{", 1)[0]  # “（点歌：”
SEND_KINDS = ("sticker", "meme", "pick")  # 发出去的是一张图：和说话一样算“开口了”
CHAT_KINDS = ("reply", "react", *SEND_KINDS)  # 群里看得到的动作（照念了人设卡、让它重说时，换掉的是这些）
RECENT_LINKS_KEEP = 30  # 每个频道记住最近多少条带链接的消息（模型可以用 read 要求打开）
MAX_RECALLS = 2
OWNER_EXTRA_TOKENS = 4000  # dsowner 叫它的那一轮：可能要整段重写人设（最多 4000 字），输出上限多给一些
MAX_OUTPUT_CAP = 8000
INTERACTIONS_PER_PERSON = 3  # 每个人记住最近几次跟它的对话（跨频道带近况用）


class Transport(Protocol):
    def channel_info(self, channel_id: int) -> ChannelInfo: ...

    async def send(
        self, channel_id: int, text: str, *, reply_to: int | None, ping: bool, mentions: Sequence[int] = ()
    ) -> int | None: ...  # mentions：这条消息里真的要提醒（ping）的人

    async def react(self, channel_id: int, message_id: int, emoji: str) -> bool: ...

    async def send_sticker(self, channel_id: int, name: str, *, reply_to: int | None, ping: bool) -> int | None: ...

    async def send_file(
        self, channel_id: int, data: bytes, filename: str, *, reply_to: int | None, ping: bool
    ) -> int | None: ...

    def typing(self, channel_id: int) -> AsyncContextManager[Any]: ...

    def locate(self, channel_id: int) -> ChannelPlace | None: ...

    def channel_names(self, guild_id: int) -> dict[str, set[int]]: ...  # 频道名 -> 频道（它看得到的）

    async def find_members(self, guild_id: int, starts: list[str]) -> dict[str, set[int]]: ...  # 按名字开头找人


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


class Jukebox(Protocol):
    """点歌（music.MusicPlayer）。"""

    async def play(
        self,
        guild_id: int,
        text_channel_id: int,
        user_id: int,
        user_name: str,
        query: str,
        *,
        can_search: Callable[[], str | None] | None = None,
        state: Any = None,
    ) -> str: ...

    async def skip(self, guild_id: int, user_id: int, *, state: Any = None) -> str: ...

    async def pause(self, guild_id: int, user_id: int) -> str: ...

    async def resume(self, guild_id: int, user_id: int) -> str: ...

    async def stop(self, guild_id: int, user_id: int) -> str: ...

    async def remove(self, guild_id: int, user_id: int, targets: Sequence[str] = (), *, state: Any = None) -> str: ...

    async def clear(self, guild_id: int, user_id: int, *, state: Any = None) -> str: ...

    def round_state(self, guild_id: int) -> Any: ...  # 一轮点歌动作共用的记录（music.RoundState）

    def queue_text(self, guild_id: int, sep: str = "\n") -> str: ...

    def status(self, guild_id: int) -> str | None: ...

    async def tick(self) -> None: ...


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
class _ReviewChunk:
    """要整理的一段聊天。"""

    lines: list[str]  # 给模型看的聊天记录
    sids: list[int]  # 这一段里群友消息的编号（只从群友的消息里取）
    last_turn: int  # 这一段最后一条记录的 id（整理完就记到这里）
    start: float  # 这一段最早一条记录的时间（判断人设卡是不是在那之后被删过）


@dataclass
class _ReviewPlan:
    chunks: list[_ReviewChunk]
    cards_ok: bool
    memory_ok: bool
    last_id: int


@dataclass
class _ReviewJob:
    """压缩前挑出来、排着队等整理的一批（原话已经挑出来了，记录里已经没有了）。"""

    conv: Conversation
    info: ChannelInfo
    plan: _ReviewPlan
    generation: int


_SAME_NAME_SUFFIX = re.compile(r"\s*[（(]\s*同名的第\s*(\d+)\s*位\s*[)）]\s*$")


def _same_name(given: str, author: str, number: int | None = None) -> bool:
    """整理记忆时模型写的名字和消息作者的名字是不是同一个。提示词里的名字中和、截短（结尾是“…”）过，
    同名的人后面加了“（同名的第 N 位）”：number 是作者是第几位，写了第几位就要对得上。"""
    def norm(x: str) -> str:
        return neutralize(" ".join(x.split())).strip("「」\"' ").casefold()

    given = given.strip()
    suffix = _SAME_NAME_SUFFIX.search(given)
    if suffix is not None:
        if number is not None and int(suffix.group(1)) != number:
            return False  # 写的是同名的另一位
        given = given[: suffix.start()]
    wanted = norm(given)
    real = norm(author)
    if wanted == real or wanted == norm(one_line(author, 32)):
        return True
    return wanted.endswith("…") and len(wanted) > 1 and real.startswith(wanted[:-1])


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


_SECRET_WORDS = ("保密", "秘密", "别告诉", "不要告诉", "别说出去", "不要说出去", "别跟别人说", "不要跟别人说", "别让别人知道", "不要让别人知道", "私下")


def _secret(lines: list[str]) -> bool:
    """私信里的这段对话提到了要保密：不带到服务器的频道里。"""
    return any(word in line for line in lines for word in _SECRET_WORDS)


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
    message_id: int = 0  # 这个人说的那条（被删了就不再带到别的频道；被编辑了就换成新的）


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
    cards_shown: dict[int, tuple[int, float]] = field(default_factory=dict)  # 人 -> (附上他的卡的那条记录, 卡的版本)
    unreviewed_rounds: int = 0  # 上次整理记忆之后又聊了几轮
    reviewing: bool = False
    last_review_at: float = 0.0
    review_failures: int = 0  # 连着整理失败了几次（越失败等得越久）
    review_bad: int = 0  # 其中模型输出不能用的次数（连着几次就放弃这批聊天）
    review_jobs: list[_ReviewJob] = field(default_factory=list)  # 整理的时候又压缩了：压掉的原话排在这里，按先后接着整理
    review_held: int = 0  # 安静时整理的那批正在问的那一段到哪条记录（压缩时只挑这之后的；压缩挑走了后面的，它问完这段就停）
    deleted: dict[int, None] = field(default_factory=dict)  # 最近被删掉的消息（有序：太多了从最早的忘）
    fixes: dict[int, IncomingMessage | None] = field(default_factory=dict)  # 要改写进记录的：消息 -> 改过的样子（None = 删了）
    fixing: bool = False
    people: dict[int, str] = field(default_factory=dict)  # 最近说过话、被 @ 过的人 -> 名字（有序：最近的在后）
    edit_notes: dict[int, tuple[int, IncomingMessage]] = field(default_factory=dict)  # 很早的消息被编辑：消息 -> (编号, 改过的样子)
    music_seen: str | None = None  # 上次告诉它“语音频道里正在放什么”的那一行（变了才再写；没在放是 None）
    private: bool = False  # 私信（guild_id 是它挂在的那个服务器）
    last_typing_at: float = 0.0  # 最近一次有人在打字
    dm_waiting: bool = False  # 私信：正在等对方说完
    dm_notice_day: str = ""  # 私信：哪天已经说过“今天聊太多了”


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
        cards: CardStore | None = None,
        memory: MemoryStore | None = None,
        music: Jukebox | None = None,
        desktop: Any = None,
    ):
        self.cfg = cfg
        self.desktop = desktop  # 操作主人的电脑（dsbot/desktop.py 的 DesktopHub；没填 DESKTOP_TOKEN 时是 None）
        self.music = music  # 点歌（没开、或者缺 yt-dlp / ffmpeg / 语音库时是 None）
        self.cards = cards  # 人设卡（data/cards.sqlite3）
        self.memory = memory  # 长期记忆（data/memory.sqlite3）
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
        self._decode_slots = asyncio.Semaphore(2)  # 同时最多解码 2 张图（大图解码很吃内存）
        self.tz = cfg.bot.timezone
        self.pricing = cfg.pricing
        self.states: dict[int, ChannelState] = {}
        self.limiter = SlidingWindow()
        self.paused_until = 0.0  # 余额不足 / key 错误时全局暂停
        self._last_housekeep = 0.0
        self._tasks: set[asyncio.Task[Any]] = set()
        self.searcher = searcher
        self.interactions: dict[tuple[int, int], deque[Interaction]] = {}  # (服务器, 人) -> 最近跟它的几次对话
        self.pinged: dict[tuple[int, int], float] = {}  # (服务器, 人) -> 上次在话里 @ 提醒他的时间
        self.ping_window = SlidingWindow()  # 每个服务器 10 分钟内提醒了几次（窗口比 limiter 的长，分开清理）
        self._interaction_seq = 0
        self._music_tick: asyncio.Task[Any] | None = None
        self.closing = False  # 正在关机：整理记忆不再接着问下一段（关机时只等聊天的轮次跑完）
        self._recited_rows: set[int] = set()  # 照念人设卡被拦下的那几笔用量记录（结果记成 recited）

    # ------------------------------------------------------------------ 输入

    def state(
        self,
        channel_id: int,
        guild_id: int,
        parent_id: int | None = None,
        category_id: int | None = None,
        private: bool = False,
    ) -> ChannelState:
        st = self.states.get(channel_id)
        if st is None:
            view = self.settings.view(guild_id, channel_id, parent_id, category_id)
            st = ChannelState(channel_id, guild_id, parent_id, QuotaBucket(view.quota_capacity), category_id)
            st.private = private
            self.states[channel_id] = st
            return st
        st.private = st.private or private
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

    def _allowed(self, msg: IncomingMessage) -> bool:
        """这条消息它看不看：频道看“允许回应”；私信看它挂的那个服务器开没开私信。"""
        if msg.private:
            return bool(self.settings.get("dm", guild_id=msg.guild_id))
        return self.enabled(msg.guild_id, msg.channel_id, msg.parent_id, msg.category_id)

    @staticmethod
    def _live(st: ChannelState, view: View) -> bool:
        """这里现在开着：频道看“允许回应”，私信看那个服务器开没开私信。"""
        return bool(view.dm) if st.private else bool(view.enabled)

    async def handle_message(self, msg: IncomingMessage) -> None:
        now = self.clock()
        msg.received_at = now
        msg.from_owner = self._is_owner(msg)
        if not self._allowed(msg):
            # 不在允许的区域：完全不看，之前攒下的也一起丢掉
            st = self.states.get(msg.channel_id)
            if st is not None and st.pending:
                st.pending.clear()
                st.first_human_at = None
            return
        st = self.state(msg.channel_id, msg.guild_id, msg.parent_id, msg.category_id, msg.private)
        if st.private and st.guild_id != msg.guild_id:
            self.move_dm(st.channel_id, msg.guild_id)  # 私信换了服务器（引擎这边还是原来的）：原来那个服务器的记录不带过去
        view = self.view(st)

        st.pending.append(msg)
        self._note_people(st, msg)
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

        if st.private:
            # 私信：每句都在跟它说话。等他说完（停下几秒、不在打字）再一起回，连着发的几条只问一次；
            # 花费另有每人每天的上限（_dm_over），不按“每分钟点名几次”限
            msg.trigger = True
            st.mention_due = True
            self._kick_dm(st)
            return
        if msg.addressed:
            if self.limiter.allow((msg.guild_id, msg.author_id), view.mention_rate_limit, 60.0, now):
                msg.trigger = True
                st.mention_due = True
                self._kick(st)
            else:
                log.info("频道 %s：%s 点名太频繁，这次不立刻回应", st.channel_id, msg.author_name)

    def _note_people(self, st: ChannelState, msg: IncomingMessage) -> None:
        """记下说话的人、被 @ 的人和他们现在的名字（它在话里 @ 人时按名字找）。"""
        found = [] if msg.is_bot else [(msg.author_id, msg.author_name)]
        for uid, name in found + list(msg.mentioned):
            if name:
                st.people.pop(uid, None)
                st.people[uid] = name
        while len(st.people) > PEOPLE_KEEP:
            del st.people[next(iter(st.people))]

    def handle_typing(self, channel_id: int) -> None:
        st = self.states.get(channel_id)
        if st is not None:
            now = self.clock()
            st.last_activity_at = max(st.last_activity_at, now)
            st.last_typing_at = max(st.last_typing_at, now)

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
        """消息被编辑、或 Discord 补上了链接预览：更新还没处理完的那条（返回 False = 不是还没处理完的）。

        还在缓冲区里的：改的时候补上了 @它就算叫它，把 @ 删掉就不回了。正在处理的那一批不改点名状态。"""
        m = self.find_message(updated.channel_id, updated.message_id)
        if m is None:
            return False
        was_addressed = m.addressed
        m.text = updated.text
        m.images = updated.images
        m.files = updated.files
        m.stickers = updated.stickers
        m.links = updated.links  # 预览到了之后，链接多了标题
        m.awaiting_embeds = updated.awaiting_embeds
        m.mentions_bot = updated.mentions_bot
        st = self.states.get(m.channel_id)
        if st is None:
            return True
        self._refresh_entries(st, m)
        if any(x is m for x in st.inflight):
            return True
        if m.addressed and not was_addressed and not m.trigger:
            view = self.view(st)
            if self.limiter.allow((m.guild_id, m.author_id), view.mention_rate_limit, 60.0, self.clock()):
                m.trigger = True
                st.mention_due = True
                self._kick(st)
        elif not m.addressed and m.trigger:
            m.trigger = False
            st.mention_due = any(x.trigger for x in st.pending)
        return True

    def _refresh_entries(self, st: ChannelState, m: IncomingMessage) -> None:
        """消息改了：带文件、带链接的缓存也换成新的样子（删掉的链接、附件就打不开了）。"""
        fentry = st.recent_files.get(m.message_id)
        if fentry is not None:
            if m.files:
                fentry.message = m
            else:
                del st.recent_files[m.message_id]
        elif m.files:
            st.recent_files[m.message_id] = FileEntry(m)
        lentry = st.recent_links.get(m.message_id)
        if lentry is not None:
            if m.links:
                lentry.message = m
            else:
                del st.recent_links[m.message_id]
        elif m.links:
            st.recent_links[m.message_id] = LinkEntry(m)

    def handle_edit(self, updated: IncomingMessage, *, edited: bool) -> None:
        """消息有更新。edited：是本人改了内容（不只是 Discord 补上了链接预览）。

        还没处理的直接更新（见 refresh_message）；已经写进记录的，把记录里那一行换成新内容，标上〔已编辑〕。"""
        st = self.states.get(updated.channel_id)
        if st is not None and updated.message_id in st.deleted:
            return  # 已经删了
        updated.from_owner = self._is_owner(updated)
        if updated.is_bot:
            # 别的 bot 常常一直在改自己的消息（播放进度、投票、倒计时……）：还没处理的照样更新，记录不跟着改，
            # 不然每改一次后面的缓存就全失效
            self.refresh_message(updated)
            return
        if self.refresh_message(updated):
            if edited and st is not None and any(m.message_id == updated.message_id for m in st.inflight):
                self._queue_fix(st, updated.message_id, updated)  # 模型看到的是旧的：这一轮写进记录后再改
            return
        if not edited:
            return  # 处理过的消息只是补上了预览：记录不用动
        st = self._state_with_history(updated.guild_id, updated.channel_id, [updated.message_id])
        if st is None:
            return
        fentry = st.recent_files.get(updated.message_id)
        lentry = st.recent_links.get(updated.message_id)
        if fentry is not None or lentry is not None:
            self._refresh_entries(st, updated)
        self._edit_interactions(st.guild_id, updated)
        self._queue_fix(st, updated.message_id, updated)

    def handle_delete(self, guild_id: int, channel_id: int, message_ids: Iterable[int]) -> None:
        """消息被删了（包括管理员一次删很多条）：缓冲区里的拿掉；正在处理的，结果回来后不回复它；
        已经写进记录的，记录里那一行改成“已删除”，它的图、文件和链接内容也一起拿掉。"""
        ids = set(message_ids)
        if not ids:
            return
        st = self._state_with_history(guild_id, channel_id, ids)
        if st is None:
            return
        for mid in ids:
            st.deleted[mid] = None
        while len(st.deleted) > DELETED_KEEP:
            del st.deleted[next(iter(st.deleted))]
        kept = [m for m in st.pending if m.message_id not in ids]
        if len(kept) != len(st.pending):
            st.pending = kept
            st.first_human_at = next((m.received_at for m in st.pending if not m.is_bot), None)
            st.mention_due = st.mention_due and any(m.trigger for m in st.pending)
        for mid in ids:
            st.recent_files.pop(mid, None)
            st.recent_links.pop(mid, None)
            st.edit_notes.pop(mid, None)
        self._forget_interactions(st.guild_id, ids)
        for mid in ids:
            self._queue_fix(st, mid, None)

    def _state_with_history(self, guild_id: int, channel_id: int, message_ids: Iterable[int]) -> ChannelState | None:
        """这个频道的状态；还没载入过（比如刚重启）的，只有记录里有这几条消息时才建。"""
        st = self.states.get(channel_id)
        if st is not None:
            return st
        if not self.store.message_rows(channel_id, message_ids):
            return None
        place = self.transport.locate(channel_id)
        return self.state(
            channel_id,
            guild_id,
            place.parent_id if place else None,
            place.category_id if place else None,
            bool(place and place.dm),
        )

    def _queue_fix(self, st: ChannelState, message_id: int, updated: IncomingMessage | None) -> None:
        if st.fixes.get(message_id, 0) is None and updated is not None:
            return  # 已经删了：之后的编辑不算
        st.fixes[message_id] = updated
        if st.fixing:
            return
        st.fixing = True
        task = asyncio.get_running_loop().create_task(self._fix_task(st))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fix_task(self, st: ChannelState) -> None:
        """把删除、编辑改写进记录。要等这个频道正在跑的那一轮写完记录（拿到锁）再改。"""
        try:
            while st.fixes:
                async with st.lock:
                    self._flush_fixes(st, self._conversation(st))
        finally:
            st.fixing = False
        if st.mention_due and st.pending:
            self._wake(st)  # 改记录的时候有人叫它：轮到它了

    def _flush_fixes(self, st: ChannelState, conv: Conversation) -> None:
        """把还没改写进记录的删除、编辑先改掉（调用方拿着锁）。开始新的一轮、压缩、整理记忆之前都先做一次，
        免得模型、摘要、整理记忆看到刚删掉的内容。"""
        if not st.fixes:
            return
        fixes, st.fixes = st.fixes, {}
        try:
            self._apply_fixes(st, conv, fixes)
        except Exception:
            log.exception("频道 %s：同步删除、编辑出错", st.channel_id)

    def _apply_fixes(self, st: ChannelState, conv: Conversation, fixes: dict[int, IncomingMessage | None]) -> None:
        rows = self.store.message_rows(st.channel_id, fixes.keys())
        changed: dict[int, Turn] = {}
        recent_from = len(conv.turns) - RECENT_EDIT_TURNS
        for mid, updated in fixes.items():
            for sid, name in rows.get(mid, []):
                if updated is None:
                    lines: list[str] | None = None
                    drop_images = drop_docs = True
                else:
                    block = render_line(updated, sid, st.sid_by_msg, self.ec.max_chars_per_message).split("\n")
                    where = next(
                        ((i, old) for i, t in enumerate(conv.turns) if t.role == "user" and (old := message_block(t.content, sid)) is not None),
                        None,
                    )
                    if where is None:
                        continue  # 已经压进摘要了
                    index, old = where
                    if old[:-1] + [old[-1].removesuffix(EDITED_MARK)] == block:
                        continue  # 内容其实没变（比如只是补上了预览）
                    if index < recent_from:
                        st.edit_notes[mid] = (sid, updated)  # 很早的消息：下一轮附一句改成了什么，旧记录不动
                        continue
                    lines = block[:-1] + [block[-1] + EDITED_MARK]
                    drop_images = not updated.images  # 图都删掉了：给它看过的图也拿掉（删了一部分的分不出是哪张，留着）
                    drop_docs = not updated.files and not updated.links
                for turn in conv.turns:
                    if turn.role != "user":
                        continue
                    new_lines = lines
                    if updated is None:
                        old = message_line(turn.content, sid)
                        new_lines = [deleted_line(sid, name, old or "")] if old is not None else None
                    content, did = rewrite_message(
                        turn.content, sid, new_lines, drop_images=drop_images, drop_docs=drop_docs, drop_notes=updated is None
                    )
                    if did:
                        turn.content = content
                        changed[turn.id] = turn
        for turn in changed.values():
            self.store.update_turn(turn.id, turn.content)
        if changed:
            deleted = sum(1 for u in fixes.values() if u is None)
            log.info(
                "频道 %s：同步了 %d 条删除、%d 条编辑，改写了 %d 条记录",
                st.channel_id,
                deleted,
                len(fixes) - deleted,
                len(changed),
            )

    def _forget_interactions(self, guild_id: int, message_ids: set[int]) -> None:
        """被删掉的消息不再当成“刚才在别的频道聊过”带到别处。"""
        for (g, _), log_ in self.interactions.items():
            if g != guild_id:
                continue
            for item in [i for i in log_ if i.message_id in message_ids]:
                log_.remove(item)

    def _edit_interactions(self, guild_id: int, updated: IncomingMessage) -> None:
        log_ = self.interactions.get((guild_id, updated.author_id))
        for item in log_ or ():
            if item.message_id == updated.message_id and item.lines:
                said = one_line(updated.text, 150) if updated.text.strip() else "（发了图片或文件）"
                item.lines[0] = f"{one_line(updated.author_name, 32)}：{said}"

    def _prune(self, st: ChannelState, now: float) -> None:
        """丢掉缓冲区里太旧的消息，以及超出上限的部分（从最早的丢，在叫它的留着）。"""
        cutoff = now - self.ec.pending_max_age_seconds
        fresh = [m for m in st.pending if m.received_at >= cutoff]
        overflow = len(fresh) - self.ec.max_pending_messages
        if overflow > 0:
            st.omitted += overflow
            drop = {id(m) for m in [m for m in fresh if not m.trigger][:overflow]}
            if len(drop) < overflow:  # 全是点名（几乎不可能）：只好从最早的丢
                drop |= {id(m) for m in [m for m in fresh if id(m) not in drop][: overflow - len(drop)]}
            fresh = [m for m in fresh if id(m) not in drop]
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
            if self._review_due(st, now):
                self._kick_review(st)  # 安静了一阵：整理一下记忆
                continue
            if st.scheduled or st.lock.locked() or not st.pending:
                continue
            if st.mention_due or self._proactive_ready(st, now):
                self._wake(st)
        if now - self._last_housekeep >= HOUSEKEEP_EVERY:
            self._last_housekeep = now
            self._housekeep(now)
        if self.music is not None and (self._music_tick is None or self._music_tick.done()):
            # 断线、没人听、放完很久：离开语音频道。单独跑：离开语音频道要等 Discord 回应，别耽误各频道的定时检查
            self._music_tick = asyncio.get_running_loop().create_task(self._tick_music())
            self._tasks.add(self._music_tick)
            self._music_tick.add_done_callback(self._tasks.discard)

    async def _tick_music(self) -> None:
        assert self.music is not None
        try:
            await self.music.tick()
        except Exception:
            log.exception("点歌的定时检查出错")

    def _housekeep(self, now: float) -> None:
        self.limiter.prune(60.0, now)
        for key in [k for k, at in self.pinged.items() if now - at >= PING_COOLDOWN]:
            del self.pinged[key]
        self.ping_window.prune(GUILD_PING_WINDOW, now)
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
        if self.closing:
            return  # 关机了：不再开新的一轮（攒着的消息退出时反正也没了）
        if st.scheduled or st.lock.locked():
            return  # 正在跑的那一轮结束后会再检查
        st.scheduled = True
        task = asyncio.get_running_loop().create_task(self._run(st))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _wake(self, st: ChannelState) -> None:
        """有人叫它、还没回：频道马上开始这一轮；私信先等对方说完。"""
        if st.private:
            self._kick_dm(st)
        else:
            self._kick(st)

    def _kick_dm(self, st: ChannelState) -> None:
        """私信：等对方说完再开始这一轮（已经在等了就不用再等一份）。"""
        if self.closing or st.dm_waiting:
            return
        st.dm_waiting = True
        task = asyncio.get_running_loop().create_task(self._dm_settle(st))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dm_settle(self, st: ChannelState) -> None:
        try:
            start = self.clock()
            for _ in range(20):
                if self.closing:
                    return
                now = self.clock()
                last = max((m.received_at for m in st.pending if not m.is_bot), default=start)
                ready = last + DM_SETTLE
                if st.last_typing_at > last:  # 发完又开始打字了：多等一会儿
                    ready = max(ready, st.last_typing_at + DM_TYPING_HOLD)
                wait = min(ready, start + DM_MAX_WAIT) - now
                if wait <= 0:
                    break
                await self.sleep(wait)
                if self.clock() <= now:
                    break  # 时间没走（测试里的假时钟）：不再等了
        finally:
            st.dm_waiting = False
        self._kick(st)

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

    def _dm_over(self, st: ChannelState, view: View, triggers: list[IncomingMessage], over: bool, now: float) -> bool:
        """私信超了预算：那个服务器今天的预算用完了（先留给群里点名），或者这个人今天私信花的到了上限。
        bot 主人自己私信只受“超过预算好几倍连点名也不回”的限制。"""
        if any(m.from_owner for m in triggers):
            return False
        if over:
            return True
        return self.store.channel_spent(self.ledger.day(now), st.channel_id) >= view.dm_daily_budget_usd

    async def _dm_limit_notice(self, st: ChannelState, last: IncomingMessage, now: float) -> None:
        """今天第一次超的时候说一声，之后只加个表情。"""
        day = self.ledger.day(now)
        if st.dm_notice_day != day:
            st.dm_notice_day = day
            if await self.transport.send(st.channel_id, DM_LIMIT_TEXT, reply_to=None, ping=False) is not None:
                return
        await self._safe_react(st, last.message_id, "💤")

    def _proactive_ready(self, st: ChannelState, now: float) -> bool:
        view = self.view(st)
        if st.private or not view.enabled or not view.proactive or st.first_human_at is None:
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
            if not self._live(st, self.view(st)):
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
            self._wake(st)

    # ------------------------------------------------------------------ 一轮

    def _conversation(self, st: ChannelState) -> Conversation:
        if st.conversation is None:
            st.conversation = self.store.load_conversation(st.channel_id, st.guild_id)
            st.listed = last_listed(st.conversation.turns)
            for sid, mid in self.store.load_msgmap(st.channel_id).items():
                st.msg_by_sid.setdefault(sid, mid)
                st.sid_by_msg.setdefault(mid, sid)
            older = [(uid, name) for uid, name in reversed(self.store.recent_authors(st.channel_id, PEOPLE_KEEP)) if uid not in st.people]
            st.people = {**dict(older), **st.people}  # 记录里的在前，刚收到的（更新的名字）在后
            # 记录里最后写的“正在放什么”（重启之后已经不在放了：下次被叫到时要告诉它）
            st.music_seen = last_music_line(st.conversation.turns)
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
        if st.private and kind == "mention" and self._dm_over(st, view, triggers, over, now):
            log.info("私信 %s：今天的私信预算用完了，不回", st.channel_id)
            for m in triggers:
                m.trigger = False
            await self._dm_limit_notice(st, triggers[-1], now)
            return

        if any(m.trigger and m.awaiting_embeds and now - m.received_at < EMBED_WAIT for m in batch):
            # GIF / 图片链接的预览图通常晚一两秒才到（消息更新事件会把图补进同一个对象里）
            await self.sleep(EMBED_WAIT)
            now = self.clock()

        conv = self._conversation(st)
        self._flush_fixes(st, conv)
        if any(m.message_id in st.deleted for m in batch):
            # 准备这一轮的时候（比如等链接预览）被删掉的：不给模型看，也不写进记录
            batch = [m for m in batch if m.message_id not in st.deleted]
            triggers = [m for m in triggers if m.message_id not in st.deleted]
            if not batch or (kind == "mention" and not triggers):
                return  # 叫它的消息都删了：这一轮不跑，剩下的消息留着以后看
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
        noted = dict(st.edit_notes)
        context = [
            EDIT_NOTE.format(sid=sid, name=neutralize(one_line(m.author_name or "某人")), text=neutralize(one_line(m.text, 300)))
            for sid, m in noted.values()
        ] + context
        playing = st.music_seen  # 写进记录的那一行（没在放是 None）
        if kind == "mention":
            status = self.music.status(st.guild_id) if self.music is not None and not st.private else None
            playing = MUSIC_NOW.format(text=neutralize(one_line(status, 300))) if status is not None else None
            if playing != st.music_seen:  # 换了歌、停了：告诉它一声（只写在这一轮里，system prompt 不变）
                context.append(playing or MUSIC_IDLE)
        card_lines, card_marks = self._card_context(st, view, conv, numbered, triggers)

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
                cards=card_lines,
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
        seen = self._seen_cards(conv, user_content)  # 这次请求里它看得到的人设卡（从记录里认：重启之后也算）
        actions, stored = self._interpret(
            st, completion, kind, targets, allow_tools=kind == "mention", extra_tools=extra, cards_here=bool(card_lines or seen)
        )
        completion, cost, row, actions, stored = await self._no_card_quotes(
            st,
            conv,
            system,
            user_content,
            kind,
            targets,
            numbered,
            seen,
            (completion, cost, row, actions, stored),
            thinking=thinking,
            generation=generation,
            max_tokens=max_tokens,
        )
        if st.generation != generation:
            log.info("频道 %s：这一轮进行中记忆被清空，丢弃结果", st.channel_id)
            self.store.set_outcome(row, "discarded")
            self._consume(st, batch, now)
            return

        # 先把这一轮写进历史（只追加），再消费缓冲区。消息编号连同发消息的人一起记下（整理记忆时要知道是谁说的）
        assigned = [(sid, m.message_id, None if m.is_bot else m.author_id, m.author_name) for sid, m in numbered]
        before = len(conv.turns)  # 这一轮之前的记录（核对写卡时：它这一轮自己写的不算“以前替他写过的”）
        self.store.append_round(conv, user_content, stored, assigned, now=now)
        self._consume(st, batch, now)
        self._mark_read(conv, planned)
        self._mark_links(conv, opened)
        self._mark_cards(st, conv, card_marks)
        for mid in noted:
            if st.edit_notes.get(mid) is noted[mid]:
                del st.edit_notes[mid]  # 已经告诉它了
        st.music_seen = playing
        st.unreviewed_rounds += 1
        st.digest_seen.update(digest_marks)
        if listed is not None:
            st.listed = listed

        tools = [a for a in actions if a.kind in TOOL_KINDS]
        if tools:
            # 模型要先看文件 / 打开链接 / 上网搜 / 找图 / 翻别的频道 / 改人设：办完再问它一次（第二步不能再用这些，
            # 只有第一步没改人设时，第二步还能改一次人设，比如看完角色卡再照着改）
            self.store.set_outcome(row, "+".join(sorted({a.kind for a in tools})))
            if kind != "mention":
                st.last_decision = "想发图，先去找"
            else:
                st.last_decision = "被点名，先去点歌" if all(a.kind == "music" for a in tools) else "被点名，先去查资料"
            allow_persona = any(m.from_owner for m in triggers) and any(a.kind == "read" for a in tools)
            step = await self._tool_step(
                st, conv, tools, targets, view, info, system, generation, budget, numbered, allow_persona, max_tokens, kind=kind
            )
            if step is None:
                return
            completion, cost, row, actions, seen = step
            late = [a for a in actions if a.kind == "persona"]
            if late:
                self.store.set_outcome(row, "persona")
                step = await self._tool_step(
                    st, conv, late, targets, view, info, system, generation, budget, numbered, False, max_tokens
                )
                if step is None:
                    return
                completion, cost, row, actions, seen = step

        # 改人设卡、记长期记忆：先办（只是写进数据库），再发消息
        actions = await self._apply_memory_actions(st, view, actions, numbered, targets, kind, seen, conv.turns[:before])
        actions = self._live_actions(st, actions, kind, targets)  # 冲着已经删掉的消息的：不发，也不算开口
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
            self.store.set_outcome(row, "recited" if row in self._recited_rows else outcome)
            self._recited_rows.discard(row)
            # 对话里附着人设卡时不给管理员看模型的“想法”（里面可能引用了别人的卡）
            st.last_decision = self._describe(kind, outcome, actions, completion, hide_reason=bool(st.cards_shown or card_lines or seen))
        # 发的时候才被删掉的那几条没发出去，不算“发不出消息”
        decided_text = any(a.kind == "reply" and a.messages for a in self._live_actions(st, actions, kind, targets))
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
            "（附着人设卡，想法略）" if st.cards_shown or card_lines or seen else one_line(completion.reasoning or "", 160),
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
        cards_here: bool = False,
    ) -> tuple[list[Action], str]:
        """解析模型输出，返回 (动作, 写进历史的内容)。cards_here：这次请求里新附了人设卡（还没记进 cards_shown）。"""
        actions, ok = parse_actions(completion.content)
        if not ok:
            # 对话里有人设卡时不把输出写进日志（里面可能照抄了别人的卡）
            shown = "（附着人设卡，内容略）" if st.cards_shown or cards_here else one_line(completion.content, 200)
            log.warning("频道 %s：模型输出不是合法 JSON：%s", st.channel_id, shown)
            text = completion.content.strip()
            # 不像 JSON 的才当成直接说的话发出去；用 ``` 框起来的是写坏了的 JSON，别把它发到频道里
            if kind == "mention" and text and not text.startswith(("{", "[", "```")):
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
                + [a for a in tools if a.kind == "who"][:MAX_WHO]
                + [a for a in tools if a.kind == "image"][:1]
                + [a for a in tools if a.kind == "music"]  # 解析时已经限过数量
                + [a for a in tools if a.kind == "desktop"]
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
    ) -> tuple[Completion, float, int, list[Action], list[str]] | None:
        now = self.clock()
        reads = [a for a in tools if a.kind == "read"]
        plan, link_plan, file_notes, wanted = self._plan_reads(st, view, reads)
        sections: list[str] = []
        gallery: Gallery | None = None
        who_marks: list[tuple[int, float]] = []  # 查到的人设卡：写进记录后记成“已经给它看过了”

        async def work() -> tuple[list[tuple[int, DocBlock, _Planned, FileText]], list[tuple[_LinkPlan, DocBlock, FileText]]]:
            nonlocal gallery
            results = await self._read_planned(st, plan, budget)
            opened = await self._read_links(st, link_plan, budget, now)
            # 点歌的动作照模型写的顺序一个一个办（“放 X，然后删掉”就是先放再删；看队列放在最后），同一轮共用一份记录
            # （music.RoundState）：编号按这一轮开始时的队列算，切歌只切那时正在放的那首，清空只清那时排着的。
            # 同一个人接连写的几个删歌一起删
            ordered = [a for a in tools if not _queue_view(a)] + [a for a in tools if _queue_view(a)]
            music_round: Any = None
            i = 0
            while i < len(ordered):
                action = ordered[i]
                i += 1
                if action.kind == "music":
                    if music_round is None and self.music is not None:
                        music_round = self.music.round_state(st.guild_id)
                    also: list[Action] = []
                    while (
                        action.op == "remove"
                        and i < len(ordered)
                        and ordered[i].kind == "music"
                        and ordered[i].op == "remove"
                        and self._music_to(ordered[i], targets) == self._music_to(action, targets)
                    ):
                        also.append(ordered[i])
                        i += 1
                    sections.append(
                        await self._music_section(st, view, action, numbered, targets, also=also, state=music_round)
                    )
                elif action.kind == "desktop":
                    sections.append(await self._desktop_section(st, view, info, action, numbered, targets))
                elif action.kind == "search":
                    sections.append(await self._search_section(st, view, action, now))
                elif action.kind == "recall":
                    sections.append(self._recall_section(st, view, action, info))
                elif action.kind == "persona":
                    sections.append(await self._persona_section(st, action, numbered, targets, self._seen_cards(conv)))
                elif action.kind == "who":
                    sections.append(self._who_section(st, view, action, who_marks))
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
            if st.generation == generation and st.conversation is conv and conv.turns and conv.turns[-1].role == "assistant":
                # 上一步记下的是“要去查”：补一句没查成，免得下一轮它以为还在等结果、又去查一次。
                # 点歌是真的办了的（歌已经在放、在排队）：把结果也记上，免得它以为没点上、又点一次
                played = [s for s in sections if s.startswith(_MUSIC_RESULT_HEAD)]
                lines = [TOOL_FAILED] if any(a.kind != "music" for a in tools) else []
                if played:
                    lines += [MUSIC_UNANSWERED, *played]
                self.store.append_round(conv, "\n".join(lines) or TOOL_FAILED, dump_actions([]), [], now=now)
            return None
        extra = ("persona",) if allow_persona else ()
        seen = self._seen_cards(conv, content)  # 记录里已经有第一步那一轮；who 查到的卡在 content 里
        actions, stored = self._interpret(
            st, completion, kind, targets, allow_tools=False, extra_tools=extra, cards_here=bool(who_marks or seen)
        )
        completion, cost, row, actions, stored = await self._no_card_quotes(
            st,
            conv,
            system,
            content,
            kind,
            targets,
            numbered,
            seen,
            (completion, cost, row, actions, stored),
            thinking=thinking,
            generation=generation,
            max_tokens=max_tokens,
        )
        if st.generation != generation:
            self.store.set_outcome(row, "discarded")
            return None
        self.store.append_round(conv, content, stored, [], now=now)
        self._mark_read(conv, [(item, result) for _, _, item, result in results])
        self._mark_links(conv, opened)
        self._mark_cards(st, conv, who_marks)
        return completion, cost, row, actions, seen

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

    # ------------------------------------------------------------------ 点歌

    @staticmethod
    def _music_to(action: Action, targets: list[int]) -> int | None:
        """music 动作替谁办：写了 to（而且那条消息在叫它）就是那条消息的人，没写就是最后一个叫它的。"""
        return action.to if action.to in targets else (targets[-1] if targets else None)

    def _music_who(
        self, st: ChannelState, action: Action, numbered: list[tuple[int, IncomingMessage]], targets: list[int]
    ) -> IncomingMessage | None:
        """替谁点歌（那条消息被删了、是 bot 发的就是 None）。"""
        to = self._music_to(action, targets)
        m = dict(numbered).get(to) if to is not None else None
        return None if m is None or m.is_bot or m.message_id in st.deleted else m

    async def _music_section(
        self,
        st: ChannelState,
        view: View,
        action: Action,
        numbered: list[tuple[int, IncomingMessage]],
        targets: list[int],
        also: Sequence[Action] = (),
        state: Any = None,
    ) -> str:
        """模型的 music 动作：替叫它的那个人点歌、切歌、删歌……also：同一个人接着写的别的删歌（和 action 一起删）；
        state：这一轮的点歌动作共用的记录（music.RoundState）。"""
        if self.music is None or st.private:
            return MUSIC_OFF
        m = self._music_who(st, action, numbered, targets)
        if m is None:
            return MUSIC_RESULT.format(text="不知道是谁要点歌，这次没点")
        op = action.op or "play"
        query = action.query or ""
        if op == "play":
            query = pick_link(query, m.text)  # 它抄的链接可能抄错：消息里正好有一个 B站链接就用消息里的
        wanted = [str(a.index) if a.index is not None else (a.query or "") for a in (action, *also)] if op == "remove" else []
        text = await self.music_request(
            st.guild_id,
            st.channel_id,
            m.author_id,
            m.author_name,
            op,
            "" if op == "remove" else query,
            view=view,
            sep="；",
            targets=wanted,
            state=state,
        )
        what = "、".join(w or "他最近点的" for w in wanted) if op == "remove" else one_line(action.query or "", 40)
        log.info("频道 %s：%s 点歌（%s %s）→ %s", st.channel_id, m.author_name, op, what, one_line(text, 80))
        return MUSIC_RESULT.format(text=neutralize(one_line(text, 800)))

    async def music_request(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        user_name: str,
        op: str,
        query: str = "",
        *,
        view: View,
        sep: str = "\n",
        targets: Sequence[str] = (),
        state: Any = None,
    ) -> str:
        """点歌、切歌、暂停、继续、停止、删歌、清空、看队列（模型的 music 动作和 /ds-music 都走这里）。返回给人看的结果。
        view：点歌的那个频道的设置（按歌名找歌要算搜索次数）。targets：删歌时要删的几首（不写就看 query）。
        state：模型这一轮的点歌动作共用的记录（music.RoundState；/ds-music 不用）。"""
        music = self.music
        if music is None:
            return "点歌没有启用"
        try:
            if op == "play":
                return await music.play(
                    guild_id,
                    channel_id,
                    user_id,
                    user_name,
                    query,
                    can_search=lambda: self._music_search(guild_id, channel_id, user_id, view),
                    state=state,
                )
            if op == "skip":
                return await music.skip(guild_id, user_id, state=state)
            if op == "pause":
                return await music.pause(guild_id, user_id)
            if op == "resume":
                return await music.resume(guild_id, user_id)
            if op == "stop":
                return await music.stop(guild_id, user_id)
            if op == "remove":
                return await music.remove(guild_id, user_id, list(targets) or ([query] if query else []), state=state)
            if op == "clear":
                return await music.clear(guild_id, user_id, state=state)
            return music.queue_text(guild_id, sep=sep)
        except Exception:
            log.exception("服务器 %s：点歌（%s）出错", guild_id, op)
            return "点歌的时候出错了"

    def _desktop_here(self, st: ChannelState, view: View, info: ChannelInfo) -> bool:
        """这里能操作电脑：开着桌面操作，而且是 dsowner 自己的私信。"""
        return (
            self.desktop is not None
            and bool(view.desktop)
            and st.private
            and self.owner_id is not None
            and info.dm_user == self.owner_id
        )

    async def _desktop_section(
        self,
        st: ChannelState,
        view: View,
        info: ChannelInfo,
        action: Action,
        numbered: list[tuple[int, IncomingMessage]],
        targets: list[int],
    ) -> str:
        """模型的 desktop 动作：只替 dsowner 本人这一轮的要求办（开关白名单里的程序、打开他自己发的网址）。"""
        if not self._desktop_here(st, view, info):
            return DESKTOP_OFF
        by_sid = dict(numbered)
        asked = [by_sid[t] for t in targets if t in by_sid and by_sid[t].from_owner and by_sid[t].message_id not in st.deleted]
        if not asked:
            return DESKTOP_NOT_OWNER
        op, target = action.op or "", (action.query or "").strip()
        if op == "url":
            said = " ".join(m.text for m in asked)
            if not target or target not in said:
                log.info("私信 %s：要打开的网址不是 dsowner 这条消息里的，没打开", st.channel_id)
                return DESKTOP_URL_NOT_HIS
        else:
            app = self.desktop.match(target)
            if app is None:
                names = "、".join(self.desktop.app_names()) or "还没有"
                return DESKTOP_NO_APP.format(name=neutralize(one_line(target, 40)), apps=names)
            target = app.key
        ok, text = await self.desktop.run(op, target)
        log.info("私信 %s：电脑 %s %s → %s", st.channel_id, op, one_line(target, 80), one_line(text, 80))
        return DESKTOP_RESULT.format(text=neutralize(one_line(("" if ok else "没办成：") + text, 300)))

    def _music_search(self, guild_id: int, channel_id: int, user_id: int, view: View) -> str | None:
        """按歌名找歌算一次联网搜索（和 search 共用每日次数）。能搜就记一次、返回 None；不能搜返回原因。
        每人每分钟最多按歌名找“每人每分钟点名”那么多次（/ds-music 不经过点名限速，免得一个人一下子把全服务器的次数用光）。"""
        limit = int(view.daily_search_limit)
        if limit <= 0:
            return "这里不能按歌名找歌（每日搜索次数上限是 0）；直接贴 B站链接还能放"
        now = self.clock()
        day = self.ledger.day(now)
        if self.store.count_usage(day, guild_id=guild_id, kind="search") >= limit:
            return f"今天这个服务器的搜索次数用完了（{limit} 次），按歌名找不了；直接贴 B站链接还能放"
        if not self.limiter.allow(("music-search", guild_id, user_id), max(1, int(view.mention_rate_limit)), 60.0, now):
            return "按歌名点歌太频繁了，过一分钟再点；直接贴 B站链接不受影响"
        self.store.record_usage(ts=now, day=day, guild_id=guild_id, channel_id=channel_id, kind="search", outcome="music")
        return None

    def _finish_search(self, st: ChannelState, row: int, usage: Usage, outcome: str) -> None:
        now = self.clock()
        cost = self.pricing.cost(usage, now)
        self.ledger.add(st.guild_id, cost, now)
        # 记录也挪到搜完的这一刻：搜的时候跨过了午夜，账本和数据库也记在同一天（重启后从库里读的预算才对得上）
        self.store.update_usage(
            row, outcome=outcome, hit=usage.hit, miss=usage.miss, output=usage.output, cost=cost, ts=now, day=self.ledger.day(now)
        )

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

    # ------------------------------------------------------------------ 人设卡和长期记忆

    def _cards_on(self, view: View) -> bool:
        return bool(view.cards) and self.cards is not None

    def _memory_on(self, view: View) -> bool:
        return bool(view.long_memory) and self.memory is not None

    def implicit_ok(self, channel_id: int, key: str) -> bool:
        """整理记忆时能不能把这个频道的聊天记进人设卡（key = cards）/ 长期记忆（long_memory）。

        人设卡、长期记忆在整个服务器都看得到：公开频道可以；私密频道要在那个频道上单独打开这一项，
        免得私密频道里聊的事在公开频道冒出来。有人明确叫它记住的不受这个限制（他自己要记的）。"""
        place = self.transport.locate(channel_id)
        if place is None:
            return False
        if place.public or place.dm:
            return True  # 私信：和服务器互通（私信里它会提醒自己，他要保密的不记）
        explicit = self.settings.overrides("channel", channel_id).get(key)
        if explicit is None and place.parent_id is not None and not place.private_thread:
            explicit = self.settings.overrides("channel", place.parent_id).get(key)
        return explicit is True

    def _card_context(
        self,
        st: ChannelState,
        view: View,
        conv: Conversation,
        numbered: list[tuple[int, IncomingMessage]],
        triggers: list[IncomingMessage],
    ) -> tuple[list[str], list[tuple[int, float | None]]]:
        """这一轮说话的人的人设卡。这段对话里已经给过、卡也没改过的不再给。返回 (文字行, [(人, 卡的版本)])。"""
        if not numbered or not self._cards_on(view):
            return [], []
        assert self.cards is not None
        oldest = conv.turns[0].id if conv.turns else None
        people: dict[int, str] = {}
        for m in triggers:  # 叫它的人优先
            if not m.is_bot:
                people.setdefault(m.author_id, m.author_name)
        for _, m in reversed(numbered):  # 再按最近说话的顺序
            if not m.is_bot:
                people.setdefault(m.author_id, m.author_name)
        shown: list[tuple[str, str | None]] = []
        marks: list[tuple[int, float | None]] = []
        for uid, name in people.items():
            if len(shown) >= max(0, self.ec.cards_per_round):
                break
            card = self.cards.get(st.guild_id, uid)
            if card is None:
                seen = st.cards_shown.get(uid)
                if seen is not None:
                    if oldest is not None and seen[0] >= oldest:
                        shown.append((name, None))  # 对话里还附着被删掉的那份：告诉它别再用
                    marks.append((uid, None))  # 这一轮写进记录之后就不用再说了
                continue
            if name and card.name != name:
                self.cards.touch_name(st.guild_id, uid, name)  # 改了昵称：按新名字也能查到
            seen = st.cards_shown.get(uid)
            if seen is not None and seen[1] == card.updated_at and oldest is not None and seen[0] >= oldest:
                continue
            shown.append((name or card.name, card.text))
            marks.append((uid, card.updated_at))
        return render_cards(shown), marks

    def _mark_cards(self, st: ChannelState, conv: Conversation, marks: list[tuple[int, float | None]]) -> None:
        """附上过的人设卡记成“已经在对话里了”（记下是哪条记录；压缩记忆删掉那条、或者卡改了，再附一次）。
        版本是 None：告诉过它这张卡删掉了，不用再记着。这一轮写进记录之后才记（没写成就下一轮再说）。"""
        if not marks or not conv.turns:
            return
        turn_id = conv.turns[-2].id if len(conv.turns) >= 2 else conv.turns[-1].id
        for uid, version in marks:
            if version is None:
                st.cards_shown.pop(uid, None)
            else:
                st.cards_shown[uid] = (turn_id, version)

    def _who_section(self, st: ChannelState, view: View, action: Action, marks: list[tuple[int, float]]) -> str:
        """who：按名字查人设卡（聊到不在场的人时）。"""
        name = action.name or ""
        if not self._cards_on(view):
            return WHO_OFF
        assert self.cards is not None
        found = self.cards.find(st.guild_id, name)
        if not found:
            return WHO_NONE.format(name=neutralize(one_line(name, 32)))
        for card in found:
            marks.append((card.user_id, card.updated_at))
        log.info("频道 %s：查了「%s」的人设卡（%d 张）", st.channel_id, one_line(name, 32), len(found))
        return render_who(name, [(card.name, card.text) for card in found])

    def _seen_cards(self, conv: Conversation, content: Any = None) -> list[str]:
        """这次请求里它看得到的人设卡原文：记录里附过的、who 查到的、它自己用 card 动作写过的（记录在数据库里，
        重启之后、卡删掉之后还留在记录里的也算；压缩掉的它就看不到了），加上这一次刚附上的（content）。"""
        texts: list[str] = []
        for t in conv.turns:
            if t.role == "user":
                texts += card_texts(t.content)
                continue
            raw = t.content if isinstance(t.content, str) else json.dumps(t.content, ensure_ascii=False)
            if "card" in raw.lower() or "人设卡" in raw:  # card、set_card……这几种写法都算改人设卡
                texts += [a.text for a in parse_actions(raw)[0] if a.kind == "card" and a.text]
        texts += card_texts(content)
        return list(dict.fromkeys(texts))

    @staticmethod
    def _said(conv: Conversation | None, *groups: Iterable[str], content: Any = None) -> Said:
        """群友公开说过的话：groups（这一批的消息之类）加上记录里（和这一次的 content 里）群友自己说的、发的文件和链接。
        bot 加的提示（模型自己写的搜索词、频道名）、附着的人设卡、别的频道带来的都不算（不然人设卡的原文能借它们“洗白”）。"""
        first = [text for group in groups for text in group]

        def make() -> list[str]:
            lines = list(first)
            for t in conv.turns if conv is not None else ():
                if t.role == "user":
                    lines += public_lines(t.content)
            return lines + public_lines(content)

        return Said(make)

    def _drafts(self, st: ChannelState, turns: Sequence[Turn], author_id: int) -> set[str]:
        """这一轮之前的记录里它替这个人写过的卡：是他自己的（以前的版本），不是“别人的卡”。
        记录里留着的 card 动作都是写成了的：没写成的（太长、照抄别人的卡……）当时就从记录里拿掉了。"""
        writes: list[tuple[int, str]] = []
        for t in turns:
            if t.role != "user":
                raw = t.content if isinstance(t.content, str) else json.dumps(t.content, ensure_ascii=False)
                if "card" in raw.lower() or "人设卡" in raw:
                    writes += [(a.to, a.text) for a in parse_actions(raw)[0] if a.kind == "card" and a.text and a.to is not None]
        if not writes:
            return set()
        authors = self.store.load_authors(st.channel_id, [to for to, _ in writes if 0 < to < 2**31])
        return {text for to, text in writes if authors.get(to, (None, ""))[0] == author_id}

    @staticmethod
    def _copies_card(text: str, others: Sequence[str], mine: Sequence[str], said: Iterable[str] | Said) -> bool:
        """新写的卡有没有照抄别人的卡。本来就在他自己卡上的那几段（几个人的卡里都有的一段，比如公会的规矩）不算抄，
        只对这种“本来就和他的卡重了”的卡放宽；别的卡照常查（一轮一轮一点点抄进自己的卡，加起来也会被拦）。"""
        shared = quoted_cards("\n".join(mine), others)
        plain = [c for c in others if c not in shared]
        public = said if isinstance(said, Said) else Said(lambda lines=list(said): lines)
        return quotes_card(text, plain, said=public) or quotes_card(text, shared, said=public.plus(mine))

    def _screen_memory(self, guild_id: int, items: list[Any], text_of: Callable[[Any], str | None], cards: list[str], said: Iterable[str]) -> list[Any]:
        """要记进长期记忆的几条：和已经记着的连起来照抄了人设卡的不记（一张卡拆成几条分开记，拼起来还是原文）。
        长期记忆每个频道的提示词里都有、管理员也看得到。text_of 返回 None 的不是要记的话，原样留着。"""
        if not cards or not any(text_of(item) for item in items):
            return items
        base = [m.text for m in self.memory.entries(guild_id)] if self.memory is not None else []
        # 以前就记着照抄了某张卡的（这个功能之前记的）：那张卡只看新的这条自己；别的卡照常连起来查
        stale = quoted_cards("\n".join(base), cards, said=said)
        fresh = [c for c in cards if c not in stale]
        public = said if isinstance(said, Said) else Said(lambda lines=list(said): lines)
        # 那张卡已经有一大块在长期记忆里了：已经记着的那几段不算，但再记它的原话，哪怕一小段（连着 8 个词）也不行
        old = public.plus(base)
        out = []
        for item in items:
            text = text_of(item)
            if text:
                if touches_card(text, stale, said=old) or quotes_card("\n".join([*base, text]), fresh, said=public):
                    continue
                base.append(text)
            out.append(item)
        return out

    async def _no_card_quotes(
        self,
        st: ChannelState,
        conv: Conversation,
        system: str,
        content: Any,
        kind: str,
        targets: list[int],
        numbered: list[tuple[int, IncomingMessage]],
        seen: list[str],
        step: tuple[Completion, float, int, list[Action], str],
        *,
        thinking: str,
        generation: int,
        max_tokens: int | None,
    ) -> tuple[Completion, float, int, list[Action], str]:
        """人设卡的原文不照念：要发的话里有一长段和它看得到的卡一模一样，被叫到时把刚才的输出和一句提醒接在后面，
        让它用自己的话重说一次（前面和刚才那次请求一样，缓存命中），群里看得到的动作换成重说的，记卡、记长期记忆照旧；
        还是照念、重说出错，或者是主动聊天（没人在等，不多花钱），就不发这些话（被叫到了加个 🤐）。
        要记进长期记忆的话照抄了卡的不记（长期记忆每个频道、管理员都看得到）。
        写进记录的是最后真的要做的：照念的那份和提醒都不进记录。日志里不写卡和话的内容。"""
        completion, cost, row, actions, stored = step
        if not seen:
            return step
        heard = self._said(conv, (m.text for _, m in numbered), content=content)  # 群友自己公开说过、发过的：照着说不算泄露

        def copied(text: str) -> bool:
            return quotes_card(text, seen, said=heard)

        def recited(acts: list[Action]) -> bool:
            return copied("\n".join(t for a in acts if a.kind == "reply" for t in a.messages))

        memo = self._screen_memory(st.guild_id, actions, lambda a: a.text if a.kind == "memory" else None, seen, heard)
        if len(memo) != len(actions):
            log.info("频道 %s：要记的长期记忆照抄了人设卡的原文，没记", st.channel_id)
            actions, stored = memo, dump_actions(memo)
        if not recited(actions):
            return completion, cost, row, actions, stored
        self.store.set_outcome(row, "recited")
        now = self.clock()
        retry = (
            kind == "mention"
            and st.generation == generation
            and now >= self.paused_until
            and not self._over_budget(st, self.view(st), now)[1]
        )
        log.warning("频道 %s：要说的话照念了人设卡的原文，%s", st.channel_id, "让它用自己的话重说" if retry else "不发")
        redone = False
        if retry:
            messages = self._messages(system, conv, content) + [
                {"role": "assistant", "content": completion.content},
                {"role": "user", "content": CARD_QUOTED},
            ]
            try:
                again, more, again_row = await self._ask(st, kind, messages, thinking, generation, max_tokens)
            except LLMError as exc:
                # 不走 _on_error：这批消息已经答过了（不用冷却、不加 ⚠️）；超时的照样记账，余额不足、key 不对照样暂停
                log.warning("频道 %s：让它重说时 DeepSeek 调用失败：%s", st.channel_id, exc)
                self._pause_on(exc, self.clock())
                self._record_error(st.guild_id, st.channel_id, kind, exc)
            else:
                completion, cost, row = again, cost + more, again_row
                redo, _ = self._interpret(st, again, kind, targets, allow_tools=False, cards_here=True)
                redone = not recited(redo)
                if redone:
                    actions = [a for a in actions if a.kind not in CHAT_KINDS] + [a for a in redo if a.kind in CHAT_KINDS]
                else:
                    log.warning("频道 %s：重说的还是照念了人设卡的原文，不发", st.channel_id)
        if not redone:
            actions = [a for a in actions if a.kind != "reply"]
            self._recited_rows.add(row)  # 最后记结果时还是 recited
        if kind == "mention" and targets and not any(a.kind in CHAT_KINDS for a in actions):
            actions.append(Action("react", targets[-1], emoji="🤐"))  # 被叫到了总得有点回应
        return completion, cost, row, actions, dump_actions(actions)

    async def _apply_memory_actions(
        self,
        st: ChannelState,
        view: View,
        actions: list[Action],
        numbered: list[tuple[int, IncomingMessage]],
        targets: list[int],
        kind: str,
        seen: Sequence[str] = (),
        history: Sequence[Turn] = (),
    ) -> list[Action]:
        """改人设卡、长期记忆（只是写进数据库）。改成了就在要求的那条消息上加 📝。返回剩下要执行的动作。
        seen：这次请求里它看得到的人设卡（不含它这次自己写的）。history：这一轮之前的记录。"""
        mine = [a for a in actions if a.kind in MEMORY_KINDS]
        if not mine:
            return actions
        by_sid = dict(numbered)
        now = self.clock()
        marked: set[int] = set()
        refused: list[Action] = []
        guild = st.guild_id
        for action in mine:
            if st.guild_id != guild:
                # 加 📝 的时候私信换了服务器：剩下的不写（不能写进别的服务器）
                log.info("私信 %s：写记忆的时候换了服务器，剩下的没写", st.channel_id)
                break
            if action.kind == "card":
                mid = self._apply_card(st, view, action, by_sid, now, seen, history)
                if mid is None and not self._card_is(st, action, by_sid):
                    refused.append(action)
            else:
                mid = self._apply_memory(st, view, action, targets, kind, now)
            if mid is not None and mid not in marked:
                marked.add(mid)
                await self._safe_react(st, mid, "📝")  # 让人一眼看到真的记下了
        if refused:
            self._drop_from_record(st, refused)
        return [a for a in actions if a.kind not in MEMORY_KINDS]

    def _card_is(self, st: ChannelState, action: Action, by_sid: dict[int, IncomingMessage]) -> bool:
        """card 动作要写的，本来就是那个人现在的卡（没写是因为没有变化，不算没写成）。"""
        m = by_sid.get(action.to) if action.to is not None else None
        if m is None or self.cards is None:
            return False
        card = self.cards.get(st.guild_id, m.author_id)
        return (card.text if card is not None else "") == clean_card(action.text or "")

    def _drop_from_record(self, st: ChannelState, refused: list[Action]) -> None:
        """没写成的 card 动作从这一轮记下的输出里拿掉：记录里留着的都是真的写成了的（以后核对写卡时，记录里替某人写过的卡
        都算他自己的）；照抄别人的卡被拦下的那份，也不会留在记录里让它下次接着抄。"""
        conv = st.conversation
        if conv is None or not conv.turns or conv.turns[-1].role != "assistant":
            return
        turn = conv.turns[-1]
        raw = turn.content if isinstance(turn.content, str) else json.dumps(turn.content, ensure_ascii=False)
        actions, ok = parse_actions(raw)
        if not ok:
            return
        gone = {(a.to, a.text) for a in refused}
        kept = [a for a in actions if not (a.kind == "card" and (a.to, a.text) in gone)]
        if len(kept) == len(actions):
            return
        turn.content = dump_actions(kept)
        self.store.update_turn(turn.id, turn.content)

    def _apply_card(
        self,
        st: ChannelState,
        view: View,
        action: Action,
        by_sid: dict[int, IncomingMessage],
        now: float,
        seen: Sequence[str] = (),
        history: Sequence[Turn] = (),
    ) -> int | None:
        """改人设卡：只认这一轮里本人叫它的那条消息，卡的主人就是那条消息的作者。返回要加 📝 的消息。"""
        if not self._cards_on(view):
            log.info("频道 %s：这里没开人设卡，没改", st.channel_id)
            return None
        assert self.cards is not None
        m = by_sid.get(action.to) if action.to is not None else None
        if m is None or m.is_bot or not m.addressed:
            log.info("频道 %s：#%s 不是本人这一轮叫它的消息，没改人设卡", st.channel_id, action.to)
            return None
        if m.message_id in st.deleted:
            log.info("频道 %s：要它记的那条（#%s）已经删了，没改人设卡", st.channel_id, action.to)
            return None
        text = clean_card(action.text or "")
        if len(text) > self.ec.card_max_chars:
            log.info("频道 %s：%s 的新人设卡太长（%d 字，最多 %d），没改", st.channel_id, m.author_name, len(text), self.ec.card_max_chars)
            return None
        # 抄进自己的卡，本人用 /ds-rp 就能看到别人的原文了：别人现在的卡，加上这次请求里它看得到的（删掉的、改之前的也算；
        # 它以前替他写成过的卡是他自己的，不算）。大家公开说过的、本来就在他自己卡上的不算
        drafts = self._drafts(st, history, m.author_id)
        others = [*self.cards.others(st.guild_id, m.author_id), *(t for t in seen if t not in drafts)]
        mine = self.cards.own_texts(st.guild_id, m.author_id)
        if self._copies_card(text, others, mine, self._said(st.conversation, (x.text for x in by_sid.values()))):
            log.info("频道 %s：%s 的新人设卡照抄了别人的人设卡，没改", st.channel_id, m.author_name)
            return None
        # 它是照着对话里附着的那份改的：那份在这期间被本人改了、删了（/ds-rp、bot 主人删卡），就不写
        seen = st.cards_shown.get(m.author_id)
        expect = seen[1] if seen is not None else None
        current = self.cards.get(st.guild_id, m.author_id)
        if (current.updated_at if current is not None else None) != expect:
            log.info("频道 %s：%s 的人设卡刚被改过或删过，这次没改", st.channel_id, m.author_name)
            return None
        if not self.cards.put(st.guild_id, m.author_id, m.author_name, text, via="chat", now=now, expect=expect):
            return None  # 没有变化
        # 只记谁的卡改了，不记内容：卡是本人的东西
        log.info("频道 %s：%s 的人设卡%s", st.channel_id, m.author_name, "已清空" if not text else f"已更新（{len(text)} 字）")
        return m.message_id

    def _apply_memory(
        self, st: ChannelState, view: View, action: Action, targets: list[int], kind: str, now: float
    ) -> int | None:
        """改长期记忆：只在被叫到的那一轮（有人要它记）。返回要加 📝 的消息。"""
        live = [t for t in targets if st.msg_by_sid.get(t) not in st.deleted]  # 叫它的消息被删了：当成不要它记了
        if not self._memory_on(view) or kind != "mention" or not live:
            log.info("频道 %s：没人叫它要记，或者这里没开长期记忆，没改", st.channel_id)
            return None
        if not self._memory_op(st.guild_id, action.op or "", action.index, action.text, via="chat", now=now):
            log.info("频道 %s：长期记忆没改成（%s %s）", st.channel_id, action.op, action.index or "")
            return None
        log.info("频道 %s：长期记忆 %s %s", st.channel_id, action.op, one_line(action.text or str(action.index or ""), 80))
        return st.msg_by_sid.get(live[-1])

    def _memory_op(self, guild_id: int, op: str, no: int | None, text: str | None, *, via: str, now: float) -> bool:
        """记一条 / 改一条 / 删一条长期记忆。超出条数、字数，或者编号不存在时不改，返回 False。"""
        assert self.memory is not None
        if op in ("add", "edit"):
            text = clean_memory(text or "")
            if not text or len(text) > self.ec.memory_entry_chars:
                return False
        if op == "add":
            entries = self.memory.entries(guild_id)
            if len(entries) >= self.ec.memory_max_entries or any(e.text == text for e in entries):
                return False
            self.memory.add(guild_id, text or "", via=via, now=now)
            return True
        if op == "edit" and no is not None:
            current = self.memory.get(guild_id, no)
            return current is not None and current.text != text and self.memory.edit(guild_id, no, text or "", via=via, now=now)
        if op == "delete" and no is not None:
            return self.memory.delete(guild_id, no)
        return False

    # ------------------------------------------------------------------ 整理记忆

    def _review_due(self, st: ChannelState, now: float) -> bool:
        """频道安静了一阵、又攒了几轮新聊天：该整理记忆了。连着失败时等得越来越久。"""
        if self.cards is None and self.memory is None:
            return False
        # 缓冲区里可能一直有没叫它的消息（没开主动发言的频道）：那些等真的聊到它再说，不影响整理已经聊过的
        if st.reviewing or st.scheduled or st.lock.locked():
            return False
        if st.unreviewed_rounds < max(1, self.ec.review_min_rounds) or now < self.paused_until:
            return False  # 没攒够；或者 DeepSeek 暂停中（余额不足、key 不对）
        wait = self.ec.review_idle_seconds * (2 ** min(st.review_failures, 5))
        if now - st.last_review_at < wait:
            return False  # 上次刚整理过（或者刚失败过）
        return now - max(st.last_activity_at, st.last_eval_at) >= self.ec.review_idle_seconds

    def _kick_review(self, st: ChannelState) -> None:
        if self.closing:
            return
        st.reviewing = True
        task = asyncio.get_running_loop().create_task(self._review_task(st))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _review_task(self, st: ChannelState) -> None:
        """安静时整理记忆。只在挑要整理的聊天时占着这个频道；问模型的时候不占，有人叫它照样马上回。
        整理的时候又压缩了旧记录：压掉的那部分排着队，整理完这批接着整理。"""
        try:
            try:
                async with st.lock:
                    conv = self._conversation(st)
                    self._flush_fixes(st, conv)
                    info = self.transport.channel_info(st.channel_id)
                    generation = st.generation
                    plan = self._review_plan(st, conv, info)
                if plan is not None:
                    await self._run_review(st, conv, info, plan, generation, before_compact=False)
            except Exception:
                log.exception("频道 %s：整理记忆出错", st.channel_id)
            st.review_held = 0  # 这批完了：没整理成、还在记录里的，压缩时再挑出来
            await self._review_queued(st)
        finally:
            self._review_done(st)

    async def _compact_review(self, st: ChannelState) -> None:
        """压缩旧记录之前挑出来的那部分聊天：在后台整理（整理的时候又压缩了，压掉的也接着整理）。"""
        try:
            await self._review_queued(st)
        finally:
            self._review_done(st)

    async def _review_queued(self, st: ChannelState) -> None:
        """按先后整理排着队的几批（压缩前挑出来的原话）。"""
        while st.review_jobs and not self.closing:
            job = st.review_jobs.pop(0)
            if job.generation != st.generation:
                continue  # 排队的时候清空过记忆
            try:
                await self._run_review(st, job.conv, job.info, job.plan, job.generation, before_compact=True)
            except Exception:
                log.exception("频道 %s：压缩前整理记忆出错", st.channel_id)

    @staticmethod
    def _review_done(st: ChannelState) -> None:
        # 和上面最后一次检查队列之间不能有 await：不然这中间压缩时排进来的那批没人整理
        st.reviewing = False
        st.review_held = 0
        st.review_jobs.clear()  # 被取消（关机）时剩下的就不整理了

    def _set_reviewed(self, st: ChannelState, conv: Conversation, turn_id: int) -> None:
        self.store.set_reviewed(conv, turn_id)
        if st.conversation is not None and st.conversation is not conv:
            st.conversation.reviewed_upto = max(st.conversation.reviewed_upto, turn_id)  # 中途重新载入过

    def _review_plan(
        self, st: ChannelState, conv: Conversation, info: ChannelInfo, upto: int | None = None, after: int = 0
    ) -> _ReviewPlan | None:
        """挑出还没整理过的聊天，分成几段。不用整理（关掉了、私密频道、没人说话）时把进度记到最后，返回 None。

        upto：只挑到这条记录（压缩旧记录之前：那部分马上要压成摘要，原话就看不到了）。
        after：这条之前的不挑（正在整理的那一段已经挑走了）。"""
        now = self.clock()
        view = self.view(st)
        start = max(conv.reviewed_upto, after)
        turns = [t for t in conv.turns if t.id > start and (upto is None or t.id <= upto)]
        if not turns:
            if upto is None:
                st.unreviewed_rounds = 0
            return None
        last_id = turns[-1].id
        cards_ok = self._cards_on(view) and self.implicit_ok(st.channel_id, "cards")
        memory_ok = self._memory_on(view) and self.implicit_ok(st.channel_id, "long_memory")
        live = self._live(st, view)
        if live and (cards_ok or memory_ok) and self._over_budget(st, view, now)[0]:
            if upto is not None:
                self._set_reviewed(st, conv, last_id)  # 这部分马上就压掉了，以后也整理不了
            st.last_review_at = now  # 今天的预算用完了：隔一阵再看（预算是按天算的）
            return None
        chunks = self._review_chunks(turns, info.bot_name) if live and (cards_ok or memory_ok) else []
        if not any(chunk.sids for chunk in chunks):
            # 这里不整理（关掉了，或者是私密频道），或者这段时间没有群友说话：以后打开了也不回头整理这些旧的
            self._set_reviewed(st, conv, last_id)
            if upto is None:
                st.unreviewed_rounds = 0
            return None
        return _ReviewPlan(chunks, cards_ok, memory_ok, last_id)

    def _review_chunks(self, turns: list[Any], bot_name: str) -> list[_ReviewChunk]:
        """把要整理的记录按字数分成几段，每段记下最后一条记录（整理完一段就记一段的进度）。"""
        limit = max(2000, self.ec.review_max_chars)
        chunks: list[_ReviewChunk] = []
        lines: list[str] = []
        sids: list[int] = []
        size = 0
        start = 0.0
        prev_id = 0  # 上一条记录（lines 不空时一定已经有了）
        for t in turns:
            if t.role == "user":
                turn_lines = review_text(t.content)
                # 消息编号只从群友的消息里取（它自己说的话一行写完，冒充不了）
                turn_sids = [int(m.group(1)) for line in turn_lines if (m := _SID_LINE.match(line))]
            else:
                turn_lines, turn_sids = review_bot_lines(t.content, bot_name), []
            kept: list[str] = []
            used = 0
            for line in turn_lines:
                line = line if len(line) <= limit // 4 else line[: limit // 4] + "…"
                if used + len(line) + 1 > limit:
                    kept.append("（后面略）")
                    break
                kept.append(line)
                used += len(line) + 1
            if lines and size + used > limit:
                chunks.append(_ReviewChunk(lines, sids, prev_id, start))
                lines, sids, size = [], [], 0
            if not lines:
                start = t.created_at
            lines += kept
            sids += [sid for sid in turn_sids if sid not in sids]
            size += used
            prev_id = t.id
        if lines:
            chunks.append(_ReviewChunk(lines, sids, prev_id, start))
        return chunks

    async def _run_review(
        self,
        st: ChannelState,
        conv: Conversation,
        info: ChannelInfo,
        plan: _ReviewPlan,
        generation: int,
        *,
        before_compact: bool,
    ) -> None:
        """一段一段问模型、照着改，每整理完一段就记下进度。连着失败几次就放弃这一批（免得一直花钱）。"""
        st.last_review_at = self.clock()
        for i, chunk in enumerate(plan.chunks[:MAX_REVIEW_CHUNKS]):
            if self.closing:
                return  # 关机了：剩下的下次启动再整理（压缩前挑出来的就不整理了）
            if not before_compact:
                if i and st.review_held > plan.chunks[i - 1].last_turn:
                    return  # 问模型的时候压缩了旧记录：后面的原话已经挑出来排着队了（没压掉的下次再整理）
                st.review_held = max(st.review_held, chunk.last_turn)  # 这一段归这次整理：压缩时不再挑
            if chunk.sids:
                result = await self._review_chunk(st, info, chunk, plan.cards_ok, plan.memory_ok, generation)
                if st.generation != generation:
                    return  # 整理的时候这个频道的记忆被清空了
                if result != "ok":
                    st.review_failures += 1  # 越失败等得越久
                    # 连不上、限流、余额不足这类暂时的问题：等恢复了再整理，不放弃这批聊天；
                    # 模型那边的问题（输出被截断、被拒、等太久没写完）连着几次就放弃，免得一直花钱
                    if result == "failed":
                        st.review_bad += 1
                    if before_compact or st.review_bad >= MAX_REVIEW_FAILURES:
                        log.warning("频道 %s：整理记忆没成功，跳过这一批聊天", st.channel_id)
                        self._set_reviewed(st, conv, plan.last_id)
                        st.review_failures = st.review_bad = 0
                        if not before_compact:
                            st.unreviewed_rounds = 0
                    return
                st.review_failures = st.review_bad = 0
            self._set_reviewed(st, conv, chunk.last_turn)
        if len(plan.chunks) > MAX_REVIEW_CHUNKS:
            if before_compact:
                self._set_reviewed(st, conv, plan.last_id)  # 剩下的马上就压掉了
            return  # 剩下的下次再整理
        if not before_compact:
            st.unreviewed_rounds = 0

    async def _review_chunk(
        self, st: ChannelState, info: ChannelInfo, chunk: _ReviewChunk, cards_ok: bool, memory_ok: bool, generation: int
    ) -> str:
        """问模型这一段聊天里有什么值得长期记住的，照着改。

        返回 "ok"；"retry"（连不上、限流、余额不足：之后再试）；"failed"（模型的输出不能用、等太久没写完）。"""
        authors = self.store.load_authors(st.channel_id, chunk.sids)  # 编号 -> (人, 名字)；bot 和旧记录没有
        guild = st.guild_id
        people: dict[int, tuple[str, list[int]]] = {}
        if cards_ok:
            assert self.cards is not None
            for sid in chunk.sids:
                who = authors.get(sid)
                if who is None:
                    continue
                deleted = self.cards.deleted_at(guild, who[0])
                if deleted is not None and deleted >= chunk.start:
                    continue  # 这段聊天之后他的卡被删过：不拿这些旧话重新写他的卡
                entry = people.setdefault(who[0], (who[1], []))
                if sid not in entry[1]:
                    entry[1].append(sid)
        do_cards = cards_ok and bool(people)
        if not do_cards and not memory_ok:
            return "ok"
        listed: list[tuple[str, list[int], str | None]] | None = None
        versions: dict[int, float | None] = {}  # 给模型看的是哪个版本：写的时候对得上才写（中途被本人改了、删了就不写）
        numbers: dict[int, int] = {}  # 同名的人：是第几位
        if do_cards:
            assert self.cards is not None
            listed = []
            names = [name for name, _ in people.values()]
            seen_names: dict[str, int] = {}
            for uid, (name, own) in people.items():
                card = self.cards.get(guild, uid)
                versions[uid] = card.updated_at if card is not None else None
                shown = one_line(name or (card.name if card else ""), 32)
                if names.count(name) > 1:  # 同名的两个人：靠消息编号分开（名字太长时先截短，免得把后缀截掉）
                    seen_names[name] = numbers[uid] = seen_names.get(name, 0) + 1
                    shown += f"（同名的第 {numbers[uid]} 位）"
                listed.append((shown, own, card.text if card else None))
        memories = None
        if memory_ok:
            assert self.memory is not None
            memories = [(m.no, m.text) for m in self.memory.entries(guild)]
        system = build_review_prompt(
            name=neutralize(one_line(info.bot_name, 40)),
            cards=do_cards,
            memory=memory_ok,
            card_max=self.ec.card_max_chars,
            entry_max=self.ec.memory_entry_chars,
            count=len(memories or []),
            max_entries=self.ec.memory_max_entries,
            dm=st.private,
        )
        prompt = [
            {"role": "system", "content": system},
            {"role": "user", "content": render_review_input(listed, memories, chunk.lines)},
        ]
        now = self.clock()
        try:
            completion = await self.llm.complete(
                prompt,
                thinking="off",
                json_mode=True,
                max_tokens=REVIEW_MAX_TOKENS,
                user_id=f"guild-{guild}",
                timeout_scale=REVIEW_TIMEOUT_SCALE,
            )
        except LLMError as exc:
            log.warning("频道 %s：整理记忆失败：%s", st.channel_id, exc)
            self._record_error(guild, st.channel_id, "review", exc)
            if exc.timeout:
                return "failed"  # 等了很久也没写完：多半是这段要写的太多，再试多半还是一样（对方可能还照样算钱）
            transient = exc.status is None or exc.status in (401, 402, 408, 429) or exc.status >= 500
            return "retry" if transient else "failed"
        now = self.clock()
        cost = self.pricing.cost(completion.usage, now)
        self.ledger.add(guild, cost, now)
        u = completion.usage
        self.store.record_usage(
            ts=now,
            day=self.ledger.day(now),
            guild_id=guild,
            channel_id=st.channel_id,
            kind="review",
            outcome="review",
            hit=u.hit,
            miss=u.miss,
            output=u.output,
            reasoning=u.reasoning,
            cost=cost,
        )
        if st.generation != generation:
            return "retry"
        parsed = parse_review(completion.content)
        if parsed is None and completion.finish_reason == "length":
            log.warning("频道 %s：整理记忆的输出太长被截断了，这次没整理成", st.channel_id)
            return "failed"
        if parsed is None:
            log.warning("频道 %s：整理记忆的输出不是合法 JSON", st.channel_id)
            return "ok"  # 当成没东西要记，不重试（免得一直花钱）
        updates, ops = parsed
        heard = _member_blocks(chunk.lines)  # 群友自己说的（一条消息连同续行算一整段）
        cards_changed = 0
        if do_cards:
            assert self.cards is not None
            done: set[int] = set()
            for to, text, name in updates:
                who = authors.get(to) if to in chunk.sids else None
                if who is None or who[0] not in people or who[0] in done:
                    continue  # 只能改这段记录里说过话的人自己的卡
                if st.msg_by_sid.get(to) in st.deleted:
                    continue  # 整理的时候这条被删了
                if name is not None and not _same_name(name, people[who[0]][0], numbers.get(who[0])):
                    log.info("频道 %s：整理记忆时 #%s 和名字「%s」对不上，没改", st.channel_id, to, one_line(name, 32))
                    continue
                text = clean_card(text)
                if not text or len(text) > self.ec.card_max_chars:
                    continue
                # 别把别人的卡抄进这个人的卡（本人用 /ds-rp 就看得到）；本来就在他自己卡上的不算
                if self._copies_card(text, self.cards.others(guild, who[0]), self.cards.own_texts(guild, who[0]), heard):
                    log.info("频道 %s：整理记忆时 #%s 的新人设卡照抄了别人的卡，没改", st.channel_id, to)  # 不写内容
                    continue
                deleted = self.cards.deleted_at(guild, who[0])
                if deleted is not None and deleted >= chunk.start:
                    continue  # 问模型的时候被删掉了
                done.add(who[0])
                if self.cards.put(guild, who[0], who[1], text, via="review", now=now, expect=versions.get(who[0])):
                    cards_changed += 1
                if len(done) >= MAX_REVIEW_CARDS:
                    break
        memory_changed = 0
        if memory_ok:
            listed_cards = [card for _, _, card in listed or [] if card]  # 给它看过的人设卡
            wanted = ops[:MAX_REVIEW_MEMORY_OPS]
            kept = self._screen_memory(guild, wanted, lambda op: op.text, listed_cards, heard)
            if len(kept) != len(wanted):
                log.info("频道 %s：整理记忆时要记的照抄了人设卡的原文，没记", st.channel_id)  # 不写内容
            for op in kept:
                if self._memory_op(guild, op.op, op.no, op.text, via="review", now=now):
                    memory_changed += 1
        log.info(
            "频道 %s：整理了记忆（%d 行聊天）：人设卡改了 %d 张，长期记忆改了 %d 处 | $%.5f",
            st.channel_id,
            len(chunk.lines),
            cards_changed,
            memory_changed,
            cost,
        )
        return "ok"

    # ------------------------------------------------------------------ 跨频道记忆

    def shares_out(self, channel_id: int, place: ChannelPlace) -> bool:
        """这个频道的内容能不能给同服务器别的频道看：开了共享；私密频道还要在这个频道上单独开。"""
        if place.dm:
            return False  # 私信里的原话不给服务器里的频道翻（要记的整理进长期记忆、人设卡）
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
        # 有人设卡的人、长期记忆：很少变，放这里（排好序、编号固定，不变就不影响缓存）
        card_names: list[str] | None = None
        more_cards = 0
        if self._cards_on(view):
            assert self.cards is not None
            all_names = self.cards.names(st.guild_id)
            card_names = [_sticker_label(n) for n in all_names[: max(0, self.ec.card_list_limit)]]
            more_cards = len(all_names) - len(card_names)
        memories: list[tuple[int, str]] | None = None
        if self._memory_on(view):
            assert self.memory is not None
            memories = [
                (m.no, neutralize_record(one_line(m.text, self.ec.memory_entry_chars + 20))) for m in self.memory.entries(st.guild_id)
            ]
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
            cards=card_names,
            more_cards=more_cards,
            memories=memories,
            memory_max=self.ec.memory_max_entries,
            card_max=self.ec.card_max_chars,
            entry_max=self.ec.memory_entry_chars,
            music=self.music is not None and not st.private,
            dm_with=neutralize(one_line(info.dm_with, 32)) if info.dm_with is not None else None,
            desktop=[neutralize(one_line(n, 40)) for n in self.desktop.app_names()] if self._desktop_here(st, view, info) else None,
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
        self,
        st: ChannelState,
        action: Action,
        numbered: list[tuple[int, IncomingMessage]],
        targets: list[int],
        cards: Sequence[str] = (),
    ) -> str:
        """改人设：只认这一轮里 dsowner 本人叫它的那条消息。改完（或没改成）的结果告诉模型，它再回话。
        cards：它看得到的人设卡原文（新人设照抄了的不改：人设管理员看得到，卡是本人的隐私）。"""
        by_sid = dict(numbered)
        to = action.to
        if to is None:
            # 没写是哪条：这一轮 dsowner 只叫了它一次，就是那条
            owners = [sid for sid in targets if sid in by_sid and self._is_owner(by_sid[sid]) and by_sid[sid].addressed]
            others = [sid for sid in targets if sid in by_sid and not self._is_owner(by_sid[sid])]
            if len(owners) != 1 or others:  # 同一批里还有别人在叫它：分不清是谁要改，不猜
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
            inherited = self._inherited_persona(st, scope, scope_id)
            # 人设管理员看得到：别把人设卡的原文搬进来。本来就在人设里的、dsowner 自己说的不算
            current = self.settings.overrides(scope, scope_id).get("persona") or ""
            if quotes_card(text, cards, said=[asker.text, current, inherited]):
                log.warning("频道 %s：dsowner 要改的人设照抄了人设卡的原文，没改", st.channel_id)  # 不写内容
                return PERSONA_CARD
            if not text or text == inherited:
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
        lines: list[str] = []
        listed: tuple[str, ...] | None = None
        sources = self.shared_sources(st.guild_id, st.channel_id) if view.share_memory else []
        if view.share_memory:
            names = tuple(self._display(p) for _, p in sources[: self.ec.share_list_limit])
            if names != st.listed:
                listed = names
                lines += render_channel_list(list(names), len(sources) - len(names))
        ttl = self.ec.share_recent_hours * 3600
        marks: dict[int, int] = {}
        allowed = {cid: p for cid, p in sources}
        # 私信和服务器的频道之间，同一个人自己跟它的对话不管开没开记忆共享都带：
        # 在频道里叫它时带上他刚才的私信，私信里带上他刚才在公开频道里跟它聊的
        for key in {(st.guild_id, m.author_id) for m in triggers if not m.is_bot}:
            for item in self.interactions.get(key, ()):
                if item.channel_id not in allowed and item.channel_id != st.channel_id:
                    place = self.transport.locate(item.channel_id)
                    if place is not None and place.guild_id == st.guild_id and (place.public if st.private else place.dm):
                        allowed[item.channel_id] = place
        for m in triggers:
            if m.is_bot or m.author_id in marks:
                continue
            since = st.digest_seen.get(m.author_id, 0)
            fresh = [
                item
                for item in self.interactions.get((st.guild_id, m.author_id), ())
                if item.channel_id in allowed
                and item.seq > since
                and now - item.at <= ttl
                and not (allowed[item.channel_id].dm and _secret(item.lines))
            ]
            if not fresh:
                continue
            marks[m.author_id] = max(item.seq for item in fresh)  # 只带最近两次；更早的就不带了
            for item in fresh[-2:]:
                place = allowed[item.channel_id]
                where = "私信里" if place.dm else f" {self._display(place)} "
                block = [f"（{neutralize(one_line(m.author_name, 32))} {_ago(now - item.at)}在{where}跟你聊过："]
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
            if m.message_id in st.deleted:
                continue  # 刚被删掉的
            said = one_line(m.text, 150) if m.text.strip() else "（发了图片或文件）"
            lines = [f"{one_line(m.author_name, 32)}：{said}"] + [f"你：{one_line(r, 150)}" for r in replies[:3]]
            key = (st.guild_id, m.author_id)
            self._interaction_seq += 1
            self.interactions.setdefault(key, deque(maxlen=INTERACTIONS_PER_PERSON)).append(
                Interaction(st.channel_id, self.clock(), self._interaction_seq, lines, m.message_id)
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
                    if not item.preview and not result.error:  # 读不了的不占名额
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
        if self._pause_on(exc, now):
            pass
        elif exc.status in BAD_REQUEST:
            # 这批消息本身被拒（比如内容审核）：丢掉它，别卡住整个频道
            log.error("频道 %s：这批消息被 DeepSeek 拒绝，已丢弃：%s", st.channel_id, exc)
            self._consume(st, batch, now)
            text = str(exc).lower()
            # 只有真的是上下文太长才压缩（max_tokens 填错之类的报错里也有 token 字样，别误判）
            too_big = exc.status == 413 or any(word in text for word in ("context", "too long", "too large", "maximum length"))
            if too_big and st.conversation is conv:
                await self._compact(st, conv, info, force=True)
        else:
            st.error_until = now + ERROR_BACKOFF
        self._record_error(st.guild_id, st.channel_id, kind, exc)
        st.last_decision = f"出错：{exc}"
        if kind == "mention" and targets:
            mid = st.msg_by_sid.get(targets[-1])
            if mid is not None:
                await self._safe_react(st, mid, "⚠️")

    def _pause_on(self, exc: LLMError, now: float) -> bool:
        """余额不足、key 不对：全局暂停一阵（返回是不是这两种）。"""
        if exc.status == 402:
            self.paused_until = now + 600
            log.error("DeepSeek 余额不足，暂停 10 分钟。请到 platform.deepseek.com 充值。")
        elif exc.status == 401:
            self.paused_until = now + 3600
            log.error("DeepSeek API key 无效，暂停 1 小时。请检查 .env 里的 DEEPSEEK_API_KEY。")
        else:
            return False
        return True

    def _record_error(self, guild_id: int, channel_id: int, kind: str, exc: LLMError) -> None:
        """调用失败也记一笔 error。超时、响应传到一半断了的带着估出来的用量（exc.usage）：对方多半照样算了钱，
        按估算记进账本和数据库，每日预算才拦得住。"""
        now = self.clock()
        u = exc.usage or Usage()
        cost = self.pricing.cost(u, now)
        if cost:
            # 先记内存账本再写库（和 _call 一样）：跨天时账本会从库里重读，顺序反了这笔会算两遍
            self.ledger.add(guild_id, cost, now)
            log.info("频道 %s：%s 没拿到结果，按估算记了 $%.5f（输入约 %d、输出约 %d token）", channel_id, kind, cost, u.prompt_tokens, u.output)
        self.store.record_usage(
            ts=now,
            day=self.ledger.day(now),
            guild_id=guild_id,
            channel_id=channel_id,
            kind=kind,
            outcome="error",
            hit=u.hit,
            miss=u.miss,
            output=u.output,
            reasoning=u.reasoning,
            cost=cost,
        )

    async def _safe_react(self, st: ChannelState, message_id: int, emoji: str) -> bool:
        try:
            return await self.transport.react(st.channel_id, message_id, emoji)
        except Exception:
            log.exception("频道 %s：加表情出错", st.channel_id)
            return False

    def _live_actions(self, st: ChannelState, actions: list[Action], kind: str, targets: list[int]) -> list[Action]:
        """去掉冲着已经删掉的消息的回复、表情、发图（没写回复谁、叫它的消息又全删了的回复也去掉）。"""
        if not st.deleted:
            return actions
        live_targets = [t for t in targets if st.msg_by_sid.get(t) not in st.deleted]
        out = []
        for a in actions:
            if a.kind in ("reply", "react", *SEND_KINDS):
                to = a.to
                if to is None and kind == "mention" and a.kind != "react":
                    if targets and not live_targets:
                        continue
                    to = live_targets[-1] if live_targets else None
                if to is not None and st.msg_by_sid.get(to) in st.deleted:
                    continue
            out.append(a)
        return out

    async def _execute(
        self, st: ChannelState, actions: list[Action], kind: str, targets: list[int], elapsed: float
    ) -> tuple[bool, bool, set[str]]:
        """返回 (发出去了什么, 加了表情, 没发出去的图是哪几种：sticker / meme / pick)。"""
        spoke = reacted = False
        failed: set[str] = set()
        sent_count = 0
        pinged: set[int] = set()  # 同一条消息只提醒一次（比如又回话又发贴图）
        directory: dict[str, Any] = {"pings_left": MAX_PINGS_PER_ROUND}  # 能 @ 的人、能链接的频道（第一次要用时才查）
        guild = st.guild_id

        def moved() -> bool:
            """假装打字的时候私信换了服务器：按原来那个服务器的人设、记忆写的话不发了。"""
            return st.private and st.guild_id != guild

        for action in actions:
            if action.kind == "react":
                mid = st.msg_by_sid.get(action.to) if action.to is not None else None
                if mid is not None and action.emoji and mid not in st.deleted:
                    reacted = await self._safe_react(st, mid, action.emoji) or reacted
                continue

            to = action.to
            if to is None and kind == "mention" and targets:
                live = [t for t in targets if st.msg_by_sid.get(t) not in st.deleted]
                if not live:
                    continue  # 叫它的消息都被删了：不回
                to = live[-1]
            reply_mid = st.msg_by_sid.get(to) if to is not None else None
            if reply_mid is not None and reply_mid in st.deleted:
                log.info("频道 %s：#%s 在它回话之前被删了，不回它", st.channel_id, to)
                continue
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
                if moved():
                    break
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
                if moved():
                    return spoke, reacted, failed
                people, channels = await self._directory(st, text, kind, directory)
                for j, chunk in enumerate(split_for_discord(text)):
                    first = i == 0 and j == 0
                    chunk, pings = self._link_mentions(st, chunk, people, channels, kind, directory)
                    try:
                        sent = await self.transport.send(
                            st.channel_id, chunk, reply_to=reply_mid if first else None, ping=ping and first, mentions=pings
                        )
                    except Exception:
                        log.exception("频道 %s：发消息出错", st.channel_id)
                        sent = None
                    if sent is not None:
                        spoke = True
                sent_count += 1
        return spoke, reacted, failed

    async def _directory(
        self, st: ChannelState, text: str, kind: str, cache: dict[str, Any]
    ) -> tuple[dict[str, frozenset[int]], dict[str, frozenset[int]]]:
        """它在话里能 @ 的人和能链接的频道。人按名字找：最近在这个频道说过话、被 @ 过的人，其次是有人设卡的人；
        有人叫它的时候，还对不上的名字再按名字开头到服务器成员里找一次。"""
        if "people" not in cache:
            recent: dict[str, set[int]] = {}
            for uid, name in st.people.items():
                for form in name_forms(name):
                    recent.setdefault(form, set()).add(uid)
            cards: dict[str, set[int]] = {}
            if self.cards is not None:
                for uid, name in self.cards.owners(st.guild_id):
                    for form in name_forms(name):
                        cards.setdefault(form, set()).add(uid)
            cache["tiers"] = [recent, cards]
            cache["people"] = merge_tiers(cache["tiers"])
            cache["channels"] = {name: frozenset(ids) for name, ids in self.transport.channel_names(st.guild_id).items()}
            cache["searched"] = set()
        if kind == "mention" and ("@" in text or "＠" in text):
            _, _, unknown = link(text, cache["people"], {})
            starts = [u for u in unknown if u[:2] not in cache["searched"]][:3]
            if starts and self.limiter.allow(("member-search", 0), MEMBER_SEARCHES_PER_MINUTE, 60.0, self.clock()):
                cache["searched"].update(u[:2] for u in starts)
                try:
                    found = await self.transport.find_members(st.guild_id, starts)
                except Exception:
                    log.exception("频道 %s：按名字找人出错", st.channel_id)
                    found = {}
                if found:
                    cache["tiers"].append(found)
                    cache["people"] = merge_tiers(cache["tiers"])
        return cache["people"], cache["channels"]

    def _link_mentions(
        self,
        st: ChannelState,
        chunk: str,
        people: dict[str, frozenset[int]],
        channels: dict[str, frozenset[int]],
        kind: str,
        cache: dict[str, Any],
    ) -> tuple[str, list[int]]:
        """把一段话里的 @名字、#频道名 换成真的。返回 (换好的话, 要提醒的人)。只有有人叫它的时候才提醒：
        这一轮最多 MAX_PINGS_PER_ROUND 个人，同一个人 PING_COOLDOWN 之内只提醒一次，每个服务器 10 分钟内最多
        GUILD_PINGS 次；其他的只显示成 @，不发通知。"""
        linked, users, _ = link(chunk, people, channels)
        if len(linked) > DISCORD_LIMIT:
            return chunk, []  # 换完太长了：原样发
        if kind != "mention" or not users:
            return linked, []
        now = self.clock()
        pings: list[int] = []
        for uid in dict.fromkeys(users):
            if cache["pings_left"] <= 0:
                break
            if now - self.pinged.get((st.guild_id, uid), float("-inf")) < PING_COOLDOWN:
                continue
            if not self.ping_window.allow(st.guild_id, GUILD_PINGS, GUILD_PING_WINDOW, now):
                break
            pings.append(uid)
            cache["pings_left"] -= 1
            self.pinged[(st.guild_id, uid)] = now  # 发出去之前就记上：同一轮后面几条里再 @ 他也不再提醒
        return linked, pings

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
        async with self._decode_slots:
            return await asyncio.to_thread(to_data_uri, raw, max_side)

    def _review_before_compact(self, st: ChannelState, conv: Conversation, info: ChannelInfo, upto: int, generation: int) -> None:
        """要压掉的那部分（到 upto 为止）还没整理过记忆的：把原话挑出来，排队在后台整理（压成摘要之后就看不到原话了）。
        正在整理的话，它正在问的那一段不再挑，挑出来的排在它后面。不等它：压缩照常做，有人叫它也不用多等。"""
        if self.cards is None and self.memory is None:
            return
        if max(conv.reviewed_upto, st.review_held) >= upto:
            return  # 都整理过了，或者正在问的那一段会整理到
        try:
            plan = self._review_plan(st, conv, info, upto=upto, after=st.review_held)
        except Exception:
            log.exception("频道 %s：压缩前整理记忆出错", st.channel_id)
            return
        if plan is None:
            return
        st.review_jobs.append(_ReviewJob(conv, info, plan, generation))
        if st.reviewing:
            st.review_held = upto  # 安静时整理的那批：问完手上这一段就停，后面的归这一批
            return  # 它整理完手上的会接着整理这批
        st.reviewing = True
        task = asyncio.get_running_loop().create_task(self._compact_review(st))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _compact(self, st: ChannelState, conv: Conversation, info: ChannelInfo, *, force: bool = False) -> None:
        """把最早的一部分对话压成摘要。只在这一次会让缓存 miss。"""
        if self.closing:
            return  # 关机了：压掉的那部分来不及整理记忆，留到下次启动再压
        self._flush_fixes(st, conv)  # 刚删掉、改过的先改进记录，别压进摘要里
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
            if exc.usage is not None:  # 超时、传到一半断了：对方多半照样算了钱，记一笔（别的错误照旧不记）
                self._record_error(st.guild_id, st.channel_id, "summary", exc)
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
        if not summary or st.generation != generation or self.closing:
            return  # 关机了：压掉的那部分来不及整理记忆，留到下次启动再压
        last_removed = conv.turns[remove - 1].id
        # 和下面的压缩之间不能有 await：挑出来的正好是压掉的那些
        self._review_before_compact(st, conv, info, last_removed, generation)
        self.store.compact(conv, summary[: self.ec.summary_max_chars * 2], remove)
        st.listed = last_listed(conv.turns)  # 名单可能在被压掉的那部分里，那样下次被叫到时重新写
        for entry in st.recent_files.values():
            # 文件内容随旧记录一起压成了摘要，被问到时要能重新读
            entry.read_turns = {k: v for k, v in entry.read_turns.items() if v > last_removed}
        for link_entry in st.recent_links.values():
            link_entry.read_turns = {k: v for k, v in link_entry.read_turns.items() if v > last_removed}
        # 附过人设卡的那条记录压掉了：那个人下次说话时再附一次
        st.cards_shown = {uid: v for uid, v in st.cards_shown.items() if v[0] > last_removed}
        if last_music_line(conv.turns) is None:
            st.music_seen = None  # “正在放什么”那句压掉了：下次被叫到时再写一次
        log.info("频道 %s：已把 %d 条旧记录压成摘要", st.channel_id, remove)

    @staticmethod
    def _describe(kind: str, outcome: str, actions: list[Action], completion: Completion, *, hide_reason: bool = False) -> str:
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
        reason = "" if hide_reason else one_line(completion.reasoning or "", 80)
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
            st.cards_shown.clear()
            st.unreviewed_rounds = 0  # 清空的记录不再整理
            st.review_jobs.clear()  # 排着队的是清空前的原话：不整理了（正在问的那一段问完会自己作废）
            st.review_held = 0
            st.edit_notes.clear()  # 旧消息被编辑的那几句：记录都清了，别带进新的对话
            st.fixes.clear()
            st.music_seen = None  # 告诉过它在放什么的那句也清掉了
        for key, log_ in list(self.interactions.items()):
            # 清空了这个频道的记忆，别的频道也不该再“想起”这里聊过的事
            kept = [item for item in log_ if item.channel_id != channel_id]
            if kept:
                self.interactions[key] = deque(kept, maxlen=INTERACTIONS_PER_PERSON)
            else:
                del self.interactions[key]

    def move_dm(self, channel_id: int, guild_id: int) -> None:
        """私信改挂到别的服务器：清掉私信的记录（里面是原来那个服务器的人设卡、记忆），之后用新服务器的。"""
        self.reset_channel(channel_id)
        self.store.move_conversation(channel_id, guild_id)
        st = self.states.get(channel_id)
        if st is not None:
            st.guild_id = guild_id
            for m in st.pending:
                m.guild_id = guild_id  # 还没回的那几条是他自己说的：换到新的服务器下接着回
            if any(m.trigger for m in st.pending):
                st.mention_due = True
                self._wake(st)

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
            "cards_on": self._cards_on(view),
            "memory_on": self._memory_on(view),
            "card_count": self.cards.count(guild_id) if self.cards is not None else 0,
            "memory_count": self.memory.count(guild_id) if self.memory is not None else 0,
            "memory_max": self.ec.memory_max_entries,
            "review_here": self.implicit_ok(channel_id, "cards") or self.implicit_ok(channel_id, "long_memory"),
            "last_review_ago": (now - st.last_review_at) if st and st.last_review_at else None,
            "music_on": self.music is not None,
            "playing": self.music.status(guild_id) if self.music is not None else None,
        }
