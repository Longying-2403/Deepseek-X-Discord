"""B站点歌：找歌、下载音频、每个服务器一个播放队列。

- 找歌：话里有 B站视频链接（BV 号、av 号、b23.tv 短链接，带不带 https:// 都行）就直接用；给的是歌名就上网找：
  有 TAVILY_API_KEY 时用 Tavily 只搜 bilibili.com（B站自己的搜索接口很容易被风控拦下来），找不到或者没有 Tavily
  才用 B站的搜索。Tavily 找到的几个结果按标题排：歌名、歌手对得上的在前，翻唱、伴奏、教学、合集之类往后
  （点歌时自己这么说的除外）；B站的搜索结果没有标题，照 B站的顺序。
- 下载：分两步。先向 B站要视频信息（标题、时长，顺便确认不是直播、能放），马上就能告诉点歌的人；
  音频（不用登录，最高 192 kbps）在后台下载，下好了才放，放完、切掉、停止都会删掉。每个服务器只先下载
  接下来的两首（一次下载一首），后面的轮到了再下载：队列再长、视频再长，硬盘上也只有几首。
- 播放：进点歌的人所在的语音频道。同一个语音频道里的人都能切歌、暂停、继续、停止、从队列里删歌、清空队列；
  在放什么谁都能问。放完了 5 分钟没人点歌、或者语音频道里只剩它 2 分钟，它就自己离开。不限时长，不限队列长短；直播不放。

和 Discord 语音打交道的部分（进频道、播放、暂停）在 discord_app.DiscordVoice 里，这里只管逻辑，方便测试。
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import logging
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from .textutil import one_line

log = logging.getLogger(__name__)

IDLE_LEAVE = 300.0  # 放完了这么久没人点歌：离开语音频道
ALONE_LEAVE = 120.0  # 语音频道里只剩它这么久：离开
STUCK_LEAVE = 60.0  # 语音一直连不上（卡在重连）这么久：先离开，队列清掉
READY_WAIT = 15.0  # 点的歌马上就轮到时，等它下载这么久：下好了就说“开始播放”，还没下好就先说“正在下载”
INFO_TIMEOUT = 60.0  # 向 B站要一个视频的信息（或者搜一次）最多等多久
DOWNLOAD_LIMIT = 900.0  # 一首最多下载 15 分钟（很长的视频遇上很慢的网）
EXPIRY_MARGIN = 120.0  # 音频地址离过期（deadline）不到这么久：先重新拿一次信息再下载
HOLD_SECONDS = 180.0  # 下载前重新拿信息时被 B站拦下来（风控）：这个服务器后面的歌先等这么久再接着下载
API_GAP = 1.5  # 两次向 B站要视频信息至少隔这么久（太密会被风控拦下来）
MAX_TRIES = 3  # 按歌名找到的结果，前面的放不了（被删、地区限制、直播）就换下一个，最多试几个
PREFETCH = 2  # 每个服务器先下载队列里接下来的几首
PLAY_TRIES = 2  # 下载好的一首放不出来，最多试几次（偶尔一次可能是临时的问题）
QUEUE_SHOWN = 15  # 看队列时最多列几首
QUEUE_CHOICES = 25  # /ds-music 删除 输入时最多列几首（Discord 最多 25 个）
FORMAT = "bestaudio/best"  # 只要音频；没有单独音频的老视频才下整个视频
EXTRACTORS = ["bilibili", "bilibilisearch"]  # yt-dlp 只用这两个（b23.tv 短链接自己先展开，不交给 yt-dlp 乱跳）
SHORT_HOSTS = ("b23.tv", "bili2233.cn")

_BV = r"[Bb][Vv][0-9A-Za-z]{10}"
_LEFT = r"(?<![A-Za-z0-9._@/-])"  # 前面不能紧挨着字母、数字、点之类：不是别的网址、别的域名的一部分（中文可以）
_VIDEO_LINK = re.compile(rf"{_LEFT}(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/(?:{_BV}|av\d+)[^\s<>\"'）)】」]*", re.I)
_FESTIVAL_LINK = re.compile(rf"{_LEFT}(?:https?://)?(?:www\.)?bilibili\.com/festival/[^\s<>\"']*?[?&]bvid=({_BV})", re.I)
_SHORT_LINK = re.compile(rf"{_LEFT}(?:https?://)?(?:b23\.tv|bili2233\.cn)/([0-9A-Za-z]+)", re.I)
_BARE_ID = re.compile(rf"(?<![0-9A-Za-z/=])(?:{_BV}|av\d{{5,12}})(?![0-9A-Za-z])", re.I)
_VIDEO_ID = re.compile(rf"/video/({_BV}|av\d+)", re.I)
_LIVE_LINK = re.compile(rf"{_LEFT}(?:https?://)?live\.bilibili\.com/", re.I)
_BILI_OTHER = re.compile(rf"{_LEFT}(?:https?://)?(?:[a-z0-9-]+\.)*bilibili\.com/", re.I)
_YOUTUBE = re.compile(rf"{_LEFT}(?:https?://)?(?:[a-z0-9-]+\.)*(?:youtube\.com|youtu\.be)/", re.I)
_TLD = r"(?:com|cn|net|org|tv|io|me|cc|co|jp|tw|hk|kr|uk|info|xyz|top|app|dev|fm|so|ly|be|gg|site|link)"
_ANY_LINK = re.compile(rf"https?://\S+|www\.\S+|(?<![A-Za-z0-9@])(?:[A-Za-z0-9-]+\.)+{_TLD}/\S*", re.I)
# 不是原曲的常见标题字样：排在后面（点歌时自己就这么说的不算）
_NOT_ORIGINAL = (
    "翻唱", "cover", "伴奏", "纯音乐", "钢琴", "吉他", "指弹", "尤克里里", "教学", "教程", "reaction", "鬼畜",
    "片段", "合集", "串烧", "循环", "一小时", "1小时", "十小时", "10小时", "抖音", "混剪", "改编", "remix", "dj",
    "试听", "铃声", "卡拉ok", "ktv", "翻弹", "翻跳", "舞蹈", "解说", "盘点", "手书",
)
_LIVE_VERSION = ("live", "现场", "演唱会")  # 现场版：稍微往后排
_ORIGINAL = ("官方", "official", "mv", "原唱", "完整版")  # 稍微往前排


class MusicError(Exception):
    """放不了的原因（直接说给点歌的人听）。retry=False：换一个结果也没用（比如被风控了），别再试。"""

    def __init__(self, message: str, *, retry: bool = True):
        super().__init__(message)
        self.retry = retry


class VoiceNotReady(MusicError):
    """语音连接还没好（比如正在重连）：这首先留在队列里，等一下再放。"""


class TryLater(MusicError):
    """下载前要重新向 B站拿信息，却被拦下来了（风控、超时）：这首先留在队列里，过几分钟再下载。"""


class _Expired(Exception):
    """音频地址过期了（下载时 403 之类）：重新拿一次信息再下载。"""


@dataclass(eq=False)  # 按对象比：同一首歌点两次是两首
class Track:
    url: str
    title: str
    uploader: str = ""
    duration: float | None = None  # 秒
    requester_id: int = 0
    requester_name: str = ""
    info: Any = field(default=None, repr=False)  # yt-dlp 拿到的视频信息（下载时用，下完就丢掉）
    resolved_at: float = 0.0  # 什么时候拿到的信息（time.monotonic）
    path: str = ""  # 下载好的音频
    state: str = "waiting"  # waiting（还没下载）/ downloading / ready / failed / cancelled
    error: str = ""  # 下载失败的原因
    fetching: bool = False  # 已经交给后台下载了（可能还在排队等前面那首下完）
    failures: int = 0  # 下载好了却放不出来的次数
    waiting: bool = False  # play() 正在等它下载完：结果由 play() 自己说，频道里不用另外说
    serial: int = 0  # 第几个点的（/ds-music 删除 输入时选的就是它：队列往前走了也认得出是哪一首）
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)  # 切掉、停止：不用再下载了
    done: asyncio.Event | None = field(default=None, repr=False)  # 下载结束（成没成都算）

    def label(self) -> str:
        """《标题》（UP主：xx） [4:29]"""
        text = f"《{one_line(self.title, 80) or '没有标题'}》"
        if self.uploader:
            text += f"（UP主：{one_line(self.uploader, 30)}）"
        if self.duration:
            text += f" [{clock(self.duration)}]"
        return text


def clock(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def canonical(url: str) -> str | None:
    """B站视频链接 → 统一的写法（分 P 的留着 ?p=N），不是视频链接返回 None。"""
    found = _VIDEO_ID.search(url)
    if found is None:
        return None
    vid = found.group(1)
    vid = ("av" if vid[:2].lower() == "av" else "BV") + vid[2:]
    query = urlsplit(url if "://" in url else "https://" + url).query
    page = (parse_qs(query).get("p") or [""])[0]
    return f"https://www.bilibili.com/video/{vid}" + (f"?p={page}" if page.isdigit() and int(page) > 1 else "")


def is_short(url: str) -> bool:
    """是不是 b23.tv 之类的短链接。"""
    host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    return host in SHORT_HOSTS


def all_links(text: str) -> list[str]:
    """话里所有的 B站视频（统一写法）和短链接，按出现的顺序，不重复。只写了 BV 号、av 号也算。"""
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for pattern in (_VIDEO_LINK, _FESTIVAL_LINK, _SHORT_LINK, _BARE_ID):
        for m in pattern.finditer(text):
            if any(start <= m.start() < end for start, end in taken):
                continue  # 已经算在前面那个链接里了（比如链接里的 BV 号）
            if pattern is _VIDEO_LINK:
                link = canonical(m.group(0))
            elif pattern is _FESTIVAL_LINK:
                link = canonical("/video/" + m.group(1))
            elif pattern is _SHORT_LINK:
                link = f"https://{m.group(0).split('://')[-1].split('/')[0].lower()}/{m.group(1)}"
            else:
                link = canonical("/video/" + m.group(0))
            if link is not None:
                found.append((m.start(), link))
                taken.append((m.start(), m.end()))
    out: list[str] = []
    for _, link in sorted(found):
        if link not in out:
            out.append(link)
    return out


def find_link(text: str) -> str | None:
    """话里的第一个 B站视频链接（统一写法），或者 b23.tv 短链接（之后再展开）。"""
    links = all_links(text)
    return links[0] if links else None


def _distance(a: str, b: str) -> int:
    """两个短字符串差了几个字（编辑距离）。"""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def pick_link(query: str, message: str) -> str:
    """模型抄链接、BV 号可能抄错一两个字：它给的链接和叫它的那条消息里的某个 B站链接只差一两个字时，用消息里的。
    完全一样、或者差得多（说的是别的视频，比如“放 #12 发的那个”）就照它的。"""
    wanted = find_link(query)
    if wanted is None:
        return query
    links = all_links(message)
    if wanted in links:
        return query
    close = [link for link in links if _distance(link, wanted) <= 2]
    return close[0] if len(close) == 1 else query


