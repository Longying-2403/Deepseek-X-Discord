"""读链接：X（Twitter）的推文走 FxTwitter 的免费接口（不用账号），别的网页交给 Tavily 去抓。

NAS 自己不去打开网页：推文由 FxTwitter 读，网页由 Tavily 的服务器读，家里的内网和 IP 都不会暴露给群友发的链接。
只有推文里的图片会由 bot 自己下载（只从 X 的图片服务器 pbs.twimg.com 下载，走 net.py 的公网检查）。
要登录才能看的网站（Threads、IG、Facebook……）多半打不开，那就只看 Discord 生成的预览。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .images import to_data_uri
from .models import FileText, LinkResult
from .net import FetchError, HttpClient, check_url, host_of
from .tavily import Tavily, TavilyError
from .textutil import clean_text, one_line

log = logging.getLogger(__name__)

FXTWITTER_API = "https://api.fxtwitter.com/2/status/{id}"
TWEET_IMAGE_HOSTS = frozenset({"pbs.twimg.com"})
MAX_TWEET_PHOTOS = 2
MAX_LINKS_PER_MESSAGE = 5
IMAGE_MAX_BYTES = 8_000_000

# 群友发的链接：遇到空白、尖括号、引号、反引号、|（剧透 ||…||）、全角标点就算结束
# （浏览器复制出来的网址里，中文都是 %E6%B0%B4 这样编码过的，不会有全角标点；“网址。还有”这种要在句号前断开）
_URL = re.compile(r"https?://[^\s<>\"'`|。，、；：！？「」『』（）【】《》…]+", re.I)
_TRAILING = ".,;:!?)]}>'\"。，、；：！？）】」』》…"
X_HOSTS = frozenset(
    {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com", "fxtwitter.com", "fixupx.com", "vxtwitter.com", "fixvx.com", "twittpr.com"}
)
_X_STATUS = re.compile(r"^/(?:[^/]+|i(?:/web)?)/status(?:es)?/(\d{2,20})(?:/|$)")
# Discord 自己的链接、GIF 网站：GIF 和图片已经按图片看了；Discord 的消息链接要登录
_SKIP_HOSTS = ("discord.com", "discordapp.com", "discord.gg", "discordapp.net", "discord.media", "tenor.com", "giphy.com", "klipy.com")
_BILI_SHORT = ("b23.tv", "bili2233.cn")  # B站的短链接（多半是视频）
_MEDIA_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".heic",
    ".mp4", ".mov", ".webm", ".mkv", ".m4v", ".mp3", ".m4a", ".wav", ".ogg", ".flac",
    ".zip", ".rar", ".7z", ".gz", ".exe", ".apk", ".dmg", ".iso",
)  # fmt: skip


def find_urls(text: str, limit: int = MAX_LINKS_PER_MESSAGE) -> list[str]:
    """从消息里找出网址（去掉粘在后面的标点；markdown 的 [文字](网址) 也认得）。"""
    out: list[str] = []
    for m in _URL.finditer(text or ""):
        url = m.group(0)
        while url and url[-1] in _TRAILING:
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break  # 维基百科那种网址里本来就带括号：(…) 成对时留着
            url = url[:-1]
        if len(url) > 10 and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def tweet_id(url: str) -> str | None:
    """X / Twitter 推文链接（包括 fxtwitter、vxtwitter 这些转换过的）的推文 ID；不是推文就返回 None。"""
    host = host_of(url)
    host = host[4:] if host.startswith("www.") else host
    if host not in X_HOSTS:
        return None
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    m = _X_STATUS.match(path)
    return m.group(1) if m else None


def openable(url: str) -> bool:
    """是不是值得打开的网页链接。图片、视频、压缩包、GIF 网站、Discord 自己的链接、内网地址都不算；
    X 只有推文算（个人主页之类要登录）。"""
    if check_url(url) is not None:
        return False
    if tweet_id(url) is not None:
        return True
    host = host_of(url)
    bare = host[4:] if host.startswith("www.") else host
    if bare in X_HOSTS:
        return False
    if any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS):
        return False
    if bare in _BILI_SHORT:
        return False
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    if (bare == "bilibili.com" or bare.endswith(".bilibili.com")) and path.startswith("/video/"):
        return False  # B站视频：网页里没什么可读的，Discord 的预览就有标题和简介（要放歌就点歌，不用先打开）
    return not path.endswith(_MEDIA_EXTS)


def site_name(url: str) -> str:
    host = host_of(url)
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# 推文
# ---------------------------------------------------------------------------


def _count(n: Any) -> str:
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n < 0:
        return ""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(int(n))


def _str(value: Any) -> str:
    return clean_text(value) if isinstance(value, str) else ""


def _date(status: dict[str, Any], tz: str) -> str:
    ts = status.get("created_timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        try:
            return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            pass
    return one_line(_str(status.get("created_at")), 40)


def _who(status: dict[str, Any]) -> tuple[str, str]:
    author = status.get("author") if isinstance(status.get("author"), dict) else {}
    return one_line(_str(author.get("name")), 40), one_line(_str(author.get("screen_name")), 40)


def _media_lines(status: dict[str, Any]) -> list[str]:
    media = status.get("media") if isinstance(status.get("media"), dict) else {}
    lines: list[str] = []
    photos = [p for p in media.get("photos") or [] if isinstance(p, dict)]
    if photos:
        alts = [one_line(_str(p.get("altText")), 80) for p in photos if _str(p.get("altText")).strip()]
        line = f"[图片 ×{len(photos)}]" if len(photos) > 1 else "[图片]"
        if alts:
            line += " 图片说明：" + "；".join(alts[:4])
        lines.append(line)
    for video in [v for v in media.get("videos") or [] if isinstance(v, dict)][:4]:
        if video.get("type") == "gif":
            lines.append("[GIF]")
            continue
        seconds = video.get("duration")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
            lines.append(f"[视频 {int(seconds) // 60}:{int(seconds) % 60:02d}]")
        else:
            lines.append("[视频]")
    return lines


def _article_text(article: Any) -> str:
    if not isinstance(article, dict):
        return ""
    parts = [f"《{one_line(_str(article.get('title')), 80)}》"] if _str(article.get("title")).strip() else []
    content = article.get("content") if isinstance(article.get("content"), dict) else {}
    blocks = [_str(b.get("text")).strip() for b in content.get("blocks") or [] if isinstance(b, dict)]
    body = "\n".join(b for b in blocks if b) or _str(article.get("preview_text")).strip()
    if body:
        parts.append(body)
    return "\n".join(parts)


def format_tweet(status: dict[str, Any], tz: str = "UTC") -> tuple[str, str]:
    """FxTwitter 返回的一条推文 → (标题, 正文)。"""
    name, handle = _who(status)
    head = f"{name}（@{handle}）" if name and handle else (f"@{handle}" if handle else name or "某人")
    when = _date(status, tz)
    lines = [f"{head} {when} 发的推文：" if when else f"{head} 发的推文："]
    replying = status.get("replying_to")
    if isinstance(replying, dict) and _str(replying.get("screen_name")):
        lines.append(f"（这是在回复 @{one_line(_str(replying.get('screen_name')), 40)}）")
    text = _str(status.get("text")).strip()
    if text:
        lines.append(text)
    article = _article_text(status.get("article"))
    if article:
        lines.append("[长文] " + article)
    lines += _media_lines(status)
    poll = status.get("poll")
    if isinstance(poll, dict):
        choices = [
            f"{one_line(_str(c.get('label')), 40)} {c.get('percentage')}%"
            for c in poll.get("choices") or []
            if isinstance(c, dict)
        ]
        if choices:
            total = _count(poll.get("total_votes"))
            lines.append(f"[投票{f'（共 {total} 票）' if total else ''}] " + " / ".join(choices))
    card = status.get("card")
    if isinstance(card, dict) and (_str(card.get("title")) or _str(card.get("description"))):
        domain = _str(card.get("domain"))
        lines.append(
            f"[链接卡片] {one_line(_str(card.get('title')), 100)}"
            + (f"：{one_line(_str(card.get('description')), 200)}" if _str(card.get("description")) else "")
            + (f"（{one_line(domain, 40)}）" if domain else "")
        )
    quote = status.get("quote")
    if isinstance(quote, dict):
        if quote.get("type") == "tombstone":
            lines.append(f"[引用的推文看不到：{one_line(_str(quote.get('message')), 80)}]")
        else:
            q_name, q_handle = _who(quote)
            q_text = one_line(_str(quote.get("text")), 600)
            q_media = " ".join(_media_lines(quote))
            lines.append(f"[引用 {q_name}（@{q_handle}）] {q_text} {q_media}".rstrip())
    note = status.get("community_note")
    if isinstance(note, dict) and _str(note.get("text")).strip():
        lines.append("[社区笔记] " + one_line(_str(note.get("text")), 500))
    stats = [
        (label, _count(status.get(key)))
        for label, key in (("赞", "likes"), ("转发", "reposts"), ("回复", "replies"), ("浏览", "views"))
    ]
    shown = " · ".join(f"{label} {value}" for label, value in stats if value)
    if shown:
        lines.append(f"（{shown}）")
    title = f"@{handle} 的推文" if handle else "推文"
    return title, "\n".join(lines)


def tweet_photos(status: dict[str, Any]) -> list[str]:
    media = status.get("media") if isinstance(status.get("media"), dict) else {}
    out = []
    for p in media.get("photos") or []:
        if isinstance(p, dict) and isinstance(p.get("url"), str) and host_of(p["url"]) in TWEET_IMAGE_HOSTS:
            out.append(p["url"])
    return out[:MAX_TWEET_PHOTOS]


# ---------------------------------------------------------------------------
# 网页
# ---------------------------------------------------------------------------

# 都限定了长度：网页是外面来的，满篇 “[” 的页面用不限长的写法要算很久（每个 “[” 都扫到结尾）
_MD_IMAGE = re.compile(r"!\[[^\]\n]{0,300}\]\([^)\n]{0,2000}\)")
_MD_LINK = re.compile(r"\[([^\]\n]{0,300})\]\((?:[^()\n]|\([^()\n]{0,300}\)){0,2000}\)")
_HTML_TAG = re.compile(r"<[^>\n]{1,200}>")
# 网址里会出现的 ASCII 字符（不含括号、引号）：中文网页的句子之间没有空格，用 \S 的话会把网址后面整段正文一起删掉
_URL_CHARS = r"[A-Za-z0-9\-._~:/?#@!$&*+,;=%]"
_DATA_URI = re.compile(rf"\bdata:[A-Za-z0-9.+/;=_-]{{0,100}},{_URL_CHARS}{{100,}}")  # 网页里直接嵌的图（一大串 base64）
_LONG_URL = re.compile(rf"https?://{_URL_CHARS}{{300,}}")  # 跟踪参数之类的超长网址：对内容没用，只占字数
_BLANKS = re.compile(r"\n{3,}")
_HEADING = re.compile(r"^#{1,3}\s+(.+)$", re.M)


def raw_limit(limit: int) -> int:
    """最后只要 limit 个字的话，原文最多留多少（markdown 的标记会占不少字，所以多留几倍）。"""
    return limit * 5 + 1000


def tidy_markdown(raw: str, limit: int | None = None) -> tuple[str, bool]:
    """Tavily 给的网页正文（markdown）：去掉图片、链接只留文字、去掉残留的 HTML 标签和多余的空行。

    limit：最后只要这么多字的话，先截掉后面用不到的部分（markdown 的标记会占不少字，所以多留几倍）。
    返回 (整理好的文字, 有没有截掉)。嵌在网页里的图（一大串 base64）和超长网址整篇先去掉再截，免得占了要读的字数。"""
    text = _DATA_URI.sub("", raw)  # 两个都是一遍扫过去的，整篇（最多 10 MB）也很快
    text = _LONG_URL.sub("", text)
    cut = limit is not None and len(text) > raw_limit(limit)
    if limit is not None:
        text = text[: raw_limit(limit)]
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = _HTML_TAG.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line.strip(" |-*_=#>") or not line.strip())
    return _BLANKS.sub("\n\n", text).strip(), cut


def clean_markdown(raw: str, limit: int | None = None) -> str:
    return tidy_markdown(raw, limit)[0]


def page_title(text: str) -> str:
    m = _HEADING.search(text[:3000])
    return one_line(m.group(1).strip(" #"), 80) if m else ""


# ---------------------------------------------------------------------------


class LinkReader:
    """打开一个链接。推文不需要任何 key；别的网页需要 Tavily。"""

    def __init__(self, http: HttpClient, tavily: Tavily | None, *, tz: str = "UTC"):
        self.http = http
        self.tavily = tavily
        self.tz = tz

    def mode(self) -> str:
        """all：推文和一般网页都能打开；x：只能打开推文（没配置 Tavily）。"""
        return "all" if self.tavily is not None else "x"

    def readable(self, url: str) -> bool:
        if tweet_id(url) is not None:
            return check_url(url) is None
        return self.tavily is not None and openable(url)

    async def read(self, url: str, *, max_chars: int, image_side: int | None = None) -> LinkResult:
        tid = tweet_id(url)
        if tid is not None:
            return await self._tweet(tid, max_chars, image_side)
        if self.tavily is None or not openable(url):
            return LinkResult("", FileText(error="这个链接打不开，只能看 Discord 的预览"))
        return await self._page(url, max_chars)

    async def _tweet(self, tid: str, max_chars: int, image_side: int | None) -> LinkResult:
        try:
            status, data = await self.http.api("GET", FXTWITTER_API.format(id=tid), timeout=15)
        except FetchError as exc:
            log.info("FxTwitter 读推文 %s 失败：%s", tid, exc)
            return LinkResult("", FileText(error="暂时读不到这条推文（FxTwitter 连不上）", transient=True))
        code = data.get("code") if isinstance(data, dict) else None
        post = data.get("status") if isinstance(data, dict) else None
        if status in (401, 403, 404) or code in (401, 403, 404):
            return LinkResult("", FileText(error="这条推文看不到（可能被删了，或者是私密账号、要登录才能看）"))
        if status != 200 or not isinstance(post, dict):
            return LinkResult("", FileText(error="暂时读不到这条推文（FxTwitter 出错了）", transient=True))
        if post.get("type") == "tombstone":
            reason = one_line(_str(post.get("message")), 80) or "看不到"
            return LinkResult("", FileText(error=f"这条推文看不到（{reason}）"))
        title, text = format_tweet(post, self.tz)
        truncated = len(text) > max_chars
        detail = "x.com" + ("，太长只读了前面" if truncated else "")
        images: list[str] = []
        if image_side and not post.get("possibly_sensitive"):
            images = await self._photos(tweet_photos(post), image_side)
        elif post.get("possibly_sensitive") and tweet_photos(post):
            detail += "，可能是敏感内容，没附图"
        return LinkResult(title, FileText(kind="X 推文", text=text[:max_chars], detail=detail, images=images, truncated=truncated))

    async def _photos(self, urls: list[str], side: int) -> list[str]:
        async def one(url: str) -> str | None:
            try:
                raw, _ = await self.http.fetch(url, max_bytes=IMAGE_MAX_BYTES, allowed_hosts=TWEET_IMAGE_HOSTS)
            except FetchError as exc:
                log.info("下载推文图片失败：%s", exc)
                return None
            return await asyncio.to_thread(to_data_uri, raw, side)

        results = await asyncio.gather(*(one(u) for u in urls))
        return [uri for uri in results if uri]

    async def _page(self, url: str, max_chars: int) -> LinkResult:
        assert self.tavily is not None
        try:
            raw = await self.tavily.extract(url)
        except TavilyError as exc:
            if exc.transient:
                reason = "读网页的服务暂时出错了"
                log.info("Tavily 打开 %s 失败：%s", site_name(url), exc)
            elif exc.status in (401, 403, 432, 433):
                reason = "读网页的服务这边出了问题"
                log.warning("Tavily 打开 %s 失败：%s", site_name(url), exc)  # key 不对、额度用完：要你处理
            else:
                reason = "网站可能要登录，或者不让机器人看"
                log.info("Tavily 打开 %s 失败：%s", site_name(url), exc)
            # key 不对、额度用完是这边的问题，不是链接打不开：之后修好了还要能再打开
            transient = exc.transient or exc.status in (401, 403, 432, 433)
            return LinkResult("", FileText(error=f"打不开（{reason}）", transient=transient))
        # 网页可能很长：放到线程里整理，别卡住别的频道
        text, cut = await asyncio.to_thread(tidy_markdown, raw, max_chars)  # cut：原文太长，后面的没整理
        text = clean_text(text)
        if not text:
            # 只看了开头、开头又全是标记的：这一轮额度小才会这样，之后还能再试
            return LinkResult("", FileText(error="打开了，但是没有能读的文字（可能是视频、图片，或者要登录）", transient=cut))
        truncated = len(text) > max_chars or cut
        detail = site_name(url) + ("，太长只读了前面" if truncated else "")
        return LinkResult(page_title(text), FileText(kind="网页", text=text[:max_chars], detail=detail, truncated=truncated))
