"""Reading links: posts on X (Twitter) go through FxTwitter's free API (no account needed); other web pages are fetched by Tavily.

The NAS never opens web pages itself: posts are read by FxTwitter and web pages by Tavily's servers, so neither the home network nor its IP is exposed to the links people post.
Only the images in posts are downloaded by the bot itself (only from X's image server pbs.twimg.com, through the public-address checks in net.py).
Sites you have to log in to see (Threads, IG, Facebook…) mostly can't be opened; then only the preview Discord generates is seen.
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

# Links people post: a link ends at whitespace, angle brackets, quotes, backticks, | (spoilers ||…||) or full-width punctuation
# (URLs copied from a browser have Chinese percent-encoded like %E6%B0%B4, so they never contain full-width punctuation; in text like
# "URL。and more" the link has to stop before the full stop)
_URL = re.compile(r"https?://[^\s<>\"'`|。，、；：！？「」『』（）【】《》…]+", re.I)
_TRAILING = ".,;:!?)]}>'\"。，、；：！？）】」』》…"
X_HOSTS = frozenset(
    {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com", "fxtwitter.com", "fixupx.com", "vxtwitter.com", "fixvx.com", "twittpr.com"}
)
_X_STATUS = re.compile(r"^/(?:[^/]+|i(?:/web)?)/status(?:es)?/(\d{2,20})(?:/|$)")
# Discord's own links and GIF sites: GIFs and images are already looked at as images; Discord message links need a login
_SKIP_HOSTS = ("discord.com", "discordapp.com", "discord.gg", "discordapp.net", "discord.media", "tenor.com", "giphy.com", "klipy.com")
_MEDIA_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".heic",
    ".mp4", ".mov", ".webm", ".mkv", ".m4v", ".mp3", ".m4a", ".wav", ".ogg", ".flac",
    ".zip", ".rar", ".7z", ".gz", ".exe", ".apk", ".dmg", ".iso",
)  # fmt: skip


def find_urls(text: str, limit: int = MAX_LINKS_PER_MESSAGE) -> list[str]:
    """Find the URLs in a message (dropping punctuation stuck to the end; markdown's [text](url) is recognized too)."""
    out: list[str] = []
    for m in _URL.finditer(text or ""):
        url = m.group(0)
        while url and url[-1] in _TRAILING:
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break  # Wikipedia-style URLs really do contain parentheses: keep them when the (…) are balanced
            url = url[:-1]
        if len(url) > 10 and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def tweet_id(url: str) -> str | None:
    """The post ID of a link to a post on X / Twitter (including converted ones like fxtwitter and vxtwitter); None if it isn't a post."""
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
    """Whether it's a web link worth opening. Images, videos, archives, GIF sites, Discord's own links and private-network addresses don't count;
    on X only posts count (profiles and the like need a login)."""
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
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    return not path.endswith(_MEDIA_EXTS)


def site_name(url: str) -> str:
    host = host_of(url)
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# Posts on X
# ---------------------------------------------------------------------------


def _count(n: Any) -> str:
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n < 0:
        return ""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
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
        line = f"[image ×{len(photos)}]" if len(photos) > 1 else "[image]"
        if alts:
            line += " Alt text: " + "; ".join(alts[:4])
        lines.append(line)
    for video in [v for v in media.get("videos") or [] if isinstance(v, dict)][:4]:
        if video.get("type") == "gif":
            lines.append("[GIF]")
            continue
        seconds = video.get("duration")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
            lines.append(f"[video {int(seconds) // 60}:{int(seconds) % 60:02d}]")
        else:
            lines.append("[video]")
    return lines


def _article_text(article: Any) -> str:
    if not isinstance(article, dict):
        return ""
    parts = [f'"{one_line(_str(article.get("title")), 80)}"'] if _str(article.get("title")).strip() else []
    content = article.get("content") if isinstance(article.get("content"), dict) else {}
    blocks = [_str(b.get("text")).strip() for b in content.get("blocks") or [] if isinstance(b, dict)]
    body = "\n".join(b for b in blocks if b) or _str(article.get("preview_text")).strip()
    if body:
        parts.append(body)
    return "\n".join(parts)


def format_tweet(status: dict[str, Any], tz: str = "UTC") -> tuple[str, str]:
    """One post as FxTwitter returns it → (title, text)."""
    name, handle = _who(status)
    head = f"{name} (@{handle})" if name and handle else (f"@{handle}" if handle else name or "someone")
    when = _date(status, tz)
    lines = [f"Post by {head}, {when}:" if when else f"Post by {head}:"]
    replying = status.get("replying_to")
    if isinstance(replying, dict) and _str(replying.get("screen_name")):
        lines.append(f"(This is a reply to @{one_line(_str(replying.get('screen_name')), 40)})")
    text = _str(status.get("text")).strip()
    if text:
        lines.append(text)
    article = _article_text(status.get("article"))
    if article:
        lines.append("[Article] " + article)
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
            lines.append(f"[poll{f' (total votes: {total})' if total else ''}] " + " / ".join(choices))
    card = status.get("card")
    if isinstance(card, dict) and (_str(card.get("title")) or _str(card.get("description"))):
        domain = _str(card.get("domain"))
        lines.append(
            f"[link card] {one_line(_str(card.get('title')), 100)}"
            + (f": {one_line(_str(card.get('description')), 200)}" if _str(card.get("description")) else "")
            + (f" ({one_line(domain, 40)})" if domain else "")
        )
    quote = status.get("quote")
    if isinstance(quote, dict):
        if quote.get("type") == "tombstone":
            lines.append(f"[quoted post unavailable: {one_line(_str(quote.get('message')), 80)}]")
        else:
            q_name, q_handle = _who(quote)
            q_text = one_line(_str(quote.get("text")), 600)
            q_media = " ".join(_media_lines(quote))
            lines.append(f"[quoting {q_name} (@{q_handle})] {q_text} {q_media}".rstrip())
    note = status.get("community_note")
    if isinstance(note, dict) and _str(note.get("text")).strip():
        lines.append("[Community Note] " + one_line(_str(note.get("text")), 500))
    stats = [
        (label, _count(status.get(key)))
        for label, key in (("Likes", "likes"), ("Reposts", "reposts"), ("Replies", "replies"), ("Views", "views"))
    ]
    shown = " · ".join(f"{label} {value}" for label, value in stats if value)
    if shown:
        lines.append(f"({shown})")
    title = f"@{handle}'s post" if handle else "Post"
    return title, "\n".join(lines)


def tweet_photos(status: dict[str, Any]) -> list[str]:
    media = status.get("media") if isinstance(status.get("media"), dict) else {}
    out = []
    for p in media.get("photos") or []:
        if isinstance(p, dict) and isinstance(p.get("url"), str) and host_of(p["url"]) in TWEET_IMAGE_HOSTS:
            out.append(p["url"])
    return out[:MAX_TWEET_PHOTOS]


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------

# Every pattern has a length cap: pages come from outside, and on a page full of "[" an unbounded pattern would take ages (each "[" scans to the end)
_MD_IMAGE = re.compile(r"!\[[^\]\n]{0,300}\]\([^)\n]{0,2000}\)")
_MD_LINK = re.compile(r"\[([^\]\n]{0,300})\]\((?:[^()\n]|\([^()\n]{0,300}\)){0,2000}\)")
_HTML_TAG = re.compile(r"<[^>\n]{1,200}>")
# ASCII characters that can appear in URLs (no brackets or quotes): Chinese pages have no spaces between sentences, so \S would delete the whole paragraph after a URL as well
_URL_CHARS = r"[A-Za-z0-9\-._~:/?#@!$&*+,;=%]"
_DATA_URI = re.compile(rf"\bdata:[A-Za-z0-9.+/;=_-]{{0,100}},{_URL_CHARS}{{100,}}")  # images embedded straight in the page (a long run of base64)
_LONG_URL = re.compile(rf"https?://{_URL_CHARS}{{300,}}")  # super-long URLs, e.g. with tracking parameters: useless for the content, they only eat up characters
_BLANKS = re.compile(r"\n{3,}")
_HEADING = re.compile(r"^#{1,3}\s+(.+)$", re.M)


def raw_limit(limit: int) -> int:
    """If only limit characters are wanted in the end, the most of the original to keep (markdown markup takes up quite a few characters, so keep several times as much)."""
    return limit * 5 + 1000


def tidy_markdown(raw: str, limit: int | None = None) -> tuple[str, bool]:
    """A page's text from Tavily (markdown): drop images, keep only the text of links, and drop leftover HTML tags and extra blank lines.

    limit: if only this many characters are wanted in the end, first cut off the later part that won't be used (markdown markup takes up quite a few characters, so several times as much is kept).
    Returns (tidied text, whether anything was cut off). Images embedded in the page (long runs of base64) and super-long URLs are removed from the whole page before cutting, so they don't use up the characters meant for reading."""
    text = _DATA_URI.sub("", raw)  # both are single passes, so even a whole page (up to 10 MB) is quick
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
    """Opens a link. Posts on X need no key at all; other web pages need Tavily."""

    def __init__(self, http: HttpClient, tavily: Tavily | None, *, tz: str = "UTC"):
        self.http = http
        self.tavily = tavily
        self.tz = tz

    def mode(self) -> str:
        """all: both posts on X and ordinary web pages can be opened; x: only posts on X (Tavily isn't set up)."""
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
            return LinkResult("", FileText(error="only Discord's preview is available for this link"))
        return await self._page(url, max_chars)

    async def _tweet(self, tid: str, max_chars: int, image_side: int | None) -> LinkResult:
        try:
            status, data = await self.http.api("GET", FXTWITTER_API.format(id=tid), timeout=15)
        except FetchError as exc:
            log.info("FxTwitter couldn't read post %s: %s", tid, exc)
            return LinkResult("", FileText(error="the post can't be read right now (FxTwitter can't be reached)", transient=True))
        code = data.get("code") if isinstance(data, dict) else None
        post = data.get("status") if isinstance(data, dict) else None
        if status in (401, 403, 404) or code in (401, 403, 404):
            return LinkResult("", FileText(error="the post isn't visible (it may have been deleted, or it's from a private account or needs a login)"))
        if status != 200 or not isinstance(post, dict):
            return LinkResult("", FileText(error="the post can't be read right now (FxTwitter returned an error)", transient=True))
        if post.get("type") == "tombstone":
            reason = one_line(_str(post.get("message")), 80) or "unavailable"
            return LinkResult("", FileText(error=f"the post isn't visible ({reason})"))
        title, text = format_tweet(post, self.tz)
        truncated = len(text) > max_chars
        detail = "x.com" + (", too long, so only the first part was read" if truncated else "")
        images: list[str] = []
        if image_side and not post.get("possibly_sensitive"):
            images = await self._photos(tweet_photos(post), image_side)
        elif post.get("possibly_sensitive") and tweet_photos(post):
            detail += ", possibly sensitive, so its images weren't attached"
        return LinkResult(title, FileText(kind="X post", text=text[:max_chars], detail=detail, images=images, truncated=truncated))

    async def _photos(self, urls: list[str], side: int) -> list[str]:
        async def one(url: str) -> str | None:
            try:
                raw, _ = await self.http.fetch(url, max_bytes=IMAGE_MAX_BYTES, allowed_hosts=TWEET_IMAGE_HOSTS)
            except FetchError as exc:
                log.info("Downloading an image from a post failed: %s", exc)
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
                reason = "the page-reading service is having trouble right now"
                log.info("Tavily couldn't open %s: %s", site_name(url), exc)
            elif exc.status in (401, 403, 432, 433):
                reason = "something's wrong with the page-reading service on our end"
                log.warning("Tavily couldn't open %s: %s", site_name(url), exc)  # wrong key or quota used up: needs your attention
            else:
                reason = "the site may need a login, or doesn't let bots in"
                log.info("Tavily couldn't open %s: %s", site_name(url), exc)
            # a wrong key or a used-up quota is a problem on our end, not a link that can't be opened: once it's fixed, the link should open again
            transient = exc.transient or exc.status in (401, 403, 432, 433)
            return LinkResult("", FileText(error=f"{reason}", transient=transient))
        # a page can be long: tidy it in a worker thread so other channels aren't held up
        text, cut = await asyncio.to_thread(tidy_markdown, raw, max_chars)  # cut: the original was too long, and the rest wasn't tidied
        text = clean_text(text)
        if not text:
            # only the beginning was looked at, and it was all markup: that only happens when this round's allowance is small, so it can be tried again later
            return LinkResult("", FileText(error="no readable text on the page (it may be a video or images, or need a login)", transient=cut))
        truncated = len(text) > max_chars or cut
        detail = site_name(url) + (", too long, so only the first part was read" if truncated else "")
        return LinkResult(page_title(text), FileText(kind="web page", text=text[:max_chars], detail=detail, truncated=truncated))