def unsupported(text: str) -> str | None:
    """话里有链接、但不是能放的 B站视频时：放不了的原因。"""
    if _LIVE_LINK.search(text):
        return "这是直播，直播不放"
    if _YOUTUBE.search(text):
        return "现在只能放 B站的，YouTube 的放不了"
    if _BILI_OTHER.search(text):
        return "这个 B站链接不是单个视频（番剧、空间、合集这些放不了），发 bilibili.com/video/… 的视频链接"
    if _ANY_LINK.search(text):
        return "只能放 B站的视频链接"
    return None


def _bigrams(word: str) -> set[str]:
    return {word[i : i + 2] for i in range(len(word) - 1)} or {word}


def rank(query: str, results: list[tuple[str, str]]) -> list[str]:
    """按标题给找到的 B站视频排序（去掉不是视频的网页和重复的）。results：[(网址, 标题)]，标题可以是空的。"""
    q = query.casefold()
    words = [w for w in q.split() if w]
    scored: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for i, (url, title) in enumerate(results):
        link = canonical(url)
        if link is None or link in seen:
            continue
        seen.add(link)
        t = title.casefold()
        score = 0.0
        for w in words:
            if w in t:
                score += 10
            else:  # “周杰伦晴天” 对 “周杰伦《晴天》”：按两个字两个字地比
                grams = _bigrams(w)
                score += 8 * sum(1 for g in grams if g in t) / len(grams)
        score -= sum(6 for word in _NOT_ORIGINAL if word in t and word not in q)
        score -= sum(3 for word in _LIVE_VERSION if word in t and word not in q)
        score += sum(2 for word in _ORIGINAL if word in t)
        scored.append((-score, i, link))
    return [link for _, _, link in sorted(scored)]


_PICKED = re.compile(r"id:(\d{1,9})")  # /ds-music 删除 输入时选的那一首（Track.serial）
_NUMBER_SEPARATORS = re.compile(r"[\s,，、;；/]+|和|跟|还有|以及")
_RANGE_SIGNS = "-~～—–到至"
# “第 2 首”“2 号”“2 - 4”：先把空格去掉再分开
_SPACED_POSITION = re.compile(rf"(?<=第)\s+|\s+(?=[首号])|\s+(?=[{_RANGE_SIGNS}])|(?<=[{_RANGE_SIGNS}])\s+")
_RANGE = re.compile(rf"第?(\d{{1,4}})[首号]?[{_RANGE_SIGNS}]第?(\d{{1,4}})[首号]?")  # 第 2 到第 4 首
MAX_RANGE = 100
_POSITION = re.compile(r"[#＃]?(?:第\s*)?(-?\d{1,4})\s*(?:首|号|\.)?")  # -1：最后一首
_LAST = ("最后", "最后一首", "最后那首", "最后面那首", "倒数第一首", "last")
_CN_POSITION = re.compile(r"第\s*([一二两三四五六七八九十]{1,3})\s*(?:首|号)?|([一二两三四五六七八九十]{1,3})\s*(?:首|号)")
_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text: str) -> int | None:
    """一 ~ 九十九：“二”“十二”“二十”“二十二”。"""
    if "十" not in text:
        return _CN_DIGITS.get(text) if len(text) == 1 else None
    tens, _, ones = text.partition("十")
    if len(tens) > 1 or len(ones) > 1 or (tens and tens not in _CN_DIGITS) or (ones and ones not in _CN_DIGITS):
        return None
    return (_CN_DIGITS[tens] if tens else 1) * 10 + (_CN_DIGITS[ones] if ones else 0)


def positions(text: str) -> list[int] | None:
    """队列里的编号：“2”“第2首”“#2”“第二首”“2 3”“2、5 和 7”“2-4”“第 2 到第 4 首”都认；
    “最后一首”“-1”是 -1（倒着数）。不全是编号（比如歌名）就是 None。中文数字要带“第”或者“首”（只写“七”可能是歌名）。"""
    parts = [p for p in _NUMBER_SEPARATORS.split(_SPACED_POSITION.sub("", text.strip())) if p]
    if not parts:
        return None
    out: list[int] = []
    for part in parts:
        if part.casefold() in _LAST:
            out.append(-1)
            continue
        m = _RANGE.fullmatch(part)
        if m is not None:
            low, high = sorted((int(m.group(1)), int(m.group(2))))
            if low < 1 or high - low >= MAX_RANGE:
                return None
            out.extend(range(low, high + 1))
            continue
        m = _POSITION.fullmatch(part)
        if m is not None:
            out.append(int(m.group(1)))
            continue
        m = _CN_POSITION.fullmatch(part)
        number = _cn_number(m.group(1) or m.group(2)) if m is not None else None
        if number is None:
            return None
        out.append(number)
    return out


def _squash(text: str) -> str:
    """比歌名用：不分大小写，去掉空格、标点、书名号。"""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def title_matches(query: str, track: Track) -> bool:
    """要删的歌名对得上队列里的这首吗：整个（去掉空格、标点）是标题的一部分，或者每个词都在标题、UP主里。
    只有一个字的要和整个标题一样，按词对的时候也要有不止一个字的词（不然“雨”“第 2 首”会对上一大堆）；
    只有数字的要在标题里单独写着。"""
    wanted = _squash(query)
    if not wanted:
        return False
    if wanted.isdigit():  # 只有数字：要单独写在标题里（“2005”里的“5”、“1080P”里的“10”都不算）
        return re.search(rf"(?<![0-9a-z]){re.escape(wanted)}(?![0-9a-z])", track.title.casefold()) is not None
    title = _squash(track.title)
    if len(wanted) == 1:
        return title == wanted
    if wanted in title:
        return True
    words = [w for w in (_squash(w) for w in query.split()) if w]
    uploader = _squash(track.uploader)
    return len(words) > 1 and any(len(w) > 1 for w in words) and all(w in title or w in uploader for w in words)


