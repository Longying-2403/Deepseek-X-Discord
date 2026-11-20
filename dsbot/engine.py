"""调度引擎：缓冲消息 → 判断时机 → 调用模型 → 执行动作。

两条路径共用同一份对话历史：
- 被叫到（@、回复、别名）：立刻跑一轮，不消耗主动额度，只受“每人每分钟”限速。
- 主动：频道开了主动发言，并且 有新消息 + (冷场 或 等太久) + 有额度 + 间隔够长 + 没超预算 时跑一轮。

每一轮固定往历史里追加 user（这批新消息）+ assistant（模型原样输出），从不改写旧内容，
所以下一轮的请求总是以上一轮的请求 + 输出开头，最大化 DeepSeek 的前缀缓存命中。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, AsyncContextManager, Awaitable, Callable, Iterable, Protocol, Sequence

from .actions import dump_actions, mentions_kinds, parse_actions
from .config import AppConfig
from .files import classify, max_bytes_for, read_file
from .images import MAX_RAW_BYTES, filename_for, to_data_uri, upload_ready
from .ledger import Ledger
from .links import site_name
from .llm import LLMError
from .memory import SHARED, CardStore, MemoryStore, clean_card, clean_memory, parse_review, reveals
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
    REMIND_AMBIGUOUS,
    REMIND_CANCELLED,
    REMIND_CONFIRM,
    REMIND_DECLINED,
    REMIND_FAILED,
    REMIND_FULL_PERSON,
    REMIND_FULL_SERVER,
    REMIND_LIST,
    REMIND_NONE,
    REMIND_NOTE_CANCELLED,
    REMIND_NOTE_DECLINED,
    REMIND_NOTE_FAILED,
    REMIND_NOTE_FIRED,
    REMIND_NOTE_FIRED_DM,
    REMIND_NOTE_MISSED,
    REMIND_NOTE_SET,
    REMIND_NOT_YOURS,
    REMIND_NO_ONE,
    REMIND_OFF,
    REMIND_SET,
    REMIND_STALE,
    REMIND_WHICH,
    BUBBLE_GREET,
    REACH_FREE,
    REACH_NOTE,
    REACH_PICK,
    REACH_TOPICS_PRIVATE,
    BUBBLE_TOPICS_KNOWN,
    BUBBLE_TOPICS_SYSTEM,
    BUBBLE_NOTE,
    BUBBLE_PICK,
    BUBBLE_SHARED,
    CARD_QUOTED,
    DEFAULT_PERSONA,
    DESKTOP_NO_APP,
    DESKTOP_NOT_OWNER,
    DESKTOP_OFF,
    DESKTOP_RESULT,
    DESKTOP_URL_NOT_HIS,
    DM_LIMIT_TEXT,
    EDIT_NOTE,
    EDIT_NOTE_LOST,
    PASSIVE_REJECTED,
    EDITED_MARK,
    IMAGE_BLOCKED,
    IMAGE_BUDGET,
    IMAGE_COOLDOWN,
    IMAGE_FAILED,
    IMAGE_LIMIT,
    IMAGE_NONE,
    IMAGE_OFF,
    MUSIC_IDLE,
    MUSIC_IDLE_AT,
    MUSIC_NOW,
    MUSIC_NOW_AT,
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
    RECALL_AMBIGUOUS,
    RECALL_MISS,
    RECALL_NO_SHARE,
    RECALL_SYNC_OFF,
    SUMMARY_SYNC_NOTE,
    SUMMARY_SYSTEM,
    SYNC_BOT_NAME,
    TOOL_FAILED,
    WHO_NONE,
    WHO_OFF,
    WHO_SYNC_OFF,
    build_review_prompt,
    build_system_prompt,
    clean_persona,
)
from .mentions import link, merge_tiers, name_forms
from .music import pick_link
from .config import BUBBLE_LEVELS, REACH_LEVELS, check_range, level_of
from .quota import SlidingWindow
from . import announce as an
from . import reminders as rm
from .reminders import Reminder
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
    drop_local,
    drop_recents,
    label_music,
    public_lines,
    drop_dm_memory,
    has_dm_memory,
    render_cards,
    render_channel_list,
    render_dm_memory,
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
# 主动发言（2.10.0 起）：平时隔几分钟看一眼（check_minutes），只接最近 FRESH_SECONDS 里说的；它开了口、或者被叫过之后
# “跟着”这场聊天：有人说话就看要不要接，直到 FOLLOW_IDLE 这么久没人说话，或者连着 FOLLOW_PASSES 次都没话说
FRESH_SECONDS = 300.0  # 平时看一眼：这么久以内说的才算“正在聊的”，更早的只当背景
FOLLOW_IDLE = 300.0  # 跟着的时候：这么久没人说话、也没人打字，就回到平时
FOLLOW_PASSES = 3  # 跟着的时候：连着这么多次判断都决定不说，就回到平时
FOLLOW_SETTLE = 5.0  # 跟着的时候：有人说完话，停这么久（没人接着说、打字）再判断
FOLLOW_MAX_WAIT = 30.0  # 跟着的时候：一直有人在说，最多等这么久也判断一次
_SEARCH_SYNTAX = ("site:", "-site:", "after:", "before:")  # 冒泡时模型自己写的这些限定去掉，由这边加
_LINK = re.compile(r"https?://[^\s<>]+")
_LINK_TAIL = "。，、；：！？）》」』)]>,.;:!?\"'"
REACH_FAIL_LIMIT = 2  # 主动私信连着几次发不出去就替他关掉


@dataclass(frozen=True)
class _Bubble:
    """冒泡的规矩：安静多久以后（最短、最长几小时）、一天最多几次（最少、最多）、连着几次没人理就停、只在几点到几点。"""

    low: float
    high: float
    least: int
    most: int
    strikes: int
    from_hour: int | None
    until_hour: int | None


def _in_hours(hour: int, start: int | None, end: int | None) -> bool:
    """hour 点在不在 start～end 点之间（不含 end）。没填、两个一样 = 全天；start 比 end 大 = 跨夜（22～2）。"""
    if start is None or end is None or start == end:
        return True
    return start <= hour < end if start < end else hour >= start or hour < end


BUBBLE_ASK_TOPICS = 3  # 看大家的兴趣：一次最多要几个话题
BUBBLE_SCOUT_MIN = 4  # 开了冒泡的频道：攒了这么多条新消息、聊天又停了一阵，就看一次大家对什么感兴趣
BUBBLE_SCOUT_QUIET = 600.0  # 停了多久算“聊天停了”
BUBBLE_SCOUT_GAP = 3600.0  # 两次之间至少隔多久（聊得再多一小时也只看一次）
SPEAK_GAP = (30.0, 45.0)  # 它说过话（跟着的时候：判断过一次）之后，隔这么久（随机）才再主动判断；叫它的不受这个限制
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
AUTO_SEARCH = "autosearch"  # usage 表里的 kind：没人叫它时它自己搜的（主动搜索），和被叫到时的 search 分开数
MEMORY_KINDS = ("card", "memory", "dm_memory")  # 改人设卡、长期记忆、私信记忆：和回话放在一起，不用再问一次模型
MAX_WHO = 2
MAX_REVIEW_CHUNKS = 4  # 一次整理最多分几段问模型（聊天记录太长时）
MAX_REVIEW_CARDS = 3  # 一次整理最多改几张人设卡（每张都要整段重写，太多了输出会很长）
MAX_REVIEW_MEMORY_OPS = 10
MAX_REVIEW_FAILURES = 3  # 同一批聊天连着整理失败几次就放弃
DELETED_KEEP = 1000  # 每个频道记住最近多少条被删掉的消息
SYNC_GUARD_TRIES = 5  # 连通、断开、清空等锁的时候频道又连通、断开了（用的记录变了）：最多照现在的重新等几次
PEOPLE_KEEP = 300  # 每个频道记住最近多少个说过话、被 @ 过的人（它 @ 人时按名字找）
MAX_REMIND_NOTES = 5  # 提醒的事（设好了、取消了、到点说过了）最多攒几句，下一轮告诉它
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

    def guild_name(self, guild_id: int) -> str | None: ...  # 服务器的名字（它不在那个服务器里了是 None）


class Searcher(Protocol):
    async def search(self, query: str, *, max_chars: int) -> SearchResult: ...  # 没搜成抛异常（可以带 paid：已经算了钱的次数）


class LinkOpener(Protocol):
    def mode(self) -> str: ...  # all：推文和网页都能打开；x：只能打开推文

    def readable(self, url: str) -> bool: ...

    # pay=False：今天的预算用完了，不用要花钱的读法（DeepSeek 的 Fetch），推文和不花钱的照常
    async def read(self, url: str, *, max_chars: int, image_side: int | None = None, pay: bool = True) -> LinkResult: ...


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
    # 连通的几个频道共用的记录：这一段是哪个频道的，照那个频道的设置整理、记进那个服务器（或组）的那份。
    # 没连通的记录不填（照整批的 _ReviewPlan）
    origin: int | None = None
    cards_ok: bool | None = None
    memory_ok: bool | None = None
    dm_ok: bool | None = None
    info: ChannelInfo | None = None
    turn_ids: list[int] = field(default_factory=list)  # 这一段的记录（连通组的记录：整理完记进 review_done）
    # 连通组的记录压缩前挑出来的那批：编号是谁说的先记下（排着队的时候组散了，库里的编号映射就跟着删掉了）
    authors: dict[int, tuple[int, str]] | None = None
    reviewed: bool = False  # 这一段整理完了（或者放弃了）：断开、清空、散组时不再交给别的那份
    part_of: _ReviewChunk | None = None  # 压缩前挑出来的：它是安静时整理正在问的那一段里要压掉的那几条（那一段问完了就不用再整理）
    dropped: bool = False  # 排着队的时候这一段的频道清空了记忆：不整理（正在问的这一段问完也不照着改）


@dataclass
class _ReviewPlan:
    chunks: list[_ReviewChunk]
    cards_ok: bool
    memory_ok: bool
    last_id: int
    dm_ok: bool = False  # 私信：顺便整理他的私信记忆
    partial: bool = False  # 连通组的记录：有的服务器今天的预算用完了，它那部分这次没整理（整理完别把进度记到最后）


@dataclass
class _ReviewJob:
    """压缩前挑出来、排着队等整理的一批（原话已经挑出来了，记录里已经没有了）。"""

    conv: Conversation
    info: ChannelInfo
    plan: _ReviewPlan
    generation: int
    frozen: bool = False  # 连通的频道连通时挑出来的它自己原来那份（不会压掉）：没整理成的不记成整理过，以后再整理


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


async def _together(*jobs: Awaitable[Any]) -> list[Any]:
    """同时办这几件互不相干的事，结果照传进来的顺序给。有一件出错（或者这一轮被取消）：别的也停下，等它们收拾完，
    再把那个错照原样抛出去（和一件一件办时一样是它本来的错，不是包了一层的）。"""
    tasks = [asyncio.ensure_future(job) for job in jobs]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        await _settle(tasks)
        raise


async def _settle(tasks: Iterable[asyncio.Future[Any]]) -> None:
    """提前开始办的那几件事用不着了（出错、关机）：还没办完的让它停下，等它收拾完（记账的那一笔别一直“进行中”）；
    办完了没人要的，把它的错取走（免得日志里多一条“没人处理的异常”）。"""
    tasks = list(tasks)
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in tasks:
        if not task.cancelled():
            task.exception()


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


# 近况里写的是“在「某某」服务器的 #大厅 跟你聊过”，能翻的频道名单里写的是“#大厅（「某某」服务器）”：照着前一种写的也认得
_SERVER_PREFIX = re.compile(r"^在?「(?P<server>[^」]{1,100})」服务器的\s*(?P<name>.+)$")  # 服务器名最长 100 字
_SERVER_SUFFIX = re.compile(r"^(?P<name>.+?)\s*[（(]「(?P<server>[^」]{1,100})」服务器[）)]$")
SERVER_NAME_CHARS = 40  # 给模型看的别的服务器的名字最多几个字（近况、能翻的频道、组名单都按这个截：模型照哪种写都对得上）


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


DM_MEMORY_ATTACH_CHARS = 2000  # 频道里附上某人的私信记忆，最多多少字


def _newest(entries: Sequence[Any], max_chars: int, max_count: int) -> list[Any]:
    """挑最近改过的几条（字数、条数都不超过上限；第一条再长也留着），再按编号排回去。"""
    picked: list[Any] = []
    used = 0
    for e in sorted(entries, key=lambda e: (e.updated_at, e.no), reverse=True):
        if len(picked) >= max_count or (picked and used + len(e.text) > max_chars):
            break
        picked.append(e)
        used += len(e.text)
    return sorted(picked, key=lambda e: e.no)


PASSIVE_REPLY = dump_actions([])  # 连通的频道：别的频道补进记录的那几条后面，记成它没说话（没问模型）
# 补进来的“（#12 阿明 改了这条消息，现在是：……）”：编号和名字（那一段被拒了换掉时，只留一句“改过”）
_EDIT_NOTE_HEAD = re.compile(
    "^" + re.escape(EDIT_NOTE.split("{text}")[0]).replace(re.escape("{sid}"), r"(\d+)").replace(re.escape("{name}"), "(.+?)")
)
SYNC_PAST_CHARS = 3000  # 连通的频道：连通之前自己的记录，给它看最近多少字（只给这个频道看）
MAX_SYNC_REVIEW_CHUNKS = 8  # 连通组的记录：安静时一次最多整理几段（几个频道分开整理，段数多一些）
PAUSED_WARN_EVERY = 3600.0  # 连通的频道变成私密的了：多久提醒一次（日志）


class _Shared:
    """ChannelState 上的这几样其实存在它的 ConvState 里：连通的几个频道共用一份（没连通的频道一个频道一份，和以前一样）。"""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        return self if obj is None else getattr(obj.conv, self.name)

    def __set__(self, obj: Any, value: Any) -> None:
        setattr(obj.conv, self.name, value)


@dataclass
class ConvState:
    """一份聊天记录，和跟着它走的状态：消息编号、读过的文件、删掉改过的消息、附过的人设卡、整理记忆的进度……
    没连通的频道一个频道一份（key 就是频道）；连通的几个频道共用一份（key 是连通组的 ID，负数）。
    连通的频道自己原来那份（连通之前的记录）也有一份：连通期间不往里写，只同步删除、编辑，给它自己的频道看个大概。"""

    key: int
    guild_id: int  # 新建记录时记在哪个服务器下（连通组是 0）
    generation: int  # 每次清空、换了记录都换一个新的（全局不重复），跑到一半的轮次据此放弃写入
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # 改这份记录（跑一轮、同步删改、挑要整理的）要拿着
    conversation: Conversation | None = None
    sid_by_msg: dict[int, int] = field(default_factory=dict)
    msg_by_sid: dict[int, int] = field(default_factory=dict)
    sid_origin: dict[int, int] = field(default_factory=dict)  # 连通组：编号 -> 那条消息在哪个频道（只能回、只能加表情给这里的）
    recent_files: dict[int, FileEntry] = field(default_factory=dict)  # message_id -> 带文件的消息
    recent_links: dict[int, LinkEntry] = field(default_factory=dict)  # message_id -> 带链接的消息
    digest_seen: dict[int, int] = field(default_factory=dict)  # 人 -> 已经带进这份记录的、他在别处最近一次对话的序号
    cards_shown: dict[int, tuple[Any, ...]] = field(default_factory=dict)  # 人 -> (附上他的卡的那条记录, 卡的版本[, 卡记在哪])
    # 人 -> (附上他私信记忆的那条记录, 附上的内容)：内容变了、或者那条记录被压缩掉了，再附一次
    dm_memory_shown: dict[int, tuple[int, str]] = field(default_factory=dict)  # 也记在库里（撤回时要找到那条记录）
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
    edit_notes: dict[int, tuple[int, IncomingMessage]] = field(default_factory=dict)  # 很早的消息被编辑：消息 -> (编号, 改过的样子)
    review_done: set[int] = field(default_factory=set)  # 连通组：这批里已经整理过的记录（几个频道分开整理，有的先整理完了）
    review_asking: _ReviewChunk | None = None  # 安静时整理正在问的那一段（压缩时：连通组连同它一起挑出来；频道自己那份见 _park_asking）
    review_parked: list[_ReviewJob] = field(default_factory=list)  # 频道自己那份：正在问的那一段里压掉了的那几条（只在断开时交给新的那份）
    review_cut: bool = False  # 连通组安静时整理的时候压缩了（压掉的挑出来排着队了）：问完手上这一段就停
    review_current: _ReviewJob | None = None  # 正在整理的那批压缩前挑出来的（清空、散组时没整理完的交给新的那份接着整理）
    scrub_due: bool = False  # 正在跑的那一轮跑着的时候组变成跨服务器了：这一轮写完、放开锁之前再查一遍（scrub_cross）
    scrub_lookups: bool = False  # 那时查出了只该原来那边看的：这一轮按原来的组要翻记录、查人设卡的动作，写完再查时也拿掉
    scrub_whole: bool = False  # 那时是组里原来有的服务器退出了服务器组（组里的频道还在一个组里）：写完再整份查一遍
    review_bad_by: dict[int | None, int] = field(default_factory=dict)  # 连通组：每个频道那部分模型输出连着不能用的次数
    # 连通的频道自己那份：暂停（变成私密的、被关掉了）时改过的、只在组里那份的消息（组里当成删了）。断开拿回来以后照这个改
    hidden_edits: dict[int, IncomingMessage] = field(default_factory=dict)
    hides: set[int] = field(default_factory=set)  # 排着的删除里，是“当成删了”的那几条（之后公开时又改了的照改，不是真删）
    # 没问模型就记进来的那几轮（别的连通频道补进来的、连通时记进它自己那份的）：带着它们的请求过了才算数，被拒了换成一句
    unvetted: list[int] = field(default_factory=list)
    # 连通组的记录里直接改成了新内容（没问过模型）的那几条消息：带着它们的请求过了才算数，被拒了就当成删了
    unvetted_edits: set[int] = field(default_factory=set)


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
    category_id: int | None = None
    pending: list[IncomingMessage] = field(default_factory=list)
    inflight: list[IncomingMessage] = field(default_factory=list)  # 正在处理的这一批
    # 连通的频道：这一批里在它暂停（变成私密的、被关掉了）时被改了的（还没编号的就不写进记录，见 _hide_unprocessed）
    withdrawn: set[int] = field(default_factory=set)
    # 连通的频道：这一轮的“连通之前的概要”带全了它自己那份里连通时记进去、还没问过模型的那几轮（请求过了它们才算数）
    past_vetted: bool = False
    omitted: int = 0
    first_human_at: float | None = None  # 缓冲区里第一条真人消息到达的时间
    last_activity_at: float = 0.0  # 最近一次有人说话或打字
    last_eval_at: float = 0.0
    last_speak_at: float = 0.0
    following: bool = False  # 正跟着这里的聊天（它开了口、或者被叫过）
    passes: int = 0  # 跟着的时候连着几次决定不说
    next_check_at: float = 0.0  # 平时：下一次什么时候看一眼（0 = 还没看过，有人说话就看）
    gap_until: float = 0.0  # 这之前不主动判断（刚说过话、刚判断过）
    waiting_since: float | None = None  # 平时这一眼从什么时候开始等冷场的
    # 冒泡（这个频道安静很久以后它自己来说点什么）
    bubble_due_at: float = 0.0  # 到这时候还一直没人说话就冒个泡（0 = 还没定）
    bubble_waiting: int = 0  # 连着冒了几次泡都没人说话（频道：1 次就等有人说话再冒；主动私信：连着 3 次）
    bubble_cap: int = 0  # bubble_cap_day 那天最多冒几次（档位给的是个范围时每天随机定一次）
    bubble_cap_day: str = ""
    bubble_last_at: float = 0.0
    bubble_day: str = ""
    bubble_count: int = 0  # bubble_day 那天冒了几次
    bubble_loaded: bool = False  # 上面这几项从数据库里读过了
    bubbling: bool = False  # 现在这一轮是冒泡
    bubble_found: bool = False  # 冒泡时替它搜的那一次有没有搜到东西
    bubble_unseen: int = 0  # 上次看大家的兴趣以后，又有多少条群友的消息
    bubble_scout_at: float = 0.0  # 上次看大家的兴趣是什么时候
    mention_due: bool = False
    scheduled: bool = False
    error_until: float = 0.0
    send_failures: int = 0
    last_image_at: float = 0.0  # 上次上网找图的时间（同一个频道隔一会儿才能再找）
    media_failures: int = 0  # 连着几次图发不出去（多半是缺“附加文件”权限）
    media_blocked_until: float = 0.0  # 发图连着失败：这之前先不发图（文字照常）
    gallery: list[Picture] = field(default_factory=list)  # 这一轮找到的候选图（pick 从这里挑）
    listed: tuple[str, ...] | None = None  # 已经写进这个频道记录里的“能翻记录的频道”名单（连通组的记录里只认它自己写的）
    last_decision: str = ""
    quiet_dm: bool = False  # 私信这一轮刚记了敏感的事：这一轮的对话不当近况带到频道里
    # 这一轮给它看的长期记忆是哪一份（_memory_key：记在哪、服务器组变过几次）：中途连组、退组了就别照着编号改
    memory_key: tuple[int, int] | None = None
    memory_shown: frozenset[int] = frozenset()  # 这一轮给它看过的长期记忆编号（没看过的编号不照着改、删）
    people: dict[int, str] = field(default_factory=dict)  # 最近说过话、被 @ 过的人 -> 名字（有序：最近的在后）
    music_seen: str | None = None  # 上次告诉它“语音频道里正在放什么”的那一行（变了才再写；没在放是 None）
    private: bool = False  # 私信（guild_id 是它挂在的那个服务器）
    last_typing_at: float = 0.0  # 最近一次有人在打字
    dm_waiting: bool = False  # 私信：正在等对方说完
    dm_notice_day: str = ""  # 私信：哪天已经说过“今天聊太多了”
    remind_notes: list[str] = field(default_factory=list)  # 提醒的事（设好了、取消了、到点说过了）：下一轮告诉它
    conv: ConvState = None  # type: ignore[assignment]  # 它现在用的那份记录（Engine.state 建的时候填上）
    conv_seen: Conversation | None = None  # 照哪次载入的记录算过上面这几样（名单、最近说话的人、正在放什么）

    # 下面这些存在 conv 里（连通的几个频道共用）：写法照旧是 st.xxx
    lock = _Shared()
    conversation = _Shared()
    generation = _Shared()
    sid_by_msg = _Shared()
    msg_by_sid = _Shared()
    recent_files = _Shared()
    recent_links = _Shared()
    digest_seen = _Shared()
    cards_shown = _Shared()
    dm_memory_shown = _Shared()
    unreviewed_rounds = _Shared()
    reviewing = _Shared()
    last_review_at = _Shared()
    review_failures = _Shared()
    review_bad = _Shared()
    review_jobs = _Shared()
    review_held = _Shared()
    deleted = _Shared()
    fixes = _Shared()
    fixing = _Shared()
    edit_notes = _Shared()


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
        self.link_reader = link_reader  # 打开链接（推文走 FxTwitter，网页由 DeepSeek 读，Tavily 备用）
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
        self.announcing = False  # 正在发更新公告（一次只发一条）
        self.rng = rng or random.Random()
        self.image_loader = image_loader or self._load_image
        self.file_reader = file_reader or self._read_file
        self._file_slots = asyncio.Semaphore(2)  # 同时最多解析 2 个文件
        self._decode_slots = asyncio.Semaphore(2)  # 同时最多解码 2 张图（大图解码很吃内存）
        self.tz = cfg.bot.timezone
        self.pricing = cfg.pricing
        self.states: dict[int, ChannelState] = {}
        self.convs: dict[int, ConvState] = {}  # 记录 -> 它的状态（没连通的频道按频道，连通组按组的 ID）
        # 散掉的连通组留下的、还有压缩前挑出来的那几批在整理的状态（不在 convs 里）：清空记忆时也要找得到
        self._orphans: list[ConvState] = []
        self._generations = itertools.count(1)  # ConvState.generation 从这里取：换了记录也不会和旧的那份撞上
        self._paused_warned: dict[int, float] = {}  # 连通的频道变成私密的了：上次提醒的时间
        self.limiter = SlidingWindow()
        self.paused_until = 0.0  # 余额不足 / key 错误时全局暂停
        self._last_housekeep = 0.0
        self._reach: dict[int, tuple[int, str, int | None, int | None]] | None = None  # 开了主动私信的人（每次 tick 重读）
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
            st = ChannelState(channel_id, guild_id, parent_id, category_id)
            st.private = private
            key = self.store.conversation_key(channel_id)
            st.conv = self._conv_state(key, guild_id if key == channel_id else 0)
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
        """这条消息它看不看：频道看“允许回应”；私信看它挂的那个服务器开没开私信。连通的频道变成私密的了也不看
        （连通暂停：这里的话会进几个服务器共用的记录）。"""
        if msg.private:
            return bool(self.settings.get("dm", guild_id=msg.guild_id))
        if not self.enabled(msg.guild_id, msg.channel_id, msg.parent_id, msg.category_id):
            return False
        if self.store.sync_of(msg.channel_id) is not None:
            if not self._sync_public(msg.channel_id):
                self._note_paused(msg.channel_id)
                return False
            self._paused_warned.pop(msg.channel_id, None)  # 又公开了：以后再变成私密的，再提醒
        return True

    def _sync_public(self, channel_id: int) -> bool:
        """连通的频道现在还是公开的文字频道（变成私密的、成人内容频道，或者找不到了：连通暂停）。"""
        place = self.transport.locate(channel_id)
        return place is not None and place.public and not place.dm and place.parent_id is None

    def _note_paused(self, channel_id: int) -> None:
        now = self.clock()
        if now - self._paused_warned.get(channel_id, float("-inf")) >= PAUSED_WARN_EVERY:
            self._paused_warned[channel_id] = now
            log.warning("连通：频道 %s 现在不是公开频道了，连通暂停（它不看这里的消息）：bot 主人断开连通，或者改回公开", channel_id)

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

        self._bubble_heard(st, view, now)
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

    @staticmethod
    def _refresh_held(cs: ConvState, m: IncomingMessage) -> None:
        """消息改了：这份记录带着的它的文件、链接换成新的样子（附件、链接都删掉了的就拿掉；没带着的不另外加）。"""
        fentry = cs.recent_files.get(m.message_id)
        if fentry is not None:
            if m.files:
                fentry.message = m
            else:
                del cs.recent_files[m.message_id]
        lentry = cs.recent_links.get(m.message_id)
        if lentry is not None:
            if m.links:
                lentry.message = m
            else:
                del cs.recent_links[m.message_id]

    def _other_convs(self, st: ChannelState, message_ids: Iterable[int]) -> list[ConvState]:
        """有这几条消息的别的记录（已经在内存里的）：连通的频道断开以后组里那份、连通之前它自己那份。没连通过就是空的。"""
        keys = sorted(self.store.message_keys(message_ids))
        return [cs for key in keys if key != st.conv.key and (cs := self.convs.get(key)) is not None]

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
            if (
                st is not None
                and self._synced(st)
                and self.find_message(updated.channel_id, updated.message_id) is not None
                and self._edit_hidden(st, updated)
            ):
                return  # 连通的频道暂停着（变成私密的、被关掉了）：改过的样子它不看，缓冲区里的照旧是改之前的
            self.refresh_message(updated)
            return
        if (
            st is not None
            and self._synced(st)
            and self.find_message(updated.channel_id, updated.message_id) is not None
            and self._edit_hidden(st, updated)
        ):
            self._hide_unprocessed(st, updated, edited=edited)
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
        mid = updated.message_id
        hidden = self._edit_hidden(st, updated)
        if hidden and st.conv.key < 0:
            # 连通的频道变成私密的、被关掉了：改过的这条（连同带着的文件、链接）不带进共用的记录，当成删了
            st.recent_files.pop(mid, None)
            st.recent_links.pop(mid, None)
            st.edit_notes.pop(mid, None)
        elif st.recent_files.get(mid) is not None or st.recent_links.get(mid) is not None:
            self._refresh_entries(st, updated)
        for cs in self._other_convs(st, [mid]):
            if hidden and cs.key < 0:
                cs.recent_files.pop(mid, None)  # 连通组的记录（断开以后还留着它说过的话的也算）：一样当成删了
                cs.recent_links.pop(mid, None)
                cs.edit_notes.pop(mid, None)
            else:
                self._refresh_held(cs, updated)  # 别的记录里带着的文件、链接也换成新的样子
        self._edit_interactions(st.guild_id, updated)
        self._queue_fix(st, mid, updated)

    def _hide_unprocessed(self, st: ChannelState, updated: IncomingMessage, *, edited: bool) -> None:
        """连通的频道变成私密的、被关掉了，还没处理完的一条又有了更新：改过的样子它不看（几个服务器共用的记录里不带）。
        本人改过的：还在缓冲区里的拿掉（当成删了）；正在处理的那一批照旧写进记录（模型看到的是改之前的），写完以后组里那份
        当成删了。它带着的文件、链接在共用的那份里也拿掉。只是 Discord 补上了预览的，什么都不改。"""
        if not edited:
            return
        mid = updated.message_id
        st.recent_files.pop(mid, None)
        st.recent_links.pop(mid, None)
        st.edit_notes.pop(mid, None)
        if any(m.message_id == mid for m in st.inflight):
            # 正在处理的那一批：写进记录以后组里那份当成删了（它自己那份断开拿回来时照改过的写）；还没编号的（等预览的时候）
            # 这一轮就不写进去。缓冲区里也拿掉：这一轮没跑成，也不会留着以后再看、被别的频道补进记录
            self._queue_fix(st, mid, updated)
            st.withdrawn.add(mid)
        kept = [m for m in st.pending if m.message_id != mid]
        if len(kept) != len(st.pending):
            st.pending = kept
            st.first_human_at = next((m.received_at for m in st.pending if not m.is_bot), None)
            st.mention_due = st.mention_due and any(m.trigger for m in st.pending)

    def handle_delete(self, guild_id: int, channel_id: int, message_ids: Iterable[int]) -> None:
        """消息被删了（包括管理员一次删很多条）：缓冲区里的拿掉；正在处理的，结果回来后不回复它；
        已经写进记录的，记录里那一行改成“已删除”，它的图、文件和链接内容也一起拿掉。"""
        ids = set(message_ids)
        if not ids:
            return
        for orphan in self._orphans:
            # 散掉的组留下的、还在整理的那几批：散掉以后才删的也要认得出（不拿删掉的消息写人设卡；清空时接手的那份用的是同一个）。
            # 只记那份记录里有的消息：别处一次删很多条，会把它记着的挤掉
            for mid in ids & set(orphan.msg_by_sid.values()):
                orphan.deleted[mid] = None
            while len(orphan.deleted) > DELETED_KEEP:
                del orphan.deleted[next(iter(orphan.deleted))]
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
        for cs in self._other_convs(st, ids):
            # 这几条也在别的记录里（断开以后组里那份、连通之前它自己那份）：那边带着的文件、链接也打不开了
            for mid in ids:
                cs.recent_files.pop(mid, None)
                cs.recent_links.pop(mid, None)
                cs.edit_notes.pop(mid, None)
                cs.deleted[mid] = None
            while len(cs.deleted) > DELETED_KEEP:
                del cs.deleted[next(iter(cs.deleted))]
        self._forget_interactions(st.guild_id, ids)
        for mid in ids:
            self._queue_fix(st, mid, None)

    def _state_with_history(self, guild_id: int, channel_id: int, message_ids: Iterable[int]) -> ChannelState | None:
        """这个频道的状态；还没载入过（比如刚重启）的，只有记录里有这几条消息时才建。"""
        st = self.states.get(channel_id)
        if st is not None:
            return st
        # 连通的频道：连通之后的在共用的记录里，连通之前的在它自己原来那份里；断开以后，连通期间的两份都有
        if not self.store.message_keys(message_ids):
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
        """删除、编辑要改进记录：这个频道现在用的那份，加上有这条消息的每一份（连通的频道连通之前的在它自己原来那份里；
        断开以后，连通期间说的还留在那个组的记录里，那边也要跟着改）。"""
        held = self.store.message_keys([message_id])
        keys = {st.conv.key} | held
        if st.conv.key != st.channel_id and not st.private:
            keys.add(st.channel_id)  # 正在处理的这一批：还没记进去，写进记录后再改
        # 连通组的记录：这个频道现在它不看了（变成私密的、被关掉了），改的新内容不写进去，那一行当成删了（它自己那份照常改）
        hidden = updated is not None and any(key < 0 for key in keys) and self._edit_hidden(st, updated)
        if st.conv.key < 0:
            own = self.convs.get(st.channel_id)
            if updated is not None and hidden and st.channel_id not in held:
                # 连通期间说的（只在组里那份）：断开拿回来以后，它自己那份照改过的写（只有它自己看）
                own = own or self._conv_state(st.channel_id, st.guild_id)
                own.hidden_edits[message_id] = updated
            elif own is not None:
                own.hidden_edits.pop(message_id, None)  # 后来删了、公开的时候又改了：照那一次
        for key in sorted(keys):
            cs = self.convs.get(key) or self._conv_state(key, st.guild_id if key > 0 else 0)
            self._queue_fix_on(cs, message_id, None if hidden and key < 0 else updated, hide=hidden and key < 0)

    def _edit_hidden(self, st: ChannelState, m: IncomingMessage) -> bool:
        """这条消息改过的新内容不能写进连通组的记录（几个频道、几个服务器共用）：那个频道现在它不看了——变成私密的（连通暂停）、
        被关掉了；以前连通过、组里还留着它说过的话的频道也一样。不在连通组的记录里的（没连通过的频道都是这样）不算。"""
        if st.conv.key > 0 and all(key > 0 for key in self.store.message_keys([m.message_id])):
            return False
        return not (self.enabled(m.guild_id, m.channel_id, m.parent_id, m.category_id) and self._sync_public(m.channel_id))

    def _queue_fix_on(self, cs: ConvState, message_id: int, updated: IncomingMessage | None, *, hide: bool = False) -> None:
        """hide：这是“当成删了”（连通的频道暂停时改的，见 _edit_hidden），不是真删：之后公开时又改了，照改过的写。"""
        if cs.fixes.get(message_id, 0) is None and updated is not None and message_id not in cs.hides:
            return  # 已经删了：之后的编辑不算
        if hide:
            cs.hides.add(message_id)
        else:
            cs.hides.discard(message_id)
        cs.fixes[message_id] = updated
        if cs.fixing:
            return
        cs.fixing = True
        task = asyncio.get_running_loop().create_task(self._fix_task(cs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fix_task(self, cs: ConvState) -> None:
        """把删除、编辑改写进记录。要等用这份记录的频道正在跑的那一轮写完记录（拿到锁）再改。"""
        try:
            while cs.fixes:
                async with cs.lock:
                    if self.convs.get(cs.key) is not cs:
                        cs.fixes.clear()  # 等锁的时候这份记录换成了新的：要改的已经交给新的那份（或者那份记录已经没了）
                        break
                    owner = self.states.get(cs.key)
                    conv = self._conversation(owner) if owner is not None and owner.conv is cs else self._load(cs)
                    self._flush_fixes(cs, conv)
        finally:
            cs.fixing = False
        for st in self._members(cs):
            if st.mention_due and st.pending:
                self._wake(st)  # 改记录的时候有人叫它：轮到它了

    def _flush_fixes(self, owner: Any, conv: Conversation) -> None:
        """把还没改写进记录的删除、编辑先改掉（调用方拿着锁；owner 是频道的状态或者那份记录）。开始新的一轮、压缩、
        整理记忆之前都先做一次，免得模型、摘要、整理记忆看到刚删掉的内容。"""
        cs = self._cs(owner)
        if not cs.fixes:
            return
        fixes, cs.fixes = cs.fixes, {}
        cs.hides.difference_update(fixes)
        try:
            self._apply_fixes(cs, conv, fixes)
        except Exception:
            log.exception("频道 %s：同步删除、编辑出错", cs.key)

    def _apply_fixes(self, cs: ConvState, conv: Conversation, fixes: dict[int, IncomingMessage | None]) -> None:
        rows = self.store.message_rows(cs.key, fixes.keys())
        changed: dict[int, Turn] = {}
        unvetted = False
        # 连通的频道自己原来那份：连通期间不往后写，前缀缓存不用顾，改了就直接改（不留到下一轮附一句）
        recent_from = 0 if self._frozen(cs) else len(conv.turns) - RECENT_EDIT_TURNS
        gone: set[int] = set()  # 连通组的记录里已经断开的频道的消息：那边不会再有一轮附上“改成了什么”，也直接改
        if cs.key < 0:
            members = set(self.store.sync_members(cs.key))
            found = [sid for hits in rows.values() for sid, _ in hits]
            gone = {sid for sid, origin in self.store.msg_origins(cs.key, found).items() if origin not in members}
        for mid, updated in fixes.items():
            for sid, name in rows.get(mid, []):
                if updated is None:
                    lines: list[str] | None = None
                    drop_images = drop_docs = True
                else:
                    block = render_line(updated, sid, cs.sid_by_msg, self.ec.max_chars_per_message).split("\n")
                    where = next(
                        ((i, old) for i, t in enumerate(conv.turns) if t.role == "user" and (old := message_block(t.content, sid)) is not None),
                        None,
                    )
                    if where is None:
                        continue  # 已经压进摘要了
                    index, old = where
                    if old[:-1] + [old[-1].removesuffix(EDITED_MARK)] == block:
                        if cs.key < 0 or index >= recent_from:
                            # 改回了记录里的样子，还没附上的“改成了什么”作废：连通组的记录里（别让别的服务器知道她收回的话），
                            # 还有那一行是最近的时候（断开时跟着频道回来的那句、清空记忆删掉了后面的：只有连通才会这样）。
                            # 频道自己那份里很早的一行照 2.7.1
                            cs.edit_notes.pop(mid, None)
                        continue  # 内容其实没变（比如只是补上了预览）
                    if index < recent_from and sid not in gone:
                        cs.edit_notes[mid] = (sid, updated)  # 很早的消息：下一轮附一句改成了什么，旧记录不动
                        continue
                    # 这一行直接改成了最新的样子：还没附上的“改成了什么”作废（以前很早的时候记下的——那一行后来成了最近的：断开时
                    # 跟着频道回到它自己那份、清空记忆删掉了后面的——不然照旧的说，被拒了改好也没用，再连通时又改回旧的）
                    cs.edit_notes.pop(mid, None)
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
                        if updated is not None and cs.key < 0:
                            cs.unvetted_edits.add(mid)  # 连通组的记录：改成的内容还没问过模型
                            unvetted = True
        if unvetted:
            self._keep_unvetted(cs)  # 先记下再改进去：停在中间的话，宁可被拒时多当成删了一条
        for turn in changed.values():
            self.store.update_turn(turn.id, turn.content)
        if changed:
            deleted = sum(1 for u in fixes.values() if u is None)
            log.info(
                "频道 %s：同步了 %d 条删除、%d 条编辑，改写了 %d 条记录",
                cs.key,
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
        self._cap_entries(st, st.recent_files, RECENT_FILES_KEEP)
        for mid in [mid for mid, e in st.recent_links.items() if e.message.received_at < cutoff]:
            del st.recent_links[mid]
        self._cap_entries(st, st.recent_links, RECENT_LINKS_KEEP)

    def _cap_entries(self, st: ChannelState, entries: dict[int, Any], keep: int) -> None:
        """带文件（链接）的消息只留最近 keep 条。连通组共用的那份里每个频道各留各的：别的频道刷屏挤不掉这个频道刚发的。"""
        if not self._synced(st):
            while len(entries) > keep:
                del entries[next(iter(entries))]
            return
        mine = [mid for mid, entry in entries.items() if entry.message.channel_id == st.channel_id]
        for mid in mine[: max(0, len(mine) - keep)]:
            del entries[mid]

    # ------------------------------------------------------------------ 调度

    async def tick(self) -> None:
        """由外部每隔几秒调用一次，检查哪些频道该跑一轮。"""
        now = self.clock()
        for channel_id, on in self.settings.scoped("channel", "bubble").items():
            if on and on != "off" and channel_id not in self.states:
                place = self.transport.locate(channel_id)  # 重启以后还没人说过话的频道：先建好状态，冒泡的钟才开始走
                if place is not None and not place.dm:
                    self.state(channel_id, place.guild_id, place.parent_id, place.category_id)
        self._reach = self.store.dm_reach_channels()
        for channel_id in self._reach:
            if channel_id not in self.states:
                place = self.transport.locate(channel_id)  # 开了主动私信的人：重启以后他还没说过话，钟也要走
                if place is not None and place.dm:
                    self.state(channel_id, place.guild_id, private=True)
        for st in list(self.states.values()):
            if self._review_due(st, now):
                self._kick_review(st)  # 安静了一阵：整理一下记忆
                continue
            if st.scheduled or st.lock.locked():
                continue
            if st.pending and (st.mention_due or self._proactive_ready(st, now)):
                self._wake(st)
            elif self._bubble_ready(st, now) or self._bubble_scout_ready(st, now):
                self._wake(st)
        self._remind_due(now)
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
        self.store.tidy_reminders(now - rm.PENDING_SECONDS, now - 7 * 86400.0)
        self.store.tidy_channel_notes(now - an.NOTE_KEEP_SECONDS)
        for key in [k for k, at in self.pinged.items() if now - at >= PING_COOLDOWN]:
            del self.pinged[key]
        self.ping_window.prune(GUILD_PING_WINDOW, now)
        ttl = self.ec.share_recent_hours * 3600
        for key, log_ in list(self.interactions.items()):
            while log_ and now - log_[0].at > ttl:
                log_.popleft()
            if not log_:
                del self.interactions[key]
        talked = {person for _, person in self.interactions}  # 跨服务器时，别的服务器的对话也会带过来
        for st in self.states.values():
            # 这个人在别处已经没有可带的对话了，记录“带到哪了”也没用了；以后新的对话序号更大，照样会带
            for person in [p for p in st.digest_seen if p not in talked]:
                del st.digest_seen[person]
        for cs in self.convs.values():
            members = self._members(cs)
            if cs.conversation is None or cs.lock.locked() or any(m.scheduled for m in members):
                continue
            if members:
                if min(now - max(m.last_activity_at, m.last_eval_at) for m in members) > IDLE_EVICT:
                    cs.conversation = None
            elif not cs.fixing and not cs.reviewing:
                cs.conversation = None  # 没人用的（连通的频道自己原来那份）：放掉，要用时再读

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
        return self._guild_over(st.guild_id, view, now)

    def _guild_over(self, guild_id: int, view: View, now: float) -> tuple[bool, bool]:
        """(超了每日预算, 超了好几倍连点名也不回)。"""
        guild = self.ledger.guild_spent(guild_id, now)
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
        if st.private or not view.enabled or st.first_human_at is None:
            return False
        if not view.proactive and not st.following:
            return False  # 没开主动发言：只有冒泡以后跟着聊的那一阵才判断
        if now < self.paused_until or now < st.error_until:
            return False
        if now < st.gap_until or self._over_budget(st, view, now)[0]:
            return False
        if st.following and (not view.follow or now - st.last_activity_at >= FOLLOW_IDLE):
            self._stop_following(st, view, now)  # 没人说话了（或者管理员刚关掉了“跟着”）：回到平时
        if st.following:
            # 跟着：有人说了话（缓冲区里有真人的消息），等他说完就看要不要接
            return now - st.last_activity_at >= FOLLOW_SETTLE or now - st.first_human_at >= FOLLOW_MAX_WAIT
        if not view.proactive or now < st.next_check_at:
            return False
        # 平时看一眼：最近没人说话就算了（不问模型），过几分钟再看；攒着的旧消息留着当背景
        newest = max((m.received_at for m in st.pending if not m.is_bot), default=0.0)
        if now - newest > FRESH_SECONDS:
            st.waiting_since = None
            st.next_check_at = now + self._check_interval(view)
            return False
        if st.waiting_since is None:
            fresh = [m.received_at for m in st.pending if not m.is_bot and now - m.received_at <= FRESH_SECONDS]
            st.waiting_since = max(st.next_check_at, min(fresh))
        # 正聊着：等停一下再开口（别人正说着不插话）；一直不停，等够了也看一次
        return now - st.last_activity_at >= view.quiet_seconds or now - st.waiting_since >= view.max_wait_seconds

    # ------------------------------------------------------------------ 冒泡

    def reach_changed(self) -> None:
        """有人改了自己的主动私信设置（/ds-dm 主动）：马上照新的来。"""
        self._reach = None

    def _bubble_profile(self, st: ChannelState) -> _Bubble | None:
        """这里的冒泡照什么规矩来（没开是 None）。频道：只认频道自己这一级的设置（开在服务器、类别上不算，免得一下子
        每个频道都冒泡），时段是 config.toml 里的。私信：看对方自己用 /ds-dm 主动 定的档位和时段。"""
        if st.private:
            if self._reach is None:
                self._reach = self.store.dm_reach_channels()
            reach = self._reach.get(st.channel_id)
            if reach is None or reach[1] not in REACH_LEVELS:
                return None
            low, high, least, most = REACH_LEVELS[reach[1]]
            return _Bubble(low, high, least, most, max(1, self.ec.reach_max_unanswered), reach[2], reach[3])
        try:
            level = level_of(self.settings.overrides("channel", st.channel_id).get("bubble", "off"))
        except ValueError:
            level = "off"
        if level == "off":
            return None
        low, high, least, most = BUBBLE_LEVELS[level]
        return _Bubble(low, high, least, most, 1, self.ec.bubble_from_hour, self.ec.bubble_until_hour)

    def _bubble_on(self, st: ChannelState) -> bool:
        return self._bubble_profile(st) is not None

    def _bubble_interval(self, st: ChannelState) -> float:
        """安静多久以后冒泡（秒）：档位给的最短到最长之间，每次随机。"""
        profile = self._bubble_profile(st)
        low, high = (profile.low, profile.high) if profile is not None else (4.0, 6.0)
        return self.rng.uniform(low * 3600.0, high * 3600.0)

    def _bubble_cap(self, st: ChannelState, profile: _Bubble, now: float) -> int:
        """今天最多冒几次：档位给的是个范围（主动私信的 5～7 次）时，每天随机定一次。"""
        day = self.ledger.day(now)
        if st.bubble_cap_day != day or not profile.least <= st.bubble_cap <= profile.most:
            st.bubble_cap_day = day
            st.bubble_cap = profile.least if profile.least == profile.most else self.rng.randint(profile.least, profile.most)
        return st.bubble_cap

    def _reach_off(self, st: ChannelState) -> None:
        reach = (self._reach or {}).get(st.channel_id)
        if reach is not None:
            self.store.set_dm_reach(reach[0], None)
            self._reach = None
            log.warning("私信 %s：主动私信连着发不出去，已经替他关掉（他可以用 /ds-dm 主动 再打开）", st.channel_id)

    def _bubble_load(self, st: ChannelState) -> None:
        if not st.bubble_loaded:
            st.bubble_loaded = True
            st.bubble_last_at, st.bubble_waiting, st.bubble_day, st.bubble_count = self.store.bubble_state(st.channel_id)

    def _bubble_save(self, st: ChannelState) -> None:
        self.store.set_bubble_state(st.channel_id, st.bubble_last_at, st.bubble_waiting, st.bubble_day, st.bubble_count)

    def _bubble_heard(self, st: ChannelState, view: View, now: float) -> None:
        """有人说话了：冒泡的钟重新走；上次冒的泡有人理了。"""
        if not self._bubble_on(st):
            st.bubble_due_at = 0.0
            return
        self._bubble_load(st)
        st.bubble_due_at = now + self._bubble_interval(st)
        st.bubble_unseen += 1
        if st.bubble_waiting:
            st.bubble_waiting = 0
            self._bubble_save(st)

    def _bubble_ready(self, st: ChannelState, now: float) -> bool:
        """该冒泡了：开着、安静够久了、在能冒的时段里、今天还没冒够、前面冒的有人理过（主动私信：没有连着几次没人回）。"""
        profile = self._bubble_profile(st)
        if profile is None:
            return False
        view = self.view(st)
        if not self._live(st, view) or self._synced(st) or now < self.paused_until or now < st.error_until:
            return False  # 连通的频道（几个频道共用一份记录）先不冒泡
        self._bubble_load(st)
        if st.bubble_due_at == 0.0:
            st.bubble_due_at = now + self._bubble_interval(st)  # 刚打开、刚启动：从现在算起
            return False
        if st.bubble_waiting >= profile.strikes or now < st.bubble_due_at:
            return False
        local = datetime.fromtimestamp(now, ZoneInfo(self.tz))
        if not _in_hours(local.hour, profile.from_hour, profile.until_hour):
            # 不在能冒的时段里：挪到下一次开始以后（加一点随机，免得几个频道整点一起冒）
            start = local.replace(hour=int(profile.from_hour or 0), minute=0, second=0, microsecond=0)
            if start <= local:
                start += timedelta(days=1)
            st.bubble_due_at = start.timestamp() + self.rng.uniform(0.0, 3600.0)
            return False
        if self._bubbles_today(st, now) >= self._bubble_cap(st, profile, now):
            return False
        if st.private and self.store.channel_spent(self.ledger.day(now), st.channel_id) >= view.dm_daily_budget_usd:
            return False  # 他今天私信花的到了每人的上限：不再主动找他
        return not self._over_budget(st, view, now)[0]

    def _bubbles_today(self, st: ChannelState, now: float) -> int:
        return st.bubble_count if st.bubble_day == self.ledger.day(now) else 0

    @staticmethod
    def _bubble_quiet(st: ChannelState, now: float) -> str:
        quiet = max(st.last_activity_at, st.last_speak_at, st.bubble_last_at)
        hours = (now - quiet) / 3600.0 if quiet else 0.0
        if hours >= 48:
            return f" {round(hours / 24)} 天"
        if hours >= 1:
            return f" {round(hours)} 个小时"
        return "好一阵"

    async def _bubble_plan(
        self, st: ChannelState, view: View, conv: Conversation, info: ChannelInfo, batch: list[IncomingMessage], now: float
    ) -> str:
        """冒泡那一轮最后写的那段说明。大多数时候先替它找一条新鲜事（看这群人对什么感兴趣 → 挑一个话题 → 搜），把搜到的
        写在说明上面让它挑着说；偶尔只让它打个招呼。不能搜、看不出兴趣、没搜到新东西时也只打招呼。"""
        if st.private:
            note = REACH_NOTE.replace("{user}", neutralize(one_line(info.dm_with or "对方", 32)).replace("{", "｛").replace("}", "｝"))
            free, picked, share = REACH_FREE, REACH_PICK, self.ec.reach_search_share
        else:
            note, free, picked, share = BUBBLE_NOTE, BUBBLE_GREET, BUBBLE_PICK, 1.0 - self.ec.bubble_greet_share
        greet = note.format(quiet=self._bubble_quiet(st, now), what=free)
        day = self.ledger.day(now)
        if not self._can_search(view) or self.store.count_usage(day, guild_id=st.guild_id, kind="search") >= int(view.daily_search_limit):
            return greet
        if self.rng.random() >= share:
            return greet
        if st.bubble_unseen or not any(k != "blocked" for _, k, _, _ in self.store.interests(st.channel_id)):
            await self._bubble_scout(st, conv, info, batch, now)  # 有还没看过的聊天（或者还什么都没记）：先看看大家对什么感兴趣
        # 从记着的兴趣里挑一个最近两天没搜过的（新记下的优先）；都搜过了就不搜，免得老说同一件事
        pool = [t for t, k, used_at, _ in self.store.interests(st.channel_id) if k != "blocked" and used_at < now - 2 * 86400.0]
        if not pool:
            return greet
        topic = self.rng.choice(pool[:3])
        st.bubble_found = False
        section = await self._search_section(st, view, Action("search", query=topic), now)
        if not st.bubble_found:
            return greet  # 没搜到新东西（或者搜不了）：这次就打个招呼
        links = self.store.bubble_links(st.channel_id, 8)
        pick = picked.format(
            topic=neutralize(one_line(topic, 40)), shared=BUBBLE_SHARED.format(links="、".join(links)) if links else ""
        )
        return section + "\n" + note.format(quiet=self._bubble_quiet(st, now), what=pick)

    def _bubble_scout_ready(self, st: ChannelState, now: float) -> bool:
        """该看一次大家的兴趣了：开了冒泡的频道，攒了几条新消息、聊天停了一阵。没开主动发言的频道里，没叫它的消息只留
        6 小时：等到冒泡时才看就晚了。"""
        if st.bubble_unseen < BUBBLE_SCOUT_MIN or not self._bubble_on(st) or self.closing:
            return False
        if now - st.last_activity_at < BUBBLE_SCOUT_QUIET or now - st.bubble_scout_at < BUBBLE_SCOUT_GAP:
            return False
        view = self.view(st)
        if not self._live(st, view) or self._synced(st) or now < self.paused_until or not self._can_search(view):
            return False
        return not self._over_budget(st, view, now)[0]

    async def _bubble_scout(self, st: ChannelState, conv: Conversation, info: ChannelInfo, batch: list[IncomingMessage], now: float) -> None:
        """看一次这个频道的人对什么感兴趣，记进兴趣里（记过的、不让搜的不动）。"""
        st.bubble_unseen = 0
        st.bubble_scout_at = now
        topics = await self._bubble_topics(st, conv, info, batch, now)
        if topics:
            self.store.note_interests(st.channel_id, topics, now, self.ec.bubble_max_topics)
            log.info("频道 %s：记下了 %d 个大家感兴趣的话题", st.channel_id, len(topics))

    async def _bubble_topics(
        self, st: ChannelState, conv: Conversation, info: ChannelInfo, batch: list[IncomingMessage], now: float
    ) -> list[str]:
        """单独问一次模型：这个频道的人对什么感兴趣（只给它看这个频道自己的聊天：别的频道带来的、人设卡、私信记忆、
        文件和搜索结果都不给）。问不成就当没看出来。不进聊天记录。"""
        lines: list[str] = []
        for t in conv.turns[-60:]:
            if t.role == "user":
                lines += review_text(t.content)
            else:
                raw = t.content if isinstance(t.content, str) else json.dumps(t.content, ensure_ascii=False)
                said = assistant_text(raw, info.bot_name)
                if said:
                    lines.append(said)
        lines += [f"{neutralize(one_line(m.author_name or '某人', 30))}：{neutralize(one_line(m.text, 300))}" for m in batch if not m.is_bot and m.text]
        chat = "\n".join(lines)[-6000:]
        if not chat.strip() and not conv.summary:
            return []
        known = [t for t, k, _, _ in self.store.interests(st.channel_id) if k != "blocked"][:20]
        system = BUBBLE_TOPICS_SYSTEM.format(
            name=info.bot_name,
            limit=BUBBLE_ASK_TOPICS,
            known=BUBBLE_TOPICS_KNOWN.format(topics="、".join(neutralize(one_line(t, 40)) for t in known)) if known else "",
            private=REACH_TOPICS_PRIVATE if st.private else "",
        )
        prompt = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"【更早的摘要】\n{conv.summary or '（无）'}\n\n【聊天记录】\n{chat or '（无）'}"},
        ]
        try:
            completion = await self.llm.complete(prompt, thinking="off", json_mode=True, max_tokens=300, user_id=f"guild-{st.guild_id}")
        except LLMError as exc:
            log.warning("频道 %s：冒泡前看大家的兴趣失败：%s", st.channel_id, exc)
            if exc.usage is not None:
                self._record_error(st.guild_id, st.channel_id, "bubble", exc)
            return []
        cost = self.pricing.cost(completion.usage, now)
        self.ledger.add(st.guild_id, cost, now)
        self.store.record_usage(
            ts=now, day=self.ledger.day(now), guild_id=st.guild_id, channel_id=st.channel_id, kind="bubble", outcome="topics",
            hit=completion.usage.hit, miss=completion.usage.miss, output=completion.usage.output, cost=cost,
        )
        try:
            found = json.loads(completion.content).get("topics")
        except (ValueError, AttributeError):
            return []
        out: list[str] = []
        for item in found if isinstance(found, list) else []:
            words = [w for w in str(item).split() if not w.lower().startswith(_SEARCH_SYNTAX)]
            topic = one_line(" ".join(words), 40).strip()
            if topic and topic.casefold() not in {t.casefold() for t in out}:
                out.append(topic)
        return out[:BUBBLE_ASK_TOPICS]

    def _bubble_queries(self, st: ChannelState, raw: str, now: float) -> tuple[str, list[str]] | None:
        """冒泡时模型要搜的话题 → (话题, 依次试的搜索词)。只要最近几天的；大部分时候先只搜 X 上的，没搜到再搜全网。
        管理员不让搜的话题返回 None。搜过的话题记进这个频道的兴趣里。"""
        words = [w for w in raw.split() if not w.lower().startswith(_SEARCH_SYNTAX)]
        topic = one_line(" ".join(words), 40).strip()
        blocked = {t.casefold() for t, k, _, _ in self.store.interests(st.channel_id) if k == "blocked"}
        if not topic or topic.casefold() in blocked:
            return None
        since = (datetime.fromtimestamp(now, ZoneInfo(self.tz)) - timedelta(days=self.ec.bubble_fresh_days)).date().isoformat()
        news = f"{topic} after:{since}"
        on_x = self.rng.random() < self.ec.bubble_x_share
        self.store.use_interest(st.channel_id, topic, now, self.ec.bubble_max_topics)
        return topic, ([f"{topic} site:x.com after:{since}", news] if on_x else [news])

    def _bubble_done(self, st: ChannelState, view: View, spoke: bool, actions: list[Action]) -> None:
        """冒泡那一轮问完了模型：钟重新走；真的说了话的算一次，记下发过的链接，等有人说话再冒下一次。"""
        now = self.clock()
        day = self.ledger.day(now)
        if st.bubble_day != day:
            st.bubble_day, st.bubble_count = day, 0
        st.bubble_due_at = now + self._bubble_interval(st)
        if spoke:
            st.bubble_count += 1
            st.bubble_last_at = now
            st.bubble_waiting += 1
            links = [u.rstrip(_LINK_TAIL) for a in actions if a.kind == "reply" for m in a.messages for u in _LINK.findall(m)]
            if links:
                self.store.add_bubble_links(st.channel_id, links, now)
        self._bubble_save(st)

    def _bubble_status(self, st: ChannelState, view: View, now: float) -> dict[str, Any]:
        self._bubble_load(st)
        profile = self._bubble_profile(st)
        assert profile is not None
        return {
            "today": self._bubbles_today(st, now),
            "limit": self._bubble_cap(st, profile, now),
            "waiting": st.bubble_waiting >= profile.strikes,
            "unanswered": st.bubble_waiting,
            "due_in": max(0.0, st.bubble_due_at - now) if st.bubble_due_at else None,
        }

    def _check_interval(self, view: View) -> float:
        """平时隔多久看一眼（秒）：check_minutes 到它的 1.5 倍之间，每次随机。"""
        low, high = check_range(view.check_minutes)
        return self.rng.uniform(low * 60.0, high * 60.0)

    def _stop_following(self, st: ChannelState, view: View, now: float) -> None:
        st.following = False
        st.passes = 0
        st.waiting_since = None
        st.next_check_at = now + self._check_interval(view)

    def _after_round(self, st: ChannelState, view: View, kind: str, spoke: bool, reacted: bool, bubble: bool = False) -> None:
        """一轮问完模型以后：定下一次什么时候再主动判断、要不要（接着）跟着这场聊天。按模型的决定算，不按发没发成功算
        （没权限发言的频道也不会一直判断）。"""
        now = self.clock()
        if spoke:
            st.last_speak_at = now
        # 冒泡以后有人接话，要能接着聊：这个频道没开主动发言也跟着（跟着的规矩一样）
        can_follow = not st.private and bool(view.follow) and (bool(view.proactive) or bubble)
        if kind == "mention":
            if spoke:
                st.gap_until = now + self.rng.uniform(*SPEAK_GAP)
                if can_follow:  # 有人叫了它、它也回了：接下来留意这场聊天
                    st.following, st.passes = True, 0
            return
        st.waiting_since = None
        if st.following:
            st.gap_until = now + self.rng.uniform(*SPEAK_GAP)  # 跟着的时候不是每句话都判断：隔一阵看一次
            st.passes = 0 if spoke or reacted else st.passes + 1
            if st.passes >= FOLLOW_PASSES:
                self._stop_following(st, view, now)  # 连着几次都没话说：这场聊天用不着它，回到平时
            return
        st.next_check_at = now + self._check_interval(view)
        if spoke:
            st.gap_until = now + self.rng.uniform(*SPEAK_GAP)
            if can_follow:
                st.following, st.passes = True, 0

    async def _run(self, st: ChannelState) -> None:
        while True:
            cs = st.conv
            async with cs.lock:
                if st.conv is not cs:
                    continue  # 等锁的时候这个频道换了记录（连通、断开、清空）：去拿现在那份的锁
                st.scheduled = False
                st.mention_due = False
                self._prune(st, self.clock())
                if not self._live(st, self.view(st)) or (self._synced(st) and not self._sync_public(st.channel_id)):
                    st.pending.clear()  # 刚被管理员关掉的区域（或者连通的频道变成私密的了）：攒着的消息也不处理了
                    st.first_human_at = None
                go = bool(st.pending) and (any(m.trigger for m in st.pending) or self._proactive_ready(st, self.clock()))
                bubble = not go and self._bubble_ready(st, self.clock())
                if not go and not bubble and self._bubble_scout_ready(st, self.clock()):
                    try:
                        await self._bubble_scout(st, self._conversation(st), self.transport.channel_info(st.channel_id), list(st.pending), self.clock())
                    except Exception:
                        log.exception("频道 %s 看大家的兴趣时出错", st.channel_id)
                        st.bubble_scout_at = self.clock()
                if go or bubble:
                    batch = list(st.pending)
                    st.inflight = batch
                    try:
                        await self._round(st, batch, bubble=bubble)
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
                        st.withdrawn.clear()
                        # 这一眼没问成模型（出错、超了预算提前回来）：也过几分钟再看，别每次 tick 都来
                        if not st.following and st.next_check_at <= self.clock() and not any(m.trigger for m in batch):
                            st.next_check_at = self.clock() + self._check_interval(self.view(st))
                        st.bubbling = False
                        if bubble and st.bubble_due_at <= self.clock():
                            # 这次冒泡没问成模型（出错、超了预算提前回来）：也等下一个间隔再来，别每次 tick 都试
                            st.bubble_due_at = self.clock() + self._bubble_interval(st)
                        st.gallery = []  # 找到的候选图只在这一轮里能挑
                        if cs.scrub_due:
                            self._scrub_now(cs)
            break
        if st.mention_due and st.pending:
            self._wake(st)
        if self._synced(st):
            for other in self._members(st.conv):
                if other is not st and other.mention_due and other.pending:
                    self._wake(other)  # 这一轮跑的时候连通的别的频道有人叫它：轮到那边了（等的是同一把锁）

    # ------------------------------------------------------------------ 提醒

    def _remind_note(self, channel_id: int, note: str) -> None:
        """提醒是程序办的（确认、取消、到点发出去）：下一轮在这个频道里告诉它一声，免得它不知道自己说过什么。"""
        st = self.states.get(channel_id)
        if st is not None:
            st.remind_notes.append(note)
            del st.remind_notes[:-MAX_REMIND_NOTES]

    async def _remind_card(self, channel_id: int, text: str, confirm: int | None = None) -> int | None:
        """程序直接发的那几句（确认、结果）。confirm：带“确认 / 取消”按钮（按钮上记着提醒的编号）。"""
        send = getattr(self.transport, "send_card", None)
        try:
            if send is not None:
                return await send(channel_id, text, confirm=confirm)
            if confirm is not None:
                return None  # 发不了按钮：没法确认，不设
            return await self.transport.send(channel_id, text, reply_to=None, ping=False)
        except Exception:
            log.exception("频道 %s：发提醒的消息出错", channel_id)
            return None

    def _remind_room(self, owner_id: int, guild_id: int, in_channel: bool) -> str | None:
        """还能不能再设一条：能是 None，满了返回要告诉他的话。"""
        mine = len(self.store.reminders_of(owner_id))
        if mine >= self.ec.remind_max_per_person:
            return REMIND_FULL_PERSON.format(n=mine, limit=self.ec.remind_max_per_person)
        if in_channel and len(self.store.guild_reminders(guild_id)) >= self.ec.remind_max_per_server:
            return REMIND_FULL_SERVER.format(limit=self.ec.remind_max_per_server)
        return None

    async def _remind_actions(
        self,
        st: ChannelState,
        view: View,
        reminds: list[Action],
        numbered: list[tuple[int, IncomingMessage]],
        targets: list[int],
        kind: str,
    ) -> None:
        """模型写的 remind：只在被叫到时办，算在叫它的那个人头上（to 写的不是这一轮叫它的消息就算最后叫它的那个人）。"""
        if kind != "mention" or not targets:
            return
        by_sid = dict(numbered)
        for action in reminds:
            sid = action.to if action.to in targets else targets[-1]
            asker = by_sid.get(sid)
            if asker is None or asker.is_bot or asker.channel_id != st.channel_id or asker.message_id in st.deleted:
                continue  # 连通的别的频道的人叫的：到那边去设
            name = one_line(asker.author_name or "某人", 32)
            if not view.reminders:
                text: str | None = REMIND_FAILED.format(name=name, reason=REMIND_OFF)
            elif action.op == "add":
                text = await self._remind_add(st, action, asker, name)
            elif action.op == "cancel":
                text = self._remind_cancel(st, action, asker, name)
            else:
                text = self._remind_list(st, asker, name)
            if text:
                await self._remind_card(st.channel_id, text)

    async def _remind_people(
        self, st: ChannelState, names: list[str], asker: IncomingMessage
    ) -> tuple[list[tuple[int, str]], str | None]:
        """模型写的名字 → 人。对不上、分不清是谁的返回原因（一个都不设）。"""
        me = name_forms(asker.author_name or "") | {"我", "自己", "本人", "me"}
        people, _ = await self._directory(st, " ".join("@" + n for n in names), "mention", {"pings_left": 0})
        out: dict[int, str] = {}
        for raw in names:
            if raw in me:
                out[asker.author_id] = one_line(asker.author_name or "某人", 32)
                continue
            ids: set[int] = set()
            for form in name_forms(raw):
                ids |= people.get(form, frozenset())
            if not ids:
                return [], REMIND_NO_ONE.format(who=one_line(raw, 32))
            if len(ids) > 1:
                return [], REMIND_AMBIGUOUS.format(who=one_line(raw, 32))
            uid = next(iter(ids))
            out[uid] = one_line(st.people.get(uid) or raw, 32)
        return list(out.items()), None

    async def _remind_add(self, st: ChannelState, action: Action, asker: IncomingMessage, name: str) -> str | None:
        """照模型写的设一条：先记成等确认的，发一条带按钮的消息让本人核对时间。返回要直接告诉他的话（没设成的原因）。"""
        now = self.clock()

        def failed(reason: str) -> str:
            self._remind_note(st.channel_id, REMIND_NOTE_FAILED.format(name=name, reason=reason))
            return REMIND_FAILED.format(name=name, reason=reason)

        text = rm.clean(action.text, rm.MAX_TEXT_CHARS)
        if not text:
            return failed("没写要提醒什么")
        due = rm.parse_when(action.at, action.minutes, now, self.tz)
        reason = rm.check_when(due, now)
        if reason is not None or due is None:
            return failed(reason or "时间没看懂")
        owner = (asker.author_id, name)
        if st.private:
            people, dm = [owner], True  # 私信里设的：只提醒他自己
        else:
            people, problem = await self._remind_people(st, action.who, asker) if action.who else ([owner], None)
            if problem is not None:
                return failed(problem)
            dm = action.dm
            if dm and len(people) != 1:
                return failed("私信提醒一次只能给一个人")
            if len(people) > self.ec.remind_max_targets:
                return failed(f"一条提醒最多 @ {self.ec.remind_max_targets} 个人")
        full = self._remind_room(asker.author_id, st.guild_id, in_channel=not dm)
        if full is not None:
            return failed(full)
        r = Reminder(
            id=0,
            owner_id=asker.author_id,
            owner_name=name,
            guild_id=st.guild_id,
            channel_id=st.channel_id,
            dm=dm,
            private=st.private,
            targets=people,
            text=text,
            say=rm.clean(action.say, rm.MAX_SAY_CHARS) or None,
            due_at=due,
            created_at=now,
            status="pending",
        )
        r.id = self.store.add_reminder(r)
        card = REMIND_CONFIRM.format(
            what=rm.describe(r, now, self.tz, you=r.owner_id), name=name, minutes=int(rm.PENDING_SECONDS // 60)
        )
        if await self._remind_card(st.channel_id, card, confirm=r.id) is None:
            self.store.move_reminder(r.id, "pending", "cancelled")
            self._remind_note(st.channel_id, REMIND_NOTE_FAILED.format(name=name, reason="确认的消息发不出去"))
        return None

    def _remind_visible(self, st: ChannelState, owner_id: int) -> list[Reminder]:
        """他在这里能看、能取消的提醒：私信里是全部；频道里只有这个服务器里设的、到点在频道里说的（别的不在这里露出来）。"""
        mine = self.store.reminders_of(owner_id)
        return mine if st.private else [r for r in mine if r.guild_id == st.guild_id and not r.dm and not r.private]

    def _remind_cancel(self, st: ChannelState, action: Action, asker: IncomingMessage, name: str) -> str:
        now = self.clock()
        mine = self._remind_visible(st, asker.author_id)
        key = (action.text or "").strip().lower()
        hits = [r for r in mine if key in r.text.lower()] if key else mine
        if len(hits) == 1 and self.store.move_reminder(hits[0].id, "set", "cancelled"):
            what = rm.describe(hits[0], now, self.tz, you=asker.author_id)
            self._remind_note(st.channel_id, REMIND_NOTE_CANCELLED.format(name=name, what=rm.describe(hits[0], now, self.tz)))
            return REMIND_CANCELLED.format(name=name, what=what)
        if not mine:
            return REMIND_NONE.format(name=name)
        lines = [f"({i}) {rm.describe(r, now, self.tz, you=asker.author_id)}" for i, r in enumerate(mine, 1)]
        return REMIND_WHICH.format(name=name, entries="\n".join(lines))

    def _remind_list(self, st: ChannelState, asker: IncomingMessage, name: str) -> str:
        now = self.clock()
        mine = self._remind_visible(st, asker.author_id)
        if not mine:
            return REMIND_NONE.format(name=name)
        lines = [f"({i}) {rm.describe(r, now, self.tz, you=asker.author_id)}" for i, r in enumerate(mine, 1)]
        return REMIND_LIST.format(name=name, entries="\n".join(lines))

    def remind_decide(self, rid: int, user_id: int, ok: bool) -> tuple[str, bool]:
        """有人点了确认消息上的按钮。返回 (要说的话, 是不是把那条确认消息换成这句话：不是就只给点的人看)。"""
        now = self.clock()
        r = self.store.reminder(rid)
        if r is None or r.status != "pending":
            return REMIND_STALE, True
        if user_id != r.owner_id:
            return REMIND_NOT_YOURS.format(name=r.owner_name), False
        if not ok:
            if not self.store.move_reminder(rid, "pending", "cancelled"):
                return REMIND_STALE, True
            self._remind_note(r.channel_id, REMIND_NOTE_DECLINED.format(name=r.owner_name, text=r.text))
            return REMIND_DECLINED.format(text=r.text), True
        if now - r.created_at > rm.PENDING_SECONDS:
            problem: str | None = "确认得太晚了，再跟我说一次"
        elif rm.check_when(r.due_at, now) is not None:
            problem = "这个时间已经过了"
        else:
            problem = self._remind_room(r.owner_id, r.guild_id, in_channel=not r.dm)
        if problem is not None:
            self.store.move_reminder(rid, "pending", "cancelled")
            self._remind_note(r.channel_id, REMIND_NOTE_FAILED.format(name=r.owner_name, reason=problem))
            return REMIND_FAILED.format(name=r.owner_name, reason=problem), True
        if not self.store.move_reminder(rid, "pending", "set"):
            return REMIND_STALE, True
        self._remind_note(r.channel_id, REMIND_NOTE_SET.format(name=r.owner_name, what=rm.describe(r, now, self.tz)))
        log.info("频道 %s：设了一条提醒（到点%s）", r.channel_id, "发私信" if r.dm else "在频道里说")
        return REMIND_SET.format(what=rm.describe(r, now, self.tz, you=r.owner_id)), True

    def remind_cancel(self, rid: int, *, owner_id: int | None = None, guild_id: int | None = None) -> Reminder | None:
        """命令里取消一条（/ds-dm 提醒：只能取消自己的；/dsadmin 提醒：只能取消这个服务器频道里的）。取消了返回那一条。"""
        r = self.store.reminder(rid)
        if r is None or r.status not in ("set", "missed"):
            return None
        if owner_id is not None and r.owner_id != owner_id:
            return None
        if guild_id is not None and (r.guild_id != guild_id or r.dm or r.private):
            return None
        if not self.store.move_reminder(rid, r.status, "cancelled"):
            return None
        if r.status == "set":
            what = rm.describe(r, self.clock(), self.tz)
            self._remind_note(r.channel_id, REMIND_NOTE_CANCELLED.format(name=r.owner_name, what=what))
        return r

    def _remind_due(self, now: float) -> None:
        """到点了的提醒：发出去。bot 刚才不在线、晚了很久的不发（列表里标“错过了”）。"""
        if self.closing:
            return
        for r in self.store.reminders_due(now):
            if now - r.due_at > self.ec.remind_late_hours * 3600.0:
                if self.store.move_reminder(r.id, "set", "missed"):
                    log.info("频道 %s：有一条提醒晚了 %s，不发了", r.channel_id, rm.span_text(now - r.due_at))
                    self._remind_note(r.channel_id, REMIND_NOTE_MISSED.format(name=r.owner_name, text=r.text))
                continue
            if not self.store.move_reminder(r.id, "set", "done"):
                continue  # 先记成办完了再发：发到一半重启了也不会发两遍
            task = asyncio.get_running_loop().create_task(self._remind_fire(r, now))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _remind_fire(self, r: Reminder, now: float) -> None:
        sent: int | None = None
        said_here: str | None = None  # 在说的那个频道（私信）里发出去的那句
        try:
            if r.private:
                said_here = rm.fire_text(r, now, by_dm=True)
                sent = await self.transport.send(r.channel_id, said_here, reply_to=None, ping=False)
            else:
                ids = [uid for uid, _ in r.targets]
                send_dm = getattr(self.transport, "send_dm", None) if r.dm else None
                if send_dm is not None:
                    sent = await send_dm(ids[0], rm.fire_text(r, now, by_dm=True, guild_name=self._guild_name(r.guild_id)))
                if sent is None:
                    # 私信发不出去（他关了私信、屏蔽了它）：回到说的那个频道里 @ 他
                    said_here = rm.fire_text(r, now, by_dm=False, fallback=r.dm)
                    sent = await self.transport.send(r.channel_id, said_here, reply_to=None, ping=False, mentions=ids)
        except Exception:
            log.exception("频道 %s：发提醒出错", r.channel_id)
            sent = None
        if sent is None:
            self.store.move_reminder(r.id, "done", "failed")
            log.warning("频道 %s：有一条提醒发不出去（缺权限？频道没了？）", r.channel_id)
            return
        log.info("频道 %s：提醒发出去了（%s）", r.channel_id, "私信" if r.private or said_here is None else "频道")
        if said_here is not None:
            self._remind_note(r.channel_id, REMIND_NOTE_FIRED.format(text=one_line(said_here, 400)))
        else:
            self._remind_note(r.channel_id, REMIND_NOTE_FIRED_DM.format(who=rm.whom_text(r), text=r.text))

    # ------------------------------------------------------------------ 更新公告

    def announce_plan(self, scope: str, now: float | None = None) -> an.Plan:
        """照现在的情况，这条公告会发到哪（/dsowner 公告 的预览、真的发的时候都用它）。"""
        now = self.clock() if now is None else now
        plan = an.Plan(scope)
        here = self._guild_ids()
        if scope in ("server", "all"):
            chosen = self.store.announce_channels()
            for gid in here:
                cid = chosen.get(gid)
                if cid is None:
                    plan.unset.append(gid)  # 没设公告频道：不收
                    continue
                place = self.transport.locate(cid)
                if place is None or place.guild_id != gid or place.dm:
                    plan.gone.append(gid)  # 设过，但那个频道没了
                else:
                    plan.channels.append((gid, cid))
        if scope in ("dm", "all"):
            for uid, gid, cid, at in self.store.dm_people():
                if self.store.announce_off(uid):
                    plan.refused += 1
                elif at < now - an.DM_DAYS * 86400.0:
                    plan.stale += 1
                elif gid not in here or not self.settings.get("dm", guild_id=gid):
                    plan.closed += 1  # 私信挂的服务器没了、关掉了私信：他现在私信它，它也不回
                else:
                    plan.people.append((uid, cid))
        return plan

    async def announce(self, scope: str, title: str, text: str) -> an.Result | None:
        """发一条更新公告（原样发，不问模型）。一条一条发，中间隔一会儿。已经有一条在发：不发，返回 None。"""
        if self.announcing:
            return None
        self.announcing = True
        try:
            now = self.clock()
            plan = self.announce_plan(scope, now)
            result = an.Result()
            public = an.compose(title, text)
            note = an.NOTE.format(text=one_line(public, an.NOTE_CHARS))
            first = True
            for gid, cid in plan.channels:
                if not first:
                    await self.sleep(an.GAP_SECONDS)
                first = False
                try:
                    sent = await self.transport.send(cid, public, reply_to=None, ping=False)
                except Exception:
                    log.exception("公告：在频道 %s 发不出去", cid)
                    sent = None
                if sent is None:
                    result.failed_channels.append((gid, cid))
                else:
                    result.channels.append((gid, cid))
                    self.store.add_channel_note(cid, note, now)
            private = an.compose(title, text, dm=True)
            send_dm = getattr(self.transport, "send_dm", None)
            for uid, cid in plan.people:
                if not first:
                    await self.sleep(an.GAP_SECONDS)
                first = False
                try:
                    sent = await send_dm(uid, private) if send_dm is not None else None
                except Exception:
                    log.exception("公告：有一条私信发不出去")
                    sent = None
                if sent is None:
                    result.dm_failed += 1
                else:
                    result.dms += 1
                    self.store.add_channel_note(cid, note, now)
            failed = len(result.failed_channels) + result.dm_failed
            self.store.add_announcement(now, scope, an.clean_title(title), text.strip()[: an.MAX_CHARS], len(result.channels), result.dms, failed)
            log.info(
                "更新公告（%s）：发到 %d 个服务器的公告频道、私信 %d 人；没发出去 %d 个",
                an.SCOPES.get(scope, scope), len(result.channels), result.dms, failed,
            )
            return result
        finally:
            self.announcing = False

    # ------------------------------------------------------------------ 一轮

    def _conversation(self, st: ChannelState) -> Conversation:
        conv = self._load(st.conv)
        if st.conv_seen is not conv:
            st.conv_seen = conv
            mine = self._mine(st, conv.turns)
            st.listed = last_listed(mine)
            origin = st.channel_id if self._synced(st) else None
            older = [
                (uid, name)
                for uid, name in reversed(self.store.recent_authors(st.conv.key, PEOPLE_KEEP, origin))
                if uid not in st.people
            ]
            st.people = {**dict(older), **st.people}  # 记录里的在前，刚收到的（更新的名字）在后
            # 记录里最后写的“正在放什么”（重启之后已经不在放了：下次被叫到时要告诉它）
            st.music_seen = last_music_line(mine)
        return conv

    # ------------------------------------------------------------------ 记录（连通的频道共用一份）

    def _conv_state(self, key: int, guild_id: int = 0) -> ConvState:
        cs = self.convs.get(key)
        if cs is None:
            cs = ConvState(key, guild_id, next(self._generations))
            # 没问过模型就记进去的那几条（重启以前记下的）：照样等带着它们的请求过了才算数，被拒了照样换掉
            cs.unvetted, cs.unvetted_edits = self.store.load_unvetted(key)
            self.convs[key] = cs
        return cs

    def _load(self, cs: ConvState) -> Conversation:
        """这份记录（没载入过、或者闲久了放掉了的，从数据库读）。"""
        if cs.conversation is None:
            cs.conversation = self.store.load_conversation(cs.key, cs.guild_id)
            for sid, mid in self.store.load_msgmap(cs.key).items():
                cs.msg_by_sid.setdefault(sid, mid)
                cs.sid_by_msg.setdefault(mid, sid)
            if cs.key < 0:
                for sid, origin in self.store.load_msg_origins(cs.key).items():
                    cs.sid_origin.setdefault(sid, origin)
            if cs.key < 0 or cs.conversation.sparse:
                # 连通组的记录几个频道分开整理；断开时拿回来的有的在组里整理过：已经整理过的那几条（重启以后也不再整理）
                cs.review_done |= self.store.reviewed_turns(cs.key, cs.conversation.reviewed_upto)
        return cs.conversation

    @staticmethod
    def _cs(owner: Any) -> ConvState:
        """频道的状态 → 它现在用的那份记录；本来就是 ConvState 的原样返回。"""
        return owner.conv if isinstance(owner, ChannelState) else owner

    @staticmethod
    def _synced(st: ChannelState) -> bool:
        """这个频道现在用的是连通组共用的记录。"""
        return st.conv.key < 0

    def _origin(self, st: ChannelState) -> int | None:
        """这一轮写进记录时记成哪个频道的：连通组的记录记上频道，没连通的不记（和以前一样）。"""
        return st.channel_id if self._synced(st) else None

    def _here(self, st: ChannelState, sid: int) -> bool:
        """#sid 是不是这个频道里的消息（连通组的记录里有别的频道的：那些不能挂成回复、不能加表情）。"""
        return not self._synced(st) or st.conv.sid_origin.get(sid, st.channel_id) == st.channel_id

    def _mine(self, st: ChannelState, turns: Sequence[Turn]) -> list[Turn]:
        """记录里和这个频道在同一个服务器的频道写的那几条（用来认最后写的“语音频道在放什么”、能翻的频道名单：这两样是按服务器的，
        连通组里同一个服务器的几个频道看到的是同一个，谁写的最新那句都算；断开了的频道写的也算）。没连通就是全部。"""
        if not self._synced(st):
            return list(turns)
        origins = {t.origin for t in turns if t.origin is not None}
        here = {o for o in origins if o == st.channel_id or st.guild_id in self._place_former(o)}
        return [t for t in turns if t.origin in here]

    def _members(self, cs: ConvState) -> list[ChannelState]:
        """现在用着这份记录的频道（连通组可能有好几个；连通的频道自己原来那份没人用）。"""
        if cs.key >= 0:
            st = self.states.get(cs.key)
            return [st] if st is not None and st.conv is cs else []
        return [st for st in self.states.values() if st.conv is cs]

    def _channel_state(self, channel_id: int) -> ChannelState | None:
        """这个频道的状态；还没有的照它现在在哪建一个（找不到这个频道了是 None）。"""
        st = self.states.get(channel_id)
        if st is not None:
            return st
        place = self.transport.locate(channel_id)
        if place is None:
            return None
        return self.state(channel_id, place.guild_id, place.parent_id, place.category_id, place.dm)

    def _frozen(self, cs: ConvState) -> bool:
        """连通的频道自己原来那份记录（连通期间不往里写，只同步删改）。"""
        return cs.key > 0 and self.store.sync_of(cs.key) is not None

    def _number(self, st: ChannelState, conv: Conversation, shown: list[IncomingMessage]) -> list[tuple[int, IncomingMessage]]:
        numbered = []
        synced = self._synced(st)
        for m in shown:
            sid = conv.next_short_id
            conv.next_short_id += 1
            st.sid_by_msg[m.message_id] = sid
            st.msg_by_sid[sid] = m.message_id
            if synced:
                st.conv.sid_origin[sid] = st.channel_id
            numbered.append((sid, m))
        if len(st.msg_by_sid) > SID_MAP_LIMIT:
            for sid in sorted(st.msg_by_sid)[: len(st.msg_by_sid) - SID_MAP_LIMIT + 500]:
                mid = st.msg_by_sid.pop(sid)
                st.conv.sid_origin.pop(sid, None)
                if st.sid_by_msg.get(mid) == sid:
                    del st.sid_by_msg[mid]
        return numbered

    def _absorb_others(self, st: ChannelState, conv: Conversation, now: float) -> None:
        """连通的频道被叫到时（调用方拿着这份记录的锁）：别的连通频道里还没进记录的消息（那边叫它的那条之前的）先补进
        共用的记录，这一轮就看得到那边刚才在聊什么。不问模型、不花钱，也不在那边回话；那边叫它的消息留给那边自己的一轮。"""
        for cid in self.store.sync_members(st.conv.key):
            other = self.states.get(cid)
            if other is None or other is st or other.conv is not st.conv:
                continue
            # 那个频道里很早的消息被改了、还没附上的那几句：它自己那一轮附（记成它的），那边一直没有自己的一轮的话就跟着补进来
            notes = {mid: v for mid, v in st.edit_notes.items() if v[1].channel_id == cid}
            if not other.pending and not notes:
                continue
            self._prune(other, now)
            if not self._live(other, self.view(other)) or not self._sync_public(cid):
                continue
            cut = next((i for i, m in enumerate(other.pending) if m.trigger), len(other.pending))
            head = other.pending[:cut]
            if not head and not notes:
                continue
            take = [m for m in head if m.message_id not in st.deleted]
            if take or notes:
                shown = take[-max(1, self.ec.max_batch_messages) :]
                numbered = self._number(other, conv, shown) if shown else []
                content = render_round(
                    numbered,
                    sid_by_msg=st.sid_by_msg,
                    kind="passive",
                    targets=[],
                    now=now,
                    tz=self.tz,
                    omitted=other.omitted + len(take) - len(shown) if head else 0,
                    max_chars=self.ec.max_chars_per_message,
                    context=[
                        EDIT_NOTE.format(sid=sid, name=neutralize(one_line(m.author_name or "某人")), text=neutralize(one_line(m.text, 300)))
                        for sid, m in notes.values()
                    ],
                    where=self._sync_label(cid),
                )
                assigned = [(sid, m.message_id, None if m.is_bot else m.author_id, m.author_name) for sid, m in numbered]
                self.store.append_round(conv, content, PASSIVE_REPLY, assigned, now=now, origin=cid)
                st.conv.unvetted.append(conv.turns[-2].id)  # 没问过模型：这一轮的请求过了才算数
                self._keep_unvetted(st.conv)
                for mid in notes:
                    if st.edit_notes.get(mid) is notes[mid]:
                        del st.edit_notes[mid]  # 已经附上了
                st.unreviewed_rounds += 1
                log.info("频道 %s：先把连通的频道 %s 刚才的 %d 条消息补进了记录", st.channel_id, cid, len(shown))
            if head:
                self._take_pending(other, head)  # 那边的冷却、发不出消息的暂停照旧（不算那边跑了一轮）

    @staticmethod
    def _take_pending(st: ChannelState, head: list[IncomingMessage]) -> None:
        """从缓冲区拿走这几条（已经记进记录了）。和 _consume 不同：不算这个频道跑了一轮，冷却、暂停都不动。"""
        gone = {id(m) for m in head}
        st.pending = [m for m in st.pending if id(m) not in gone]
        st.omitted = 0
        st.first_human_at = next((m.received_at for m in st.pending if not m.is_bot), None)

    def _sync_label(self, channel_id: int) -> str:
        """连通的频道给模型看的名字：「服务器」#频道（频道找不到了、变成私密的了，用连的时候记下的名字：私密频道现在叫什么，
        别的频道的人不该知道）。"""
        place = self.transport.locate(channel_id)
        saved = self.store.sync_place(channel_id)
        if place is not None and not place.dm and (saved is None or self._sync_public(channel_id)):
            # 和能翻的频道名单一样写成“#大厅（「某某」服务器）”：模型照着写，发出去时认得出是别的服务器的（不链到这里同名的频道）
            return f"{self._display(place)}（「{self._server_label(place.guild_id)}」服务器）"
        if saved is None:
            return f"#{channel_id}"
        return f"#{neutralize(one_line(saved[1], 40))}（「{self._server_label(saved[0])}」服务器）"

    def _sync_foreign(self, st: ChannelState) -> bool:
        """这个频道和别的服务器（连成一组的不算）的频道连通了：几个服务器共用的记录里，不能放只有这里能看的东西（别的频道的
        近况、翻到的记录、私信里聊的、这里的人设卡……）。"""
        if not self._synced(st):
            return False
        return self.sync_foreign_at(st.channel_id, st.guild_id)

    def sync_foreign_at(self, channel_id: int, guild_id: int) -> bool:
        """这个频道（在 guild_id 这个服务器）和别的服务器（连成一组的不算）的频道连通了；没连通是 False。"""
        return bool(self._sync_guilds(channel_id) - set(self._group_guilds(guild_id)))

    def _sync_guilds(self, channel_id: int) -> set[int]:
        """和这个频道连通的几个频道在哪些服务器（自己的也在里面；没连通是空的）。"""
        sync = self.store.sync_of(channel_id)
        if sync is None:
            return set()
        return self._guilds_of(self.store.sync_members(sync))

    def _guilds_of(self, channels: Iterable[int]) -> set[int]:
        """这几个频道在哪些服务器（找不到了的用连的时候记下的）。"""
        out: set[int] = set()
        for cid in channels:
            place = self.transport.locate(cid)
            saved = self.store.sync_place(cid) if place is None else None
            if place is not None:
                out.add(place.guild_id)
            elif saved is not None:
                out.add(saved[0])
        return out

    def channels_cross(self, channels: Iterable[int]) -> bool:
        """这几个频道不全在同一个服务器（连成一组的几个服务器算一个）。"""
        guilds = self._guilds_of(channels)
        return len(guilds) > 1 and bool(guilds - set(self._group_guilds(min(guilds))))

    def brings_cross_off(self, before: Iterable[int], joined: Iterable[int]) -> bool:
        """连进来的这几个频道带来了组里原来没有、又没开“跨服务器”的服务器（那边平时看不到服务器组外的近况）。"""
        new = self._guilds_of(joined) - self._guilds_of(before)
        return any(not self.settings.view(g).cross_server for g in new)

    def _past_digest(self, st: ChannelState, bot_name: str) -> str | None:
        """连通的频道连通之前自己的记录（只给它自己的频道看）：更早的摘要，加上最近几行（不带编号：那是原来那份的编号）。"""
        own = self._conv_state(st.channel_id, st.guild_id)
        conv = self._load(own)
        lines = history_lines([(t.role, t.content) for t in conv.turns], bot_name)
        recent: list[str] = []
        size = 0
        for line in reversed(lines):
            if size + len(line) + 1 > SYNC_PAST_CHARS:
                break
            recent.append(line)
            size += len(line) + 1
        recent.reverse()
        st.past_vetted = False
        if own.unvetted:
            # 连通时记进去、还没问过模型的那几轮在最后（连通期间它自己那份不往后写）：全在这份概要里，这一轮的请求过了才算数
            first = min(own.unvetted)
            tail = history_lines([(t.role, t.content) for t in conv.turns if t.id >= first], bot_name)
            st.past_vetted = sum(len(line) + 1 for line in tail) <= SYNC_PAST_CHARS
        parts = []
        if conv.summary:
            parts.append("更早的摘要：" + conv.summary.strip())
        if recent:
            parts.append("最后聊的：\n" + "\n".join(recent))
        return "\n".join(parts) or None

    def _select(self, batch: list[IncomingMessage]) -> list[IncomingMessage]:
        """最近的 N 条，再加上更早但在叫它的消息（点名不能因为后面消息太多被挤掉）。"""
        limit = self.ec.max_batch_messages
        recent = batch[-limit:]
        older = batch[: max(0, len(batch) - limit)]
        return [m for m in older if m.trigger] + recent

    async def _round(self, st: ChannelState, batch: list[IncomingMessage], bubble: bool = False) -> None:
        """bubble：这一轮是冒泡（没人说话，它自己来说点什么）：照主动的那一套走，多一句说明，可以搜一次。"""
        now = self.clock()
        view = self.view(st)
        generation = st.generation
        triggers = [m for m in batch if m.trigger]
        kind = "mention" if triggers else "proactive"
        bubble = bubble and not triggers
        st.bubbling = bubble
        if bubble and st.following:
            self._stop_following(st, view, now)  # 安静了这么久：不是接着刚才那场聊天

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
        if kind == "mention" and self._synced(st):
            self._absorb_others(st, conv, now)  # 连通的别的频道刚才在聊的，先补进记录
        if any(m.message_id in st.deleted or m.message_id in st.withdrawn for m in batch):
            # 准备这一轮的时候（比如等链接预览）被删掉的（连通的频道暂停时被改了的也算）：不给模型看，也不写进记录
            batch = [m for m in batch if m.message_id not in st.deleted and m.message_id not in st.withdrawn]
            triggers = [m for m in triggers if m.message_id not in st.deleted and m.message_id not in st.withdrawn]
            if not batch or (kind == "mention" and not triggers):
                return  # 叫它的消息都删了：这一轮不跑，剩下的消息留着以后看
        shown = self._select(batch)
        omitted = st.omitted + len(batch) - len(shown)
        numbered = self._number(st, conv, shown)
        shown_ids = {id(m) for m in shown}
        targets = [st.sid_by_msg[m.message_id] for m in triggers if id(m) in shown_ids]
        budget = _Budget(
            self.ec.max_files_per_round,
            self.ec.file_round_max_chars,
            MAX_DOC_IMAGES,
            bool(view.vision),
            links=max(0, self.ec.max_links_per_round),
        )
        # 看图（下载、缩小）和读文件、打开链接互不相干：同时办，不用等图都下完了才去读
        images: dict[int, list[tuple[str, str]]] = {}
        if view.vision:
            images, (docs, planned, opened) = await _together(
                self._images(shown, triggers), self._round_content(st, view, kind, shown, triggers, budget, now)
            )
        else:
            docs, planned, opened = await self._round_content(st, view, kind, shown, triggers, budget, now)
        if st.generation != generation:
            # 读文件的时候记忆被清空了：这一轮作废，点名留着，马上用新的记忆重来
            st.mention_due = st.mention_due or any(m.trigger for m in batch)
            return
        info = self.transport.channel_info(st.channel_id)
        bubble_hint = await self._bubble_plan(st, view, conv, info, batch, now) if bubble else None
        if st.generation != generation:
            return  # 替它找新鲜事的时候记忆被清空了：这次不冒了
        system = self._system_prompt(st, view, conv, info)
        context, digest_marks, listed = self._shared_context(st, view, triggers, now) if kind == "mention" else ([], {}, None)
        # 连通组的记录：只附这个频道里的消息被改成了什么（别的频道的留给那边，免得记成这个频道的）
        noted = {mid: v for mid, v in st.edit_notes.items() if not self._synced(st) or v[1].channel_id == st.channel_id}
        context = [
            EDIT_NOTE.format(sid=sid, name=neutralize(one_line(m.author_name or "某人")), text=neutralize(one_line(m.text, 300)))
            for sid, m in noted.values()
        ] + context
        remind_seen = list(st.remind_notes)  # 提醒的事（设好了、取消了、到点说过了）：程序办的，告诉它一声
        told = self.store.channel_notes(st.channel_id)  # 程序替它在这里发过的话（更新公告）：也告诉它一声
        context = remind_seen + [note for _, note in told] + context
        playing = st.music_seen  # 写进记录的那一行（没在放是 None）
        if kind == "mention":
            status = self.music.status(st.guild_id) if self.music is not None and not st.private else None
            if self._synced(st) and len(self._sync_guilds(st.channel_id)) > 1:
                # 几个服务器共用的记录（连成一组的服务器也是各放各的）：写明是哪个服务器的语音频道（别的服务器的频道看到了
                # 也不会当成自己那边的）
                server = self._server_label(st.guild_id)
                playing = MUSIC_NOW_AT.format(server=server, text=neutralize(one_line(status, 300))) if status is not None else None
                idle = MUSIC_IDLE_AT.format(server=server)
            else:
                playing = MUSIC_NOW.format(text=neutralize(one_line(status, 300))) if status is not None else None
                idle = MUSIC_IDLE
            if playing != st.music_seen:  # 换了歌、停了：告诉它一声（只写在这一轮里，system prompt 不变）
                context.append(playing or idle)
        card_lines, card_marks = self._card_context(st, view, conv, numbered, triggers)
        dm_lines, dm_marks = self._dm_memory_context(st, view, conv, triggers) if kind == "mention" else ([], [])
        context = context + dm_lines
        where = self._sync_label(st.channel_id) if self._synced(st) else None  # 连通组的记录：每段写明是哪个频道的


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
                fresh_since=now - FRESH_SECONDS if kind == "proactive" and not st.following else None,
                following=kind == "proactive" and st.following,
                hint=bubble_hint,
                where=where,
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
                if not self._too_big(exc):
                    self._withdraw_unvetted(st)  # 没问过模型就记进记录的那几条也换掉，免得以后每一轮都被拒
            await self._on_error(st, kind, exc, now, batch, targets, conv, info)
            return
        self._vetted(st)

        # 主动聊天时也能上网找图（发图是群友的日常）；其他要查资料的动作只在被叫到时用。
        # 主动聊天时找不了（冷却中、次数用完）就当它没说要找图，不为它多问一次模型
        extra = ("image",) if kind == "proactive" and self._image_ready(st, view, now) else ()
        if kind == "proactive" and self._auto_search_ready(st, view, now):
            extra += ("search",)  # 主动搜索：没人叫它、它想接话时，不懂的可以先搜一下
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
        stored = self._clean_reply(st, stored)
        self.store.append_round(conv, user_content, stored, assigned, now=now, origin=self._origin(st))
        self._consume(st, batch, now)
        self._mark_read(conv, planned)
        self._mark_links(conv, opened)
        self._mark_cards(st, conv, card_marks)
        self._mark_dm_memory(st, view, conv, dm_marks)
        for mid in noted:
            if st.edit_notes.get(mid) is noted[mid]:
                del st.edit_notes[mid]  # 已经告诉它了
        del st.remind_notes[: len(remind_seen)]  # 已经告诉它了
        if told:
            self.store.drop_channel_notes(i for i, _ in told)
        st.music_seen = playing
        st.unreviewed_rounds += 1
        st.digest_seen.update(digest_marks)
        if listed is not None:
            st.listed = listed
        if self._synced(st):
            for other in self._members(st.conv):
                if other is not st and other.guild_id == st.guild_id:
                    # 同一个服务器的别的连通频道：在放什么、能翻的频道名单是同一个，它们下次照这一句比（见 _mine）
                    other.music_seen = playing
                    if listed is not None:
                        other.listed = listed

        tools = [a for a in actions if a.kind in TOOL_KINDS]
        if tools:
            # 模型要先看文件 / 打开链接 / 上网搜 / 找图 / 翻别的频道 / 改人设：办完再问它一次（第二步不能再用这些，
            # 只有第一步没改人设时，第二步还能改一次人设，比如看完角色卡再照着改）
            self.store.set_outcome(row, "+".join(sorted({a.kind for a in tools})))
            if kind != "mention":
                st.last_decision = "想接话，先去搜一下" if any(a.kind == "search" for a in tools) else "想发图，先去找"
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
        # 这一轮只有群友说的话（没看文件、链接、搜索结果……）：私信记忆的敏感标记才能照他说的往下调
        trusted = not tools and not docs
        actions = await self._apply_memory_actions(st, view, actions, numbered, targets, kind, seen, conv.turns[:before], trusted)
        reminds = [a for a in actions if a.kind == "remind"]  # 提醒：回完话再办（发一条带确认按钮的消息）
        actions = [a for a in actions if a.kind != "remind"]
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
            self._after_round(st, view, kind, decided_reply, decided_react, bubble)
            if bubble:
                self._bubble_done(st, view, decided_reply, actions)
            self.store.set_outcome(row, "recited" if row in self._recited_rows else outcome)
            self._recited_rows.discard(row)
            # 对话里附着人设卡时不给管理员看模型的“想法”（里面可能引用了别人的卡）
            private_bits = bool(st.cards_shown or card_lines or seen or dm_lines or st.dm_memory_shown or self._dm_memory_in_prompt(st, view))
            st.last_decision = self._describe(kind, outcome, actions, completion, hide_reason=private_bits)
        if reminds and st.generation == generation:
            try:
                await self._remind_actions(st, view, reminds, numbered, targets, kind)
            except Exception:
                log.exception("频道 %s：设提醒时出错", st.channel_id)
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
        if bubble and st.private and decided_text and not spoke and st.send_failures >= REACH_FAIL_LIMIT:
            self._reach_off(st)  # 主动私信连着发不出去（他关了私信、屏蔽了它）：替他关掉，免得一直白花钱

        u = completion.usage
        log.info(
            "[%s #%s] %s → %s | 输入 %d（命中 %d）输出 %d | $%.5f | %s",
            info.guild_name,
            info.channel_name,
            "点名" if kind == "mention" else ("冒泡" if bubble else "主动"),
            outcome,
            u.prompt_tokens,
            u.hit,
            u.output,
            cost,
            "（附着人设卡或私信记忆，想法略）"
            if st.cards_shown or card_lines or seen or dm_lines or st.dm_memory_shown or self._dm_memory_in_prompt(st, view)
            else one_line(completion.reasoning or "", 160),
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
            shown = "（附着人设卡或私信记忆，内容略）" if st.cards_shown or st.dm_memory_shown or st.private or cards_here else one_line(completion.content, 200)
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
                + [a for a in tools if a.kind == "search"][: max(1, self.ec.max_searches_per_round) if kind == "mention" else 1]
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
            # 点歌的动作照模型写的顺序一个一个办（“放 X，然后删掉”就是先放再删；看队列放在最后），同一轮共用一份记录
            # （music.RoundState）：编号按这一轮开始时的队列算，切歌只切那时正在放的那首，清空只清那时排着的。
            # 同一个人接连写的几个删歌一起删
            ordered = [a for a in tools if not _queue_view(a)] + [a for a in tools if _queue_view(a)]
            # 上网搜、找图只是去拿资料，和读文件、打开链接互不相干：不等前面的办完，一开始就同时去办（以前是读完文件、打开完
            # 链接才一个一个搜），结果还是放在模型写的那个位置。规矩照旧：
            # - 每天的次数照模型写的顺序占（各自一开始就先记一笔占住名额，再去搜、去找）；
            # - 排在点歌后面的不提前：按歌名找歌和搜索共用每天的搜索次数，谁先占要照原来的顺序（现在 _interpret 总把搜索、
            #   找图排在点歌前面，这一条是保险）；
            # - 打开链接用不用要花钱的读法，在它们开始之前就定好（和原来先读完再搜时一样）。搜不搜、找不找图，则是在这一轮
            #   打开链接的钱记上之前定的：预算正好卡在这中间时，最多多搜这一轮的这几次
            pay = not self._over_budget(st, view, now)[0]
            early: dict[int, asyncio.Future[Any]] = {}
            for index, action in enumerate(ordered):
                if action.kind == "music":
                    break
                if action.kind == "search":
                    early[index] = asyncio.ensure_future(
                        self._search_section(st, view, action, now, auto=kind != "mention" and not st.bubbling)
                    )
                elif action.kind == "image":
                    early[index] = asyncio.ensure_future(self._image_section(st, view, action, now))
            try:
                results = await self._read_planned(st, plan, budget)
                opened = await self._read_links(st, view, link_plan, budget, now, pay=pay)
                music_round: Any = None
                i = 0
                while i < len(ordered):
                    index, action = i, ordered[i]
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
                        sections.append(
                            await early[index] if index in early else await self._search_section(st, view, action, now)
                        )
                    elif action.kind == "recall":
                        sections.append(self._recall_section(st, view, action, info))
                    elif action.kind == "persona":
                        sections.append(await self._persona_section(st, action, numbered, targets, self._seen_cards(conv)))
                    elif action.kind == "who":
                        sections.append(self._who_section(st, view, action, who_marks))
                    elif action.kind == "image":
                        note, found = await early[index] if index in early else await self._image_section(st, view, action, now)
                        if found:
                            gallery = Gallery(action.query or "", [(p.description, p.uri) for p in found])
                        elif note:
                            sections.append(note)
            finally:
                await _settle(early.values())  # 平时到这里都办完、取走了；中途出错、关机时让还在搜的停下
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
                self.store.append_round(
                    conv, "\n".join(lines) or TOOL_FAILED, dump_actions([]), [], now=now, origin=self._origin(st)
                )
                if played and conv.channel_id < 0:
                    # 连通组的记录：点歌的结果（歌名）没问过模型（这一步没问成、被拒了都一样，被拒的话可能就是它）：带着它的请求
                    # 过了才算数，被拒了就换掉
                    st.conv.unvetted.append(conv.turns[-2].id)
                    self._keep_unvetted(st.conv)
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
        stored = self._clean_reply(st, stored)
        self.store.append_round(conv, content, stored, [], now=now, origin=self._origin(st))
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

    def _auto_search_on(self, st: ChannelState, view: View) -> bool:
        """这里开着主动搜索（没人叫它时也能搜）。私信里每句都算叫它，用不上。"""
        return bool(view.proactive_search) and self._can_search(view) and not st.private

    def _auto_search_ready(self, st: ChannelState, view: View, now: float) -> bool:
        """这一轮（没人叫它）现在就能搜：开着、不是冒泡（冒泡自己会搜）、没超预算、今天还有次数（上限是 0 = 不限）。"""
        if not self._auto_search_on(st, view) or st.bubbling or self._over_budget(st, view, now)[0]:
            return False
        limit = int(view.daily_proactive_search_limit)
        return limit <= 0 or self.store.count_usage(self.ledger.day(now), guild_id=st.guild_id, kind=AUTO_SEARCH) < limit

    async def _search_section(self, st: ChannelState, view: View, action: Action, now: float, *, auto: bool = False) -> str:
        """auto：没人叫它时它自己要搜的（主动搜索）。另外记数，不占被叫到时的每日搜索上限。"""
        query = action.query or ""
        shown = neutralize(one_line(query, 60))
        queries = [query]
        if st.bubbling:
            picked = self._bubble_queries(st, query, now)
            if picked is None:
                return ""  # 不让搜的话题（_bubble_plan 不会挑到）
            queries = picked[1]
        if not self._can_search(view):
            return f"（想搜「{shown}」，但这里不能上网搜。凭你已有的知识回答，拿不准就直说。）"
        limit = int(view.daily_proactive_search_limit) if auto else int(view.daily_search_limit)
        day = self.ledger.day(now)
        usage_kind = AUTO_SEARCH if auto else "search"
        if (limit > 0 or not auto) and self.store.count_usage(day, guild_id=st.guild_id, kind=usage_kind) >= limit:
            return f"（想搜「{shown}」，但今天这个服务器的搜索次数用完了（{limit} 次）。凭你已有的知识回答，拿不准就直说。）"
        if self._over_budget(st, view, now)[0]:
            return f"（想搜「{shown}」，但今天的预算用完了。凭你已有的知识回答，拿不准就直说。）"
        assert self.searcher is not None
        # 先记一笔占住名额，再去搜：别的频道同时在搜时，也不会一起超过上限
        row = self.store.record_usage(ts=now, day=day, guild_id=st.guild_id, channel_id=st.channel_id, kind=usage_kind, outcome="pending")
        try:
            for query in queries:  # 冒泡：先只搜 X 上的，没搜到再搜全网（平时就一个搜索词）
                result = await self.searcher.search(query, max_chars=self.ec.search_max_chars)
                if result.sources or query is queries[-1]:
                    break
                self._finish_search(st, row, result.usage, "search", result.paid)  # 这一次也照实记账，再记一笔搜下一个
                row = self.store.record_usage(ts=now, day=day, guild_id=st.guild_id, channel_id=st.channel_id, kind=usage_kind, outcome="pending")
        except asyncio.CancelledError:
            self._finish_search(st, row, Usage(), "error")  # 关机时被取消：别留下一笔一直“进行中”的记录
            raise
        except Exception as exc:  # SearchError，以及意料之外的错误：都只算这次没搜成（已经算了钱的照样记账）
            log.warning("频道 %s：搜索「%s」失败：%s", st.channel_id, one_line(query, 60), exc)
            paid = getattr(exc, "paid", 0)
            self._finish_search(st, row, getattr(exc, "usage", None) or Usage(), "error", paid if isinstance(paid, int) else 0)
            return f"（搜「{shown}」失败了。凭你已有的知识回答，拿不准就直说。）"
        self._finish_search(st, row, result.usage, "search", result.paid)
        st.bubble_found = bool(result.sources)
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
                ticket: dict[str, int] = {}  # 这次找歌记账的那一行（row）、DeepSeek 算了几次钱（paid）
                return await music.play(
                    guild_id,
                    channel_id,
                    user_id,
                    user_name,
                    query,
                    can_search=lambda: self._music_search(guild_id, channel_id, user_id, view, ticket),
                    paid=lambda: self._music_paid(guild_id, ticket),
                    pay=not self._guild_over(guild_id, view, self.clock())[0],  # 超了预算不用要花钱的搜索
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

    def _music_search(
        self, guild_id: int, channel_id: int, user_id: int, view: View, ticket: dict[str, int] | None = None
    ) -> str | None:
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
        row = self.store.record_usage(ts=now, day=day, guild_id=guild_id, channel_id=channel_id, kind="search", outcome="music")
        if ticket is not None:
            ticket["row"] = row  # 搜到了要算钱时，记在这一行上（见 _music_paid）
        return None

    def _music_paid(self, guild_id: int, ticket: dict[str, int]) -> None:
        """按歌名找歌时 DeepSeek 的 Search 搜到了结果：按次算钱，记在这次找歌的那一行上。"""
        now = self.clock()
        self.ledger.add(guild_id, self.pricing.search_cost(1), now)
        ticket["paid"] = ticket.get("paid", 0) + 1
        if "row" in ticket:  # 记在花钱的这一刻（跨过午夜的话，和账本记在同一天）
            cost = self.pricing.search_cost(ticket["paid"])
            self.store.update_usage(ticket["row"], outcome="music", cost=cost, ts=now, day=self.ledger.day(now))

    def _finish_search(self, st: ChannelState, row: int, usage: Usage, outcome: str, paid: int = 0) -> None:
        """paid：DeepSeek 的 Search 接口算了钱的次数（按次收费，不看 token）。"""
        now = self.clock()
        cost = self.pricing.cost(usage, now) + self.pricing.search_cost(paid)
        self.ledger.add(st.guild_id, cost, now)
        # 记录也挪到搜完的这一刻：搜的时候跨过了午夜，账本和数据库也记在同一天（重启后从库里读的预算才对得上）
        self.store.update_usage(
            row, outcome=outcome, hit=usage.hit, miss=usage.miss, output=usage.output, cost=cost, ts=now, day=self.ledger.day(now)
        )

    # ------------------------------------------------------------------ 链接

    def _links_mode(self, view: View) -> str:
        """打开链接的能力：all（推文和网页）/ x（只有推文：DeepSeek、Tavily 都没有）/ off。"""
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
                    if st.conv.key < 0 and m.message_id not in shown_ids and st.conv.key not in self.store.message_keys([m.message_id]):
                        # 连通组的记录：不在这一批里、也没记进这份记录的（连通之前的闲聊、暂停着的频道里的、上一轮没跑成的）不读进来
                        continue
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
            if self.link_reader.readable(link.url)  # 打不开的网页（比如媒体文件）就只看预览，不用提
        ]
        return await self._read_links(st, view, plan, budget, now)

    async def _read_links(
        self, st: ChannelState, view: View, plan: list[_LinkPlan], budget: _Budget, now: float, *, pay: bool | None = None
    ) -> list[tuple[_LinkPlan, DocBlock, FileText]]:
        """打开这些链接（同时打开，最多几秒）。名额、字数、每日次数不够的，告诉模型没打开。
        超了预算就不用要花钱的读法（DeepSeek 的 Fetch 按次收费），和超了预算不搜一样；推文、Tavily 不花钱，照常。
        pay：调用方已经定好了用不用要花钱的读法（查资料那一步：同时在搜的那几次的钱不算在前面）；不填就现在看预算。"""
        if not plan:
            return []
        assert self.link_reader is not None
        if pay is None:
            pay = not self._over_budget(st, view, now)[0]
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
            return await self.link_reader.read(item.link.url, max_chars=cap, image_side=side, pay=pay)

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
                cost = self.pricing.fetch_cost(got.paid)  # DeepSeek 读到了网页：按次算钱（没读成的不算）
                if cost:
                    done = self.clock()
                    self.ledger.add(st.guild_id, cost, done)
                    # 记在花钱的这一刻：读的时候跨过了午夜，账本和数据库也记在同一天
                    self.store.update_usage(
                        row, outcome="error" if result.error else "link", cost=cost, ts=done, day=self.ledger.day(done)
                    )
                else:
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

    def _scope(self, guild_id: int) -> int:
        """这个服务器的长期记忆记在哪：连成一组的服务器共用组里那一份（组的 ID），不然就是自己。人设卡见 _card_scope。"""
        return self.store.memory_scope(guild_id)

    def _cards_shared(self) -> bool:
        """所有服务器共用一张人设卡（config.toml 的 [bot] shared_cards，启动时换好了）。"""
        return self.cards is not None and self.cards.shared

    def _card_scope(self, guild_id: int) -> int:
        """这个服务器的人设卡记在哪：所有服务器共用一张时都在 SHARED，不然和长期记忆一样（连成一组的记在组里）。
        和这个服务器自己的 ID 不一样，就是别的服务器的人的卡也在里面：只列、只 @ 在这里出现过的人，按名字查时分档。"""
        return SHARED if self._cards_shared() else self._scope(guild_id)

    def _card_near(self, guild_id: int) -> set[int] | None:
        """按名字查卡时，除了这个服务器还算哪些服务器的人：所有服务器共用一张卡时只算组里别的服务器（没连组就没有：
        不相干的服务器的人在这里查不到）；不然是 None（组里那份本来就只有组里几个服务器的人）。"""
        return set(self._group_guilds(guild_id)[1:]) if self._cards_shared() else None

    def _memory_key(self, guild_id: int) -> tuple[int, int]:
        """(记在哪, 这个服务器的组变过几次)：问模型前后对不上，就是中途连组、退组了（连了又退回来，记在哪一样，
        编号却变了）。别的服务器连组、退组不算。"""
        return self._scope(guild_id), self.store.group_epoch(guild_id)

    def _names_here(self, st: ChannelState) -> bool:
        """卡上记的名字跟着这里的昵称改吗：私信里不改（私信里是他的全局名字，和服务器里的昵称来回换，提示词就一直变）；
        连成一组时只跟着组里第一个服务器的改；所有服务器共用一张卡时都不改（各服务器的昵称记在 card_seen 里）。"""
        if st.private or self._cards_shared():
            return False
        group = self.store.group_of(st.guild_id)
        return group is None or self.store.group_members(group)[:1] == [st.guild_id]

    def _note_seen(self, st: ChannelState, user_id: int, name: str) -> None:
        """卡里有别的服务器的人时（连成一组、所有服务器共用一张）：记下这个人的卡在这个服务器用过（退出组时只带走在那里
        出现过的人的卡；@ 人、查卡时认他在这里的名字）。私信里只记“用过”，不记名字（私信里是他的全局名字，不是在服务器里的昵称）。"""
        scope = self._card_scope(st.guild_id)
        if scope != st.guild_id and self.cards is not None:
            self.cards.note_seen(scope, user_id, st.guild_id, "" if st.private else name)

    def _group_guilds(self, guild_id: int) -> list[int]:
        """和这个服务器连成一组的服务器，自己排第一个、别的按加入的先后；不在组里就只有自己。"""
        group = self.store.group_of(guild_id)
        if group is None:
            return [guild_id]
        return [guild_id] + [g for g in self.store.group_members(group) if g != guild_id]

    def _guild_name(self, guild_id: int) -> str:
        name = getattr(self.transport, "guild_name", None)
        return (name(guild_id) if name is not None else None) or str(guild_id)

    def _server_label(self, guild_id: int) -> str:
        """给模型看的别的服务器的名字（截短、中和过）。"""
        return neutralize(one_line(self._guild_name(guild_id), SERVER_NAME_CHARS))

    def _foreign_memories(self, scope: int, guild_id: int, entries: Sequence[Any] | None = None) -> dict[int, str]:
        """连成一组时：组里现在别的服务器记的长期记忆 {编号: 那个服务器的名字}（这里整理时只能看）。没连成组是空的。"""
        if scope == guild_id or self.memory is None:
            return {}
        others = set(self._group_guilds(guild_id)[1:])
        entries = self.memory.entries(scope) if entries is None else entries
        return {m.no: self._server_label(m.origin) for m in entries if m.origin is not None and m.origin in others}

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
        scope = self._card_scope(st.guild_id)
        grouped = scope != st.guild_id
        for uid, name in people.items():
            full = len(shown) >= max(0, self.ec.cards_per_round)
            if full and not grouped:
                break
            card = self.cards.get(scope, uid)
            if full:  # 附满了：卡里有别的服务器的人时，剩下的人也记下“在这里出现过”（退组时这个服务器带走他们的卡）
                if card is not None:
                    self._note_seen(st, uid, name)
                continue
            if card is None:
                seen = self._shown_card(st, uid, scope)
                if seen is not None:
                    if oldest is not None and seen[0] >= oldest:
                        shown.append((name, None))  # 对话里还附着被删掉的那份：告诉它别再用
                    marks.append((uid, None))  # 这一轮写进记录之后就不用再说了
                continue
            if name and card.name != name and self._names_here(st):
                self.cards.touch_name(scope, uid, name)  # 改了昵称：按新名字也能查到
            self._note_seen(st, uid, name)  # 名字没变就不写库
            seen = self._shown_card(st, uid, scope)
            if seen is not None and seen[1] == card.updated_at and oldest is not None and seen[0] >= oldest:
                continue
            shown.append((name or card.name, card.text))
            marks.append((uid, card.updated_at))
        if self._sync_foreign(st) and not self._cards_shared():
            return [], []  # 和别的服务器的频道连通了、卡又是各服务器自己的：附进共用的记录那边就看到了
        return render_cards(shown), marks

    @staticmethod
    def _shown_card(st: ChannelState, uid: int, scope: int) -> tuple[Any, ...] | None:
        """记录里附过的这个人的卡（附的是这里这份才算：连通组的记录里，别的服务器附的是那边的卡，不能当成这里的卡附过、删了）。"""
        seen = st.cards_shown.get(uid)
        # 没连通的记录照旧：只有这里会附，连组、退组以后附过的那份照样算（和 2.7 一样）
        return seen if seen is None or len(seen) < 3 or st.conv.key >= 0 or seen[2] == scope else None

    def _mark_cards(self, st: ChannelState, conv: Conversation, marks: list[tuple[int, float | None]]) -> None:
        """附上过的人设卡记成“已经在对话里了”（记下是哪条记录；压缩记忆删掉那条、或者卡改了，再附一次）。
        版本是 None：告诉过它这张卡删掉了，不用再记着。这一轮写进记录之后才记（没写成就下一轮再说）。"""
        if not marks or not conv.turns:
            return
        turn_id = conv.turns[-2].id if len(conv.turns) >= 2 else conv.turns[-1].id
        scope = self._card_scope(st.guild_id)
        for uid, version in marks:
            if version is None:
                st.cards_shown.pop(uid, None)
            else:
                st.cards_shown[uid] = (turn_id, version, scope)  # 卡记在哪：连通组的记录里也有别的服务器附的那张

    def _who_section(self, st: ChannelState, view: View, action: Action, marks: list[tuple[int, float]]) -> str:
        """who：按名字查人设卡（聊到不在场的人时）。"""
        name = action.name or ""
        if not self._cards_on(view):
            return WHO_OFF
        if self._sync_foreign(st):
            return WHO_SYNC_OFF  # 查到的卡会进几个服务器共用的记录
        assert self.cards is not None
        scope = self._card_scope(st.guild_id)
        if scope != st.guild_id:  # 卡里有别的服务器的人：先找在这里出现过的人，别的服务器同名的人标出来（免得安到这里的人头上）
            hits = self.cards.find_here(scope, name, st.guild_id, near=self._card_near(st.guild_id))
        else:
            hits = [(card, card.name, "", True) for card in self.cards.find(scope, name)]
        if not hits:
            return WHO_NONE.format(name=neutralize(one_line(name, 32)))
        for card, _, _, local in hits:
            if local:
                # 还没在这里出现过的人不记成“卡已经在对话里了”：那份写着他不在这里，等他来这里说话时照常附上
                marks.append((card.user_id, card.updated_at))
        log.info("频道 %s：查了「%s」的人设卡（%d 张）", st.channel_id, one_line(name, 32), len(hits))
        return render_who(name, [(shown, card.text, note) for card, shown, note, _ in hits])

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
        authors = self.store.load_authors(st.conv.key, [to for to, _ in writes if 0 < to < 2**31])
        return {text for to, text in writes if authors.get(to, (None, ""))[0] == author_id}

    @staticmethod
    def _copies_card(text: str, others: Sequence[str], mine: Sequence[str], said: Iterable[str] | Said) -> bool:
        """新写的卡有没有照抄别人的卡。本来就在他自己卡上的那几段（几个人的卡里都有的一段，比如公会的规矩）不算抄，
        只对这种“本来就和他的卡重了”的卡放宽；别的卡照常查（一轮一轮一点点抄进自己的卡，加起来也会被拦）。"""
        shared = quoted_cards("\n".join(mine), others)
        plain = [c for c in others if c not in shared]
        public = said if isinstance(said, Said) else Said(lambda lines=list(said): lines)
        return quotes_card(text, plain, said=public) or quotes_card(text, shared, said=public.plus(mine))

    def _screen_memory(self, scope: int, items: list[Any], text_of: Callable[[Any], str | None], cards: list[str], said: Iterable[str]) -> list[Any]:
        """要记进长期记忆（scope：记在哪，见 _scope）的几条：和已经记着的连起来照抄了人设卡的不记（一张卡拆成几条分开记，
        拼起来还是原文）。长期记忆每个频道的提示词里都有、管理员也看得到。text_of 返回 None 的不是要记的话，原样留着。"""
        base = [m.text for m in self.memory.entries(scope)] if self.memory is not None else []
        return self._screen_texts(base, items, text_of, cards, said)

    def _screen_texts(
        self, base: list[str], items: list[Any], text_of: Callable[[Any], str | None], cards: list[str], said: Iterable[str]
    ) -> list[Any]:
        """要记下来的几条：和 base（已经记着的）连起来照抄了人设卡的不记。"""
        if not cards or not any(text_of(item) for item in items):
            return items
        base = list(base)
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

        memo = self._screen_memory(self._scope(st.guild_id), actions, lambda a: a.text if a.kind == "memory" else None, seen, heard)
        if len(memo) != len(actions):
            log.info("频道 %s：要记的长期记忆照抄了人设卡的原文，没记", st.channel_id)
            actions, stored = memo, dump_actions(memo)
        user = self.transport.channel_info(st.channel_id).dm_user if st.private else None
        if user is not None and self.memory is not None:
            # 私信记忆本人用 /ds-dm 记忆 看得到：别人的人设卡原文不能抄进来（自己的卡照抄没关系）
            own = set(self.cards.own_texts(self._card_scope(st.guild_id), user)) if self.cards is not None else set()
            others = [card for card in seen if card not in own]
            base = [e.text for e in self.memory.dm_entries(user)]
            memo = self._screen_texts(base, actions, lambda a: a.text if a.kind == "dm_memory" else None, others, heard)
            if len(memo) != len(actions):
                log.info("私信 %s：要记的私信记忆照抄了别人的人设卡，没记", st.channel_id)
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
        trusted: bool = False,
    ) -> list[Action]:
        """改人设卡、长期记忆、私信记忆（只是写进数据库）。改成了就在要求的那条消息上加 📝。返回剩下要执行的动作。
        seen：这次请求里它看得到的人设卡（不含它这次自己写的）。history：这一轮之前的记录。
        trusted：这一轮只有群友说的话（没有文件、链接、搜索结果之类外来的内容），私信记忆的敏感标记才能往下调。"""
        mine = [a for a in actions if a.kind in MEMORY_KINDS]
        if not mine:
            return actions
        by_sid = dict(numbered)
        now = self.clock()
        marked: set[int] = set()
        refused: list[Action] = []
        guild = st.guild_id
        secrets: list[str] = []
        if st.private:
            mine, secrets = self._dm_secrets(st, view, mine)
        # 这一轮里有按编号改、删长期记忆的：组刚变过时整轮都不写（新记的那条可能是要替掉某一条的，只记它会留下重复的）
        by_number = any(a.kind == "memory" and a.op in ("edit", "delete") for a in mine)
        # 和别的服务器的频道连通了、人设卡又是各服务器分开的：写卡的动作一个都不留在记录里（写进去的是整张卡，那边看得到）
        hidden_cards = self._sync_foreign(st) and not self._cards_shared()
        for action in mine:
            if st.guild_id != guild:
                # 加 📝 的时候私信换了服务器：剩下的不写（不能写进别的服务器）
                log.info("私信 %s：写记忆的时候换了服务器，剩下的没写", st.channel_id)
                break
            if secrets and action.kind in ("card", "memory") and self._reveals_secret(st, action, by_sid, secrets):
                # 人设卡、长期记忆服务器里的人看得到：私信里说到敏感的事的不写（那些只记进私信记忆）
                log.info("私信 %s：要写进%s的说到了敏感的事，没写", st.channel_id, "人设卡" if action.kind == "card" else "长期记忆")
                if action.kind == "card":
                    refused.append(action)
                continue
            if action.kind == "card":
                mid = self._apply_card(st, view, action, by_sid, now, seen, history)
                if mid is None and (hidden_cards or not self._card_is(st, action, by_sid)):
                    refused.append(action)
            elif action.kind == "dm_memory":
                continue  # 私信记忆下面一起改（一批里先删后加，也要认得出说的是敏感的事）
            else:
                mid = self._apply_memory(st, view, action, targets, kind, now, by_number=by_number)
            if mid is not None and mid not in marked:
                marked.add(mid)
                await self._safe_react(st, mid, "📝")  # 让人一眼看到真的记下了
        notes = [a for a in mine if a.kind == "dm_memory"]
        if notes and st.guild_id == guild:
            mid = self._apply_dm_memory(st, view, notes, targets, kind, now, by_sid, trusted)
            if mid is not None and mid not in marked:
                marked.add(mid)
                await self._safe_react(st, mid, "📝")
        if refused:
            self._drop_from_record(st, refused)
        return [a for a in actions if a.kind not in MEMORY_KINDS]

    def _card_is(self, st: ChannelState, action: Action, by_sid: dict[int, IncomingMessage]) -> bool:
        """card 动作要写的，本来就是那个人现在的卡（没写是因为没有变化，不算没写成）。"""
        m = by_sid.get(action.to) if action.to is not None else None
        if m is None or self.cards is None:
            return False
        card = self.cards.get(self._card_scope(st.guild_id), m.author_id)
        return (card.text if card is not None else "") == clean_card(action.text or "")

    def _clean_reply(self, st: ChannelState, stored: str) -> str:
        """和别的服务器的频道连通了：记下的回话里不留改长期记忆的动作（里面写着这个服务器那一条的原文，长期记忆是各服务器自己的），
        人设卡又是各服务器分开的话，写卡的动作也不留（解析不出来的也算：里面写着整张卡，那边看得到；跨服务器以后的检查也就不会
        再改这条记录）。别的时候原样。"""
        if not self._sync_foreign(st):
            return stored
        kinds = {"memory"} | (set() if self._cards_shared() else {"card"})
        return self._without_actions(stored, kinds) or stored

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
        if self._sync_foreign(st) and not self._cards_shared():
            # 和别的服务器的频道连通了、卡又是各服务器自己的：他现在的卡这里不附（附进共用的记录那边就看到了），不照着改
            log.info("频道 %s：和别的服务器的频道连通了（人设卡各服务器分开），这里不改人设卡", st.channel_id)
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
        scope = self._card_scope(st.guild_id)
        drafts = self._drafts(st, history, m.author_id)
        others = [*self.cards.others(scope, m.author_id), *(t for t in seen if t not in drafts)]
        mine = self.cards.own_texts(scope, m.author_id)
        if self._copies_card(text, others, mine, self._said(st.conversation, (x.text for x in by_sid.values()))):
            log.info("频道 %s：%s 的新人设卡照抄了别人的人设卡，没改", st.channel_id, m.author_name)
            return None
        # 它是照着对话里附着的那份改的：那份在这期间被本人改了、删了（/ds-rp、bot 主人删卡、连进了服务器组），就不写
        seen = self._shown_card(st, m.author_id, scope)  # 附的是这里这份才算（连通组的记录里可能是别的服务器附的那张）
        expect = seen[1] if seen is not None else None
        current = self.cards.get(scope, m.author_id)
        if (current.updated_at if current is not None else None) != expect:
            log.info("频道 %s：%s 的人设卡刚被改过或删过，这次没改", st.channel_id, m.author_name)
            return None
        name = m.author_name if current is None or self._names_here(st) else current.name  # 卡上的名字别跟着别处的昵称变
        if not self.cards.put(scope, m.author_id, name, text, via="chat", now=now, expect=expect):
            return None  # 没有变化
        self._note_seen(st, m.author_id, m.author_name)
        # 只记谁的卡改了，不记内容：卡是本人的东西
        log.info("频道 %s：%s 的人设卡%s", st.channel_id, m.author_name, "已清空" if not text else f"已更新（{len(text)} 字）")
        return m.message_id

    def _apply_memory(
        self, st: ChannelState, view: View, action: Action, targets: list[int], kind: str, now: float, *, by_number: bool = True
    ) -> int | None:
        """改长期记忆：只在被叫到的那一轮（有人要它记）。返回要加 📝 的消息。
        by_number：这一轮里有没有按编号改、删的（组刚变过时，只有新记、没有改删的那一轮照样记）。"""
        live = [t for t in targets if st.msg_by_sid.get(t) not in st.deleted]  # 叫它的消息被删了：当成不要它记了
        if not self._memory_on(view) or kind != "mention" or not live:
            log.info("频道 %s：没人叫它要记，或者这里没开长期记忆，没改", st.channel_id)
            return None
        scope = self._scope(st.guild_id)
        if st.memory_key is not None and self._memory_key(st.guild_id) != st.memory_key and (action.op != "add" or by_number):
            # 它看到的是连组（或退组）之前的那份：编号对不上，改错了就改到别的条目上了。新记一条不看编号，照样记进现在这份
            log.info("频道 %s：服务器组刚变过，它看到的长期记忆已经不是现在这份，这次没改", st.channel_id)
            return None
        if action.op in ("edit", "delete") and action.index not in st.memory_shown:
            # 没给它看过的编号（问的时候有服务器连进组、并进来的接在后面）：多半是编的，别改到那些条目上
            log.info("频道 %s：长期记忆第 %s 条没给它看过，没改", st.channel_id, action.index)
            return None
        if not self._memory_op(scope, action.op or "", action.index, action.text, via="chat", now=now, origin=st.guild_id):
            log.info("频道 %s：长期记忆没改成（%s %s）", st.channel_id, action.op, action.index or "")
            return None
        log.info("频道 %s：长期记忆 %s %s", st.channel_id, action.op, one_line(action.text or str(action.index or ""), 80))
        return st.msg_by_sid.get(live[-1])

    def _dm_secrets(self, st: ChannelState, view: View, actions: list[Action]) -> tuple[list[Action], list[str]]:
        """私信里这一轮要写的记忆：标着敏感的长期记忆改记进私信记忆（开着的话），不然不写；
        再返回他敏感的那些事（已经记着的、这一轮要记成敏感的），写人设卡、长期记忆时拿来对照。"""
        user = self.transport.channel_info(st.channel_id).dm_user
        if user is None or self.memory is None:
            return actions, []
        out: list[Action] = []
        for a in actions:
            if a.kind == "memory" and a.sensitive and a.op in ("add", "edit"):
                if a.op == "add" and a.text and self._dm_memory_on(view):
                    out.append(Action("dm_memory", a.to, op="add", text=a.text, sensitive=True))
                log.info("私信 %s：标着敏感的长期记忆没写进长期记忆", st.channel_id)
                continue
            out.append(a)
        secrets = [e.text for e in self.memory.dm_entries(user) if e.sensitive]
        secrets += [a.text for a in out if a.kind == "dm_memory" and a.op in ("add", "edit") and a.text and a.sensitive is not False]
        return out, secrets

    def _reveals_secret(self, st: ChannelState, action: Action, by_sid: dict[int, IncomingMessage], secrets: list[str]) -> bool:
        """要写进人设卡、长期记忆的说到了他敏感的事（他人设卡上本来就有的不算：那是他自己写的）。"""
        text = action.text or ""
        if not text:
            return False
        already = ""
        if action.kind == "card" and self.cards is not None:
            m = by_sid.get(action.to) if action.to is not None else None
            card = self.cards.get(self._card_scope(st.guild_id), m.author_id) if m is not None else None
            already = card.text if card is not None else ""
        return reveals(text, secrets, already)

    def _apply_dm_memory(
        self,
        st: ChannelState,
        view: View,
        actions: list[Action],
        targets: list[int],
        kind: str,
        now: float,
        by_sid: dict[int, IncomingMessage] | None = None,
        trusted: bool = False,
    ) -> int | None:
        """改私信记忆（这一轮的几条一起改）：只在私信里、他要它记的那一轮。改成了任何一条就返回要加 📝 的消息。
        内容可能是敏感的，日志里不写。"""
        live = [t for t in targets if st.msg_by_sid.get(t) not in st.deleted]
        user = self.transport.channel_info(st.channel_id).dm_user if st.private else None
        if user is None or not self._dm_memory_on(view) or kind != "mention" or not live:
            log.info("频道 %s：不是私信、没开私信记忆，或者没人叫它要记，私信记忆没改", st.channel_id)
            return None
        assert self.memory is not None
        cleared = self.memory.dm_cleared_at(user)
        said = [m.received_at for t in live if (m := (by_sid or {}).get(t)) is not None and m.received_at]
        if cleared is not None and said and cleared >= max(said):
            log.info("私信 %s：他说完之后清空过私信记忆，这一轮要记的不记", st.channel_id)
            return None
        ops = [(a.op or "", a.index, a.text, a.sensitive) for a in actions]
        done, secret = self._apply_dm_ops(user, ops, via="chat", now=now, may_lower=trusted)
        if secret:
            st.quiet_dm = True  # 刚说了敏感的事：这一轮的对话不当近况带到频道里
        for (op, no, _, _), ok in zip(ops, done):
            log.info("私信 %s：私信记忆 %s %s%s", st.channel_id, op, no or "", "" if ok else "（没改成）")
        return st.msg_by_sid.get(live[-1]) if any(done) else None

    def _dm_memory_on(self, view: View) -> bool:
        return self.memory is not None and bool(view.dm_memory)

    def _dm_memory_in_prompt(self, st: ChannelState, view: View) -> bool:
        """这一轮的请求里有私信记忆：私信里（system prompt 里有，敏感的也在），或者频道的记录里附着某人的
        （从记录里认：重启之后也算）。"""
        if st.private:
            return self._dm_memory_on(view)
        conv = st.conversation
        return conv is not None and any(t.role == "user" and has_dm_memory(t.content) for t in conv.turns)

    def _apply_dm_ops(
        self,
        user_id: int,
        ops: list[tuple[str, int | None, str | None, bool | None]],
        *,
        via: str,
        now: float,
        may_lower: bool = False,
    ) -> tuple[list[bool], bool]:
        """一批私信记忆的改动（聊天里这一轮的、整理记忆这一次的）：[(add / edit / delete, 编号, 内容, 敏感)]。
        返回 (每一条改成了没有, 这一批有没有记下、改成敏感的)。

        宁可少说：新记的没写敏感不敏感、改了内容没写，或者说的是敏感的那件事（这一批开始之前就是敏感的、这一批要记成
        敏感的：先删后加、先删后改也认得出），都当成敏感。敏感标记只有本人能往下调：聊天里“只改敏感标记”的那种写法，
        而且这一轮只有他说的话（may_lower），或者 /ds-dm 记忆；改内容、整理记忆都只能往上调。
        频道里附过的撤回：有条目改成了敏感；或者拿掉、改掉了不敏感的条目，又是他自己要的（聊天里），
        或者这一批同时记了敏感的（多半是把它并进了敏感的那条）。只是整理时改改说法、合并掉的不撤，免得记录老被改写。"""
        assert self.memory is not None
        before = {e.no: e for e in self.memory.dm_entries(user_id)}
        secrets = [e.text for e in before.values() if e.sensitive]
        secrets += [clean_memory(t) for op, _, t, s in ops if op in ("add", "edit") and t and s is not False]
        done: list[bool] = []
        raised = removed = secret = False
        for op, no, text, sensitive in ops:
            ok, flag = self._dm_one(user_id, op, no, text, sensitive, secrets, via=via, now=now, may_lower=may_lower)
            done.append(ok)
            if not ok:
                continue
            old = before.get(no) if no is not None else None
            if op == "add":
                secret = secret or flag
            elif op == "edit" and old is not None and not old.sensitive:
                raised = raised or flag
                removed = removed or (text is not None and clean_memory(text) != old.text)
            elif op == "delete" and old is not None and not old.sensitive:
                removed = True
        if raised or (removed and (via == "chat" or secret)):
            self.withdraw_dm_memory(user_id)
        if raised or secret:
            self._forget_dm_talk(user_id)  # 私信里刚说了敏感的事：私信的近况别再带到频道里
        return done, raised or secret

    def _dm_one(
        self,
        user_id: int,
        op: str,
        no: int | None,
        text: str | None,
        sensitive: bool | None,
        secrets: list[str],
        *,
        via: str,
        now: float,
        may_lower: bool,
    ) -> tuple[bool, bool]:
        """改一条私信记忆（规矩见 _apply_dm_ops）。返回 (改成了没有, 改完是不是敏感)。"""
        assert self.memory is not None
        if op == "add" or (op == "edit" and text is not None):
            text = clean_memory(text or "")
            if not text or len(text) > self.ec.dm_memory_entry_chars:
                return False, False
        if op == "add":
            entries = self.memory.dm_entries(user_id)
            if len(entries) >= self.ec.dm_memory_max_entries or any(e.text == text for e in entries):
                return False, False
            flag = sensitive is not False or reveals(text or "", secrets)
            self.memory.dm_add(user_id, text or "", sensitive=flag, via=via, now=now)
            return True, flag
        if op == "edit" and no is not None:
            current = self.memory.dm_get(user_id, no)
            if current is None:
                return False, False
            if text is not None:
                flag = True if sensitive is None else bool(sensitive or current.sensitive)
                flag = flag or reveals(text, secrets)
            elif sensitive:
                flag = True
            elif sensitive is False:
                flag = False if may_lower and via == "chat" else current.sensitive
            else:
                return False, False
            return self.memory.dm_edit(user_id, no, text, sensitive=flag, via=via, now=now), flag
        if op == "delete" and no is not None:
            return self.memory.dm_delete(user_id, no), False
        return False, False

    def _forget_dm_talk(self, user_id: int) -> None:
        """他私信里刚说了敏感的事：他私信的近况别再带到频道里（换了说法的认不出来，干脆都不带）。"""
        for key in [k for k in self.interactions if k[1] == user_id]:
            kept = [i for i in self.interactions[key] if not ((p := self.transport.locate(i.channel_id)) is not None and p.dm)]
            if kept:
                self.interactions[key] = deque(kept, maxlen=INTERACTIONS_PER_PERSON)
            else:
                del self.interactions[key]

    def withdraw_dm_memory(
        self,
        user_id: int | None = None,
        *,
        guild_id: int | None = None,
        keep: Callable[[int, int], bool] | None = None,
    ) -> int:
        """把频道记录里附过的私信记忆拿掉（隐私优先，和群友删消息一样直接改记录）：某个人的（他删了、改了、标成敏感、
        清空了），或者某个服务器的（关掉了私信记忆、跨服务器，退出了服务器组，私信改挂到别的服务器）。
        下次他叫它时，照当时能带的重新附。keep(频道, 人) 为真的那些照样留着（只撤回现在不能带的）。
        返回改了几条记录。"""
        rows = self.store.dm_memory_turns(user_id=user_id, guild_id=guild_id)
        if keep is not None:
            rows = [r for r in rows if not keep(r[1], r[2])]
        changed = 0
        for turn_id, channel_id, _, name in rows:
            content = self.store.turn_content(turn_id)
            if content is None:
                continue  # 已经压缩掉、清空了
            new = drop_dm_memory(content, name)
            if new == content:
                continue
            self.store.update_turn(turn_id, new)
            changed += 1
            for cs in self._channel_convs(channel_id):
                if cs.conversation is not None:
                    for turn in cs.conversation.turns:
                        if turn.id == turn_id:
                            turn.content = new
                # 同名的人附在这条记录里的那段也一起拿掉了：都记成没附过，下次叫它时重新附
                for person in [p for p, (t, _) in cs.dm_memory_shown.items() if t == turn_id]:
                    del cs.dm_memory_shown[person]
        if rows:
            self.store.forget_dm_memory([(turn_id, uid) for turn_id, _, uid, _ in rows])
        if keep is not None:
            for _, channel_id, uid, _ in rows:
                for cs in self._channel_convs(channel_id):
                    cs.dm_memory_shown.pop(uid, None)
        else:
            for st in self.states.values():
                if guild_id is not None and st.guild_id != guild_id:
                    continue
                if user_id is None:
                    st.dm_memory_shown.clear()
                else:
                    st.dm_memory_shown.pop(user_id, None)
        if changed:
            log.info("撤回了 %d 条记录里附着的私信记忆", changed)
        return changed

    def _channel_convs(self, channel_id: int) -> list[ConvState]:
        """这个频道的记录（已经在内存里的）：它自己那份，连通了的话还有共用的那份。"""
        keys = dict.fromkeys([channel_id, self.store.conversation_key(channel_id)])
        return [cs for key in keys if (cs := self.convs.get(key)) is not None]

    def _dm_memory_texts(self, st: ChannelState, view: View, user_id: int) -> list[str] | None:
        """频道里能附上的这个人的私信记忆：不敏感的、最近改过的几条。这里不能带（私信里、没开私信记忆、
        不是他私信挂的服务器（或者和它连成一组的服务器）又没开跨服务器）是 None。"""
        if st.private or not self._dm_memory_on(view):
            return None
        home = self.store.dm_home(user_id)
        if not ((home is not None and home[0] in self._group_guilds(st.guild_id)) or view.cross_server):
            return None
        assert self.memory is not None
        public = [e for e in self.memory.dm_entries(user_id) if not e.sensitive]
        return [e.text for e in _newest(public, DM_MEMORY_ATTACH_CHARS, max(0, self.ec.dm_memory_attach_entries))]

    def _dm_memory_context(
        self, st: ChannelState, view: View, conv: Conversation, triggers: list[IncomingMessage]
    ) -> tuple[list[str], list[tuple[int, str | None, str]]]:
        """服务器的频道里被叫到时，附上叫它的人私信记忆里不敏感的那些：私信挂在这个服务器（或者和它连成一组的服务器）的
        一定带，别的服务器要开了“跨服务器”才带。已经附过、内容没变、那条记录也还在的，不再附。
        返回 (要附的几行, [(人, 附的内容或 None, 写的名字)])。"""
        if st.private or not triggers or not self._dm_memory_on(view) or self._synced(st):
            return [], []  # 连通的频道：私信记忆不带进几个服务器共用的记录
        oldest = conv.turns[0].id if conv.turns else None
        lines: list[str] = []
        marks: list[tuple[int, str | None, str]] = []
        done: set[int] = set()
        for m in triggers:
            if m.is_bot or m.author_id in done:
                continue
            done.add(m.author_id)
            entries = self._dm_memory_texts(st, view, m.author_id)
            if entries is None:
                continue
            who = neutralize(one_line(m.author_name, 32))  # 和 render_dm_memory 写的一样：撤回时按它找
            seen = st.dm_memory_shown.get(m.author_id)
            still = seen is not None and oldest is not None and seen[0] >= oldest  # 附过的那份还在对话里
            if not entries:
                if seen is not None:
                    if still:
                        lines += render_dm_memory(m.author_name, [])  # 对话里还附着清掉之前的那些：告诉它别再用
                    marks.append((m.author_id, None, who))
                continue
            shown = "\n".join(entries)
            if still and seen is not None and seen[1] == shown:
                continue
            lines += render_dm_memory(m.author_name, entries)
            marks.append((m.author_id, shown, who))
        return lines, marks

    def _mark_dm_memory(
        self, st: ChannelState, view: View, conv: Conversation, marks: list[tuple[int, str | None, str]]
    ) -> None:
        """附上过的私信记忆记成“已经在对话里了”（这一轮写进记录之后才记，和人设卡一样），也记进库里（撤回时要找到它）。
        这一轮进行中他改了、删了、标成敏感（或者这里关掉了）：刚写进记录的那份马上拿掉。"""
        if not marks or not conv.turns:
            return
        turn = conv.turns[-2] if len(conv.turns) >= 2 else conv.turns[-1]
        for uid, shown, who in marks:
            if shown is None:
                st.dm_memory_shown.pop(uid, None)
                continue
            now_texts = self._dm_memory_texts(st, view, uid)
            if now_texts is None or "\n".join(now_texts) != shown:
                new = drop_dm_memory(turn.content, who)
                if new != turn.content:
                    turn.content = new
                    self.store.update_turn(turn.id, new)
                st.dm_memory_shown.pop(uid, None)
                continue
            st.dm_memory_shown[uid] = (turn.id, shown)
            self.store.note_dm_memory(turn.id, st.channel_id, uid, who)

    def _memory_op(
        self, scope: int, op: str, no: int | None, text: str | None, *, via: str, now: float, origin: int | None = None
    ) -> bool:
        """记一条 / 改一条 / 删一条长期记忆（scope：记在哪，见 _scope；origin：哪个服务器记的）。
        超出条数、字数，或者编号不存在时不改，返回 False。"""
        assert self.memory is not None
        if op in ("add", "edit"):
            text = clean_memory(text or "")
            if not text or len(text) > self.ec.memory_entry_chars:
                return False
        if op == "add":
            entries = self.memory.entries(scope)
            if len(entries) >= self.ec.memory_max_entries or any(e.text == text for e in entries):
                return False
            self.memory.add(scope, text or "", via=via, now=now, origin=origin)
            return True
        if op == "edit" and no is not None:
            current = self.memory.get(scope, no)
            return current is not None and current.text != text and self.memory.edit(scope, no, text or "", via=via, now=now)
        if op == "delete" and no is not None:
            return self.memory.delete(scope, no)
        return False

    # ------------------------------------------------------------------ 整理记忆

    def _review_due(self, st: ChannelState, now: float) -> bool:
        """频道安静了一阵、又攒了几轮新聊天：该整理记忆了。连着失败时等得越来越久。
        连通的几个频道共用一份记录：几个频道都安静了才整理。"""
        if self.cards is None and self.memory is None:
            return False
        members = self._members(st.conv) or [st]
        # 缓冲区里可能一直有没叫它的消息（没开主动发言的频道）：那些等真的聊到它再说，不影响整理已经聊过的
        if st.reviewing or any(m.scheduled for m in members) or st.lock.locked():
            return False
        if st.unreviewed_rounds < max(1, self.ec.review_min_rounds) or now < self.paused_until:
            return False  # 没攒够；或者 DeepSeek 暂停中（余额不足、key 不对）
        wait = self.ec.review_idle_seconds * (2 ** min(st.review_failures, 5))
        if now - st.last_review_at < wait:
            return False  # 上次刚整理过（或者刚失败过）
        return now - max(max(m.last_activity_at, m.last_eval_at) for m in members) >= self.ec.review_idle_seconds

    def _kick_review(self, owner: Any) -> None:
        if self.closing:
            return
        cs = self._cs(owner)
        cs.reviewing = True
        task = asyncio.get_running_loop().create_task(self._review_task(cs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _review_task(self, cs: ConvState) -> None:
        """安静时整理记忆。只在挑要整理的聊天时占着这份记录；问模型的时候不占，有人叫它照样马上回。
        整理的时候又压缩了旧记录：压掉的那部分排着队，整理完这批接着整理。"""
        try:
            try:
                async with cs.lock:
                    if self.convs.get(cs.key) is not cs:
                        return  # 等锁的时候这份记录换成了新的（清空、断开、散组）：新的那份另外整理
                    owner = self.states.get(cs.key)
                    conv = self._conversation(owner) if owner is not None and owner.conv is cs else self._load(cs)
                    self._flush_fixes(cs, conv)
                    generation = cs.generation
                    info = self._review_info(cs)
                    cs.review_cut = False  # 之前压缩时挑走的已经不在记录里了：这批照现在的记录挑
                    plan = self._review_plan(cs, conv, info)
                if plan is not None:
                    await self._run_review(cs, conv, info, plan, generation, before_compact=False)
            except Exception:
                log.exception("频道 %s：整理记忆出错", cs.key)
            cs.review_held = 0  # 这批完了：没整理成、还在记录里的，压缩时再挑出来
            cs.review_asking = None
            cs.review_parked.clear()  # 问的那一段问完了（没问成的话，压掉的那几条跟没连通过时一样不再整理）
            cs.review_cut = False
            await self._review_queued(cs)
            if cs.key < 0 and not self.closing:
                for cid in self.store.sync_members(cs.key):
                    self._review_frozen(cid)  # 连的时候没整理成的（暂时出错、预算用完了）：组里整理的时候再试
        finally:
            self._review_done(cs)

    async def _compact_review(self, owner: Any) -> None:
        """压缩旧记录之前挑出来的那部分聊天：在后台整理（整理的时候又压缩了，压掉的也接着整理）。"""
        cs = self._cs(owner)
        try:
            await self._review_queued(cs)
        finally:
            self._review_done(cs)

    async def _review_queued(self, owner: Any) -> None:
        """按先后整理排着队的几批（压缩前挑出来的原话）。"""
        cs = self._cs(owner)
        while cs.review_jobs and not self.closing:
            job = cs.review_jobs.pop(0)
            if job.generation != cs.generation:
                continue  # 排队的时候清空过记忆
            cs.review_current = job
            try:
                await self._run_review(cs, job.conv, job.info, job.plan, job.generation, before_compact=True, frozen=job.frozen)
            except Exception:
                log.exception("频道 %s：压缩前整理记忆出错", cs.key)
            finally:
                cs.review_current = None

    def _review_done(self, owner: Any) -> None:
        # 和上面最后一次检查队列之间不能有 await：不然这中间压缩时排进来的那批没人整理
        cs = self._cs(owner)
        cs.reviewing = False
        cs.review_held = 0
        cs.review_asking = None
        cs.review_parked.clear()
        cs.review_cut = False
        cs.review_current = None
        cs.review_jobs.clear()  # 被取消（关机）时剩下的就不整理了
        if any(o is cs for o in self._orphans):
            self._orphans = [o for o in self._orphans if o is not cs]  # 散掉的组留下的那几批整理完了

    def _set_reviewed(self, owner: Any, conv: Conversation, turn_id: int) -> None:
        cs = self._cs(owner)
        if conv.channel_id < 0 and not self.store.sync_members(conv.channel_id):
            return  # 连通组散了（共用的记录删掉了）：散之前排着队的那批照样整理完，进度不用记
        self.store.set_reviewed(conv, turn_id)
        if cs.review_done:
            cs.review_done = {t for t in cs.review_done if t > turn_id}
        if cs.conversation is not None and cs.conversation is not conv:
            cs.conversation.reviewed_upto = max(cs.conversation.reviewed_upto, turn_id)  # 中途重新载入过

    def _review_info(self, cs: ConvState) -> ChannelInfo:
        """整理这份记录时给模型看的名字（没连通的就是那个频道的；连通组的每一段另外按那段的频道算）。"""
        return self.transport.channel_info(cs.key if cs.key > 0 else next(iter(self.store.sync_members(cs.key)), 0))

    def _review_plan(
        self, owner: Any, conv: Conversation, info: ChannelInfo, upto: int | None = None, after: int = 0, frozen: bool = False
    ) -> _ReviewPlan | None:
        """挑出这份记录里还没整理过的聊天（owner：频道的状态或者那份记录；见 _plan_review）。"""
        cs = self._cs(owner)
        if cs.key != conv.channel_id:
            cs = self._conv_state(conv.channel_id)
        return self._plan_review(cs, conv, upto, after, frozen)

    def _plan_review(
        self, cs: ConvState, conv: Conversation, upto: int | None = None, after: int = 0, frozen: bool = False
    ) -> _ReviewPlan | None:
        """挑出还没整理过的聊天，分成几段。不用整理（关掉了、私密频道、没人说话）时把进度记到最后，返回 None。

        连通的几个频道共用的记录：按每段是哪个频道的分开，每段照那个频道的设置整理、记进那个服务器（或组）的那份；
        那个频道找不到了的那段不整理。

        upto：只挑到这条记录（压缩旧记录之前：那部分马上要压成摘要，原话就看不到了）。
        after：这条之前的不挑（正在整理的那一段已经挑走了）。
        frozen：连通的频道连通时挑它自己原来那份（不会压掉）：预算用完了不记成整理过，以后再整理。
        已经整理过的那几条（review_done：连通组分开整理完的、断开时拿回来在组里整理过的）不再挑。"""
        now = self.clock()
        start = max(conv.reviewed_upto, after)
        turns = [t for t in conv.turns if t.id > start and (upto is None or t.id <= upto)]
        if not turns:
            if upto is None:
                cs.unreviewed_rounds = 0
            return None
        last_id = turns[-1].id
        shared = cs.key < 0
        held = False
        runs: list[tuple[int | None, list[Any]]] = []
        if shared:
            # 连通组的记录：每个频道的聊天归在一起整理（几个频道轮流说话时不切成一小段一小段）；这批里已经整理过的、
            # 已经断开的频道的（断开时拿回去了，在它自己那份里整理）不再整理
            # 现在在组里的频道这次从哪条开始（退出又连回来的：之前那次连通时说的，断开时拿回去整理过了）；断开时没能拿回去的
            # 频道（它自己那份被旧版本写过）也在里面，那部分照样在组里整理
            joined = self.store.sync_joined_turns(cs.key)
            asking = set(cs.review_asking.turn_ids) if cs.review_asking is not None else set()
            groups: dict[int | None, list[Any]] = {}
            for t in turns:
                if t.id in cs.review_done:
                    continue  # 这批里已经整理过的
                if t.id in asking:
                    held = True  # 安静时整理正在问的那一段：要压掉的话，_review_before_compact 连同它一起排进去
                    continue
                if t.origin is not None and (t.origin not in joined or t.id <= joined[t.origin]):
                    continue
                groups.setdefault(t.origin, []).append(t)
            runs = list(groups.items())
        else:
            for t in turns:
                if t.id in cs.review_done:
                    continue  # 断开时拿回来的、在连通组里已经整理过的（没连通过的记录没有这种）
                if runs and runs[-1][0] == t.origin:
                    runs[-1][1].append(t)
                else:
                    runs.append((t.origin, [t]))
        chunks: list[_ReviewChunk] = []
        flags: tuple[bool, bool, bool] | None = None
        blocked = False  # 连通组：有的服务器今天的预算用完了，它那部分这次不整理
        for origin, run in runs:
            channel = origin if origin is not None else cs.key
            home = self._channel_state(channel) if channel > 0 else None
            if home is None:
                continue  # 那个频道找不到了
            view = self.view(home)
            info = self.transport.channel_info(channel)
            cards_ok = self._cards_on(view) and self.implicit_ok(channel, "cards")
            memory_ok = self._memory_on(view) and self.implicit_ok(channel, "long_memory")
            dm_ok = home.private and info.dm_user is not None and self._dm_memory_on(view)
            live = self._live(home, view)
            if live and (cards_ok or memory_ok or dm_ok) and self._over_budget(home, view, now)[0]:
                if shared:
                    # 这个服务器今天的预算用完了：只是它那部分不整理（别的服务器的照常）。安静时整理的等明天再说；
                    # 马上要压掉的只好不整理
                    blocked = blocked or upto is None
                    continue
                if upto is not None and not frozen:
                    self._set_reviewed(cs, conv, last_id)  # 这部分马上就压掉了，以后也整理不了（连通时挑出来的那份不会压掉）
                cs.last_review_at = now  # 今天的预算用完了：隔一阵再看（预算是按天算的）
                return None
            part = self._review_chunks(run, info.bot_name) if live and (cards_ok or memory_ok or dm_ok) else []
            if origin is not None:
                for chunk in part:
                    chunk.origin, chunk.cards_ok, chunk.memory_ok, chunk.dm_ok, chunk.info = origin, cards_ok, memory_ok, dm_ok, info
            chunks += part
            flags = flags or (cards_ok, memory_ok, dm_ok)
        if not any(chunk.sids for chunk in chunks):
            if blocked:
                cs.last_review_at = now  # 有的服务器的预算用完了：隔一阵再看
                return None
            if held and upto is not None:
                return None  # 正在问的那一段也要压掉：等它（连同排进去的那批）整理完再记进度
            # 这里不整理（关掉了，或者是私密频道），或者这段时间没有群友说话：以后打开了也不回头整理这些旧的
            self._set_reviewed(cs, conv, last_id)
            if upto is None:
                cs.unreviewed_rounds = 0
            return None
        cards_ok, memory_ok, dm_ok = flags or (False, False, False)
        return _ReviewPlan(chunks, cards_ok, memory_ok, last_id, dm_ok, partial=blocked)

    def _review_chunks(self, turns: list[Any], bot_name: str) -> list[_ReviewChunk]:
        """把要整理的记录按字数分成几段，每段记下最后一条记录（整理完一段就记一段的进度）。"""
        limit = max(2000, self.ec.review_max_chars)
        chunks: list[_ReviewChunk] = []
        lines: list[str] = []
        sids: list[int] = []
        size = 0
        start = 0.0
        prev_id = 0  # 上一条记录（lines 不空时一定已经有了）
        ids: list[int] = []
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
                chunks.append(_ReviewChunk(lines, sids, prev_id, start, turn_ids=ids))
                lines, sids, size, ids = [], [], 0, []
            if not lines:
                start = t.created_at
            lines += kept
            sids += [sid for sid in turn_sids if sid not in sids]
            size += used
            prev_id = t.id
            ids.append(t.id)
        if lines:
            chunks.append(_ReviewChunk(lines, sids, prev_id, start, turn_ids=ids))
        return chunks

    async def _run_review(
        self,
        owner: Any,
        conv: Conversation,
        info: ChannelInfo,
        plan: _ReviewPlan,
        generation: int,
        *,
        before_compact: bool,
        frozen: bool = False,
    ) -> None:
        """一段一段问模型、照着改，每整理完一段就记下进度。连着失败几次就放弃这一批（免得一直花钱）。
        owner：频道的状态或者那份记录；连通组的记录每一段照那段的频道整理（见 _plan_review）。
        frozen：连通的频道连通时挑出来的它自己原来那份：不会压掉，暂时的问题、段数到了上限都不记成整理过，以后再整理。"""
        cs = self._cs(owner)
        if cs.key < 0:
            await self._run_shared_review(cs, conv, info, plan, generation, before_compact=before_compact)
            return
        cs.last_review_at = self.clock()
        for i, chunk in enumerate(plan.chunks[:MAX_REVIEW_CHUNKS]):
            if self.closing:
                return  # 关机了：剩下的下次启动再整理（压缩前挑出来的就不整理了）
            if not before_compact:
                if i and cs.review_held > plan.chunks[i - 1].last_turn:
                    return  # 问模型的时候压缩了旧记录：后面的原话已经挑出来排着队了（没压掉的下次再整理）
                cs.review_held = max(cs.review_held, chunk.last_turn)  # 这一段归这次整理：压缩时不再挑
                if chunk.cards_ok is None:
                    chunk.cards_ok, chunk.memory_ok, chunk.dm_ok = plan.cards_ok, plan.memory_ok, plan.dm_ok
                cs.review_asking = chunk  # 问的时候压缩了旧记录：压掉的那几条另外记下（断开时用，见 _park_asking）
            if chunk.sids:
                home = self._channel_state(chunk.origin if chunk.origin is not None else cs.key)
                cards_ok = plan.cards_ok if chunk.cards_ok is None else chunk.cards_ok
                memory_ok = plan.memory_ok if chunk.memory_ok is None else chunk.memory_ok
                dm_ok = plan.dm_ok if chunk.dm_ok is None else chunk.dm_ok
                where = chunk.info or info
                if home is None:
                    result = "ok"  # 那个频道找不到了：这段不整理
                else:
                    result = await self._review_chunk(home, where, chunk, cards_ok, memory_ok, generation, dm_ok, cs=cs)
                if result == "regroup" and cs.generation == generation and not self.closing and home is not None:
                    # 问的时候连组、退组了：照新的组马上再问一次（压缩前挑出来的这批没有下一次）
                    result = await self._review_chunk(home, where, chunk, cards_ok, memory_ok, generation, dm_ok, cs=cs)
                if result == "regroup":
                    result = "retry"  # 又变了：当成暂时的问题（平时的下次再整理；压缩前挑出来的那批只能跳过）
                if cs.generation != generation:
                    return  # 整理的时候这份记录被清空了
                if result != "ok":
                    cs.review_failures += 1  # 越失败等得越久
                    # 连不上、限流、余额不足这类暂时的问题：等恢复了再整理，不放弃这批聊天；
                    # 模型那边的问题（输出被截断、被拒、等太久没写完）连着几次就放弃，免得一直花钱
                    if result == "failed":
                        cs.review_bad += 1
                    if (before_compact and not frozen) or cs.review_bad >= MAX_REVIEW_FAILURES:
                        log.warning("频道 %s：整理记忆没成功，跳过这一批聊天", cs.key)
                        self._set_reviewed(cs, conv, plan.last_id)
                        for other in plan.chunks:
                            other.reviewed = True  # 放弃了：断开时也不再整理
                        cs.review_failures = cs.review_bad = 0
                        if not before_compact:
                            cs.unreviewed_rounds = 0
                    return
                cs.review_failures = cs.review_bad = 0
            self._set_reviewed(cs, conv, chunk.last_turn)
            chunk.reviewed = True  # 断开时只把没整理完的交给新的那份（整理进度可能因为别的原因已经记过了它）
        if len(plan.chunks) > MAX_REVIEW_CHUNKS:
            if before_compact and not frozen:
                self._set_reviewed(cs, conv, plan.last_id)  # 剩下的马上就压掉了
                for other in plan.chunks:
                    other.reviewed = True
            return  # 剩下的下次再整理
        if not before_compact:
            cs.unreviewed_rounds = 0

    async def _run_shared_review(
        self, cs: ConvState, conv: Conversation, info: ChannelInfo, plan: _ReviewPlan, generation: int, *, before_compact: bool
    ) -> None:
        """连通组的记录：每个频道的聊天分开整理（照那个频道的设置、记进那个服务器）。几段不是按先后连着的：整理完一段记一段
        （review_done，库里每条记录上也记着，断开、重启以后都不再整理），进度只在整批都整理完时才记到最后。
        - 一个频道的那段没整理成：这次先不问它后面的几段，别的频道的照常整理；连着失败几次就只放弃它那部分。压缩前挑出来的
          那批没整理成的只跳过那一段，也不算进失败的次数（那是安静时整理用来等一等、放弃的）。
        - 安静时整理一次最多问 MAX_SYNC_REVIEW_CHUNKS 段（跳过的不算）；正在问的那一段记在 review_asking：问的时候压缩了旧记录，
          压掉的连同这一段挑出来排着队（这一段问完了，那批就跳过它），问完这段就停。
        - 问的时候有频道断开了：它的那几段不整理，问到一半的那段也不照着改（拿回它自己那份里整理）。"""
        cs.last_review_at = self.clock()
        limit = len(plan.chunks) if before_compact else MAX_SYNC_REVIEW_CHUNKS
        failed: set[int | None] = set()  # 这次没整理成的频道：它后面的几段这次不问（下次再整理）
        given_up: set[int | None] = set()  # 连着失败、放弃了的频道：它的几段都记成整理过了
        stopped = False  # 这次有没整理到的：进度别记到最后
        counted = False  # 这次的失败算没算进等待的次数（一次整理只算一次，不按失败了几段算）
        asked = 0
        for chunk in plan.chunks:
            if self.closing:
                return
            if not before_compact and cs.review_cut:
                return  # 问模型的时候压缩了旧记录：压掉的那部分挑出来排着队了，剩下的下次照记录重新挑
            if chunk.origin in given_up:
                continue
            if chunk.origin in failed:
                stopped = True
                continue
            if before_compact and self._reviewed_already(chunk):
                continue  # 压缩时安静时整理正在问的那一段：已经问完了
            if before_compact:
                # 它那部分连着失败的那批聊天压掉了（这一批整理了它，成没成都不再算）：以后的另算
                cs.review_bad_by.pop(chunk.origin, None)
            since: int | None = None
            if chunk.origin is not None and not before_compact:
                joined = self.store.sync_joined_turns(cs.key)
                if chunk.origin not in joined or chunk.last_turn <= joined[chunk.origin]:
                    continue  # 问的时候这个频道断开了：它这部分拿回去了，在它自己那份里整理
                since = joined[chunk.origin]
            if chunk.sids and asked >= limit:
                stopped = True  # 这次问够了：剩下的下次再整理
                break
            if not before_compact:
                cs.review_asking = chunk  # 这一段归这次（压缩时连同它一起挑出来）
            if chunk.sids:
                asked += 1
                home = self._channel_state(chunk.origin if chunk.origin is not None else cs.key)
                cards_ok = plan.cards_ok if chunk.cards_ok is None else chunk.cards_ok
                memory_ok = plan.memory_ok if chunk.memory_ok is None else chunk.memory_ok
                dm_ok = plan.dm_ok if chunk.dm_ok is None else chunk.dm_ok
                where = chunk.info or info
                result = "ok"
                if home is not None:
                    result = await self._review_chunk(
                        home, where, chunk, cards_ok, memory_ok, generation, dm_ok, cs=cs, member_since=since
                    )
                    if result == "regroup" and cs.generation == generation and not self.closing:
                        result = await self._review_chunk(
                            home, where, chunk, cards_ok, memory_ok, generation, dm_ok, cs=cs, member_since=since
                        )
                    if result == "regroup":
                        result = "retry"
                if cs.generation != generation:
                    return  # 整理的时候这份记录被清空了（压缩前挑出来、没整理完的交给新的那份接着整理）
                if result == "left":
                    continue  # 问的时候这个频道断开了：没照着改，在它自己那份里整理
                if result != "ok":
                    if before_compact:
                        # 马上就压掉了，没有下一次：只跳过这一段，别的频道的照常整理（不算进安静时整理的失败次数）
                        log.warning("频道 %s：整理记忆没成功，跳过频道 %s 的这一段聊天", cs.key, chunk.origin)
                        continue
                    if not counted:
                        cs.review_failures += 1  # 越失败等得越久（这一次整理只算一次）
                        counted = True
                    if result == "failed":
                        cs.review_bad_by[chunk.origin] = cs.review_bad_by.get(chunk.origin, 0) + 1  # 每个频道各数各的
                    if cs.review_bad_by.get(chunk.origin, 0) < MAX_REVIEW_FAILURES:
                        failed.add(chunk.origin)  # 暂时的问题：它这部分下次再整理（别的频道的照常）
                        stopped = True
                        continue
                    log.warning("频道 %s：整理记忆没成功，跳过频道 %s 的这一批聊天", cs.key, chunk.origin)
                    if not failed:
                        # 放弃了这一批：等待从头算（这次别的频道也失败了的话别清零，它下次还要等一等）；清零以后，这次后面
                        # 再有失败的照样算一次
                        cs.review_failures = cs.review_bad = 0
                        counted = False
                    cs.review_bad_by.pop(chunk.origin, None)
                    given_up.add(chunk.origin)
                    for other in plan.chunks:
                        if other.origin == chunk.origin:
                            self._chunk_done(cs, other, before_compact)  # 只放弃这个频道的那部分
                    continue
                if not before_compact:
                    cs.review_bad_by.pop(chunk.origin, None)  # 这个频道这一段整理成了：它连着失败的次数从头数
                if not failed and not before_compact:
                    cs.review_failures = cs.review_bad = 0  # 有频道刚失败过的话别清零（它下次还要等一等）
            self._chunk_done(cs, chunk, before_compact)
        if stopped or plan.partial:
            if before_compact:
                self._set_reviewed(cs, conv, plan.last_id)  # 剩下的马上就压掉了
            return  # 剩下的下次再整理（整理过的记在 review_done、库里每条记录上）
        self._set_reviewed(cs, conv, plan.last_id)
        if not before_compact:
            cs.unreviewed_rounds = 0

    def _chunk_done(self, cs: ConvState, chunk: _ReviewChunk, before_compact: bool) -> None:
        """连通组的记录：这一段整理完了（或者放弃了）。也记到库里每条记录上（断开时拿回去、重启以后都认；压缩掉的那些
        已经不在库里了，只记在内存里）。"""
        chunk.reviewed = True
        cs.review_done.update(chunk.turn_ids)
        self.store.mark_reviewed(chunk.turn_ids)

    @staticmethod
    def _reviewed_already(chunk: _ReviewChunk) -> bool:
        """压缩前挑出来的这一段已经整理过了（它是安静时整理当时正在问的那一段里压掉的那几条：那一段后来问完了）。只认这一段
        自己：整理进度可能因为别的原因记过了它（压缩时别的部分不用整理、问到一半断开的那段），它却还没整理过。"""
        # 它的频道排着队的时候清空了记忆的，也当成不用整理了
        return chunk.dropped or chunk.reviewed or (chunk.part_of is not None and chunk.part_of.reviewed)

    async def _review_chunk(
        self,
        st: ChannelState,
        info: ChannelInfo,
        chunk: _ReviewChunk,
        cards_ok: bool,
        memory_ok: bool,
        generation: int,
        dm_ok: bool = False,
        *,
        cs: ConvState | None = None,
        member_since: int | None = None,
    ) -> str:
        """问模型这一段聊天里有什么值得长期记住的，照着改。st：这段聊天在哪个频道（照它的服务器记）；
        cs：整理的是哪份记录（不填就是 st 现在用的那份；连通组的记录里的一段，或者连通的频道自己原来那份）。
        member_since：连通组安静时整理：这一段的频道这次连进来时的那条记录（问的时候它断开了，就不照着改）。

        返回 "ok"；"retry"（连不上、限流、余额不足：之后再试）；"failed"（模型的输出不能用、等太久没写完）；
        "regroup"（问的时候这个服务器连组、退组了，它看到的长期记忆不是现在这份：照新的再问）；"left"（问的时候这一段的
        频道断开了：它这部分拿回去了，在它自己那份里整理）。"""
        cs = cs or st.conv
        # 编号 -> (人, 名字)；bot 和旧记录没有。连通组压缩前挑出来的那批先记下了（组散了，库里就没了）
        authors = chunk.authors if chunk.authors is not None else self.store.load_authors(cs.key, chunk.sids)
        guild = st.guild_id  # 花费记在这个服务器
        key = self._memory_key(guild)
        scope = key[0]  # 长期记忆记在哪（连成一组的服务器共用组里那份）
        card_scope = self._card_scope(guild)  # 人设卡记在哪（所有服务器共用一张时是 SHARED，不然同上）
        people: dict[int, tuple[str, list[int]]] = {}
        if cards_ok:
            assert self.cards is not None
            for sid in chunk.sids:
                who = authors.get(sid)
                if who is None:
                    continue
                deleted = self.cards.forgotten_at(card_scope, who[0])
                if deleted is not None and deleted >= chunk.start:
                    continue  # 这段聊天之后他的卡被删过：不拿这些旧话重新写他的卡
                entry = people.setdefault(who[0], (who[1], []))
                if sid not in entry[1]:
                    entry[1].append(sid)
        do_cards = cards_ok and bool(people)
        dm_user = info.dm_user if dm_ok and self.memory is not None else None
        if dm_user is not None:
            cleared = self.memory.dm_cleared_at(dm_user) if self.memory is not None else None
            if cleared is not None and cleared >= chunk.start:
                dm_user = None  # 这段聊天之后他清空过私信记忆：不拿这些旧话重新写
        if not do_cards and not memory_ok and dm_user is None:
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
                card = self.cards.get(card_scope, uid)
                versions[uid] = card.updated_at if card is not None else None
                shown = one_line(name or (card.name if card else ""), 32)
                if names.count(name) > 1:  # 同名的两个人：靠消息编号分开（名字太长时先截短，免得把后缀截掉）
                    seen_names[name] = numbers[uid] = seen_names.get(name, 0) + 1
                    shown += f"（同名的第 {numbers[uid]} 位）"
                listed.append((shown, own, card.text if card else None))
        memories = None
        foreign: dict[int, str] = {}  # 组里别的服务器记的长期记忆：这段聊天看不到那边的事，只给看、不让改
        if memory_ok:
            assert self.memory is not None
            entries = self.memory.entries(scope)
            memories = [(m.no, m.text) for m in entries]
            foreign = self._foreign_memories(scope, guild, entries)
        dm_list = None
        if dm_user is not None:
            assert self.memory is not None
            dm_list = [(e.no, e.text, e.sensitive) for e in self.memory.dm_entries(dm_user)]
        system = build_review_prompt(
            name=neutralize(one_line(info.bot_name, 40)),
            cards=do_cards,
            memory=memory_ok,
            card_max=self.ec.card_max_chars,
            entry_max=self.ec.memory_entry_chars,
            count=len(memories or []),
            max_entries=self.ec.memory_max_entries,
            dm=st.private,
            dm_memory=dm_list is not None,
            dm_count=len(dm_list or []),
            dm_max_entries=self.ec.dm_memory_max_entries,
            dm_entry_max=self.ec.dm_memory_entry_chars,
            # 私信里不说“这段在服务器里”（私信的规矩更严：敏感的、要保密的都不写进组里共用的记忆）
            group=neutralize(one_line(info.guild_name, 40)) if scope != guild and not st.private else None,
            foreign=len(foreign),
            shared=neutralize(one_line(info.guild_name, 40)) if self._cards_shared() and not st.private else None,
        )
        prompt = [
            {"role": "system", "content": system},
            {"role": "user", "content": render_review_input(listed, memories, chunk.lines, dm_list, foreign)},
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
        if cs.generation != generation:
            return "retry"
        if chunk.dropped:
            return "ok"  # 问的时候这一段的频道清空了记忆：不照着改
        if member_since is not None and self.store.sync_joined_turns(cs.key).get(chunk.origin) != member_since:
            return "left"
        if self._memory_key(st.guild_id) != key:
            # 问模型的时候这个服务器连组、退组了：它看到的不是现在这份（长期记忆的编号对不上），照新的马上再问一次
            log.info("频道 %s：整理记忆的时候服务器组变了，照现在的再整理一次", st.channel_id)
            return "regroup"
        parsed = parse_review(completion.content)
        if parsed is None and completion.finish_reason == "length":
            log.warning("频道 %s：整理记忆的输出太长被截断了，这次没整理成", st.channel_id)
            return "failed"
        if parsed is None:
            log.warning("频道 %s：整理记忆的输出不是合法 JSON", st.channel_id)
            return "ok"  # 当成没东西要记，不重试（免得一直花钱）
        updates, ops, dm_ops = parsed
        heard = _member_blocks(chunk.lines)
        secrets: list[str] = []  # 私信里他敏感的那些事：人设卡、长期记忆服务器里的人看得到，说到这些的不写
        if st.private and info.dm_user is not None and self.memory is not None:
            secrets = [e.text for e in self.memory.dm_entries(info.dm_user) if e.sensitive]
        if dm_user is not None:
            secrets += [op.text for op in dm_ops if op.op in ("add", "edit") and op.text and op.sensitive is not False]  # 群友自己说的（一条消息连同续行算一整段）
        cards_changed = 0
        if do_cards:
            assert self.cards is not None
            done: set[int] = set()
            for to, text, name in updates:
                who = authors.get(to) if to in chunk.sids else None
                if who is None or who[0] not in people or who[0] in done:
                    continue  # 只能改这段记录里说过话的人自己的卡
                if cs.msg_by_sid.get(to) in cs.deleted:
                    continue  # 整理的时候这条被删了
                if name is not None and not _same_name(name, people[who[0]][0], numbers.get(who[0])):
                    log.info("频道 %s：整理记忆时 #%s 和名字「%s」对不上，没改", st.channel_id, to, one_line(name, 32))
                    continue
                text = clean_card(text)
                if not text or len(text) > self.ec.card_max_chars:
                    continue
                # 别把别人的卡抄进这个人的卡（本人用 /ds-rp 就看得到）；本来就在他自己卡上的不算
                if self._copies_card(text, self.cards.others(card_scope, who[0]), self.cards.own_texts(card_scope, who[0]), heard):
                    log.info("频道 %s：整理记忆时 #%s 的新人设卡照抄了别人的卡，没改", st.channel_id, to)  # 不写内容
                    continue
                deleted = self.cards.forgotten_at(card_scope, who[0])
                if deleted is not None and deleted >= chunk.start:
                    continue  # 问模型的时候被删掉了
                current = self.cards.get(card_scope, who[0])
                if secrets and reveals(text, secrets, current.text if current is not None else ""):
                    log.info("频道 %s：整理私信时 #%s 的新人设卡说到了敏感的事，没改", st.channel_id, to)  # 不写内容
                    continue
                done.add(who[0])
                name = who[1] if current is None or self._names_here(st) else current.name  # 卡上的名字别跟着别处的昵称变
                if self.cards.put(card_scope, who[0], name, text, via="review", now=now, expect=versions.get(who[0])):
                    cards_changed += 1
                    self._note_seen(st, who[0], who[1])
                if len(done) >= MAX_REVIEW_CARDS:
                    break
        memory_changed = 0
        if memory_ok:
            listed_cards = [card for _, _, card in listed or [] if card]  # 给它看过的人设卡
            wanted = ops[:MAX_REVIEW_MEMORY_OPS]
            kept = self._screen_memory(scope, wanted, lambda op: op.text, listed_cards, heard)
            if len(kept) != len(wanted):
                log.info("频道 %s：整理记忆时要记的照抄了人设卡的原文，没记", st.channel_id)  # 不写内容
            if secrets:
                before = len(kept)
                kept = [op for op in kept if not (op.text and reveals(op.text, secrets))]
                if len(kept) != before:
                    log.info("频道 %s：整理私信时要记进长期记忆的说到了敏感的事，没记", st.channel_id)  # 不写内容
            # 组里别的服务器记的：这里看不到那边的聊天，不知道还有没有用（要删让那边整理、或者管理员删）。
            # 按问之前和现在的都算（问的时候可能有服务器连进来），没给它看过的编号也不动
            shown = {no for no, _ in memories or []}
            blocked = set(foreign) | set(self._foreign_memories(scope, guild))
            # 想把这里的并进别的服务器那条（那条不让改）：这里要删的也先不删，免得内容哪儿都没了
            merging = any(op.op == "edit" and op.no in blocked for op in kept)
            for op in kept:
                if op.op in ("edit", "delete") and (op.no in blocked or op.no not in shown or (merging and op.op == "delete")):
                    log.info("频道 %s：整理记忆时想%s第 %s 条（组里别的服务器记的，或者没给它看过），没照做", st.channel_id, "改" if op.op == "edit" else "删", op.no)
                    continue
                if self._memory_op(scope, op.op, op.no, op.text, via="review", now=now, origin=guild):
                    memory_changed += 1
        dm_changed = 0
        if dm_user is not None:
            assert self.memory is not None
            cleared = self.memory.dm_cleared_at(dm_user)
            if cleared is None or cleared < chunk.start:  # 问模型的时候他清空了：不写
                wanted_dm = [(op.op, op.no, op.text, op.sensitive) for op in dm_ops[:MAX_REVIEW_MEMORY_OPS]]
                done, _ = self._apply_dm_ops(dm_user, wanted_dm, via="review", now=now)
                dm_changed = sum(done)
        log.info(
            "频道 %s：整理了记忆（%d 行聊天）：人设卡改了 %d 张，长期记忆改了 %d 处%s | $%.5f",
            st.channel_id,
            len(chunk.lines),
            cards_changed,
            memory_changed,
            f"，私信记忆改了 {dm_changed} 处" if dm_user is not None else "",
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
        """这个频道可以翻记录的别的频道（连成一组的话，也有组里别的服务器的）：这个服务器的在前，组里别的服务器按加入的
        先后；同一个服务器里普通频道在前，帖子在后，各自按名字排。"""
        out = []
        order = self._group_guilds(guild_id)
        sync = self.store.sync_of(channel_id)
        linked = set(self.store.sync_members(sync)) if sync is not None else set()  # 连通的频道本来就在记录里，不用翻
        for g in order:
            for cid in self.store.conversation_channels(g):
                if cid == channel_id or cid in linked:
                    continue
                place = self.transport.locate(cid)
                if place is not None and place.guild_id == g and self.shares_out(cid, place):
                    out.append((cid, place))
        out.sort(key=lambda item: (order.index(item[1].guild_id), item[1].parent_id is not None, item[1].name, item[0]))
        return out

    def _display(self, place: ChannelPlace) -> str:
        """给模型看的频道名。帖子写成“帖子「标题」（在 #所在频道）”，群友起个和频道一样的帖子名也冒充不了频道。"""
        name = neutralize(one_line(place.name, 40))
        if place.parent_id is None:
            return f"#{name}"
        parent = self.transport.locate(place.parent_id)
        where = f"（在 #{neutralize(one_line(parent.name, 30))}）" if parent is not None else ""
        return f"帖子「{name}」{where}"

    def _source_name(self, channel_id: int, place: ChannelPlace, guild_id: int) -> str:
        """能翻记录的频道给模型看的名字：组里别的服务器的，后面写上是哪个服务器。"""
        name = self._display(place)
        if place.guild_id != guild_id:
            server = neutralize(one_line(self.transport.channel_info(channel_id).guild_name, SERVER_NAME_CHARS))
            name += f"（「{server}」服务器）"
        return name

    def _find_source(
        self, sources: list[tuple[int, ChannelPlace]], wanted: str, guild_id: int, channel_id: int | None = None
    ) -> tuple[int, ChannelPlace] | list[tuple[int, ChannelPlace]] | None:
        """按名字找要翻的频道（guild_id：现在在哪个服务器）。找到返回它；分不出是哪个（同名的帖子，组里几个服务器都有
        这个名字的频道）时返回候选列表；找不到返回 None。
        写明了是哪个服务器的（“#大厅（「某某」服务器）”“「某某」服务器的 #大厅”）：只在那个服务器的频道里找，
        那边没有就是找不到（不会退回这个服务器同名的频道，免得把这里的聊天当成那边的）。"""
        raw = " ".join(wanted.split()).strip()
        named = _SERVER_PREFIX.match(raw) or _SERVER_SUFFIX.match(raw)
        if named:
            # 先照名单里的写法原样对（“#大厅（「某某」服务器）”：只有别的服务器的频道才这样列）：组里几个服务器同名、
            # 或者名字截短后一样时，也认得出是名单里的哪一个（不然会落到这里同名的频道上）
            def bare(text: str) -> str:
                return " ".join(text.split()).lstrip("#＃")

            label = bare(f"{named['name'].strip()}（「{named['server'].strip()}」服务器）")
            exact = [s for s in sources if s[1].guild_id != guild_id and bare(self._source_name(s[0], s[1], guild_id)) == label]
            chans = [s for s in exact if s[1].parent_id is None]
            if len(exact) == 1 or (chans and len({s[1].guild_id for s in chans}) == 1):
                return (chans or exact)[0]  # 同一个服务器里同名的频道：和下面一样认第一个
            if exact:
                return chans or exact  # 几个服务器都有，或者是同名的帖子：不猜
            # 这里只认给它看的写法（见 _here_named）：别的服务器截短的名字丢了“…”，碰巧和这里前面一样，也不当成这里
            servers = self._guilds_named(named["server"], {p.guild_id for _, p in sources} - {guild_id})
            if self._here_named(named["server"], guild_id, channel_id):
                servers.add(guild_id)
            if not servers:
                # 不是组里哪个服务器的名字：可能频道、帖子自己就叫这个（名字里正好带着“（「X」服务器）”），先认频道。
                # 写的是组里的服务器时不走这里：免得群友起个“大厅（「别的服」服务器）”的帖子就冒充了那边的频道
                bare = raw.lstrip("#＃")
                literal = [s for s in sources if bare in (s[1].name, neutralize(s[1].name))]
                literal = [s for s in literal if s[1].parent_id is None] or literal
                return literal[0] if len(literal) == 1 else None
            sources = [s for s in sources if s[1].guild_id in servers]
            raw = named["name"].strip()
        key = _norm_channel(raw).rstrip("…")
        if not key or not sources:
            return None

        def shown(cid: int, place: ChannelPlace) -> tuple[str, ...]:
            return (place.name, self._display(place), self._source_name(cid, place, guild_id), neutralize(place.name))

        def names(cid: int, place: ChannelPlace, labeled: bool = False) -> set[str]:
            out = {_norm_channel(place.name), _norm_channel(self._display(place)), _norm_channel(neutralize(place.name))}
            if labeled:  # 模糊比对时不带服务器名：免得写个服务器名就对上了那个服务器的随便哪个频道
                out.add(_norm_channel(self._source_name(cid, place, guild_id)))
            return out

        tests = (
            lambda c, p: raw in shown(c, p) or raw.lstrip("#＃") == p.name,  # 原样一致
            lambda c, p: key in names(c, p, labeled=True),
            lambda c, p: any(n.startswith(key) for n in names(c, p)),  # 名单里的长名字被截断过（结尾是“…”）
            lambda c, p: any(key in n or (len(n) >= 2 and n in key) for n in names(c, p)),  # “游戏频道”找“游戏”之类
        )
        for test in tests:
            found = [s for s in sources if test(*s)]
            channels = [s for s in found if s[1].parent_id is None]
            if channels:
                here = [s for s in channels if s[1].guild_id == guild_id]
                if here or len({s[1].guild_id for s in channels}) == 1:
                    return (here or channels)[0]  # 频道是管理员建的，同名时优先频道；先认这个服务器自己的
                return channels  # 组里别的几个服务器都有这个名字的频道：不猜
            if len(found) == 1:
                return found[0]
            if found:
                return found  # 好几个帖子都对得上：不猜，免得被群友起的同名帖子冒充
        return None

    def _other_server_named(self, guild_id: int, channel_id: int | None = None) -> Callable[[str], bool]:
        """发出去的话里写明“「X」服务器”的频道名，要不要别换成这里的频道链接（会指到这里同名的频道；不链总比链错好）：
        - X 是这里（见 _here_named：全名、中和过、截到 60 字的；它自己截短、带着“…”的，或者截到 40 字的也算，只是它看得到
          别的服务器的名字时，bot 知道的别的服务器也对得上就不算）：换。和给它看过的别的服务器同名（组里的；这里开了跨服务器
          时，开了跨服务器的）就不换：分不出是哪个。
        - X 是 bot 知道的别的服务器（它在的、组里的、最近有人跟它聊过的）：不换。
        - 认不出是哪个服务器（游戏里的“生存服务器”，或者名字写错了、自己截短又没带“…”）：它看得到别的服务器的名字时（见
          _sees_other_servers）就不换；不然照常换（它没见过别的服务器，写的多半就是这里）。"""

        def other(server: str) -> bool:
            group = self._group_guilds(guild_id)
            cross = bool(self.settings.view(guild_id).cross_server)
            known = self._known_others(guild_id)
            if self._here_named(server, guild_id, channel_id):
                shown = set(group[1:]) | ({g for g in known if self.settings.view(g).cross_server} if cross else set())
                if channel_id is not None:
                    shown |= self._sync_guilds(channel_id) - {guild_id}  # 连通的频道所在的别的服务器（记录里写着名字）
                return bool(self._guilds_named(server, shown))
            return bool(self._guilds_named(server, known)) or self._sees_other_servers(guild_id, channel_id)

        return other

    def _known_server_named(self, guild_id: int, channel_id: int | None = None) -> Callable[[str], bool]:
        """只写了“「X」#频道”（没写“服务器”）时：X 是 bot 知道的别的服务器、又认不出是这里，就不换成这里的频道链接。
        「」里是别的字（“「必看」#公告”）照常换。"""

        def known(server: str) -> bool:
            return bool(self._guilds_named(server, self._known_others(guild_id))) and not self._here_named(server, guild_id, channel_id)

        return known

    def _sees_other_servers(self, guild_id: int, channel_id: int | None = None) -> bool:
        """这里的提示词里会不会有别的服务器的名字：连成了组（组里的名单、能翻的频道）、开了跨服务器（近况），或者共用人设卡
        （卡上会写着在别的服务器的事）；这个频道和别的服务器的频道连通了（记录里每段写着是哪个服务器的）。
        以前的聊天记录里也会留着。"""
        return (
            len(self._group_guilds(guild_id)) > 1
            or bool(self.settings.view(guild_id).cross_server)
            or self._cards_shared()
            or (channel_id is not None and bool(self._sync_guilds(channel_id) - {guild_id}))
        )

    def _guild_ids(self) -> list[int]:
        """bot 现在在的服务器。"""
        ids = getattr(self.transport, "guild_ids", None)
        return list(ids()) if ids is not None else []

    def _known_others(self, guild_id: int) -> set[int]:
        """bot 知道的别的服务器：它在的、组里的、最近有人跟它聊过的。"""
        return (set(self._group_guilds(guild_id)[1:]) | {g for g, _ in self.interactions} | set(self._guild_ids())) - {guild_id}

    def _here_named(self, server: str, guild_id: int, channel_id: int | None = None) -> bool:
        """模型写的服务器名是不是这个服务器：全名，或者提示词里给它看的（中和过、截到 60 字的；带不带结尾的“…”都认）。
        别的截法（它自己截的、带着“…”，或者整理记忆时看到的 40 字）也认；只是它看得到别的服务器的名字时（见
        _sees_other_servers），bot 知道的别的服务器也对得上就不算：组里别的服务器给它看的名字截到 40 字，前面一样长的两个
        服务器截完是一样的，不能当成这里。正好是 bot 知道的别的服务器的全名的，也不算。"""
        full = self._guild_name(guild_id)
        want = _norm_channel(server)
        cut = want.endswith("…")
        want = want.rstrip("…")
        if not want:
            return False
        shown = (full, neutralize(full), one_line(full, 60), neutralize(one_line(full, 60)))
        if want in {_norm_channel(name).rstrip("…") for name in shown}:
            return True
        forms = {_norm_channel(full), _norm_channel(neutralize(full))}
        if want == _norm_channel(self._server_label(guild_id)).rstrip("…") or (cut and any(f.startswith(want) for f in forms)):
            known = self._known_others(guild_id)
            if not cut and any(want in {_norm_channel(n), _norm_channel(neutralize(n))} for n in map(self._guild_name, known)):
                return False  # 这里截到 40 字、丢了“…”，正好是别的服务器的全名：算那边的
            return not (self._sees_other_servers(guild_id, channel_id) and self._guilds_named(server, known))
        return False

    def _guilds_named(self, server: str, guilds: Iterable[int]) -> set[int]:
        """模型写的服务器名对得上哪几个服务器：全名，或者给它看的截短的名字（带不带结尾的“…”都认）。"""
        want = _norm_channel(server)
        cut = want.endswith("…")
        want = want.rstrip("…")
        out: set[int] = set()
        for g in guilds:
            full = self._guild_name(g)
            forms = {_norm_channel(full), _norm_channel(neutralize(full)), _norm_channel(self._server_label(g)).rstrip("…")}
            if want and (want in forms or (cut and any(f.startswith(want) for f in forms))):
                out.add(g)
        return out

    def _system_prompt(self, st: ChannelState, view: View, conv: Conversation, info: ChannelInfo) -> str:
        # 频道名单不放这里：它会变（新频道、帖子归档），放在 system prompt 里每变一次整段缓存都失效。
        # 表情、贴图很少变，放这里（名单已经排好序，不变就不影响缓存）
        emojis = info.emojis[: max(0, self.ec.emoji_list_limit)]
        stickers = [_sticker_label(n) for n in info.stickers[: max(0, self.ec.sticker_list_limit)]]
        meme_names = self.memes.names(st.guild_id) if self._memes_on(view) and self.memes is not None else []
        memes = [_sticker_label(n) for n in meme_names[: max(0, self.ec.meme_list_limit)]]
        # 有人设卡的人、长期记忆：很少变，放这里（排好序、编号固定，不变就不影响缓存）。连成一组的服务器用组里共用的那份
        st.memory_key = self._memory_key(st.guild_id)
        scope = st.memory_key[0]
        card_names: list[str] | None = None
        more_cards = elsewhere = 0
        if self._cards_on(view):
            assert self.cards is not None
            card_scope = self._card_scope(st.guild_id)
            if card_scope != st.guild_id:
                # 卡里有别的服务器的人（连成一组、所有服务器共用一张）：只列在这里出现过的人、用他在这里的名字
                # （别的服务器同名的人不在这里）；别的服务器的只说还有
                owners = self.cards.named_here(card_scope, st.guild_id)
                names_here = {n for _, n in owners}
                here = {uid for uid, _ in owners}
                partner = info.dm_user if st.private else None
                if partner is not None and partner not in here and info.dm_with and self.cards.get(card_scope, partner) is not None:
                    # 私信里：对面这个人的卡就附在聊天记录里，列上他（他在这个服务器可能只在私信里出现过，没记名字）
                    names_here.add(info.dm_with)
                    here.add(partner)
                all_names = sorted(names_here)
                near = self._card_near(st.guild_id)
                if near is None:
                    elsewhere = max(0, self.cards.count(card_scope) - len(here))
                else:
                    # 所有服务器共用一张卡：只算在这里只在私信里出现过的、组里别的服务器的人（别处的人在这里查不到）
                    elsewhere = len(self.cards.owners_near(card_scope, st.guild_id, near) - here)
            else:
                all_names = self.cards.names(card_scope)
                if st.private and info.dm_user is not None and info.dm_with:
                    mine = self.cards.get(card_scope, info.dm_user)
                    if mine is not None and not mine.name:
                        # 卡上没记名字（改回每个服务器一张时，他在这里只在私信里出现过）：私信里照样列上他（他的卡附着呢）
                        all_names = sorted({*all_names, info.dm_with})
            card_names = [_sticker_label(n) for n in all_names[: max(0, self.ec.card_list_limit)]]
            more_cards = len(all_names) - len(card_names)
        memories: list[tuple[int, str]] | None = None
        foreign = 0  # 组里别的服务器记的几条（记满了时别为了腾地方删它们）
        memory_more = 0  # 太多了没列出来的几条
        if self._memory_on(view):
            assert self.memory is not None
            entries = self.memory.entries(scope)
            # 记得很多时只列最近改过的那些（最多 memory_prompt_chars 字）；没列出来的它不能照编号改、删，整理记忆时全都看得到
            picked = _newest(entries, max(0, self.ec.memory_prompt_chars), len(entries))
            memory_more = len(entries) - len(picked)
            memories = [(m.no, neutralize_record(one_line(m.text, self.ec.memory_entry_chars + 20))) for m in picked]
            foreign = len(self._foreign_memories(scope, st.guild_id, entries))
        st.memory_shown = frozenset(no for no, _ in memories or [])
        group = [self._server_label(g) for g in self._group_guilds(st.guild_id)[1:]] or None
        dm_memories, dm_more = self._dm_memory_entries(st, view, info)
        sync = sync_here = past = None
        if self._synced(st):
            sync = [self._sync_label(c) for c in self.store.sync_members(st.conv.key) if c != st.channel_id]
            sync_here = self._sync_label(st.channel_id)
            past = self._past_digest(st, info.bot_name)
        return build_system_prompt(
            guild=neutralize(one_line(info.guild_name, 60)),
            channel=neutralize(one_line(info.channel_name, 60)),
            name=neutralize(one_line(info.bot_name, 40)),
            persona=view.persona,
            summary=conv.summary,
            search=self._can_search(view),
            auto_search=self._auto_search_on(st, view),
            share=bool(view.share_memory) and not self._sync_foreign(st),
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
            cards_elsewhere=elsewhere,
            cards_shared=self._cards_shared(),
            memories=memories,
            memory_max=self.ec.memory_max_entries,
            card_max=self.ec.card_max_chars,
            entry_max=self.ec.memory_entry_chars,
            music=self.music is not None and not st.private,
            remind=bool(view.reminders),
            dm_with=neutralize(one_line(info.dm_with, 32)) if info.dm_with is not None else None,
            desktop=[neutralize(one_line(n, 40)) for n in self.desktop.app_names()] if self._desktop_here(st, view, info) else None,
            dm_memories=dm_memories,
            dm_memory_max=self.ec.dm_memory_max_entries,
            dm_entry_max=self.ec.dm_memory_entry_chars,
            dm_memory_more=dm_more,
            group=group,
            memory_foreign=foreign,
            memory_more=memory_more,
            shared_notes=self._shared_notes(st, view) if self.ec.dm_shared_prompt_chars > 0 else [],
            sync=sync,
            sync_here=sync_here,
            sync_past=past,
            sync_foreign=self._sync_foreign(st),
            sync_cards=self._cards_shared() or not self._cards_on(view),
        )

    def _dm_memory_entries(
        self, st: ChannelState, view: View, info: ChannelInfo
    ) -> tuple[list[tuple[int, str, bool]] | None, int]:
        """私信里给它看的私信记忆（敏感的也在，只在私信里）和没列出来的条数。不是私信、没开私信记忆是 (None, 0)。
        太多了只列最近改过的那些（最多 dm_memory_prompt_chars 字），按编号排；整理记忆时还是全部都看得到。"""
        if not st.private or info.dm_user is None or not self._dm_memory_on(view):
            return None, 0
        assert self.memory is not None
        limit = self.ec.dm_memory_entry_chars + 20
        entries = self.memory.dm_entries(info.dm_user)
        picked = _newest(entries, max(0, self.ec.dm_memory_prompt_chars), len(entries))
        shown = [
            (e.no, neutralize_record(one_line(e.text, limit)) + ("〔可共享〕" if e.shared_as and not e.sensitive else ""), e.sensitive)
            for e in picked
        ]
        return shown, len(entries) - len(picked)

    def _shared_notes(self, st: ChannelState, view: View) -> list[tuple[str, str]]:
        """服务器的频道里：群友自己标成“可共享”的私信记忆 [(名字, 内容)]。只列私信挂在这个服务器（或者和它连成一组的
        服务器）的人的；没开私信记忆、连通到别的服务器的频道不列。太多了只列最近改过的（dm_shared_prompt_chars 字）。"""
        if st.private or not self._dm_memory_on(view) or self._sync_foreign(st):
            return []
        assert self.memory is not None
        here = set(self._group_guilds(st.guild_id))
        homes: dict[int, bool] = {}
        out: list[tuple[str, str]] = []
        used = 0
        limit = self.ec.dm_memory_entry_chars + 20
        for user_id, e in self.memory.dm_shared():
            if user_id not in homes:
                home = self.store.dm_home(user_id)
                homes[user_id] = home is not None and home[0] in here
            if not homes[user_id]:
                continue
            if out and used + len(e.text) > max(0, self.ec.dm_shared_prompt_chars):
                break
            used += len(e.text)
            out.append((neutralize(one_line(e.shared_as or "某人", 32)), neutralize_record(one_line(e.text, limit))))
        return sorted(out)  # 排好序：放在 system prompt 里，顺序一乱缓存就失效

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
        """这一级不单独设置时会用的人设（和 /dsadmin 人设 的“上一级”一致）。"""
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
        if self._sync_foreign(st):
            return RECALL_SYNC_OFF  # 翻到的会进几个服务器共用的记录
        if not view.share_memory:
            return RECALL_NO_SHARE.format(channel=shown)
        sources = self.shared_sources(st.guild_id, st.channel_id)
        match = self._find_source(sources, action.channel or "", st.guild_id, st.channel_id)
        if match is None:
            names = "、".join(self._source_name(c, p, st.guild_id) for c, p in sources[:10]) or "没有"
            return RECALL_MISS.format(channel=shown, names=names)
        if isinstance(match, list):
            names = "、".join(self._source_name(c, p, st.guild_id) for c, p in match[:10])
            return RECALL_AMBIGUOUS.format(channel=shown, names=names)
        channel_id, place = match
        turns, summary = self._channel_history(channel_id, place)
        lines = history_lines([(t.role, t.content) for t in turns], info.bot_name)
        lines = _pick_lines(lines, action.query, self.ec.recall_max_chars)
        return render_recall(self._source_name(channel_id, place, st.guild_id), summary, lines, action.query)

    def _channel_history(self, channel_id: int, place: ChannelPlace) -> tuple[list[Turn], str | None]:
        """翻记录时那个频道的记录和摘要。连通的频道：它自己原来那份（连通之前的）在前，后面接上共用记录里它自己的那部分
        （别的频道说的不给翻：那些频道没开给这里翻）；摘要只给它自己原来那份的（共用记录的摘要里有别的频道的事）。"""
        own = self.convs.get(channel_id)
        conv = own.conversation if own is not None and own.conversation is not None else None
        if conv is None:
            conv = self.store.load_conversation(channel_id, place.guild_id)
        turns = list(conv.turns)
        sync = self.store.sync_of(channel_id)
        if sync is not None:
            shared = self.convs.get(sync)
            whole = shared.conversation if shared is not None and shared.conversation is not None else None
            if whole is None:
                whole = self.store.load_conversation(sync, 0)
            turns += [t for t in whole.turns if t.origin == channel_id]
        return turns, conv.summary

    def _shared_context(
        self, st: ChannelState, view: View, triggers: list[IncomingMessage], now: float
    ) -> tuple[list[str], dict[int, int], tuple[str, ...] | None]:
        """被叫到时放在这一轮前面的跨频道内容：能翻记录的频道名单（变了才写），以及叫它的人最近在别的频道跟它聊过的话。

        返回 (文字行, 带进来的对话序号, 这次写进去的名单)。
        """
        lines: list[str] = []
        listed: tuple[str, ...] | None = None
        if self._sync_foreign(st):
            return lines, {}, listed  # 和别的服务器的频道连通了：别处的近况、能翻的频道名单都不放进几个服务器共用的记录
        sources = self.shared_sources(st.guild_id, st.channel_id) if view.share_memory else []
        if view.share_memory:
            names = tuple(self._source_name(c, p, st.guild_id) for c, p in sources[: self.ec.share_list_limit])
            if names != st.listed:
                listed = names
                lines += render_channel_list(list(names), len(sources) - len(names))
        ttl = self.ec.share_recent_hours * 3600
        marks: dict[int, int] = {}
        allowed = {cid: p for cid, p in sources}
        linked = set(self.store.sync_members(st.conv.key)) if self._synced(st) else set()  # 本来就在共用的记录里
        # 连成一组的服务器当成一个服务器：组里几个服务器的都算“这里”的
        here = self._group_guilds(st.guild_id)
        # 私信和服务器的频道之间，同一个人自己跟它的对话不管开没开记忆共享都带：
        # 在频道里叫它时带上他刚才的私信，私信里带上他刚才在公开频道里跟它聊的
        for key in {(g, m.author_id) for g in here for m in triggers if not m.is_bot}:
            for item in self.interactions.get(key, ()):
                if item.channel_id not in allowed and item.channel_id != st.channel_id:
                    place = self.transport.locate(item.channel_id)
                    if place is not None and place.guild_id in here and (place.public if st.private else place.dm):
                        if place.dm and self._synced(st):
                            continue  # 连通的频道：私信里聊的不带进共用的记录
                        allowed[item.channel_id] = place
        # 跨服务器：开了“跨服务器”的服务器之间，同一个人跟它的对话跟着他走（私信看它挂的那个服务器开没开：
        # 私信里聊的会记进那个服务器的人设卡、长期记忆，那边没开就不带）
        cross = bool(view.cross_server)
        if cross and self._synced(st):
            # 连通了组里别的服务器的频道：共用的记录那几个服务器都看得到，都开了“跨服务器”才带别的服务器的近况
            cross = all(self.settings.view(g).cross_server for g in self._sync_guilds(st.channel_id))
        crossed: set[int] = set()  # 只是因为“跨服务器”才带得上的频道：说了要保密的那段不带（组里的服务器也一样）
        for m in triggers:
            if m.is_bot or m.author_id in marks:
                continue
            since = st.digest_seen.get(m.author_id, 0)
            pool = [item for g in here for item in self.interactions.get((g, m.author_id), ())]
            if cross:
                before = set(allowed)
                pool += self._elsewhere(here, m.author_id, allowed)
                crossed |= set(allowed) - before
            pool.sort(key=lambda item: item.seq)
            fresh = [
                item
                for item in pool
                if item.channel_id in allowed
                and item.channel_id not in linked
                and item.seq > since
                and now - item.at <= ttl
                and not (allowed[item.channel_id].dm and (_secret(item.lines) or self._dm_sensitive(m.author_id, item.lines[:1])))
                and not (item.channel_id in crossed and _secret(item.lines))
            ]
            if not fresh:
                continue
            marks[m.author_id] = max(item.seq for item in fresh)  # 只带最近两次；更早的就不带了
            for item in fresh[-2:]:
                place = allowed[item.channel_id]
                said = item.lines
                if place.dm:
                    where = "私信里"
                    said = item.lines[:1]  # 只带他自己的话：它在私信里的回话可能用到了私信记忆、别的服务器的事
                elif place.guild_id != st.guild_id:
                    server = neutralize(one_line(self.transport.channel_info(item.channel_id).guild_name, SERVER_NAME_CHARS))
                    where = f"「{server}」服务器的 {self._display(place)} "
                else:
                    where = f" {self._display(place)} "
                block = [f"（{neutralize(one_line(m.author_name, 32))} {_ago(now - item.at)}在{where}跟你聊过："]
                block += ["  " + neutralize(line) for line in said]
                block[-1] += "）"
                lines += block
        return lines, marks, listed

    def _dm_sensitive(self, user_id: int, lines: list[str]) -> bool:
        """私信里的这几句说到了他私信记忆里标成敏感的事。"""
        if self.memory is None:
            return False
        secrets = [e.text for e in self.memory.dm_entries(user_id) if e.sensitive]
        return bool(secrets) and any(reveals(line, secrets) for line in lines)

    def _elsewhere(self, here: list[int], author_id: int, allowed: dict[int, ChannelPlace]) -> list[Interaction]:
        """这个人最近在别的服务器跟它的对话（here：这里在最前面，后面是连成一组的服务器）：那个服务器开了“跨服务器”，
        而且是公开频道里的（私密频道、私信不带），说了要保密的不带。带得上的频道顺便记进 allowed。
        组里别的服务器的对话本来就在要带的里面（没开记忆共享的频道也照样跨服务器带），这里只记进 allowed、不再返回。"""
        out: list[Interaction] = []
        for (guild, person), log_ in self.interactions.items():
            if person != author_id or guild == here[0] or not self.settings.view(guild).cross_server:
                continue
            for item in log_:
                place = self.transport.locate(item.channel_id)
                if place is None or place.dm or not place.public or place.guild_id != guild or _secret(item.lines):
                    continue
                allowed[item.channel_id] = place
                if guild not in here:
                    out.append(item)
        return out

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
        if st.quiet_dm:
            st.quiet_dm = False  # 私信这一轮刚记了敏感的事：不当近况带到频道里
            return
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
                    if st.conv.key < 0 and m.message_id not in shown_ids and st.conv.key not in self.store.message_keys([m.message_id]):
                        # 连通组的记录：不在这一批里、也没记进这份记录的（连通之前的闲聊、暂停着的频道里的、上一轮没跑成的）不读进来
                        continue
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
            if self._too_big(exc) and st.conversation is conv:
                await self._compact(st, conv, info, force=True)
        else:
            st.error_until = now + ERROR_BACKOFF
        self._record_error(st.guild_id, st.channel_id, kind, exc)
        st.last_decision = f"出错：{exc}"
        if kind == "mention" and targets:
            mid = st.msg_by_sid.get(targets[-1])
            if mid is not None:
                await self._safe_react(st, mid, "⚠️")

    @staticmethod
    def _too_big(exc: LLMError) -> bool:
        """被拒是因为上下文太长（只有真的是才压缩：max_tokens 填错之类的报错里也有 token 字样，别误判）。"""
        text = str(exc).lower()
        return exc.status == 413 or any(word in text for word in ("context", "too long", "too large", "maximum length"))

    def _vetted(self, st: ChannelState) -> None:
        """这一轮的请求过了：这份记录里没问过模型就记进去的那几轮（别的连通频道补进来的）也跟着过了。它自己那份里连通时记进去的，
        只有这一轮的“连通之前的概要”（只带最后几千字）带全了才算；没带全的等断开以后带着整份记录的请求过了再算。"""
        kept = bool(st.conv.unvetted or st.conv.unvetted_edits)
        st.conv.unvetted.clear()
        st.conv.unvetted_edits.clear()
        own = self.convs.get(st.channel_id)
        if kept:
            self._keep_unvetted(st.conv)  # 库里记着的也划掉
        if self._synced(st) and st.past_vetted and own is not None and own is not st.conv:
            kept = bool(own.unvetted)
            own.unvetted.clear()
            if kept:
                self._keep_unvetted(own)

    def _keep_unvetted(self, cs: ConvState) -> None:
        """记下这份记录里还没问过模型的那几条（记在库里：重启以后照样认得，被拒了照样换掉）。"""
        self.store.save_unvetted(cs.key, cs.unvetted, cs.unvetted_edits)

    def _withdraw_unvetted(self, st: ChannelState) -> None:
        """这一轮的请求被 DeepSeek 拒了（不是太长）：记录里没问过模型就记进去的那几轮多半就是原因（2.7.1 里每一批都是问过模型
        才记的，被拒的那批丢掉）。换成一句“没记下”，免得以后每一轮都被拒。"""
        group = st.conv
        if group.key < 0 and group.unvetted_edits:
            # 连通组的记录里直接改成了新内容的那几条（没问过模型）：多半就是它们，当成删了
            edits, group.unvetted_edits = group.unvetted_edits, set()
            conv = group.conversation if group.conversation is not None else self._load(group)
            self._apply_fixes(group, conv, {mid: None for mid in edits})
            self._keep_unvetted(group)
            log.warning("频道 %s：这一轮被 DeepSeek 拒了，记录 %s 里直接改过的 %d 条消息当成删了", st.channel_id, group.key, len(edits))
        targets = [st.conv]
        own = self.convs.get(st.channel_id)
        if own is not None and own is not st.conv:
            targets.append(own)
        for cs in targets:
            if not cs.unvetted:
                continue
            ids, cs.unvetted = set(cs.unvetted), []
            conv = cs.conversation if cs.conversation is not None else self._load(cs)
            updates: dict[int, Any] = {}
            for t in conv.turns:
                if t.id in ids and t.role == "user":
                    text = t.content if isinstance(t.content, str) else ""
                    head = text.split("\n", 1)[0]
                    # 跟着补进来的“改成了什么”：改成的内容不留（可能就是它被拒），只说一声那条改过了（别让它一直照着旧的说）
                    lost = [
                        EDIT_NOTE_LOST.format(sid=found.group(1), name=found.group(2))
                        for line in text.split("\n")
                        if (found := _EDIT_NOTE_HEAD.match(line)) is not None
                    ]
                    updates[t.id] = "\n".join(([head] if head else []) + [PASSIVE_REJECTED] + lost)
            self._rewrite_turns(updates, {t.id: t for t in conv.turns})
            self._keep_unvetted(cs)
            if updates:
                log.warning("频道 %s：这一轮被 DeepSeek 拒了，记录 %s 里没问过模型就记进去的 %d 段换成了“没记下”", st.channel_id, cs.key, len(updates))

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
                # 连通的别的频道的消息：不跑到那边加表情
                mid = st.msg_by_sid.get(action.to) if action.to is not None and self._here(st, action.to) else None
                if mid is not None and action.emoji and mid not in st.deleted:
                    reacted = await self._safe_react(st, mid, action.emoji) or reacted
                continue

            to = action.to
            if to is None and kind == "mention" and targets:
                live = [t for t in targets if st.msg_by_sid.get(t) not in st.deleted]
                if not live:
                    continue  # 叫它的消息都被删了：不回
                to = live[-1]
            # 回连通的别的频道的消息：在这里直接说（挂不成回复：Discord 只能回同一个频道的消息）
            reply_mid = st.msg_by_sid.get(to) if to is not None and self._here(st, to) else None
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
            if self.cards is not None and not self._sync_foreign(st):  # 和别的服务器的频道连通时：写的名字多半是那边的人
                scope = self._card_scope(st.guild_id)
                # 卡里有别的服务器的人时只认在这个服务器出现过的人、按他在这里的名字（别的服务器的同名的人不在这里，@ 不到）
                owners = self.cards.owners(scope) if scope == st.guild_id else self.cards.owners_seen(scope, st.guild_id)
                for uid, name in owners:
                    for form in name_forms(name):
                        cards.setdefault(form, set()).add(uid)
            cache["tiers"] = [recent, cards]
            cache["people"] = merge_tiers(cache["tiers"])
            cache["channels"] = {name: frozenset(ids) for name, ids in self.transport.channel_names(st.guild_id).items()}
            cache["searched"] = set()
        if kind == "mention" and ("@" in text or "＠" in text) and not self._sync_foreign(st):
            # 和别的服务器的频道连通时不按名字到这里的成员里找：写的多半是那边的人，这里同名的人会被 @ 到
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
        linked, users, _ = link(
            chunk,
            people,
            channels,
            other_server=self._other_server_named(st.guild_id, st.channel_id),
            known_server=self._known_server_named(st.guild_id, st.channel_id),
        )
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
        queue = [
            (m, att, m.message_id in trigger_ids)  # 叫它的人发的图看清楚点，其他图看个大概
            for previews in (False, True)
            for m in reversed(shown)
            for att in m.images
            if (att.kind == "preview") == previews
        ]
        # 一批一批同时下载、缩小（以前是一张下完再下一张）：每批只拿“一张一张来也一定会下载”的那几张（还剩几个名额就拿几张），
        # 有没下成的（不占名额）再拿后面的补。选中哪几张、先后顺序都和一张一张来一样，也不会多下载
        start = 0
        while budget > 0 and start < len(queue):
            wave = queue[start : start + budget]
            start += len(wave)
            loaded = await _together(
                *(self.image_loader(att, self.ec.image_max_side if wanted else self.ec.low_detail_max_side) for _, att, wanted in wave)
            )
            for (m, _, wanted), uri in zip(wave, loaded):
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

    def _review_before_compact(
        self, st: Any, conv: Conversation, info: ChannelInfo, upto: int, generation: int, frozen: bool = False
    ) -> None:
        """要压掉的那部分（到 upto 为止）还没整理过记忆的：把原话挑出来，排队在后台整理（压成摘要之后就看不到原话了）。
        正在整理的话，它正在问的那一段不再挑，挑出来的排在它后面。不等它：压缩照常做，有人叫它也不用多等。
        st：频道的状态或者那份记录。frozen：连通的频道连通时，它自己原来那份也这样挑出来整理（以后不往里写，平时的整理
        轮不到它；不会压掉：没整理成的不记成整理过，组里整理时再试）。"""
        if self.cards is None and self.memory is None:
            return
        cs = self._cs(st)
        if cs.key != conv.channel_id:
            cs = self._conv_state(conv.channel_id)
        if cs.key > 0 and not frozen:
            self._park_asking(cs, conv, info, upto, generation)
        if max(conv.reviewed_upto, cs.review_held) >= upto:
            return  # 都整理过了，或者正在问的那一段会整理到
        try:
            plan = self._review_plan(cs, conv, info, upto=upto, after=cs.review_held, frozen=frozen)
        except Exception:
            log.exception("频道 %s：压缩前整理记忆出错", cs.key)
            return
        part = self._asking_part(cs, conv, info, upto) if cs.key < 0 else []
        if plan is None and not part:
            return
        if part:
            # 安静时整理正在问的那一段里也有要压掉的：那几条另外分段排进去。那一段问完了，这几段就跳过；没问成（出错、记录清空
            # 作废了），由这批整理。没压掉的那些还在记录里，下次照常整理（断开的话跟着拿回去）
            plan = _ReviewPlan(part, False, False, upto) if plan is None else replace(plan, chunks=[*part, *plan.chunks])
        assert plan is not None
        if cs.key < 0:
            for chunk in plan.chunks:
                # 编号是谁说的先记下：排着队的时候组散了（共用的记录连同编号映射都删掉了）也照样整理得了
                if chunk.authors is None:
                    chunk.authors = self.store.load_authors(cs.key, chunk.sids) if chunk.sids else {}
        cs.review_jobs.append(_ReviewJob(conv, info, plan, generation, frozen))
        if cs.reviewing:
            if cs.key < 0:
                cs.review_cut = True  # 连通组安静时整理的那批：问完手上这一段就停（剩下的下次照记录重新挑）
            else:
                cs.review_held = upto  # 安静时整理的那批：问完手上这一段就停，后面的归这一批
            return  # 它整理完手上的会接着整理这批
        cs.reviewing = True
        task = asyncio.get_running_loop().create_task(self._compact_review(cs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _asking_part(self, cs: ConvState, conv: Conversation, info: ChannelInfo, upto: int) -> list[_ReviewChunk]:
        """安静时整理正在问的那一段里马上要压掉的那几条（到 upto 为止），分好段（part_of 指着那一段）。连通组的记录里那个频道
        已经断开了的不算：它这部分（连同要压掉的）断开时拿回去了，在它自己那份里整理。"""
        asking = cs.review_asking
        if asking is None or not asking.turn_ids or min(asking.turn_ids) > upto:
            return []
        if asking.origin is not None and cs.key < 0:
            joined = self.store.sync_joined_turns(cs.key)
            if asking.origin not in joined or asking.last_turn <= joined[asking.origin]:
                return []
        ids = set(asking.turn_ids)
        run = [t for t in conv.turns if t.id in ids and t.id <= upto]
        part = self._review_chunks(run, (asking.info or info).bot_name) if run else []
        for chunk in part:
            chunk.origin, chunk.cards_ok, chunk.memory_ok, chunk.dm_ok = asking.origin, asking.cards_ok, asking.memory_ok, asking.dm_ok
            chunk.info, chunk.part_of = asking.info, asking
        return part

    def _park_asking(self, cs: ConvState, conv: Conversation, info: ChannelInfo, upto: int, generation: int) -> None:
        """频道自己那份：安静时整理正在问的那一段里马上要压掉的那几条，分好段另外记下，不排队（那一段问完了就用不着；没问成的话
        跟没连通过时一样，压掉的那几条不再整理）。断开时那一段问完会作废（换了 generation）：这几条交给新的那份整理（见
        _salvage_jobs）。"""
        try:
            part = self._asking_part(cs, conv, info, upto)
        except Exception:
            log.exception("频道 %s：压缩前整理记忆出错", cs.key)
            return
        if part:
            cs.review_parked.append(_ReviewJob(conv, info, _ReviewPlan(part, False, False, upto), generation))

    async def _compact(self, st: ChannelState, conv: Conversation, info: ChannelInfo, *, force: bool = False) -> None:
        """把最早的一部分对话压成摘要。只在这一次会让缓存 miss。"""
        if self.closing:
            return  # 关机了：压掉的那部分来不及整理记忆，留到下次启动再压
        self._flush_fixes(st, conv)  # 刚删掉、改过的先改进记录，别压进摘要里
        remove = plan_compaction([t.content for t in conv.turns], force=force)
        if remove < 2:
            return
        generation = st.generation
        # 连通组的记录：它在几个服务器里的昵称可能不一样，摘要里统一这么叫它（照哪个服务器的写，别的服务器看着都像别人）
        bot_name = SYNC_BOT_NAME if conv.channel_id < 0 else info.bot_name
        lines = []
        for t in conv.turns[:remove]:
            if t.role == "user":
                # 别的频道带来的近况、翻到的记录、搜索结果不进摘要：那些频道清空或不再共享后，这里也不该一直记着
                # 连通组的记录：只有原来那个服务器能看的那几句（想翻的频道找不到时列出来的名单之类）也不进摘要（摘要以后不会再查）
                lines.append(own_text(drop_local(t.content, keep_cards=True) if conv.channel_id < 0 else t.content))
            else:
                raw = t.content if isinstance(t.content, str) else json.dumps(t.content, ensure_ascii=False)
                if conv.channel_id < 0:
                    # 连通组的记录：改长期记忆（写着那个服务器那一条的原文）、按名字查人设卡的动作不进摘要（摘要以后不会再查）
                    raw = self._without_actions(raw, {"memory", "who"}) or raw
                said = assistant_text(raw, bot_name)
                if said:
                    lines.append(said)
        summary_system = SUMMARY_SYSTEM.format(name=bot_name, limit=self.ec.summary_max_chars)
        if self._synced(st):
            summary_system += SUMMARY_SYNC_NOTE  # 几个连通的频道共用的记录：写明每件事是哪个频道的
        prompt = [
            {"role": "system", "content": summary_system},
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
        for member in self._members(st.conv) or [st]:
            mine = self._mine(member, conv.turns)
            member.listed = last_listed(mine)  # 名单可能在被压掉的那部分里，那样下次被叫到时重新写
            if last_music_line(mine) is None:
                member.music_seen = None  # “正在放什么”那句压掉了：下次被叫到时再写一次
        for entry in st.recent_files.values():
            # 文件内容随旧记录一起压成了摘要，被问到时要能重新读
            entry.read_turns = {k: v for k, v in entry.read_turns.items() if v > last_removed}
        for link_entry in st.recent_links.values():
            link_entry.read_turns = {k: v for k, v in link_entry.read_turns.items() if v > last_removed}
        # 附过人设卡的那条记录压掉了：那个人下次说话时再附一次
        st.cards_shown = {uid: v for uid, v in st.cards_shown.items() if v[0] > last_removed}
        st.dm_memory_shown = {uid: v for uid, v in st.dm_memory_shown.items() if v[0] > last_removed}
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
        """清空这个频道的记忆。连通的频道（或者以前连通过、组里还留着它说过的话）：组的记录里只删它那部分，见 _reset_linked；
        要等正在跑的那一轮的话用 reset_linked。"""
        self._drop_cleared(channel_id)
        for orphan in [o for o in self._orphans if self._holds_origin(o, channel_id)]:
            # 散掉的组留下的、还在整理的那几批里有这个频道压掉的聊天：不整理了（清空以后别再记进人设卡、长期记忆），别的频道的
            # 接着整理（正在问的那一段问完会作废：换了 generation）
            fresh = ConvState(orphan.key, 0, next(self._generations))
            fresh.deleted, fresh.msg_by_sid = orphan.deleted, orphan.msg_by_sid
            jobs = self._salvage_jobs(orphan, fresh, drop=channel_id)
            orphan.generation = next(self._generations)
            self._orphans = [o for o in self._orphans if o is not orphan]
            self._start_jobs(fresh, jobs)
            if fresh.reviewing:
                self._orphans.append(fresh)
        sync = self.store.sync_of(channel_id)
        past = [s for s in self.store.origin_syncs(channel_id) if s != sync]
        for group in past:
            self._drop_from_group(channel_id, group, member=False)  # 断开以后，连通期间说的还留在那个组的记录里
        if sync is not None:
            self._reset_linked(channel_id, sync)
            return
        self.store.reset_conversation(channel_id)
        st = self.states.get(channel_id)
        if st is not None:
            st.generation = next(self._generations)
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
            st.dm_memory_shown.clear()
            st.unreviewed_rounds = 0  # 清空的记录不再整理
            st.review_jobs.clear()  # 排着队的是清空前的原话：不整理了（正在问的那一段问完会自己作废）
            st.conv.review_parked.clear()
            st.review_held = 0
            st.edit_notes.clear()  # 旧消息被编辑的那几句：记录都清了，别带进新的对话
            st.fixes.clear()
            st.music_seen = None  # 告诉过它在放什么的那句也清掉了
            st.conv.unvetted.clear()  # 记录清空了（库里记着的 reset_conversation 一起删了）
            st.conv.unvetted_edits.clear()
        elif (old := self.convs.get(channel_id)) is not None:
            # 还没有这个频道的状态（刚断开连通、重启以后还没人说过话）：它那份记录在内存里的也换成新的，不然下次有人说话时，
            # 接着用的还是清空前载入的那份
            old.generation = next(self._generations)
            old.conversation = None
            self.convs[channel_id] = ConvState(channel_id, old.guild_id, next(self._generations), lock=old.lock)
        for key, log_ in list(self.interactions.items()):
            # 清空了这个频道的记忆，别的频道也不该再“想起”这里聊过的事
            kept = [item for item in log_ if item.channel_id != channel_id]
            if kept:
                self.interactions[key] = deque(kept, maxlen=INTERACTIONS_PER_PERSON)
            else:
                del self.interactions[key]

    async def reset_linked(self, channel_id: int, timeout: float = 60.0) -> None:
        """清空记忆，先等用这几份记录的频道正在跑的那一轮跑完（连通的频道：别的频道正在回的话不会被打断、作废）。
        等太久抛 TimeoutError，什么都不改。"""
        async with self.sync_guard([channel_id], timeout, extra=lambda: self.store.origin_syncs(channel_id)):
            self.reset_channel(channel_id)

    def _reset_linked(self, channel_id: int, sync: int) -> None:
        """连通的频道清空记忆：共用记录里这个频道的那部分（连同共用的摘要）删掉，它自己原来那份也清空；别的频道说的不动。"""
        self._drop_from_group(channel_id, sync)
        self.store.reset_conversation(channel_id)
        own = self.convs.get(channel_id)
        if own is not None:
            own.generation = next(self._generations)  # 正在整理它原来那份的那一段作废（清空了）
            own.conversation = None  # 还拿着它的别照着内存里清空前的记录做
            self.convs[channel_id] = ConvState(channel_id, own.guild_id, next(self._generations), lock=own.lock)
        st = self.states.get(channel_id)
        if st is not None:
            st.conv_seen = None
            st.listed = None
            st.music_seen = None  # 告诉过它在放什么的那句也清掉了
        for key, log_ in list(self.interactions.items()):
            kept = [item for item in log_ if item.channel_id != channel_id]
            if kept:
                self.interactions[key] = deque(kept, maxlen=INTERACTIONS_PER_PERSON)
            else:
                del self.interactions[key]
        log.info("频道 %s：清空了它在连通组 %s 里的记录", channel_id, sync)

    def _drop_from_group(self, channel_id: int, sync: int, *, member: bool = True) -> None:
        """连通组的记录里删掉这个频道说过的那部分（连同共用的摘要：里面分不开；以前在组里的频道——member 不是真的——只在它说的
        压进过那份摘要时才连同摘要删）。那份记录换成新的状态从数据库重新读：
        正在跑的那一轮作废（reset_linked 会先等它们跑完）。别的频道的照旧：还没改进去的删改、还没附上的“改成了什么”、
        压缩前挑出来还没整理完的（接着整理）。"""
        self.store.drop_origin(sync, channel_id, member=member)
        old = self.convs.get(sync)
        fresh = ConvState(sync, 0, next(self._generations))
        jobs: list[_ReviewJob] = []
        if old is not None:
            fresh.lock = old.lock  # 等着旧的那份锁的（连通、断开、清空、排着的一轮），等的就是新的这份
            # 还记着的删除、别的频道的文件链接、这批里已经整理过的接着用；这个频道读过的内容随记录一起清掉了
            fresh.deleted = old.deleted
            fresh.review_done = set(old.review_done)
            fresh.unreviewed_rounds = old.unreviewed_rounds  # 别的频道还没整理的照样要整理
            fresh.fixes = {mid: m for mid, m in old.fixes.items() if m is None or m.channel_id != channel_id}
            fresh.hides = {mid for mid in old.hides if mid in fresh.fixes}
            fresh.edit_notes = {mid: v for mid, v in old.edit_notes.items() if v[1].channel_id != channel_id}
            remaining = self.store.turn_ids(sync)
            fresh.unvetted = [t for t in old.unvetted if t in remaining]  # 别的频道补进来、还没问过模型的（这个频道的删了）
            fresh.unvetted_edits = set(old.unvetted_edits)
            for mid, entry in old.recent_files.items():
                if entry.message.channel_id == channel_id:
                    entry.read_turns.clear()
                    entry.failed.clear()
                else:
                    # 别的频道的文件在这个频道的那几轮里读过的：那几条删掉了，被问到时要能重新读
                    entry.read_turns = {k: v for k, v in entry.read_turns.items() if v in remaining}
                fresh.recent_files[mid] = entry
            for mid, link_entry in old.recent_links.items():
                if link_entry.message.channel_id == channel_id:
                    link_entry.read_turns.clear()
                    link_entry.failed.clear()
                else:
                    link_entry.read_turns = {k: v for k, v in link_entry.read_turns.items() if v in remaining}
                fresh.recent_links[mid] = link_entry
            jobs = self._salvage_jobs(old, fresh, drop=channel_id)  # 别的频道的那几段接着整理（这个频道的不整理了）
            old.generation = next(self._generations)
            old.conversation = None  # 还拿着它的（等锁的整理、改删）别照着内存里清空前的记录做
            old.fixes = {}
        else:
            # 重启以后还没载入过这份记录：库里记着的、还没问过模型的接着用（这个频道的删了）
            listed, fresh.unvetted_edits = self.store.load_unvetted(sync)
            remaining = self.store.turn_ids(sync)
            fresh.unvetted = [t for t in listed if t in remaining]
        self._keep_unvetted(fresh)
        self.convs[sync] = fresh
        for st in self.states.values():
            if old is not None and st.conv is old:
                st.conv = fresh
                st.conv_seen = None
        if fresh.fixes:
            fixes, fresh.fixes, hides = fresh.fixes, {}, set(fresh.hides)
            for mid, updated in fixes.items():
                self._queue_fix_on(fresh, mid, updated, hide=mid in hides)
        if jobs:
            self._load(fresh)  # 整理时要认得出哪条被删掉了
            self._start_jobs(fresh, jobs)

    def _drop_cleared(self, channel_id: int) -> None:
        """清空记忆：每份记录（散掉的组留下的也算）排着队的、正在整理的那几批里这个频道的那几段都不整理了，正在问的问完也不
        照着改。不靠组的摘要记着谁：别的频道清空时摘要整个删了，那几批里它的聊天还在。"""
        for cs in [*self.convs.values(), *self._orphans]:
            jobs = [*cs.review_parked, *([cs.review_current] if cs.review_current is not None else []), *cs.review_jobs]
            for job in jobs:
                for chunk in job.plan.chunks:
                    if chunk.origin == channel_id:
                        chunk.dropped = True
            if cs.review_asking is not None and cs.review_asking.origin == channel_id:
                cs.review_asking.dropped = True

    @staticmethod
    def _holds_origin(cs: ConvState, channel_id: int) -> bool:
        """这份状态排着队的（和正在整理的）压缩前挑出来的那几批里，有没有这个频道的。"""
        jobs = ([cs.review_current] if cs.review_current is not None else []) + list(cs.review_jobs)
        return any(chunk.origin == channel_id for job in jobs for chunk in job.plan.chunks)

    def _salvage_jobs(self, old: ConvState, target: ConvState, drop: int | None) -> list[_ReviewJob]:
        """old 这份记录排着队的（和正在整理的）压缩前挑出来的那几批，连同频道自己那份安静时整理正在问的那一段里压掉了的那几条
        （见 _park_asking）：原话已经不在记录里了，不整理就没了。去掉 drop 这个频道的、已经整理过的那几段，改成 target 的接着整理
        （old 正在问的那一段问完会作废：换了 generation）。"""
        pending = [*old.review_parked, *([old.review_current] if old.review_current is not None else []), *old.review_jobs]
        old.review_jobs.clear()
        old.review_parked = []
        out: list[_ReviewJob] = []
        for job in pending:
            if job.generation != old.generation:
                continue  # 排队的时候就清空过：早就不整理了
            # 整理过的那几段每段自己记着（整理进度可能因为别的原因记过了它，它却还没整理过）
            chunks = [c for c in job.plan.chunks if (drop is None or c.origin != drop) and not self._reviewed_already(c)]
            if chunks:
                out.append(_ReviewJob(job.conv, job.info, replace(job.plan, chunks=chunks), target.generation, job.frozen))
        return out

    def _start_jobs(self, target: ConvState, jobs: list[_ReviewJob]) -> None:
        """接过来的那几批排进 target 的队里，在后台整理。"""
        if not jobs or self.closing:
            return
        target.review_jobs.extend(jobs)
        if target.reviewing:
            return  # 它整理完手上的会接着整理这几批
        target.reviewing = True
        task = asyncio.get_running_loop().create_task(self._compact_review(target))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @contextlib.asynccontextmanager
    async def sync_guard(
        self, channels: Iterable[int], timeout: float = 60.0, extra: Iterable[int] | Callable[[], Iterable[int]] = ()
    ) -> Any:
        """连通、断开、清空之前：等这几个频道正在跑的一轮跑完（拿着它们现在用的记录的锁：它们自己那份、它们现在在的组的那份，
        组里别的频道自己那份也算），没写进去的删改先写进去，免得记录换到一半。extra：另外要等的记录（可以是函数，每次照现在的
        算）。等的时候频道连通、断开了（用的记录变了）：放开，照现在的重新等。等太久（一直有人在叫它）抛 TimeoutError，
        什么都不改。"""
        channels = list(channels)

        def wanted() -> list[int]:
            chans = set(channels)
            for c in channels:
                sync = self.store.sync_of(c)
                if sync is not None:
                    chans.update(self.store.sync_members(sync))  # 等的时候连进来的：断开、散组时它那份也要换
            more = extra() if callable(extra) else extra
            return sorted({key for c in chans for key in (c, self.store.conversation_key(c))} | set(more))

        held: list[asyncio.Lock] = []
        keys = wanted()
        try:
            for _ in range(SYNC_GUARD_TRIES):
                for key in keys:
                    cs = self.convs.get(key)
                    if cs is None:
                        # 等的时候才有人说话的频道也要等：先建好，它那一轮就会排在这把锁后面
                        place = self.transport.locate(key) if key > 0 else None
                        cs = self._conv_state(key, place.guild_id if place is not None else 0)
                    if any(cs.lock is lock for lock in held):
                        continue
                    await asyncio.wait_for(cs.lock.acquire(), timeout)
                    held.append(cs.lock)
                if wanted() == keys and all(
                    (cs := self.convs.get(key)) is None or any(cs.lock is lock for lock in held) for key in keys
                ):
                    break
                # 等的时候频道连通、断开了：放开，照现在的重新按顺序等（一直拿着再去等别的锁，会和别的命令互相等着）
                for lock in reversed(held):
                    lock.release()
                held = []
                keys = wanted()
            else:
                raise asyncio.TimeoutError
            for key in keys:
                cs = self.convs.get(key)
                if cs is not None and cs.fixes:
                    self._flush_fixes(cs, self._load(cs))  # 现在用的那份（等的时候被换掉的那份，要改的已经交过来了）
            yield
        finally:
            for lock in reversed(held):
                lock.release()
            # 等的时候有人叫它、那一轮排不上（锁被占着）：现在轮到它了（不等下一次定时检查）
            involved = set(channels) | set(keys)
            for st in list(self.states.values()):
                if (st.channel_id in involved or st.conv.key in involved) and st.mention_due and st.pending and not st.scheduled:
                    self._wake(st)

    def sync_changed(self, channels: Iterable[int], *, dissolved: int | None = None) -> None:
        """连通、断开以后（channels：这次连进来的、断开的频道；调用方还拿着 sync_guard）：每个频道改用现在的那份记录。
        - 连进来的：改用共用的那份；它自己原来那份留着（只同步删改、给它自己看个大概），还没整理过的聊天排队整理掉
          （以后不往里写，平时的整理也不会再轮到它）。
        - 断开的：自己原来那份后面接上了它在共用记录里的那部分：换成新的状态从数据库重新读。
        - 散掉的组（dissolved）：共用的那份没了（压缩前挑出来、还没整理完的那几批照样整理完）。
        - 组里的频道不全在同一个服务器了：共用的记录里只有原来那个服务器能看的东西拿掉（见 scrub_cross）。
        读过的文件、链接、刚删掉的消息、还没附上的“改成了什么”跟着频道走。返回拿掉了几条记录里的内容。"""
        old_shared = self.convs.pop(dissolved, None) if dissolved is not None else None
        touched: list[int] = []
        joined_by: dict[int, list[int]] = {}  # 每个组这次连进来的频道
        for cid in dict.fromkeys(channels):
            key = self.store.conversation_key(cid)
            st = self.states.get(cid)
            before = st.conv if st is not None else None
            if key == cid:
                old = self.convs.get(cid)
                guild = st.guild_id if st is not None else (old.guild_id if old is not None else 0)
                fresh = ConvState(cid, guild, next(self._generations), lock=old.lock if old is not None else asyncio.Lock())
                if old is not None:
                    # 连通时记进它自己那份、还没问过模型的那几轮：编号不变（拿回来的只是接在后面），断开以后的请求过了才算数
                    fresh.unvetted = list(old.unvetted)
                else:
                    fresh.unvetted = self.store.load_unvetted(cid)[0]  # 重启以后还没载入过它那份：库里记着的
                # 共用的那份在前：连通期间删掉、改掉的以它为准（文件、链接连的时候已经从它自己那份搬过去了）
                sources: list[ConvState] = []
                for source in (before, old_shared, old):
                    if source is not None and all(source is not s for s in sources):
                        sources.append(source)
                gone = {mid for source in sources for mid in source.deleted}
                for source in sources:
                    self._carry(source, fresh, cid, skip=gone)
                for shared in [c for c in self.convs.values() if c.key < 0] + ([old_shared] if old_shared is not None else []):
                    mine = [m for m, (_, upd) in shared.edit_notes.items() if upd.channel_id == cid]
                    for mid in mine:
                        # 很早的消息被改了、还没来得及告诉它的那句：拿回去的记录里编号照旧，跟着它走
                        note = fresh.edit_notes[mid] = shared.edit_notes.pop(mid)
                        if shared is not old_shared:
                            shared.fixes.setdefault(mid, note[1])  # 组里的记录：它不会再在那边说话了，直接改进去
                    if mine and shared is not old_shared:
                        self._flush_fixes(shared, self._load(shared))
                own_jobs: list[_ReviewJob] = []
                if old is not None:
                    # 它自己那份压缩前挑出来、还没整理完的（连进来之前就在整理了）：原话已经不在记录里，交给新的这份接着整理
                    # （连的时候挑出来的那批不用：那些还在记录里，新的这份照常整理）
                    own_jobs = [job for job in self._salvage_jobs(old, fresh, drop=None) if not job.frozen]
                    old.generation = next(self._generations)
                self.convs[cid] = fresh
                conv = self._load(fresh)
                if old is not None and old.hidden_edits:
                    # 暂停（变成私密的、被关掉了）时改过的、组里那份当成删了的消息：拿回来的那一行照改过的写（只有它自己看）
                    for mid, later in old.hidden_edits.items():
                        if mid not in gone:
                            fresh.fixes.setdefault(mid, later)
                    old.hidden_edits = {}
                    self._flush_fixes(fresh, conv)
                # 拿回来的、连通之前没整理完的：照样要整理（不用等它再聊几轮）
                fresh.unreviewed_rounds = sum(
                    1 for t in conv.turns if t.role == "user" and t.id > conv.reviewed_upto and t.id not in fresh.review_done
                )
                self._start_jobs(fresh, own_jobs)
                target = fresh
            else:
                if key not in touched:
                    touched.append(key)
                joined_by.setdefault(key, []).append(cid)
                target = self._conv_state(key, 0)
                target.review_bad_by.pop(cid, None)  # 退出又连回来的：以前那次连着失败的次数不算
                if st is not None and st.pending and before is not None and before.key == cid:
                    self._settle_before_link(st)  # 连通之前攒着、还没进记录的：记进它自己那份（不进共用的）
                if target.conversation is not None:
                    # 连的时候库里的编号往后挪过（接在连进来的频道自己用过的后面）：内存里的跟上，免得同一个编号用两次
                    target.conversation.next_short_id = max(target.conversation.next_short_id, self.store.next_short_id(key))
                own = self.convs.get(cid)
                if own is not None and own.edit_notes:
                    # 很早的消息被改了、还没附上的那句：它自己那份以后不往后写了，直接改进去（连通之前的概要照改过的写）
                    notes, own.edit_notes = own.edit_notes, {}
                    for mid, (_, updated) in notes.items():
                        own.fixes.setdefault(mid, updated)
                    self._flush_fixes(own, self._load(own))
                for source in (own, before):
                    if source is not None and source is not target:
                        self._carry(source, target, cid, move=True)
                self._review_frozen(cid)
            if st is not None:
                st.conv = target
                st.conv_seen = None
                st.listed = None
                st.music_seen = None
        if old_shared is not None:
            # 组散了：压缩前挑出来、还没整理完的那几批照样整理完（各记进那个频道的服务器，用不着共用的记录）
            orphan = ConvState(old_shared.key, 0, next(self._generations))
            orphan.deleted, orphan.msg_by_sid = old_shared.deleted, old_shared.msg_by_sid
            jobs = self._salvage_jobs(old_shared, orphan, drop=None)
            old_shared.generation = next(self._generations)
            self._start_jobs(orphan, jobs)
            if orphan.reviewing:
                self._orphans.append(orphan)
        scrubbed = 0
        for key in touched:
            try:
                scrubbed += self.scrub_cross(key)
            except Exception:
                log.exception("连通组 %s：跨服务器了，拿掉只有原来那边能看的内容时出错（下次启动再查）", key)
            try:
                scrubbed += self._scrub_crossed(key, joined_by.get(key, []))  # 还在一个服务器组里，连进来的服务器没开“跨服务器”
            except Exception:
                log.exception("连通组 %s：拿掉从服务器组外带来的近况时出错", key)
            try:
                self.scrub_outsiders(key)  # 断开了的频道的服务器已经不在组里了：新来的频道不该看那边才能看的（退组时没查成的）
            except Exception:
                log.exception("连通组 %s：拿掉断开了的频道那边才能看的内容时出错", key)
            try:
                self._label_music(key)  # 开始跨好几个服务器了：之前写的“在放什么”写明是哪个服务器的
            except Exception:
                log.exception("连通组 %s：给之前写的“在放什么”补上服务器时出错", key)
        return scrubbed

    def _label_music(self, sync_id: int) -> int:
        """连通组的频道开始跨好几个服务器了（跨服务器的、服务器组里的都算）：之前写进共用记录的“语音频道在放什么”
        写明是哪个服务器的（当时只有一个服务器，没写；别的服务器的频道看到了会当成自己那边的）。返回改了几条。"""
        if len(self._guilds_of(self.store.sync_members(sync_id))) < 2:
            return 0
        cs = self.convs.get(sync_id)
        loaded = {t.id: t for t in cs.conversation.turns} if cs is not None and cs.conversation is not None else {}
        servers: dict[int, str | None] = {}
        updates: dict[int, Any] = {}
        for turn_id, role, content, origin in self.store.record_turns(sync_id):
            if role != "user" or origin is None:
                continue
            if origin not in servers:
                guilds = self._guilds_of([origin])
                servers[origin] = self._server_label(next(iter(guilds))) if guilds else None
            server = servers[origin]
            if server is None:
                continue  # 那个频道找不到了
            new = label_music(content, server)
            if new != content:
                updates[turn_id] = new
        self._rewrite_turns(updates, loaded)
        if updates and cs is not None:
            for st in self._members(cs):
                st.conv_seen = None  # “正在放什么”照改过的重新认
        return len(updates)

    def _rewrite_turns(self, updates: dict[int, Any], loaded: dict[int, Turn]) -> None:
        """改写几条记录：库里一次改完（一个事务），已经载入的那份跟着改。"""
        if not updates:
            return
        self.store.update_turns(updates.items())
        for turn_id, content in updates.items():
            if turn_id in loaded:
                loaded[turn_id].content = content

    @staticmethod
    def _without_actions(content: Any, kinds: set[str]) -> str | None:
        """它的一条回话去掉这几种动作（规范地重写一遍，和 _drop_from_record 一样）；认不出、没有这几种是 None。"""
        raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        actions, ok = parse_actions(raw)
        if not ok:
            return None
        kept = [a for a in actions if a.kind not in kinds]
        if len(kept) == len(actions) and not mentions_kinds(raw, kinds):
            return None  # 原话里也没写这几种（写错了没认出来的也算写了：整条按认出来的重写，那些就没了）
        new = dump_actions(kept)
        return None if new == raw else new

    def scrub_cross(self, sync_id: int, *, lookups: bool = False, whole: bool = False) -> int:
        """连通组跨服务器了（连进来别的服务器的频道、组里的服务器退出了服务器组）：共用的记录里只有原来那个服务器（组）
        能看的东西拿掉——别的频道带来的近况、能翻记录的频道名单、翻到的记录、按名字查到的人设卡，人设卡不是所有服务器共用
        的话附着的卡也拿掉（写进去的时候还不跨服务器；跨服务器的连通本来就不放这些）。它自己的回话里也一样：人设卡不是所有
        服务器共用的话，它写过的卡（card 动作里是整张卡）拿掉；结果拿掉了的那几步，前面那条回话里的“翻记录”“查人设卡”
        动作也拿掉（里面写着频道名、找的是什么）。和群友删消息一样直接改记录。跨服务器的连通自己会写的都不动：再查一遍
        不会改记录。不跨服务器就什么都不做。返回改了几条记录。lookups：刚跑完的那一轮问模型的时候组还没变成跨服务器
        （见 groups_changed）：它要翻记录、查人设卡的动作也拿掉。whole：组里的频道还在一个服务器组里，可是组里原来有的服务器
        退出了服务器组：不跨服务器也照样整份查一遍（只在那个时候查一次）。"""
        if not whole and not self.channels_cross(self.store.sync_members(sync_id)):
            return 0
        keep_cards = self._cards_shared()
        cs = self.convs.get(sync_id)
        loaded = {t.id: t for t in cs.conversation.turns} if cs is not None and cs.conversation is not None else {}
        rows = self.store.record_turns(sync_id)
        updates: dict[int, Any] = {}
        for turn_id, role, content, _ in rows:
            if role == "user":
                new = drop_local(content, keep_cards=keep_cards)
                if new != content:
                    updates[turn_id] = new
        # 记录里有只该原来那边看的（刚拿掉了）：那段时间它要翻记录、查人设卡的动作也都拿掉（动作里写着频道名、找的是什么；
        # 查到的拿掉了，没查成的也一样）。跨服务器以后的那些动作当时就被拒了，留着不碍事，这一次一并拿掉；以后再查，没什么
        # 要拿的就不碰它们。人设卡各服务器分开时，它写过的卡（card 动作里是整张卡）每次都拿：跨服务器的连通里写卡的动作
        # 当时就从记录里拿掉了，不会一次次改记录
        kinds = {"memory"} | (set() if keep_cards else {"card"}) | ({"recall", "who"} if updates or lookups else set())
        if kinds:
            for turn_id, role, content, _ in rows:
                if role == "assistant":
                    new = self._without_actions(content, kinds)
                    if new is not None:
                        updates[turn_id] = new
        self._rewrite_turns(updates, loaded)
        changed = len(updates)
        if changed:
            if cs is not None:
                cs.cards_shown.clear()  # 附着的、查到的卡拿掉了：照现在能附的重新附
                cs.digest_seen.clear()
                for st in self._members(cs):
                    st.listed = None
            log.info("连通组 %s 跨服务器了：共用的记录里有 %d 条拿掉了只有原来那个服务器能看的内容", sync_id, changed)
        return changed

    def _scrub_crossed(self, sync_id: int, joined: Iterable[int]) -> int:
        """连通组的频道还在一个服务器（组）里，连进来的频道（joined）带来了组里原来没有、又没开“跨服务器”的服务器：共用的记录里
        写着是哪个服务器的近况都拿掉（组外的只有都开了“跨服务器”时才会带进来，没开的那边平时看不到；写下来的服务器名分不出当时
        是组里还是组外的——同名、改过名、bot 退出了那个服务器——组里的也一起拿掉，只是少了点背景）。原来就在组里的服务器看过的，
        它自己后来关掉了“跨服务器”也照旧留着。返回改了几条记录。"""
        members = self.store.sync_members(sync_id)
        joined = list(joined)
        guilds = self._guilds_of(members)
        if not guilds or self.channels_cross(members) or not self.brings_cross_off([c for c in members if c not in joined], joined):
            return 0
        cs = self.convs.get(sync_id)
        loaded = {t.id: t for t in cs.conversation.turns} if cs is not None and cs.conversation is not None else {}
        updates: dict[int, Any] = {}
        for turn_id, role, content, _ in self.store.record_turns(sync_id):
            if role == "user":
                new = drop_recents(content)
                if new != content:
                    updates[turn_id] = new
        self._rewrite_turns(updates, loaded)
        if updates:
            log.info("连通组 %s：有服务器没开“跨服务器”，共用的记录里有 %d 条拿掉了从服务器组外带来的近况", sync_id, len(updates))
        return len(updates)

    def scrub_outsiders(self, sync_id: int) -> int:
        """连通组还在一个服务器（组）里，记录里留着已经断开的频道说过的话，它的服务器现在不在这个组里了（退出了服务器组）：它那几轮
        里只有它那个服务器能看的东西拿掉，和组变成跨服务器时一样（见 scrub_cross：别的频道带来的近况、能翻的频道名单、翻到的记录、
        按名字查到的人设卡，人设卡各服务器一张时连同附着的卡；它的回话里写卡、改长期记忆、翻记录、查人设卡的动作）。它不会再往
        这份记录里写，查一次就够（退组时、有频道连进来时、启动时都会查）。返回改了几条记录。"""
        members = self.store.sync_members(sync_id)
        guilds = self._guilds_of(members)
        if not guilds or self.channels_cross(members):
            return 0  # 跨服务器的组：scrub_cross 整份都查
        group = set(self._group_guilds(min(guilds)))
        rows = self.store.record_turns(sync_id)
        origins = {origin for _, _, _, origin in rows if origin is not None and origin not in members}
        outside = {o for o in origins if not (self._place_former(o) & group)}
        if not outside:
            return 0
        keep_cards = self._cards_shared()
        kinds = {"memory", "recall", "who"} | (set() if keep_cards else {"card"})
        cs = self.convs.get(sync_id)
        loaded = {t.id: t for t in cs.conversation.turns} if cs is not None and cs.conversation is not None else {}
        updates: dict[int, Any] = {}
        for turn_id, role, content, origin in rows:
            if origin not in outside:
                continue
            new = drop_local(content, keep_cards=keep_cards) if role == "user" else self._without_actions(content, kinds)
            if new is not None and new != content:
                updates[turn_id] = new
        self._rewrite_turns(updates, loaded)
        if updates:
            if cs is not None:
                cs.cards_shown.clear()
                for st in self._members(cs):
                    st.listed = None
            log.info("连通组 %s：断开了的频道的服务器不在组里了，它那几轮里有 %d 条拿掉了只有那边能看的内容", sync_id, len(updates))
        return len(updates)

    def _place_former(self, channel_id: int) -> set[int]:
        """断开了的频道在哪个服务器：找得到的照现在的；找不到（还没连上 Discord、频道删了、bot 退出了那个服务器）照它自己那份记录
        记下的；都没有是空的（当成组外的）。"""
        guilds = self._guilds_of([channel_id])
        if guilds:
            return guilds
        guild = self.store.record_guild(channel_id)
        return {guild} if guild is not None else set()

    def _scrub_now(self, cs: ConvState) -> None:
        """这一轮跑的时候组变成跨服务器了（服务器退出了服务器组）：还拿着锁，照现在的组把写进去的再查一遍（排在后面等锁的
        轮次就看不到了）。"""
        lookups, whole = cs.scrub_lookups, cs.scrub_whole
        cs.scrub_due, cs.scrub_lookups, cs.scrub_whole = False, False, False
        try:
            stored = self.store.pending_scrub(cs.key)
            if stored is not None:
                # 库里还记着以前哪一轮写完以后要查、没查成的（出错了）：一起查（下面划掉了就再也不查了）
                lookups, whole = lookups or stored[0], whole or stored[1]
            self.scrub_cross(cs.key, lookups=lookups, whole=whole)
            self.store.clear_scrub(cs.key)  # 查过了（出错的话库里还记着：下次启动时再查）
        except Exception:
            log.exception("连通组 %s：这一轮跑完以后拿掉只有原来那边能看的内容时出错", cs.key)

    @staticmethod
    def _carry(
        source: ConvState, target: ConvState, channel_id: int, *, move: bool = False, skip: set[int] | frozenset[int] = frozenset()
    ) -> None:
        """这个频道的消息带着的文件、链接（读过的记成没读过：内容在原来那份记录里）和刚删掉的消息，搬到它现在用的那份。
        move：原来那份里的拿走（连进来时：它自己那份以后不用了；断开时只以共用的那份为准，连通期间删掉、改掉的不会又冒出来）；
        不拿走的话给 target 另外一份（原来那份还要接着用：读过没读过各记各的）。skip：刚删掉的消息（不带过去）。"""
        for mid, entry in list(source.recent_files.items()):
            if entry.message.channel_id != channel_id:
                continue
            if mid not in target.recent_files and mid not in skip:
                if move:
                    entry.read_turns.clear()
                    target.recent_files[mid] = entry
                else:
                    target.recent_files[mid] = FileEntry(entry.message, failed=set(entry.failed))
            if move:
                del source.recent_files[mid]
        for mid, link_entry in list(source.recent_links.items()):
            if link_entry.message.channel_id != channel_id:
                continue
            if mid not in target.recent_links and mid not in skip:
                if move:
                    link_entry.read_turns.clear()
                    target.recent_links[mid] = link_entry
                else:
                    target.recent_links[mid] = LinkEntry(
                        link_entry.message, failed=set(link_entry.failed), titles=dict(link_entry.titles)
                    )
            if move:
                del source.recent_links[mid]
        for mid in source.deleted:
            target.deleted.setdefault(mid, None)
        while len(target.deleted) > DELETED_KEEP:
            del target.deleted[next(iter(target.deleted))]

    def _settle_before_link(self, st: ChannelState) -> None:
        """刚连进来的频道（调用方拿着 sync_guard，st.conv 还是它自己那份）：缓冲区里连通之前的闲聊记进它自己那份记录，
        当成它看过、没说话；不进共用的记录（连通之前的聊天不给别的频道看）。叫它的那几条留着，连通以后照常回。"""
        quiet = [m for m in st.pending if not m.trigger]  # 叫它的那几条留着（连通以后回），别的都是连通之前的闲聊
        if not quiet:
            return
        now = self.clock()
        take = [m for m in quiet if m.message_id not in st.deleted]
        if take:
            conv = self._conversation(st)
            shown = take[-max(1, self.ec.max_batch_messages) :]
            numbered = self._number(st, conv, shown)
            content = render_round(
                numbered,
                sid_by_msg=st.sid_by_msg,
                kind="proactive",
                targets=[],
                now=now,
                tz=self.tz,
                omitted=st.omitted + len(take) - len(shown),
                max_chars=self.ec.max_chars_per_message,
            )
            assigned = [(sid, m.message_id, None if m.is_bot else m.author_id, m.author_name) for sid, m in numbered]
            self.store.append_round(conv, content, PASSIVE_REPLY, assigned, now=now)
            st.conv.unvetted.append(conv.turns[-2].id)  # 没问过模型：连通以后带着它（连通之前的概要）的请求过了才算数
            self._keep_unvetted(st.conv)
            st.unreviewed_rounds += 1
            # 连的时候按它自己那份原来的编号记下了从哪接着编：用掉了几个，跟着往后挪
            self.store.raise_join_marks(st.channel_id, conv.next_short_id)
        self._take_pending(st, quiet)

    def _review_frozen(self, channel_id: int) -> None:
        """连通的频道：它自己原来那份里还没整理过的聊天，排队整理掉（以后不往里写，平时的整理轮不到它）。连进来时整理一次；
        没整理成的（暂时出错、预算用完了）不记成整理过，组里整理记忆时再试。"""
        if self.cards is None and self.memory is None:
            return
        try:
            own = self.convs.get(channel_id)
            if own is None:
                place = self.transport.locate(channel_id)
                if place is None:
                    return
                own = self._conv_state(channel_id, place.guild_id)
            if own.reviewing:
                return  # 正在整理它这份（上次排的还没整理完）
            conv = self._load(own)
            if not conv.turns or conv.reviewed_upto >= conv.turns[-1].id:
                return
            self._review_before_compact(
                own, conv, self.transport.channel_info(channel_id), conv.turns[-1].id, own.generation, frozen=True
            )
        except Exception:
            log.exception("频道 %s：连通前整理记忆出错", channel_id)

    def groups_changed(self, guilds: Iterable[int], *, left: bool) -> None:
        """服务器组变了（guilds：组里原来的、加进来的、退出的服务器）。人设卡、长期记忆、翻记录、近况每次都按现在的组算，
        不用另外处理；退出了（组散了）的话，频道里附过的私信记忆里现在不能带的撤回：附的时候是按原来的组算的（私信挂在
        组里别的服务器），现在不一定还能带。还能带的不动（免得整个组的频道记录都改一遍、缓存全失效）。和组里别的服务器的频道
        连通的连通组也跨服务器了：共用的记录里只有原来那边能看的拿掉（scrub_cross）。"""
        if not left:
            return
        affected = list(dict.fromkeys(guilds))
        for sync_id, members in self.store.syncs().items():
            member_guilds = self._guilds_of(members)
            if member_guilds & set(affected):
                changed = 0
                # 组里的频道还在一个服务器组里，可是组里原来有的服务器退出了：记录里那个服务器才能看的（附着、查到的人设卡，翻到的
                # 记录，近况……组里的频道那几轮里也有）分不出是哪个服务器的，整份记录照变成跨服务器时那样查一遍
                departed = not self.channels_cross(members) and bool(
                    set(affected) - set(self._group_guilds(min(member_guilds))) - member_guilds
                )
                try:
                    changed = self.scrub_cross(sync_id, whole=departed)  # 和组里别的服务器的频道连通的：现在跨服务器了
                except Exception:
                    log.exception("连通组 %s：服务器退出服务器组以后，拿掉只有原来那边能看的内容时出错", sync_id)
                try:
                    self.scrub_outsiders(sync_id)  # 还在一个组里，但记录里断开了的频道是退出的那个服务器的
                except Exception:
                    log.exception("连通组 %s：服务器退出服务器组以后，拿掉断开了的频道那边才能看的内容时出错", sync_id)
                cs = self.convs.get(sync_id)
                if cs is not None and any(st.inflight for st in self._members(cs)):
                    # 正在跑的那一轮（按原来的组带上的东西）写进去以后、放开锁之前再查一遍（锁被连通、断开的命令占着的话不用：
                    # 等它的那几轮是退组以后才开始的）
                    cs.scrub_due = True
                    # 记录里有只该原来那边看的（刚拿掉了）：那一轮按原来的组要翻记录、查人设卡的动作，写完以后也拿掉
                    # （名单在更早的一轮里的话，到时候就查不出别的了）
                    cs.scrub_lookups = cs.scrub_lookups or changed > 0
                    cs.scrub_whole = cs.scrub_whole or departed
                    if cs.scrub_lookups or cs.scrub_whole:
                        try:
                            # 也记在库里：那一轮写完以后、再查之前就停了（断电、强制关掉）的话，下次启动时照样查（只看跨不跨服务器的
                            # 那一遍，启动时本来就查）
                            self.store.mark_scrub(cs.key, lookups=cs.scrub_lookups, whole=cs.scrub_whole)
                        except Exception:
                            log.exception("连通组 %s：没能记下正在跑的那一轮写完以后要再查一遍", sync_id)
        for guild_id in affected:
            view = self.settings.view(guild_id)
            here = self._group_guilds(guild_id)

            def keep(channel_id: int, user_id: int, view: View = view, here: list[int] = here) -> bool:
                home = self.store.dm_home(user_id)
                return self._dm_memory_on(view) and ((home is not None and home[0] in here) or bool(view.cross_server))

            self.withdraw_dm_memory(guild_id=guild_id, keep=keep)

    def move_dm(self, channel_id: int, guild_id: int) -> None:
        """私信改挂到别的服务器：清掉私信的记录（里面是原来那个服务器的人设卡、记忆），之后用新服务器的。"""
        user = self.transport.channel_info(channel_id).dm_user
        self.reset_channel(channel_id)
        self.store.move_conversation(channel_id, guild_id)
        if user is not None:
            # 原来那个服务器没开跨服务器的话，不该再看到；别的服务器能带的，下次他叫它时会重新附
            self.withdraw_dm_memory(user)
        st = self.states.get(channel_id)
        if st is not None:
            st.guild_id = guild_id
            st.conv.guild_id = guild_id  # 私信不会连通：记录就是它自己的
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
        return {
            "following": bool(st and st.following and view.follow and now - st.last_activity_at < FOLLOW_IDLE),
            "bubble": self._bubble_status(st, view, now) if st is not None and self._bubble_on(st) else None,
            "next_check_in": max(0.0, st.next_check_at - now) if st and st.next_check_at else None,
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
            "auto_search": bool(view.proactive_search) and self._can_search(view),
            "auto_searches": self.store.count_usage(self.ledger.day(now), guild_id=guild_id, kind=AUTO_SEARCH),
            "images_found": self.store.count_usage(self.ledger.day(now), guild_id=guild_id, kind="image"),
            "links_opened": self.store.count_usage(self.ledger.day(now), guild_id=guild_id, kind="link"),
            "can_find_images": self._can_find_images(view),
            "memes_on": self._memes_on(view),
            "meme_counts": self.memes.counts(guild_id) if self.memes is not None else (0, 0),
            "links_mode": self._links_mode(view),
            "media_blocked_for": max(0.0, st.media_blocked_until - now) if st else 0.0,
            "cards_on": self._cards_on(view),
            "memory_on": self._memory_on(view),
            "card_count": self.cards.count(self._card_scope(guild_id)) if self.cards is not None else 0,
            "cards_shared": self._cards_shared(),  # 所有服务器共用一张人设卡（上面是一共几张）
            "memory_count": self.memory.count(self._scope(guild_id)) if self.memory is not None else 0,
            "group": self._group_guilds(guild_id)[1:],  # 连成一组的别的服务器（不在组里是空的）
            # 和这个频道连通的别的频道（没连通是空的）
            "sync": [c for c in self.store.sync_members(sync) if c != channel_id]
            if (sync := self.store.sync_of(channel_id)) is not None
            else [],
            "sync_paused": sync is not None and not self._sync_public(channel_id),  # 连通的频道变成私密的了
            "memory_max": self.ec.memory_max_entries,
            "review_here": self.implicit_ok(channel_id, "cards") or self.implicit_ok(channel_id, "long_memory"),
            "last_review_ago": (now - st.last_review_at) if st and st.last_review_at else None,
            "music_on": self.music is not None,
            "playing": self.music.status(guild_id) if self.music is not None else None,
        }