def missing_parts() -> str:
    """点歌要用的东西缺了哪些（都在就是空的）。"""
    missing = [
        name
        for module, name in (("yt_dlp", "yt-dlp"), ("nacl", "PyNaCl"), ("davey", "davey"))
        if importlib.util.find_spec(module) is None
    ]
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")
    return ("缺少 " + "、".join(missing) + "（用新的 Dockerfile 重新构建镜像）") if missing else ""


def load_ytdlp() -> Any:
    try:
        import yt_dlp  # type: ignore[import-not-found]
    except ImportError:
        raise MusicError("bot 没装 yt-dlp，放不了歌（用新的 Dockerfile 重新构建镜像）", retry=False) from None
    return yt_dlp


_RISK = re.compile(r"\b412\b|precondition failed|rate limit|\(352\)|(?<![\d])-352(?![\d])")
_GONE = re.compile(r"\b404\b|not found|不存在|已失效")


def explain(error: BaseException, *, searching: bool = False) -> MusicError:
    """yt-dlp 的报错 → 说给人听的原因。"""
    text = str(error)
    low = text.casefold()
    if _RISK.search(low):
        tail = "，或者直接贴 B站链接" if searching else ""
        return MusicError(f"B站暂时把请求拦下来了（风控），过几分钟再试{tail}", retry=False)
    if "ffmpeg" in low or "ffprobe" in low:
        return MusicError("bot 这边的 ffmpeg 有问题，放不了（用新的 Dockerfile 重新构建镜像）", retry=False)
    if "unable to extract initial state" in low:
        return MusicError("B站的页面解析不了（可能被风控了，或者 B站改版了），过一会儿再试", retry=False)
    if "timed out" in low or "timeout" in low:
        return MusicError("连 B站超时了，过一会儿再试", retry=False)
    if "may be deleted" in low:
        return MusicError("这个视频不存在、被删了，或者在这里看不了（地区限制）")
    if _GONE.search(low):
        return MusicError("这个视频不存在，或者已经被删了")
    if "geo" in low or "from your location" in low or "your country" in low or "地区" in text or "地域" in text:
        return MusicError("这个视频在这里看不了（地区限制）")
    if any(
        k in low
        for k in ("premium", "login", "log in", "cookies", "registered users", "supporter", "purchase", "大会员", "付费", "登录")
    ):
        return MusicError("这个视频要登录、开大会员或者付费才能看")
    if "requested format is not available" in low or "no video formats" in low:
        return MusicError("这个视频没有能放的音频")
    if "unsupported url" in low or "no suitable extractor" in low:
        return MusicError("这个链接放不了（不是单个 B站视频，比如跳到了番剧之类的页面）")
    if "cancel" in low:
        return MusicError("取消了")
    return MusicError(f"出错了（{one_line(text.removeprefix('ERROR: '), 100)}）")


class _QuietLogger:
    """yt-dlp 的输出只进调试日志。"""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    info = debug

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp 警告: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp 错误: %s", msg)


def _options(**extra: Any) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "color": "no_color",
        "logger": _QuietLogger(),
        "socket_timeout": 20,
        "retries": 3,
        "cachedir": False,
        "noplaylist": True,  # 多 P 的只放链接里的那一 P（没写就是第 1 P）
        "allowed_extractors": EXTRACTORS,
        **extra,
    }


def _track(info: Any, url: str) -> Track:
    """yt-dlp 拿到的视频信息 → Track（还没下载）。"""
    if not isinstance(info, dict):
        raise MusicError("没拿到这个视频的信息")
    kind = info.get("_type", "video")
    if kind == "multi_video":
        raise MusicError("这是很老的分段视频，放不了")
    if kind != "video":
        raise MusicError("这是合集、播放列表或者互动视频，不是单个视频，放不了；挑里面的一个视频发链接")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise MusicError("这是直播，直播不放")
    duration = info.get("duration")
    return Track(
        url=canonical(str(info.get("webpage_url") or url)) or url,
        title=str(info.get("title") or "没有标题"),
        uploader=str(info.get("uploader") or ""),
        duration=float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        info=info,
        resolved_at=time.monotonic(),
    )


class Source(Protocol):
    async def search(self, query: str) -> list[str]: ...  # 按歌名找到的 B站视频（排好序）

    async def resolve(self, url: str) -> Track: ...  # 拿视频信息；放不了时抛 MusicError

    async def download(self, track: Track) -> str: ...  # 下载音频，返回文件；放不了、取消了抛 MusicError

    def discard(self, track: Track) -> None: ...  # 删掉下载的音频


class Redirects(Protocol):
    async def redirect_target(self, url: str) -> str | None: ...  # 这个网址跳转到哪里（不跳转是 None）


class BiliSource:
    def __init__(self, cache_dir: str | Path, tavily: Any | None = None, http: Redirects | None = None):
        self.cache = Path(cache_dir)
        self.tavily = tavily
        self.http = http  # 展开 b23.tv 短链接用（net.HttpClient）
        self._downloads = asyncio.Semaphore(3)  # 所有服务器加起来同时最多下载三首（每个服务器一次一首）
        self._api = asyncio.Lock()  # 向 B站要信息、搜歌：一次一个，隔开一点（太密会被风控）
        self._last_api = 0.0
        self._seq = itertools.count(1)  # 下载的文件名：同时下载的几首不会撞名

    def reset_cache(self) -> None:
        """启动时清掉上次没来得及删的音频。"""
        shutil.rmtree(self.cache, ignore_errors=True)
        self.cache.mkdir(parents=True, exist_ok=True)

    async def _call(self, func: Callable[..., Any], *args: Any) -> Any:
        async with self._api:
            wait = self._last_api + API_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await asyncio.wait_for(asyncio.to_thread(func, *args), INFO_TIMEOUT)
            except asyncio.TimeoutError:
                raise MusicError("B站那边太久没回应，过一会儿再试", retry=False) from None
            finally:
                self._last_api = time.monotonic()

    async def search(self, query: str) -> list[str]:
        if self.tavily is not None:
            try:
                found = rank(query, await self.tavily.links(query, domains=["bilibili.com"], max_results=10))
            except Exception as exc:
                log.warning("用 Tavily 找歌「%s」失败：%s", one_line(query, 40), exc)
            else:
                if found:
                    return found
        return rank(query, await self._call(self._bilisearch, query))  # 没有 Tavily（或者没找到）：用 B站自己的搜索

    def _bilisearch(self, query: str) -> list[tuple[str, str]]:
        yt_dlp = load_ytdlp()
        try:
            with yt_dlp.YoutubeDL(_options(extract_flat=True)) as ydl:
                info = ydl.extract_info(f"bilisearch5:{query}", download=False)
        except Exception as exc:
            raise explain(exc, searching=True) from None
        out: list[tuple[str, str]] = []
        for entry in (info or {}).get("entries") or []:
            if isinstance(entry, dict):
                url = entry.get("url") or entry.get("webpage_url")
                if isinstance(url, str):
                    out.append((url, str(entry.get("title") or "")))
        return out

    async def expand(self, url: str) -> str:
        """b23.tv 短链接 → B站视频链接。自己一步一步跟着跳转，只认 bilibili.com 的视频，别的地方都不去。"""
        if self.http is None:
            raise MusicError("短链接展开不了，发完整的 bilibili.com/video/… 链接")
        current = url
        for hop in range(3):
            try:
                target = await self.http.redirect_target(current)
            except Exception as exc:
                log.info("展开短链接 %s 失败：%s", current, exc)
                raise MusicError("短链接打不开（连不上 b23.tv），发完整的 B站视频链接试试") from None
            if not target:
                if hop == 0:
                    raise MusicError("这个短链接展开不了（失效了？），发完整的 bilibili.com/video/… 链接")
                break
            host = (urlsplit(target).hostname or "").lower()
            if host == "bilibili.com" or host.endswith(".bilibili.com"):
                video = canonical(target)
                if video is not None:
                    return video
                break
            if host not in SHORT_HOSTS:
                break
            current = target
        raise MusicError("这个短链接指向的不是 B站视频")

    async def resolve(self, url: str) -> Track:
        if is_short(url):
            url = await self.expand(url)
        return await self._call(self._resolve, url)

    def _resolve(self, url: str) -> Track:
        yt_dlp = load_ytdlp()
        try:
            # extract_flat：链接是合集之类的话，别把里面每个视频都去拿一遍信息
            with yt_dlp.YoutubeDL(_options(format=FORMAT, extract_flat="in_playlist")) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise explain(exc) from None
        return _track(info, url)

    async def download(self, track: Track) -> str:
        async with self._downloads:
            if track.cancel.is_set():
                raise MusicError("取消了")
            if _expired(track.info):
                await self._refresh(track)  # 排了很久，音频地址快过期了：先重新拿一次
            track.state = "downloading"
            try:
                return await asyncio.to_thread(self._download, track)
            except _Expired:
                log.info("%s 的音频地址过期了，重新拿一次信息", track.url)
            await self._refresh(track)
            try:
                return await asyncio.to_thread(self._download, track)
            except _Expired:
                raise MusicError("B站不让下载这个视频的声音") from None

    async def _refresh(self, track: Track) -> None:
        """重新向 B站拿一次视频信息（音频地址过期了）。被拦下来了（风控、超时）就抛 TryLater：过几分钟再试，别直接跳过。"""
        try:
            fresh = await self.resolve(track.url)
        except MusicError as exc:
            if not exc.retry:
                raise TryLater(str(exc), retry=False) from None
            raise
        track.info, track.resolved_at = fresh.info, fresh.resolved_at
        if track.cancel.is_set():
            raise MusicError("取消了")

    def _download(self, track: Track) -> str:
        yt_dlp = load_ytdlp()
        stem = f"{time.time_ns()}-{next(self._seq)}"
        started = time.monotonic()

        def check(_status: dict[str, Any]) -> None:  # 下载过程中不停地被调用：切掉了、太久了就中断
            if track.cancel.is_set():
                raise yt_dlp.utils.DownloadCancelled("cancelled")
            if time.monotonic() - started > DOWNLOAD_LIMIT:
                raise yt_dlp.utils.DownloadCancelled("timed out")

        opts = _options(
            format=FORMAT,
            outtmpl=str(self.cache / f"{stem}.%(ext)s"),
            progress_hooks=[check],
            match_filter=yt_dlp.utils.match_filter_func("!is_live"),  # 直播：不下载
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # 用第一步拿到的信息直接下载（和 yt-dlp 的 --load-info-json 一样），不用再向 B站要一次
                info = ydl.process_ie_result(yt_dlp.YoutubeDL.sanitize_info(track.info, True), download=True)
            path = _downloaded(info)
            if track.cancel.is_set():
                raise MusicError("取消了")
            if not path or not Path(path).is_file():
                raise MusicError("音频没下载下来")
            return path
        except MusicError:
            self._sweep(stem)
            raise
        except Exception as exc:
            self._sweep(stem)
            if track.cancel.is_set():
                raise MusicError("取消了") from None
            if time.monotonic() - started > DOWNLOAD_LIMIT:
                raise MusicError("下载太久了（超过 15 分钟），没下完") from None
            if _EXPIRED_ERROR.search(str(exc).casefold()):
                raise _Expired() from None
            raise explain(exc) from None
        finally:
            track.info = None  # 用不着了（很大）

    def _sweep(self, stem: str) -> None:
        """下载失败、取消：删掉下了一半的文件。"""
        for leftover in self.cache.glob(f"{stem}.*"):
            leftover.unlink(missing_ok=True)

    def discard(self, track: Track) -> None:
        if track.path:
            path = Path(track.path)
            if path.parent == self.cache:  # 只删自己下载的
                path.unlink(missing_ok=True)
            track.path = ""


def _expired(info: Any) -> bool:
    """B站的音频地址带着过期时间（deadline=…）：已经过期、或者快过期了吗。没有过期时间的当成没过期（下载时 403 再重拿）。"""
    if not isinstance(info, dict):
        return True
    urls = [info.get("url")] + [f.get("url") for f in info.get("requested_formats") or [] if isinstance(f, dict)]
    for url in urls:
        if isinstance(url, str):
            deadline = (parse_qs(urlsplit(url).query).get("deadline") or [""])[0]
            if deadline.isdigit():
                return time.time() > int(deadline) - EXPIRY_MARGIN
    return False


_AGAIN = re.compile(r"，过(?:几分钟|一会儿)再试.*$")  # 原因后面的“过几分钟再试”（暂停下载时换个说法）
_EXPIRED_ERROR = re.compile(r"http error (?:403|404|410)\b")


def _downloaded(info: Any) -> str:
    if not isinstance(info, dict):
        return ""
    for item in info.get("requested_downloads") or []:
        if isinstance(item, dict) and item.get("filepath"):
            return str(item["filepath"])
    return ""


class Voice(Protocol):
    def user_channel(self, guild_id: int, user_id: int) -> int | None: ...  # 这个人在哪个语音频道

    def bot_channel(self, guild_id: int) -> int | None: ...  # 它自己在哪个语音频道（连着就算，重连中也算）

    def connected_guilds(self) -> list[int]: ...  # 它在哪些服务器的语音频道里

    def listeners(self, guild_id: int) -> int: ...  # 它所在的语音频道里除了它还有几个真人

    def channel_name(self, channel_id: int) -> str: ...

    async def connect(self, guild_id: int, channel_id: int) -> None: ...  # 进（或换到）这个语音频道；不行时抛 MusicError

    def play(self, guild_id: int, path: str, on_end: Callable[[Exception | None], None]) -> None: ...

    def pause(self, guild_id: int) -> None: ...

    def resume(self, guild_id: int) -> None: ...

    def stop(self, guild_id: int) -> None: ...  # 停掉正在放的（之后会调 on_end）

    async def disconnect(self, guild_id: int) -> None: ...


@dataclass
class _Guild:
    queue: deque[Track] = field(default_factory=deque)  # 等着放的（前面一两首可能在下载）
    current: Track | None = None
    paused: bool = False
    text_channel_id: int | None = None  # 放歌的提示发到哪里（最后一次点歌的频道）
    idle_since: float | None = None
    alone_since: float | None = None
    stuck_since: float | None = None  # 下好的歌一直放不出去（语音卡在重连）从什么时候开始
    hold_until: float | None = None  # 被 B站拦下来了：这之前先不下载后面的歌
    hold_reason: str = ""
    skipped: bool = False  # 刚切过歌：接着放的那首不用再说“现在放”（切歌的回复里已经说了）


@dataclass(eq=False)
class _Attempt:
    """这一轮里的一次点歌。"""

    user_id: int
    track: Track | None = None  # 点上的那首（没点上是 None）


@dataclass(eq=False)
class RoundState:
    """模型一次输出的几个点歌动作（同一轮）共用的记录。动作照模型写的顺序一个一个办（看队列除外，放在最后），但是：
    - 编号按这一轮开始时的队列数（群友说“第 2 首”时看到的就是它）：前面先删了、切了，后面的编号也不会错位；
    - 切歌只切这一轮开始时正在放的那首：它已经被删歌切掉了、放完了，或者那时根本没在放，就不切；
    - 清空只清这一轮开始时排着的：“放 X，后面那些都不要了”不会把 X 也清掉；
    - “放 X，然后删掉”删的是 X，X 没点上就不删；
    - 这一轮里见过、现在已经不在的歌（删掉的、放完的、下载失败的）都还认得：按歌名、“他最近点的”找到的是
      它们，就说已经不在了，不会改删另一首同名的、或者他更早点的。"""

    current: Track | None  # 这一轮开始时正在放的
    queue: list[Track]  # 这一轮开始时排着的
    playing: int | None  # 这一轮开始时切歌会切掉的那首（Track.serial；什么都没有是 None）
    gone: list[Track] = field(default_factory=list)  # 这一轮里删掉、切掉、清掉的
    added: list[Track] = field(default_factory=list)  # 这一轮里点上的
    tried: list[_Attempt] = field(default_factory=list)  # 这一轮里的每次点歌（点没点上都算）

    def seen(self) -> list[Track]:
        """这一轮见过的歌：开始时在放的、排着的，这一轮里点上的、删掉的。"""
        return ([self.current] if self.current is not None else []) + self.queue + self.added + self.gone


_NOT_QUEUED = "这首已经不在队列里了（可能刚放完，或者被删了）"


class MusicPlayer:
    """每个服务器一个播放队列。给模型的 music 动作和 /ds-music 共用。"""

    def __init__(
        self,
        source: Source,
        voice: Voice,
        *,
        announce: Callable[[int, str], Awaitable[Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.source = source
        self.voice = voice
        self.announce = announce  # (文字频道, 内容)：自动放下一首、下载失败、离开时在频道里说一声
        self.clock = clock
        self._guilds: dict[int, _Guild] = {}
        self._locks: dict[int, asyncio.Lock] = {}  # 进、出语音频道
        self._fetch_locks: dict[int, asyncio.Lock] = {}  # 每个服务器一次下载一首
        self._busy: dict[int, int] = {}  # 正在点歌（找歌、拿信息、进频道）的数量：这期间定时检查别把它当成没事干
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._serials = itertools.count(1)  # Track.serial

    # ------------------------------------------------------------------ 点歌

    async def play(
        self,
        guild_id: int,
        text_channel_id: int,
        user_id: int,
        user_name: str,
        query: str,
        *,
        can_search: Callable[[], str | None] | None = None,
        state: RoundState | None = None,
    ) -> str:
        """点一首歌：query 是歌名或者 B站链接。can_search：按歌名找之前问一下还能不能搜（能搜就顺便记一次，返回 None；
        不能搜返回原因）。state：同一轮的记录（见 RoundState）。"""
        attempt = _Attempt(user_id)
        if state is not None:
            state.tried.append(attempt)
        if self._closing:
            return "bot 正在重启，过一会儿再点"
        channel = self.voice.user_channel(guild_id, user_id)
        if channel is None:
            return "点歌的人要先进一个语音频道，我才知道去哪里放"
        problem = self._busy_elsewhere(guild_id, channel)
        if problem:
            return problem
        query = " ".join(query.split())
        if not query:
            return "没说要放什么"
        self._busy[guild_id] = self._busy.get(guild_id, 0) + 1
        try:
            return await self._play(guild_id, text_channel_id, channel, user_id, user_name, query, can_search, state, attempt)
        finally:
            self._busy[guild_id] -= 1
            if not self._busy[guild_id]:
                del self._busy[guild_id]

    async def _play(
        self,
        guild_id: int,
        text_channel_id: int,
        channel: int,
        user_id: int,
        user_name: str,
        query: str,
        can_search: Callable[[], str | None] | None,
        state: RoundState | None,
        attempt: _Attempt,
    ) -> str:
        link = find_link(query)
        if link is not None:
            urls = [link]
        else:
            reason = unsupported(query)
            if reason:
                return reason
            refused = can_search() if can_search is not None else None
            if refused:
                return refused
            try:
                urls = await self.source.search(query)
            except MusicError as exc:
                return str(exc)
            except Exception as exc:
                log.warning("找歌「%s」出错：%r", one_line(query, 40), exc)
                return "找歌的时候出错了"
            if not urls:
                return f"在 B站没找到「{one_line(query, 40)}」"
        track: Track | None = None
        problems: list[str] = []
        for url in urls[:MAX_TRIES]:
            try:
                track = await self.source.resolve(url)
                break
            except MusicError as exc:
                problems.append(str(exc))
                if not exc.retry:
                    break
            except Exception as exc:
                log.warning("拿 %s 的信息出错：%r", url, exc)
                problems.append("拿视频信息的时候出错了")
        if track is None:
            return problems[0] if problems else "没找到能放的"
        track.requester_id, track.requester_name = user_id, user_name

        async with self._lock(guild_id):  # 和离开语音频道错开：正在离开的话，等它离开完再进
            if self._closing:
                return "bot 正在重启，过一会儿再点"
            problem = self._busy_elsewhere(guild_id, channel)
            if problem:
                return problem
            try:
                await self.voice.connect(guild_id, channel)
            except MusicError as exc:
                return str(exc)
            except Exception as exc:
                log.warning("服务器 %s：进语音频道出错：%r", guild_id, exc)
                return "进不了那个语音频道"
            if self._closing:
                return "bot 正在重启，过一会儿再点"  # 进频道的时候开始关机了
            g = self._guilds.setdefault(guild_id, _Guild())
            g.text_channel_id = text_channel_id
            g.idle_since = None
            ahead = len(g.queue) + (g.current is not None)
            track.waiting = ahead == 0
            track.done = asyncio.Event()
            track.serial = next(self._serials)
            g.queue.append(track)
            attempt.track = track
            if state is not None:
                state.added.append(track)
            self._pump(guild_id, g)
        if ahead:
            return f"加进队列了（前面还有 {ahead} 首）：{track.label()}"
        if g.hold_until is not None and not track.fetching:
            track.waiting = False  # 刚被 B站拦下来：不用等了，直接说
            return f"{g.hold_reason}，{track.label()}过几分钟再开始下载"
        try:
            await asyncio.wait_for(track.done.wait(), READY_WAIT)
        except asyncio.TimeoutError:
            pass
        track.waiting = False
        if track.state == "failed":
            return track.error or "下载失败了"
        g = self._guilds.get(guild_id)
        if g is not None and g.current is track:
            return f"开始播放：{track.label()}"
        if track.state == "cancelled" or g is None or not any(t is track for t in g.queue):
            return f"{track.label()}还没开始放就被切掉了"  # 等它下载的时候被切掉、删掉、停掉了
        if track.state == "ready":
            return f"下载好了，等一下就开始放：{track.label()}"
        if g is not None and g.hold_until is not None and not track.fetching:
            return f"{g.hold_reason}，{track.label()}过几分钟再开始下载"
        return f"正在下载{track.label()}，下好了马上开始放"

    def _lock(self, guild_id: int) -> asyncio.Lock:
        """进、出语音频道（连同改播放队列）一次只做一件。"""
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def _busy_elsewhere(self, guild_id: int, channel: int) -> str | None:
        here = self.voice.bot_channel(guild_id)
        g = self._guilds.get(guild_id)
        if here is not None and here != channel and g is not None and (g.current is not None or g.queue):
            return f"我正在「{self.voice.channel_name(here)}」放歌，过来一起听，或者等那边放完"
        return None

    def _listening(self, guild_id: int, user_id: int) -> str | None:
        """切歌、暂停、停止、删歌要在它放歌的那个语音频道里。可以就返回 None，不行就返回原因。"""
        here = self.voice.bot_channel(guild_id)
        if here is None:
            return "我没在语音频道里"
        if self.voice.user_channel(guild_id, user_id) != here:
            return f"要在我放歌的「{self.voice.channel_name(here)}」里才能操作"
        return None

    # ------------------------------------------------------------------ 控制

    async def skip(self, guild_id: int, user_id: int, *, state: RoundState | None = None) -> str:
        """切歌。state：同一轮的记录（见 RoundState）：只切这一轮开始时正在放的那首。它已经不在放了（比如同一轮里
        删歌删到了它），就别再把下一首也切掉；那时根本没在放，就别把这一轮刚点的切掉。"""
        g = self._guilds.get(guild_id)
        if g is None or (g.current is None and not g.queue):
            return "现在没在放歌"
        problem = self._listening(guild_id, user_id)
        if problem:
            return problem
        if state is not None and self.playing_serial(guild_id) != state.playing:
            if state.playing is None:
                return "叫我切歌的时候还没在放歌，刚点的这首照放"
            return "要切的那首已经不在放了"
        gone = self._skip(guild_id, g)
        if state is not None:
            state.gone.append(gone)
        return f"跳过了{gone.label()}{self._after_skip(g)}"

    def round_state(self, guild_id: int) -> RoundState:
        """一轮点歌动作开始之前，记下现在的队列（见 RoundState）。"""
        g = self._guilds.get(guild_id)
        if g is None:
            return RoundState(None, [], None)
        return RoundState(g.current, list(g.queue), self.playing_serial(guild_id))

    def playing_serial(self, guild_id: int) -> int | None:
        """现在切歌会切掉的那一首（正在放的；还没开始放就是队列里的第一首）的 Track.serial。"""
        g = self._guilds.get(guild_id)
        if g is None:
            return None
        track = g.current if g.current is not None else (g.queue[0] if g.queue else None)
        return track.serial if track is not None else None

    def _skip(self, guild_id: int, g: _Guild) -> Track:
        """切掉正在放的那首（还没开始放的话就是队列里的第一首），接着放下一首。"""
        if g.current is not None:
            gone = g.current
            g.current = None  # 先放下一首，正在放的那首结束的回调来了也不管
            try:
                self.voice.stop(guild_id)
            except Exception as exc:
                log.info("服务器 %s：停止播放出错：%r", guild_id, exc)
            self.source.discard(gone)
        else:
            gone = g.queue.popleft()  # 第一首还没开始放（还在下载，或者语音还没连好）
            self._drop(gone)
        g.skipped = True  # 回复里会说接下来放什么，频道里不用再说“现在放”
        self._start_next(guild_id, g)
        return gone

    def _after_skip(self, g: _Guild) -> str:
        """切歌之后接着放什么（接在回复后面）。"""
        if g.current is not None:
            return f"，接下来放{g.current.label()}"
        if g.queue:
            nxt = g.queue[0]
            if nxt.state == "ready":
                return f"，下一首{nxt.label()}等一下就放"
            if g.hold_until is not None and not nxt.fetching:
                return f"，{g.hold_reason}，下一首{nxt.label()}过几分钟再开始下载"
            return f"，下一首{nxt.label()}还在下载，下好了就放"
        return "，后面没有歌了"

    def _drop(self, track: Track) -> None:
        """从队列里拿掉的一首：不用再下载了，下好的删掉。"""
        track.cancel.set()  # 还在下载的：下完（或者中断）后由 _fetch 删掉
        self.source.discard(track)
        if not track.fetching and track.state == "waiting":
            track.state = "cancelled"  # 还没开始下载（没有 _fetch 来收尾）
            if track.done is not None:
                track.done.set()

    async def remove(
        self, guild_id: int, user_id: int, targets: Sequence[str] = (), *, state: RoundState | None = None
    ) -> str:
        """从队列里删歌。targets 每一项是队列里的编号（"2"、"第2首"、"2 3"、"2-4"）、歌名，或者 /ds-music 删除
        输入时选的那一首（"id:17"）；都没写就是这个人自己最近点的那首。删到正在放的那首就是切歌。
        一次删好几首时，编号都按删之前的队列算。state：同一轮的记录（见 RoundState），编号按这一轮开始时的队列算。"""
        g = self._guilds.get(guild_id)
        if g is None or (g.current is None and not g.queue):
            return "现在没在放歌"
        problem = self._listening(guild_id, user_id)
        if problem:
            return problem
        reasons: list[str] = []
        picked: list[Track] = []
        for target in self._wanted(targets):
            found, problem = self._find(g, user_id, target, state)
            if problem and problem not in reasons:
                reasons.append(problem)
            for track in found:
                if not any(track is p for p in picked):  # “2 2”“3 最后一首”：同一首只删一次
                    picked.append(track)
        if not picked:
            return "；".join(reasons) or "没找到要删的歌"
        current, head = g.current, (g.queue[0] if g.queue else None)
        dropped = [t for t in picked if t is not current]
        for track in dropped:
            if any(t is track for t in g.queue):
                g.queue.remove(track)
            self._drop(track)
        if state is not None:
            state.gone.extend(dropped)
        parts: list[str] = []
        if dropped:
            names = "、".join(f"{t.label()}（{t.requester_name} 点的）" for t in dropped)
            parts.append(f"从队列里删掉了{names}" if len(dropped) == 1 else f"从队列里删掉了 {len(dropped)} 首：{names}")
        if current is not None and any(t is current for t in picked):
            self._skip(guild_id, g)
            if state is not None:
                state.gone.append(current)
            parts.append(f"跳过了正在放的{current.label()}{self._after_skip(g)}")
        elif current is None and head is not None and any(t is head for t in dropped):
            g.skipped = True  # 删掉的是马上就要放的那首（还在下载）：接着放下一首，回复里说
            self._start_next(guild_id, g)
            parts[-1] += self._after_skip(g)
        else:
            self._pump(guild_id, g)  # 后面的往前挪了：该下载的接着下载
            parts[-1] += f"，后面还有 {len(g.queue)} 首" if g.queue else "，后面没有排着的歌了"
        return "；".join(parts + reasons)

    @staticmethod
    def _wanted(targets: Sequence[str]) -> list[str]:
        """要删的那几项：去掉多余的空格和重复的；什么都没写就是 [""]（他自己最近点的那首）。"""
        return list(dict.fromkeys(" ".join((t or "").split()) for t in targets)) or [""]

    def _find(
        self, g: _Guild, user_id: int, target: str, state: RoundState | None
    ) -> tuple[list[Track], str | None]:
        """要删的是哪几首。返回 (找到的, 找不到的原因)。编号按 state 记下的这一轮开始时的队列数（没有就按现在的）。"""
        current, live = g.current, list(g.queue)
        alive = ([current] if current is not None else []) + live
        ghosts = self._ghosts(state, alive)
        if not target:
            attempts = [a for a in state.tried if a.user_id == user_id] if state is not None else []
            if attempts:
                latest = attempts[-1].track  # “放 X，然后删掉”：删的是这一轮刚点的 X
                if latest is None:
                    return [], "这次点的歌没点上，没有要删的"
                mine = [latest]
            else:
                mine = sorted((t for t in alive + ghosts if t.requester_id == user_id), key=lambda t: t.serial)
            if not mine:
                return [], "队列里没有你点的歌；要删别人点的，说是第几首或者歌名"
            return ([mine[-1]], None) if any(mine[-1] is t for t in alive) else ([], _NOT_QUEUED)
        picked = _PICKED.fullmatch(target)
        if picked is not None:
            serial = int(picked.group(1))
            for track in alive:
                if track.serial == serial:
                    return [track], None
            return [], _NOT_QUEUED
        numbers = positions(target)
        if numbers is None:
            return self._by_title(current, live, ghosts, user_id, target)
        counted = state.queue if state is not None else live
        numbers = [len(counted) + n + 1 if n < 0 else n for n in numbers]  # 倒着数的：-1 是最后一首
        found: list[Track] = []
        problems: list[str] = []
        for n in numbers:
            if 1 <= n <= len(counted):
                track = counted[n - 1]
                if any(track is t for t in alive):
                    found.append(track)
                elif (text := f"第 {n} 首{track.label()}已经不在队列里了") not in problems:
                    problems.append(text)  # 这一轮前面已经删掉、切掉了
        bad = [n for n in numbers if not 1 <= n <= len(counted)]
        if bad:
            if not found and not problems and target.isdigit() and len(target) >= 3:
                # 歌名就是数字（比如《1874》），队列又没那么长：标题里单独写着这个数的（“2005”里的“5”不算），
                # 照按歌名找的规矩（同名的、刚删掉的），但不算正在放的那首（说的是编号，别切歌）
                if any(title_matches(target, t) for t in live + ghosts):
                    return self._by_title(None, live, ghosts, user_id, target)
            then = "说的时候" if state is not None and len(live) != len(counted) else ""  # 这一轮里队列变过
            if not counted:
                problems.append(f"{then}后面没有排着的歌" + ("" if then else "了（正在放的这首要用切歌）"))
            elif any(n <= 0 for n in bad):
                problems.append("编号从 1 开始（1 是下一首；正在放的这首要用切歌）")
            else:
                listed = "、".join(str(n) for n in bad[:5]) + ("…" if len(bad) > 5 else "")
                problems.append(f"{then}队列里只有 {len(counted)} 首，没有第 {listed} 首")
        return found, "；".join(problems) or None

    @staticmethod
    def _ghosts(state: RoundState | None, alive: list[Track]) -> list[Track]:
        """这一轮见过、现在已经不在的歌（这一轮里删掉的，还有中途放完的、下载失败的、被别人删掉的）。"""
        out: list[Track] = []
        for track in state.seen() if state is not None else []:
            if not any(track is t for t in alive) and not any(track is t for t in out):
                out.append(track)
        return out

    def _by_title(
        self, current: Track | None, live: list[Track], ghosts: list[Track], user_id: int, target: str
    ) -> tuple[list[Track], str | None]:
        """按歌名找。这一轮见过、已经不在的也算进来：对上的是它，就说已经不在了，不改删另一首同名的。
        current 是 None：不算正在放的那首。"""
        alive = ([current] if current is not None else []) + live
        found = [t for t in alive + list(ghosts) if title_matches(target, t)]
        if not found:
            return [], f"队列里没有对得上「{one_line(target, 30)}」的歌"
        if len(found) > 1:
            mine = [t for t in found if t.requester_id == user_id]
            if len({t.url for t in found}) == 1:
                found = [max(found, key=lambda t: t.serial)]  # 同一首点了好几次：删后点的那次（正在放的接着放）
            elif len(mine) == 1:
                found = mine  # 好几首对得上，其中只有一首是他点的
            else:

                def where(t: Track) -> str:
                    if t is current:
                        return f"正在放的{t.label()}"
                    if any(t is x for x in live):
                        return f"第 {live.index(t) + 1} 首{t.label()}"
                    return f"已经不在队列里的{t.label()}"

                listed = "、".join(where(t) for t in found[:5])
                return [], f"有好几首对得上「{one_line(target, 30)}」：{listed}，说一下是第几首"
        track = found[0]
        return ([track], None) if any(track is t for t in alive) else ([], _NOT_QUEUED)

    async def clear(self, guild_id: int, user_id: int, *, state: RoundState | None = None) -> str:
        """清掉排着的歌，正在放的这首接着放完。state：同一轮的记录（见 RoundState）：只清这一轮开始时排着的，
        这一轮里新点的留着；那时排着的、这会儿已经开始放了（前面先切了歌）的也一起切掉。"""
        g = self._guilds.get(guild_id)
        if g is None or (g.current is None and not g.queue):
            return "现在没在放歌"
        problem = self._listening(guild_id, user_id)
        if problem:
            return problem
        alive = ([g.current] if g.current is not None else []) + list(g.queue)
        gone = list(g.queue) if state is None else [t for t in state.queue if any(t is a for a in alive)]
        if not gone:
            return "后面没有排着的歌"
        playing = g.current is not None and any(t is g.current for t in gone)
        for track in gone:
            if track is not g.current:
                if any(t is track for t in g.queue):
                    g.queue.remove(track)
                self._drop(track)
        if state is not None:
            state.gone.extend(gone)
        if playing:
            self._skip(guild_id, g)
        elif g.current is None:
            self._start_next(guild_id, g)  # 什么都没在放（第一首还在下载就清掉了）：放这一轮新点的，或者开始算闲置
        else:
            self._pump(guild_id, g)
        if g.queue:
            ours = [t for t in g.queue if state is not None and any(t is a for a in state.added)]
            text = f"清掉了之前排着的 {len(gone)} 首"
            if ours:
                text += "，这次点的" + "、".join(t.label() for t in ours[:3]) + ("等" if len(ours) > 3 else "") + "留着"
            if len(ours) < len(g.queue):
                text += f"，后面还有别人刚点的 {len(g.queue) - len(ours)} 首"
            return text
        if g.current is not None:
            return f"清掉了后面排着的 {len(gone)} 首，正在放的{g.current.label()}放完就没了"
        return f"清掉了队列里的 {len(gone)} 首，现在没有歌了"

    async def pause(self, guild_id: int, user_id: int) -> str:
        g = self._guilds.get(guild_id)
        if g is None or g.current is None:
            return "第一首还没开始放（还在下载）" if g is not None and g.queue else "现在没在放歌"
        problem = self._listening(guild_id, user_id)
        if problem:
            return problem
        if g.paused:
            return "已经暂停了"
        self.voice.pause(guild_id)
        g.paused = True
        return f"暂停了{g.current.label()}"

    async def resume(self, guild_id: int, user_id: int) -> str:
        g = self._guilds.get(guild_id)
        if g is None or g.current is None:
            return "第一首还没开始放（还在下载），下好了就放" if g is not None and g.queue else "现在没在放歌"
        problem = self._listening(guild_id, user_id)
        if problem:
            return problem
        if not g.paused:
            return "没有暂停，正在放"
        self.voice.resume(guild_id)
        g.paused = False
        return f"继续放{g.current.label()}"

    async def stop(self, guild_id: int, user_id: int) -> str:
        g = self._guilds.get(guild_id)
        queued = len(g.queue) if g is not None else 0
        anything = queued or (g is not None and g.current is not None)
        if self.voice.bot_channel(guild_id) is None:
            if g is None:
                return "我没在语音频道里"
            await self._leave(guild_id)  # 语音已经断了：把剩下的队列清掉
            return "我没在语音频道里" + ("，队列也清掉了" if anything else "")
        problem = self._listening(guild_id, user_id)
        if problem:
            return problem
        await self._leave(guild_id)
        return "停了，我先离开语音频道" + (f"（后面排着的 {queued} 首也清掉了）" if queued else "")

    def queue_text(self, guild_id: int, sep: str = "\n") -> str:
        g = self._guilds.get(guild_id)
        if g is None or (g.current is None and not g.queue):
            return "现在没在放歌"
        lines = []
        if g.current is not None:
            state = "暂停中" if g.paused else "正在放"
            lines.append(f"{state}：{g.current.label()}（{g.current.requester_name} 点的）")
        for i, track in enumerate(list(g.queue)[:QUEUE_SHOWN], 1):
            tail = "（下载中）" if track.fetching and track.state in ("waiting", "downloading") else ""
            lines.append(f"{i}. {track.label()}{tail}（{track.requester_name} 点的）")
        if len(g.queue) > QUEUE_SHOWN:
            lines.append(f"……后面还有 {len(g.queue) - QUEUE_SHOWN} 首")
        return sep.join(lines)

    def status(self, guild_id: int) -> str | None:
        """一句话：这个服务器现在在放什么（没在放就是 None）。"""
        g = self._guilds.get(guild_id)
        if g is None or g.current is None:
            return None
        here = self.voice.bot_channel(guild_id)
        where = f"语音频道「{self.voice.channel_name(here)}」" if here is not None else "语音频道"
        state = "暂停在" if g.paused else "正在放"
        tail = f"，后面还有 {len(g.queue)} 首" if g.queue else ""
        return f"{where}{state}：{g.current.label()}，{g.current.requester_name} 点的{tail}"

    def queued(self, guild_id: int) -> int:
        """后面排着几首。"""
        g = self._guilds.get(guild_id)
        return len(g.queue) if g is not None else 0

    def choices(self, guild_id: int, typed: str = "") -> list[tuple[str, str]]:
        """/ds-music 删除 输入时列出来的歌：[(显示的字, 值)]。值是 "id:编号"：选好之后队列往前走了也不会删错。
        typed：已经输入的字（编号的开头，或者歌名、点歌的人的一部分）。"""
        g = self._guilds.get(guild_id)
        if g is None:
            return []
        rows: list[tuple[str, Track]] = [("", g.current)] if g.current is not None else []
        rows += [(str(i), t) for i, t in enumerate(g.queue, 1)]
        typed = " ".join(typed.split())
        numbers = positions(typed) if typed else None
        if numbers is not None and len(numbers) != 1:
            return []  # 一次写了好几个编号：不列出来（照写的删）
        prefix = None
        if numbers:
            n = numbers[0]
            prefix = str(len(g.queue) + n + 1 if n < 0 else n) if not typed.isdigit() else typed
        out: list[tuple[str, str]] = []
        for no, track in rows:
            if prefix is not None:
                if not no.startswith(prefix):
                    continue
            elif typed:
                wanted = _squash(typed)
                where = "\n".join(_squash(x) for x in (track.title, track.uploader, track.requester_name))
                if wanted and wanted not in where and not title_matches(typed, track):
                    continue
            head = f"{no}. " if no else "正在放："
            name = f"{head}{one_line(track.title, 70) or '没有标题'}（{one_line(track.requester_name, 20)} 点的）"
            out.append((name[:100], f"id:{track.serial}"))
            if len(out) >= QUEUE_CHOICES:
                break
        return out

    # ------------------------------------------------------------------ 内部

    def _pump(self, guild_id: int, g: _Guild) -> None:
        """队列里接下来的几首交给后台下载（不是全部：队列很长、视频很长时，硬盘和网速都吃不消）。"""
        if self._closing or (g.hold_until is not None and self.clock() < g.hold_until):
            return  # 要关机了；或者刚被 B站拦下来，过几分钟再接着下载
        for track in list(g.queue)[:PREFETCH]:
            if not track.fetching and track.state == "waiting":
                track.fetching = True
                self._spawn(self._fetch(guild_id, track))

    async def _fetch(self, guild_id: int, track: Track) -> None:
        """后台下载一首；下好了、而且轮到它了就开始放。"""
        try:
            async with self._fetch_locks.setdefault(guild_id, asyncio.Lock()):  # 同一个服务器按顺序一首一首下载
                if track.cancel.is_set():
                    raise MusicError("取消了")
                g = self._guilds.get(guild_id)
                if g is not None and g.hold_until is not None and self.clock() < g.hold_until:
                    track.fetching = False  # 排在后面的这首等的时候，前面那首被 B站拦下来了：先别下载，等够了按顺序接着来
                    return
                track.path = await self.source.download(track)
            track.state = "ready"
        except TryLater as exc:
            track.state, track.fetching = "waiting", False  # 留在队列里：过几分钟（定时检查时）再下载
            if not track.cancel.is_set():
                self._hold(guild_id, str(exc))
            return
        except asyncio.CancelledError:
            track.state = "cancelled"
            if track.done is not None:
                track.done.set()
            raise
        except MusicError as exc:
            track.state = "cancelled" if track.cancel.is_set() else "failed"
            track.error = str(exc)
        except Exception as exc:
            log.warning("下载 %s 出错：%r", track.url, exc)
            track.state, track.error = "failed", "下载的时候出错了"
        if track.done is not None:
            track.done.set()
        g = self._guilds.get(guild_id)
        if g is None or not any(t is track for t in g.queue):
            self.source.discard(track)  # 下载的时候被切掉、停掉了
            return
        if track.state != "ready":
            g.queue.remove(track)
            if track.state == "failed" and not track.waiting and g.text_channel_id is not None:
                log.info("服务器 %s：%s 下载失败：%s", guild_id, track.url, track.error)
                self._say(g.text_channel_id, f"⚠️ {track.label()}下载失败（{track.error}），跳过")
        if g.current is None:
            self._start_next(guild_id, g)
        else:
            self._pump(guild_id, g)

    def _hold(self, guild_id: int, reason: str) -> None:
        """下载前重新拿信息时被 B站拦下来了：这个服务器后面的歌先别下载，过几分钟再接着来（只说一次）。"""
        g = self._guilds.get(guild_id)
        if g is None:
            return
        first = g.hold_until is None
        g.hold_until = self.clock() + HOLD_SECONDS
        g.hold_reason = _AGAIN.sub("", reason) or reason
        log.info("服务器 %s：%s，后面的歌过几分钟再下载", guild_id, reason)
        if first and g.text_channel_id is not None:
            self._say(g.text_channel_id, f"⚠️ {g.hold_reason}，后面的歌过几分钟再接着下载")

    def _start_next(self, guild_id: int, g: _Guild) -> None:
        """放队列里的下一首。下一首还没下载完就先等着（下载完会再调这里）。"""
        announce = not g.skipped
        g.skipped = False
        g.current, g.paused = None, False
        stuck, g.stuck_since = g.stuck_since, None  # 只有这一次还是因为语音没连好而放不出去，才接着算“卡住了多久”
        self._pump(guild_id, g)
        while g.queue:
            head = g.queue[0]
            if head.state != "ready":
                if head.state in ("waiting", "downloading"):
                    return
                g.queue.popleft()  # 失败、取消了的（一般不会还在队列里）
                continue
            if self.voice.bot_channel(guild_id) is None:
                return  # 语音断了：队列先留着，定时检查时清掉（只说一次）
            try:
                self.voice.play(guild_id, head.path, self._on_end(guild_id, head))
            except VoiceNotReady:
                g.stuck_since = stuck if stuck is not None else self.clock()  # 语音还没连好：留着，定时检查时再放
                return
            except Exception as exc:
                log.warning("服务器 %s：放不出来 %s：%r", guild_id, head.url, exc)
                head.failures += 1
                if head.failures < PLAY_TRIES:
                    return  # 可能是临时的问题：留在队列里，定时检查时再试一次
                g.queue.popleft()
                self.source.discard(head)
                if head.waiting:
                    head.state, head.error = "failed", "放不出来，再点一次试试"
                elif g.text_channel_id is not None:
                    self._say(g.text_channel_id, f"⚠️ {head.label()}放不出来，跳过")
                self._pump(guild_id, g)
                continue
            g.queue.popleft()
            g.current, g.idle_since = head, None
            if announce and not head.waiting and g.text_channel_id is not None:
                self._say(g.text_channel_id, f"🎵 现在放：{head.label()}（{head.requester_name} 点的）")
            self._pump(guild_id, g)  # 队列往前走了：接着下载后面的
            return
        g.idle_since = self.clock()

    def _on_end(self, guild_id: int, track: Track) -> Callable[[Exception | None], None]:
        loop = asyncio.get_running_loop()

        def on_end(error: Exception | None) -> None:  # discord.py 在放音频的线程里调它
            try:
                loop.call_soon_threadsafe(self._ended, guild_id, track, error)
            except RuntimeError:
                pass  # 正在关机

        return on_end

    def _ended(self, guild_id: int, track: Track, error: Exception | None) -> None:
        self.source.discard(track)
        g = self._guilds.get(guild_id)
        if g is None or g.current is not track:
            return  # 已经切掉、停掉了
        if error is not None:
            log.warning("服务器 %s：放 %s 的时候出错：%r", guild_id, track.url, error)
            if g.text_channel_id is not None:
                self._say(g.text_channel_id, f"⚠️ {track.label()}放到一半出错了，跳过")
        self._start_next(guild_id, g)

    async def _leave(self, guild_id: int) -> None:
        g = self._guilds.pop(guild_id, None)
        if g is not None:
            for track in g.queue:
                track.cancel.set()  # 还在下载的：下完（或者中断）后由 _fetch 删掉
                self.source.discard(track)
            g.queue.clear()
            current, g.current = g.current, None
            if current is not None:
                try:
                    self.voice.stop(guild_id)
                except Exception as exc:
                    log.info("服务器 %s：停止播放出错：%r", guild_id, exc)
                self.source.discard(current)
        async with self._lock(guild_id):
            if guild_id in self._guilds:
                return  # 等锁的时候又有人点了歌（已经进了语音频道）：别把它踢出去
            try:
                await self.voice.disconnect(guild_id)
            except Exception as exc:
                log.info("服务器 %s：离开语音频道出错：%r", guild_id, exc)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _say(self, channel_id: int, text: str) -> None:
        if self.announce is not None and not self._closing:
            self._spawn(self._announce(channel_id, text))

    async def _announce(self, channel_id: int, text: str) -> None:
        assert self.announce is not None
        try:
            await self.announce(channel_id, text)
        except Exception as exc:
            log.info("点歌的提示发不出去（频道 %s）：%r", channel_id, exc)

    async def tick(self) -> None:
        """定时检查：断线了清掉队列；频道里没人、或者放完很久没人点歌，就离开。"""
        now = self.clock()
        for guild_id in self.voice.connected_guilds():
            if guild_id not in self._guilds and not self._busy.get(guild_id) and not self._lock(guild_id).locked():
                await self._leave(guild_id)  # 没在放歌却还在语音频道里（比如上次离开时出错了）
        for guild_id in list(self._guilds):
            g = self._guilds.get(guild_id)
            if g is None or self._busy.get(guild_id) or self._lock(guild_id).locked():
                continue
            channel = g.text_channel_id
            if self.voice.bot_channel(guild_id) is None:
                had = g.current is not None or bool(g.queue)
                await self._leave(guild_id)
                if had and channel is not None:
                    log.info("服务器 %s：语音连接断了，清掉播放队列", guild_id)
                    self._say(channel, "⚠️ 语音连接断了，播放队列清掉了")
                continue
            if self.voice.listeners(guild_id) == 0:
                if g.alone_since is None:
                    g.alone_since = now
                elif now - g.alone_since >= ALONE_LEAVE:
                    await self._leave(guild_id)
                    if channel is not None:
                        self._say(channel, "👋 语音频道里没人了，我先走了")
                    continue
            else:
                g.alone_since = None
            if g.hold_until is not None and now >= g.hold_until:
                g.hold_until = None  # 等够了：接着下载
                self._pump(guild_id, g)
            if g.current is None and g.queue:
                g.idle_since = None
                if g.queue[0].state == "ready":
                    self._start_next(guild_id, g)  # 上次没放出来（比如语音刚好在重连）：再试一次
                    if g.current is None and g.stuck_since is not None and now - g.stuck_since >= STUCK_LEAVE:
                        log.info("服务器 %s：语音一直连不上，先离开", guild_id)
                        await self._leave(guild_id)
                        if channel is not None:
                            self._say(channel, "⚠️ 语音一直连不上，我先离开了（队列清掉了），等一下再点")
                        continue
            elif g.current is None:
                if g.idle_since is None:
                    g.idle_since = now
                elif now - g.idle_since >= IDLE_LEAVE:
                    await self._leave(guild_id)  # 放完了很久没人点歌：安静地离开

    def closing(self) -> None:
        """要关机了：不再接新的点歌，正在下载的都停掉（下载线程不停下来，进程就退不出去）。"""
        self._closing = True
        for g in self._guilds.values():
            for track in g.queue:
                track.cancel.set()

    async def close(self) -> None:
        self.closing()
        for guild_id in list(self._guilds):
            await self._leave(guild_id)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
