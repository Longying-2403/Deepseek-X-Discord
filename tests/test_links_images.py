"""1.4.0: reading links (posts on X go through FxTwitter, web pages through Tavily), finding pictures online (the model checks them before sending) and the meme library."""

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, T0, make_engine, msg

from dsbot.actions import parse_actions
from dsbot.config import ADMIN_KEYS, BUILTIN_DEFAULTS, OWNER_KEYS, AppConfig, load_config
from dsbot.convert import convert_message
from dsbot.links import (
    LinkReader,
    clean_markdown,
    find_urls,
    format_tweet,
    openable,
    page_title,
    tweet_id,
    tweet_photos,
)
from dsbot.llm import LLMError
from dsbot.memes import MemeLibrary, clean_name
from dsbot.models import FileText, LinkRef, LinkResult, Picture
from dsbot.net import FetchError, address_is_public, check_url
from dsbot.pictures import PictureFinder
from dsbot.prompts import (
    GALLERY_FOOT,
    GALLERY_HEAD,
    HINT_PROACTIVE,
    IMAGE_BLOCKED,
    IMAGE_COOLDOWN,
    IMAGE_FAILED,
    IMAGE_LIMIT,
    IMAGE_NONE,
    IMAGE_OFF,
    SYSTEM_RULES,
    build_system_prompt,
)
from dsbot.render import Gallery, assistant_text, history_lines, own_text, render_doc, render_tool_result
from dsbot.store import Store
from dsbot.tavily import Tavily, TavilyError, parse_extract, parse_images

ACTIONS = lambda *items: json.dumps({"actions": list(items)}, ensure_ascii=False)  # noqa: E731
REPLY = lambda to, *texts: {"type": "reply", "to": to, "messages": list(texts)}  # noqa: E731
TWEET = "https://x.com/someone/status/1234567890"
PAGE = "https://news.example.com/a/1"

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


def png(color=(255, 0, 0), size=(8, 8)) -> bytes:
    if Image is None:
        raise unittest.SkipTest("needs Pillow")
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def user_text(call):
    content = call["messages"][-1]["content"]
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)


def image_parts(call):
    content = call["messages"][-1]["content"]
    return [] if isinstance(content, str) else [p for p in content if p.get("type") == "image_url"]


def system_text(call):
    return call["messages"][0]["content"]


def scripted(outputs):
    state = {"i": 0}

    def responder(call):
        if not call["json_mode"]:
            return "summary"
        out = outputs[min(state["i"], len(outputs) - 1)]
        state["i"] += 1
        return out

    return responder


def link_msg(message_id, text, urls, **kw):
    return msg(message_id, text, links=[LinkRef(u) for u in urls], **kw)


class FakeReader:
    """Fake link reader: with mode=all it can open anything; with mode=x only posts on X."""

    def __init__(self, mode="all", results=None):
        self._mode = mode
        self.results = results or {}
        self.calls = []

    def mode(self):
        return self._mode

    def readable(self, url):
        return self._mode == "all" or tweet_id(url) is not None

    async def read(self, url, *, max_chars, image_side=None):
        self.calls.append((url, max_chars, image_side))
        out = self.results.get(url)
        if isinstance(out, Exception):
            raise out
        if out is None:
            out = LinkResult("Page title", FileText(kind="web page", text=f"text of {url}", detail="example.com"))
        return out


class FakePictures:
    def __init__(self, pictures=None, error=None):
        self.pictures = pictures if pictures is not None else [pic(1, "a capybara"), pic(2, "capybara meme")]
        self.error = error
        self.calls = []

    async def find(self, query):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return list(self.pictures)


def pic(n, desc=""):
    return Picture(png((n * 40 % 256, 0, 0)), "image/png", f"data:image/jpeg;base64,{n}", description=desc, host="img.example.com")


class FakeHttp:
    """Fake HttpClient: api() returns (status code, JSON) by URL, and fetch() returns content by URL."""

    def __init__(self, api=None, files=None):
        self.api_map = api or {}
        self.files = files or {}
        self.api_calls = []
        self.fetches = []

    async def api(self, method, url, *, payload=None, headers=None, timeout=None):
        self.api_calls.append((method, url, dict(payload) if isinstance(payload, dict) else payload, headers))
        out = self.api_map.get(url)
        if callable(out):
            out = out(payload)
        if isinstance(out, Exception):
            raise out
        return out

    async def fetch(self, url, *, max_bytes, allowed_hosts=None):
        self.fetches.append((url, allowed_hosts))
        out = self.files.get(url)
        if isinstance(out, Exception):
            raise out
        if out is None:
            raise FetchError("HTTP 404")
        return out, "image/png"


# ---------------------------------------------------------------------------
# Public addresses only
# ---------------------------------------------------------------------------


class PublicOnly(unittest.TestCase):
    def test_addresses(self):
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"):
            self.assertTrue(address_is_public(ip), ip)
        for ip in (
            "10.0.0.1", "172.16.5.4", "192.168.1.1", "127.0.0.1", "169.254.169.254", "100.64.1.1", "0.0.0.0",
            "224.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fe80::1%eth0", "fc00::1", "::ffff:192.168.1.1",
            "::192.168.1.1", "64:ff9b::c0a8:101", "2002:c0a8:101::1", "198.18.0.1", "not an ip", "",
        ):  # fmt: skip
            self.assertFalse(address_is_public(ip), ip)

    def test_urls(self):
        for url in ("https://example.com/a.png", "http://8.8.8.8/x.jpg", "https://img.example.co.jp/x", "http://example.com:443/x"):
            self.assertIsNone(check_url(url), url)
        for url in (
            "ftp://example.com/a", "file:///etc/passwd", "http://example.com:8080/", "http://user:pass@example.com/",
            "http://localhost/", "http://LOCALHOST./x", "http://nas.local/", "http://nas/", "http://router.lan/",
            "http://192.168.1.10/", "http://[::1]/", "http://127.1/", "http://2130706433/", "http://0x7f000001/",
            "http://017.0.0.1/", "http://10.0.0.1.nip.io.local/", "https://example.com:99999/", "http:///x", "not a url",
        ):  # fmt: skip
            self.assertIsNotNone(check_url(url), url)

    def test_hosts_written_with_fullwidth_characters(self):
        """aiohttp applies IDNA, which turns full-width digits and full-width dots into ordinary ones: if that gives a private address, it isn't connected to either."""
        for url in (
            "http://１２７.０.０.１/x.jpg", "http://127.0.0。1/x.jpg", "http://192.168.1．1/cam.jpg",
            "http://192.168.1.１/cam.jpg", "http://ｌｏｃａｌｈｏｓｔ/x", "http://nas．local/x", "http://［::1］/x",
        ):  # fmt: skip
            self.assertIsNotNone(check_url(url), url)
        self.assertIsNone(check_url("https://例子.台湾/a.png"))  # a normal internationalized (Chinese) domain name still connects

    def test_yarl_view_when_available(self):
        try:
            import yarl  # noqa: F401
        except ImportError:
            self.skipTest("aiohttp / yarl isn't installed")
        from dsbot.net import _check_as_aiohttp_sees_it

        self.assertIsNotNone(_check_as_aiohttp_sees_it("http://１２７.０.０.１/x.jpg", None))
        self.assertIsNone(_check_as_aiohttp_sees_it("https://example.com/a.png", None))

    def test_allowed_hosts(self):
        hosts = frozenset({"pbs.twimg.com"})
        self.assertIsNone(check_url("https://pbs.twimg.com/media/a.jpg", allowed_hosts=hosts))
        self.assertIsNotNone(check_url("https://evil.com/a.jpg", allowed_hosts=hosts))
        self.assertIsNotNone(check_url("https://pbs.twimg.com.evil.com/a.jpg", allowed_hosts=hosts))
        self.assertIsNotNone(check_url("https://192.168.1.1/a.jpg", allowed_hosts=frozenset({"192.168.1.1"})))


# ---------------------------------------------------------------------------
# Links: finding them, recognizing posts on X, whether they can be opened
# ---------------------------------------------------------------------------


class FindLinks(unittest.TestCase):
    def test_find_urls(self):
        # as in Chinese text, the first link runs straight into a full-width full stop and more text with no space: the link ends at the full stop
        text = (
            "look at this https://example.com/a。also <https://example.com/b> and [article](https://example.com/c) "
            "||https://example.com/d|| https://en.wikipedia.org/wiki/Foo_(bar) https://example.com/a"
        )
        self.assertEqual(
            find_urls(text),
            ["https://example.com/a", "https://example.com/b", "https://example.com/c", "https://example.com/d", "https://en.wikipedia.org/wiki/Foo_(bar)"],
        )
        self.assertEqual(len(find_urls(" ".join(f"https://e.com/{i}" for i in range(10)))), 5)
        self.assertEqual(find_urls("https://example.com/x)，"), ["https://example.com/x"])

    def test_tweet_id(self):
        for url, tid in (
            ("https://x.com/user/status/123456", "123456"),
            ("https://twitter.com/user/statuses/123456?s=20", "123456"),
            ("https://mobile.twitter.com/user/status/99", "99"),
            ("https://www.fxtwitter.com/u/status/1234/photo/1", "1234"),
            ("https://fixupx.com/u/status/1234", "1234"),
            ("https://x.com/i/web/status/1234", "1234"),
        ):
            self.assertEqual(tweet_id(url), tid, url)
        for url in ("https://x.com/user", "https://example.com/u/status/123", "https://x.com/user/status/abc"):
            self.assertIsNone(tweet_id(url), url)

    def test_openable(self):
        for url in (TWEET, PAGE, "https://www.youtube.com/watch?v=abc", "https://example.com/report.pdf"):
            self.assertTrue(openable(url), url)
        for url in (
            "https://x.com/someone", "https://example.com/a.png", "https://example.com/v.MP4", "https://tenor.com/view/x",
            "https://discord.com/channels/1/2/3", "https://cdn.discordapp.com/attachments/a.txt", "https://media.discordapp.net/x",
            "http://192.168.1.1/", "http://nas.local/admin", "https://example.com:5001/",
        ):  # fmt: skip
            self.assertFalse(openable(url), url)


# ---------------------------------------------------------------------------
# Parsing posts on X, web pages and image searches
# ---------------------------------------------------------------------------

STATUS = {
    "type": "status",
    "text": "Official: starting next month, all coffee is buy one get one free",
    "created_timestamp": T0,
    "author": {"name": "Coffee Intel", "screen_name": "someone"},
    "likes": 12000,
    "reposts": 3000,
    "replies": 50,
    "views": 1_500_000,
    "replying_to": {"screen_name": "boss"},
    "media": {
        "photos": [
            {"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg", "altText": "a cup of coffee"},
            {"type": "photo", "url": "https://evil.example.com/b.jpg"},
        ],
        "videos": [{"type": "video", "duration": 75.4}, {"type": "gif", "duration": 3}],
    },
    "poll": {"choices": [{"label": "Believe it", "percentage": 30}, {"label": "Don't", "percentage": 70}], "total_votes": 1234},
    "card": {"title": "Promo page", "description": "See the details", "domain": "coffee.example.com"},
    "quote": {"type": "status", "text": "the quoted post", "author": {"name": "The Boss", "screen_name": "boss"}},
    "community_note": {"text": "This isn't an official account"},
    "article": {"title": "Article title", "content": {"blocks": [{"text": "First paragraph"}, {"text": "Second paragraph"}]}},
}


class Tweets(unittest.TestCase):
    def test_format(self):
        title, text = format_tweet(STATUS, "Asia/Taipei")
        self.assertEqual(title, "@someone's post")
        self.assertTrue(text.startswith("Post by Coffee Intel (@someone), 2026-10-07 20:00:"))
        for piece in (
            "(This is a reply to @boss)",
            "Official: starting next month, all coffee is buy one get one free",
            '[Article] "Article title"\nFirst paragraph\nSecond paragraph',
            "[image ×2] Alt text: a cup of coffee",
            "[video 1:15]",
            "[GIF]",
            "[poll (total votes: 1234)] Believe it 30% / Don't 70%",
            "[link card] Promo page: See the details (coffee.example.com)",
            "[quoting The Boss (@boss)] the quoted post",
            "[Community Note] This isn't an official account",
            "(Likes 12.0K · Reposts 3000 · Replies 50 · Views 1.5M)",
        ):
            self.assertIn(piece, text)
        self.assertEqual(tweet_photos(STATUS), ["https://pbs.twimg.com/media/a.jpg"])  # only downloaded from X's image server

    def test_tombstone_quote_and_missing_fields(self):
        title, text = format_tweet({"text": "x", "quote": {"type": "tombstone", "message": "This post was deleted"}, "author": "bad data"})
        self.assertEqual(title, "Post")
        self.assertIn("[quoted post unavailable: This post was deleted]", text)


class PagesAndImages(unittest.TestCase):
    def test_clean_markdown(self):
        raw = "# Title\n\n![pic](https://x/a.png)\nThe body has a [link](https://x/b).\n\n\n\n| --- |\n<div>The end</div>"
        cleaned = clean_markdown(raw)
        self.assertEqual(cleaned, "# Title\n\nThe body has a link.\n\nThe end")
        self.assertEqual(page_title(cleaned), "Title")
        self.assertEqual(page_title("No heading here"), "")

    def test_embedded_images_and_long_urls_are_dropped_but_text_stays(self):
        blob = "QUJD" * 300
        self.assertEqual(clean_markdown(f"before![pic](data:image/png;base64,{blob})after"), "beforeafter")
        cleaned = clean_markdown(f"before data:image/svg+xml;charset=utf-8;base64,{blob} after")
        self.assertNotIn("QUJD", cleaned)
        self.assertIn("after", cleaned)
        tracking = "https://example.com/r?" + "utm_source=x&" * 40
        # Chinese has no spaces between sentences: the text right after a URL mustn't be deleted along with it
        # ("see [here](…). Today let's talk about coffee" / "Source: …, today let's talk about coffee")
        self.assertEqual(clean_markdown(f"详见[这里]({tracking})。今天我们来聊聊咖啡"), "详见这里。今天我们来聊聊咖啡")
        self.assertEqual(clean_markdown(f"原文：{tracking}，今天我们来聊聊咖啡"), "原文：，今天我们来聊聊咖啡")
        self.assertEqual(clean_markdown("Metadata:" + "x" * 300), "Metadata:" + "x" * 300)  # not a data URI

    def test_clean_markdown_is_fast_on_hostile_pages(self):
        import time

        for pattern in ("[", "![", "![](", "[a](", "data:,", "https://a"):
            raw = pattern * (300_000 // len(pattern))
            started = time.monotonic()
            clean_markdown(raw, 4000)
            clean_markdown(raw[:64_000])
            self.assertLess(time.monotonic() - started, 2.0, pattern)

    def test_parse_extract(self):
        self.assertEqual(parse_extract({"results": [{"url": PAGE, "raw_content": "body text"}]}, PAGE), "body text")
        with self.assertRaisesRegex(TavilyError, r"couldn't open it \(403 Forbidden\)"):
            parse_extract({"results": [], "failed_results": [{"url": PAGE, "error": "403 Forbidden"}]}, PAGE)
        with self.assertRaises(TavilyError):
            parse_extract("bad data", PAGE)

    def test_parse_images(self):
        data = {
            "images": [
                "https://a.example/1.jpg",
                {"url": "https://a.example/2.png", "description": "  a capybara\nin a hot spring "},
                {"url": "https://a.example/1.jpg"},
                {"url": "https://a.example/logo.svg"},
                {"url": "javascript:alert(1)"},
                {"url": "https://a.example/3.jpg\nfake"},
            ],
            "results": [{"images": [{"url": "https://b.example/4.jpg", "description": "a picture from the page"}]}, "bad data"],
        }
        self.assertEqual(
            parse_images(data),
            [("https://a.example/1.jpg", ""), ("https://a.example/2.png", "a capybara in a hot spring"), ("https://b.example/4.jpg", "a picture from the page")],
        )
        self.assertEqual(parse_images(None), [])


class Readers(unittest.IsolatedAsyncioTestCase):
    async def test_tweet(self):
        api = "https://api.fxtwitter.com/2/status/1234567890"
        http = FakeHttp({api: (200, {"code": 200, "status": STATUS})}, {"https://pbs.twimg.com/media/a.jpg": png()})
        got = await LinkReader(http, None, tz="Asia/Taipei").read(TWEET, max_chars=4000, image_side=512)
        self.assertEqual((got.title, got.result.kind, got.result.error), ("@someone's post", "X post", ""))
        self.assertIn("Official: starting next month", got.result.text)
        self.assertEqual(len(got.result.images), 1)
        self.assertTrue(got.result.images[0].startswith("data:image/jpeg;base64,"))
        self.assertEqual(http.fetches, [("https://pbs.twimg.com/media/a.jpg", frozenset({"pbs.twimg.com"}))])

    async def test_tweet_without_images_sensitive_and_truncated(self):
        api = "https://api.fxtwitter.com/2/status/1234567890"
        http = FakeHttp({api: (200, {"code": 200, "status": dict(STATUS, possibly_sensitive=True)})})
        reader = LinkReader(http, None)
        got = await reader.read(TWEET, max_chars=20, image_side=512)
        self.assertEqual(http.fetches, [])  # possibly sensitive: the images aren't downloaded
        self.assertIn("possibly sensitive, so its images weren't attached", got.result.detail)
        self.assertTrue(got.result.truncated)
        self.assertEqual(len(got.result.text), 20)
        await reader.read(TWEET, max_chars=4000, image_side=None)
        self.assertEqual(http.fetches, [])  # the preview already has the images: they aren't downloaded again

    async def test_tweet_errors(self):
        api = "https://api.fxtwitter.com/2/status/1234567890"
        cases = [
            ((404, {"code": 404, "status": None}), "isn't visible", False),
            ((200, {"code": 401, "status": None}), "isn't visible", False),
            ((200, {"code": 200, "status": {"type": "tombstone", "message": "the account was suspended"}}), "the account was suspended", False),
            ((500, {"code": 500}), "can't be read right now", True),
            (FetchError("timed out", transient=True), "can't be read right now", True),
        ]
        for response, words, transient in cases:
            got = await LinkReader(FakeHttp({api: response}), None).read(TWEET, max_chars=100)
            self.assertIn(words, got.result.error)
            self.assertEqual(got.result.transient, transient)

    async def test_page_through_tavily(self):
        extract = "https://api.tavily.com/extract"
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "# Big news\n\nThe story" * 3}]})})
        reader = LinkReader(http, Tavily(http, "tvly-key"))
        self.assertEqual(reader.mode(), "all")
        got = await reader.read(PAGE, max_chars=4000)
        self.assertEqual((got.title, got.result.kind, got.result.detail), ("Big news", "web page", "news.example.com"))
        method, url, payload, headers = http.api_calls[0]
        self.assertEqual((method, url, payload["urls"], payload["extract_depth"]), ("POST", extract, [PAGE], "basic"))
        self.assertEqual(headers, {"Authorization": "Bearer tvly-key"})

    async def test_page_failures(self):
        extract = "https://api.tavily.com/extract"
        cases = [
            ((200, {"results": [], "failed_results": [{"url": PAGE, "error": "login required"}]}), "the site may need a login", False, "INFO"),
            # a wrong key: needs your attention, and it doesn't count as the link not opening (once it's fixed, the link can be opened again)
            ((401, {"detail": {"error": "bad key"}}), "something's wrong with the page-reading service on our end", True, "WARNING"),
            ((500, {}), "having trouble right now", True, "INFO"),
        ]
        for response, words, transient, level in cases:
            http = FakeHttp({extract: response})
            with self.assertLogs("dsbot.links", level):
                got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=100)
            self.assertIn(words, got.result.error)
            self.assertEqual(got.result.transient, transient)
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "![x](y)"}]})})
        got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=100)
        self.assertIn("no readable text", got.result.error)

    async def test_long_raw_page_counts_as_truncated(self):
        extract = "https://api.tavily.com/extract"
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "![x](y)" * 2000 + "body text"}]})})
        got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=500)
        self.assertTrue(got.result.transient)  # only the beginning was tidied, and it was all markup: it can be tried again later with a bigger allowance
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "content " * 3000}]})})
        got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=500)
        self.assertTrue(got.result.truncated)

    async def test_inline_images_do_not_use_up_the_budget(self):
        extract = "https://api.tavily.com/extract"
        cover = "![cover](data:image/png;base64," + "QUJD" * 9000 + ")"
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "# Title\n" + cover + "\n" + "body " * 700}]})})
        got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=4000)
        self.assertEqual(got.result.text.count("body"), 700)
        self.assertFalse(got.result.truncated)

    async def test_without_tavily_only_tweets(self):
        reader = LinkReader(FakeHttp(), None)
        self.assertEqual(reader.mode(), "x")
        self.assertTrue(reader.readable(TWEET))
        self.assertFalse(reader.readable(PAGE))
        self.assertIn("only Discord's preview is available", (await reader.read(PAGE, max_chars=100)).result.error)

    async def test_tavily_images_drop_safe_search_when_rejected(self):
        search = "https://api.tavily.com/search"

        def respond(payload):
            if "safe_search" in payload:
                return (400, {"detail": {"error": "safe_search is not available on your plan"}})
            return (200, {"images": [{"url": "https://a.example/1.jpg", "description": "capybara"}]})

        http = FakeHttp({search: respond})
        tavily = Tavily(http, "k")
        with self.assertLogs("dsbot.tavily", "WARNING"):
            self.assertEqual(await tavily.images("capybara"), [("https://a.example/1.jpg", "capybara")])
        self.assertEqual(await tavily.images("capybara"), [("https://a.example/1.jpg", "capybara")])
        self.assertEqual(["safe_search" in c[2] for c in http.api_calls], [True, False, False])  # left out from then on
        payload = http.api_calls[0][2]
        self.assertEqual((payload["search_depth"], payload["include_images"], payload["include_image_descriptions"]), ("basic", True, True))

    async def test_safe_search_rejection_spelled_differently(self):
        search = "https://api.tavily.com/search"

        def respond(payload):
            if "safe_search" in payload:
                return (422, {"detail": [{"loc": ["body", "safeSearch"], "msg": "Safe search is not supported"}]})
            return (200, {"images": ["https://a.example/1.jpg"]})

        http = FakeHttp({search: respond})
        with self.assertLogs("dsbot.tavily", "WARNING"):
            self.assertEqual(await Tavily(http, "k").images("capybara"), [("https://a.example/1.jpg", "")])

    async def test_other_bad_requests_keep_safe_search(self):
        search = "https://api.tavily.com/search"
        http = FakeHttp({search: (400, {"detail": {"error": "Query contains unsafe content"}})})
        tavily = Tavily(http, "k")
        with self.assertRaises(TavilyError):
            await tavily.images("x" * 500)
        self.assertEqual(len(http.api_calls), 1)  # an error that has nothing to do with safe_search: no retry without it
        self.assertTrue(tavily._safe_search)

    async def test_tavily_errors(self):
        search = "https://api.tavily.com/search"
        for status, words, transient in ((401, "key is wrong", False), (432, "credits for this month are used up", False), (429, "HTTP 429", True)):
            with self.assertRaisesRegex(TavilyError, words) as ctx:
                await Tavily(FakeHttp({search: (status, {})}), "k").images("x")
            self.assertEqual(ctx.exception.transient, transient)

    async def test_picture_finder(self):
        a, b = png((1, 2, 3)), png((4, 5, 6))
        urls = [f"https://img{i}.example.com/{i}.png" for i in range(8)]
        files = {urls[0]: a, urls[1]: FetchError("403"), urls[2]: b"<html>not an image", urls[3]: a, urls[4]: b}
        http = FakeHttp({"https://api.tavily.com/search": (200, {"images": [{"url": u, "description": f"caption {i}"} for i, u in enumerate(urls)]})}, files)
        found = await PictureFinder(http, Tavily(http, "k"), keep=4).find("capybara")
        self.assertEqual([(p.description, p.host, p.mime) for p in found], [("caption 0", "img0.example.com", "image/png"), ("caption 4", "img4.example.com", "image/png")])
        self.assertEqual(found[0].data, a)  # what gets sent is the original
        self.assertTrue(found[0].uri.startswith("data:image/jpeg;base64,"))  # what the model sees is a downsized version
        self.assertEqual(found[0].filename, "image.png")
        self.assertEqual(len(http.fetches), 8)  # keep=4: at most 8 are tried

    async def test_huge_pictures_are_skipped_before_decoding(self):
        if Image is None:
            self.skipTest("needs Pillow")
        huge = io.BytesIO()
        Image.new("1", (5000, 5000)).save(huge, format="PNG")  # a tiny file, but decoding it takes a lot of memory
        url = "https://a.example/huge.png"
        http = FakeHttp({"https://api.tavily.com/search": (200, {"images": [url]})}, {url: huge.getvalue()})
        self.assertEqual(await PictureFinder(http, Tavily(http, "k")).find("x"), [])

    async def test_animated_pictures_are_skipped(self):
        if Image is None:
            self.skipTest("needs Pillow")
        frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 255, 0))]
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
        still = io.BytesIO()
        frames[0].save(still, format="GIF")
        urls = ["https://a.example/anim.gif", "https://a.example/still.gif"]
        http = FakeHttp({"https://api.tavily.com/search": (200, {"images": urls})}, {urls[0]: buf.getvalue(), urls[1]: still.getvalue()})
        found = await PictureFinder(http, Tavily(http, "k")).find("x")
        self.assertEqual([p.mime for p in found], ["image/gif"])  # only the still one is left: the model can't check the later frames of an animated one
        self.assertEqual(found[0].data, still.getvalue())


# ---------------------------------------------------------------------------
# The meme library
# ---------------------------------------------------------------------------


class UploadReady(unittest.TestCase):
    """Pictures to be sent: people in the chat must see the same thing the model saw, and they can't go over the upload limit."""

    def test_plain_small_pictures_go_out_untouched(self):
        from dsbot.images import upload_ready

        raw = png()
        self.assertEqual(upload_ready(raw, 1_000_000), (raw, "image/png"))

    def test_transparency_is_flattened_like_the_preview(self):
        from dsbot.images import to_data_uri, upload_ready

        alpha = io.BytesIO()
        Image.new("RGBA", (8, 8), (255, 255, 255, 0)).save(alpha, format="PNG")  # e.g. white text on a transparent background
        key = io.BytesIO()
        Image.new("RGB", (8, 8), (255, 255, 255)).save(key, format="PNG", transparency=(255, 255, 255))  # one color marked as transparent
        for raw in (alpha.getvalue(), key.getvalue()):
            data, mime = upload_ready(raw, 1_000_000)
            self.assertEqual(mime, "image/jpeg")
            with Image.open(io.BytesIO(data)) as img:
                self.assertEqual(img.convert("RGB").getpixel((0, 0)), (255, 255, 255))  # given a white background
            self.assertIsNotNone(to_data_uri(raw, 64))

    def test_too_big_is_shrunk_or_dropped(self):
        from dsbot.images import upload_ready

        noisy = Image.effect_noise((2600, 2600), 100).convert("RGB")
        buf = io.BytesIO()
        noisy.save(buf, format="PNG")
        raw = buf.getvalue()
        data, mime = upload_ready(raw, 2_000_000)
        self.assertEqual(mime, "image/jpeg")
        self.assertLessEqual(len(data), 2_000_000)
        self.assertIsNone(upload_ready(raw, 1_000))  # doesn't fit however much it's shrunk
        self.assertIsNone(upload_ready(b"not an image", 1_000_000))

    def test_opaque_alpha_channel_goes_out_untouched(self):
        from dsbot.images import upload_ready

        buf = io.BytesIO()
        Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(buf, format="PNG")  # screenshots are often saved like this: an alpha channel, but fully opaque
        self.assertEqual(upload_ready(buf.getvalue(), 1_000_000), (buf.getvalue(), "image/png"))
        pal = Image.new("P", (8, 8), 0)
        pal.putpalette([200, 0, 0, 0, 200, 0] + [0] * 762)
        buf = io.BytesIO()
        pal.save(buf, format="PNG", transparency=1)  # a transparent color is set, but never used
        self.assertEqual(upload_ready(buf.getvalue(), 1_000_000)[1], "image/png")

    def test_extremely_wide_picture_is_shrunk(self):
        from dsbot.images import to_data_uri, upload_ready

        buf = io.BytesIO()
        Image.new("RGBA", (66000, 20), (0, 0, 0, 0)).save(buf, format="PNG")  # JPEG allows at most 65500 pixels across
        self.assertIsNotNone(to_data_uri(buf.getvalue(), 512))
        data, mime = upload_ready(buf.getvalue(), 1_000_000)
        with Image.open(io.BytesIO(data)) as img:
            self.assertEqual((mime, img.width), ("image/jpeg", 2048))

    def test_sixteen_bit_grayscale(self):
        import base64

        from dsbot.images import to_data_uri, upload_ready

        gray = Image.new("I;16", (8, 8), 0x4000)  # 16-bit grayscale: converting straight to RGB would turn it all white
        buf = io.BytesIO()
        gray.save(buf, format="PNG")
        uri = to_data_uri(buf.getvalue(), 64)
        with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as img:
            self.assertLess(img.convert("L").getpixel((0, 0)), 100)
        self.assertEqual(upload_ready(buf.getvalue(), 1_000_000), (buf.getvalue(), "image/png"))  # browsers display the original just fine
        buf = io.BytesIO()
        gray.save(buf, format="PNG", transparency=0x4000)
        data, mime = upload_ready(buf.getvalue(), 1_000_000)  # the transparent color no longer matches after scaling to 8 bits: treated as opaque, same as what the model is shown
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(data)) as img:
            self.assertLess(img.convert("L").getpixel((0, 0)), 100)

    def test_multi_picture_jpeg_is_not_animated(self):
        from dsbot.images import is_animated

        out = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 10, 10)).save(out, format="MPO", save_all=True, append_images=[Image.new("RGB", (8, 8))])
        self.assertFalse(is_animated(out.getvalue()))  # stereo photos, HDR gain maps: looking at the first picture is enough
        gif = io.BytesIO()
        Image.new("RGB", (8, 8), (255, 0, 0)).save(gif, format="GIF", save_all=True, append_images=[Image.new("RGB", (8, 8), (0, 0, 255))])
        self.assertTrue(is_animated(gif.getvalue()))


class Memes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.shared = root / "memes"
        self.server = root / "server-memes"
        self.shared.mkdir()
        (self.shared / "facepalm-cat.png").write_bytes(png())
        (self.shared / "Doge.gif").write_bytes(b"GIF89a" + b"\0" * 20)
        (self.shared / ".hidden.png").write_bytes(png())
        (self.shared / "readme.txt").write_text("not a picture")
        (self.shared / "empty.png").write_bytes(b"")
        self.lib = MemeLibrary(self.shared, self.server, max_bytes=100_000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_names_find_and_load(self):
        self.assertEqual(self.lib.names(GUILD), ["Doge", "facepalm-cat"])
        self.assertEqual(self.lib.find(GUILD, "doge").name, "Doge")  # case doesn't matter
        self.assertEqual(self.lib.find(GUILD, " facepalm-cat ").name, "facepalm-cat")
        self.assertIsNone(self.lib.find(GUILD, "no-such-meme"))
        data, filename = self.lib.load(self.lib.find(GUILD, "facepalm-cat"))
        self.assertEqual(filename, "meme.png")  # uploaded under a plain English file name, not the meme's own name
        self.assertEqual(self.lib.counts(GUILD), (0, 2))

    def test_add_remove_and_server_first(self):
        meme = self.lib.add(GUILD, '  doge / smug "?" ', png((0, 0, 255)))
        self.assertEqual(meme.name, "doge smug")
        self.assertEqual(self.lib.names(GUILD), ["Doge", "doge smug", "facepalm-cat"])
        own = self.lib.add(GUILD, "DOGE", png((0, 255, 0)))  # same name as one in the shared library: this server uses its own
        self.assertEqual([n for n in self.lib.names(GUILD) if n.lower() == "doge"], ["DOGE"])
        self.assertFalse(self.lib.find(GUILD, "doge").shared)
        self.assertEqual(self.lib.names(2), ["Doge", "facepalm-cat"])  # other servers don't see it
        self.assertEqual(stat.S_IMODE(os.stat(own.path).st_mode), 0o644)  # visible on the NAS
        self.assertEqual(stat.S_IMODE(os.stat(own.path.parent).st_mode), 0o755)
        with self.assertRaisesRegex(ValueError, "already has a meme called"):
            self.lib.add(GUILD, "doge", png())
        with self.assertRaisesRegex(ValueError, "only PNG, JPG, GIF and WebP"):
            self.lib.add(GUILD, "broken", b"not an image")
        with self.assertRaisesRegex(ValueError, "too big"):
            self.lib.add(GUILD, "huge", b"\x89PNG\r\n\x1a\n" + b"\0" * 200_000)
        with self.assertRaisesRegex(ValueError, "can't be empty"):
            self.lib.add(GUILD, "../..", png())
        self.assertEqual(self.lib.remove(GUILD, "doge"), "removed")
        self.assertEqual(self.lib.remove(GUILD, "facepalm-cat"), "shared")
        self.assertEqual(self.lib.remove(GUILD, "no-such-meme"), "missing")
        self.assertEqual(self.lib.find(GUILD, "doge").name, "Doge")  # with its own one removed, the shared one shows through again

    def test_per_server_cap(self):
        import dsbot.memes as memes_module

        old = memes_module.SERVER_MAX
        memes_module.SERVER_MAX = 2
        try:
            self.lib.add(GUILD, "one", png((1, 1, 1)))
            self.lib.add(GUILD, "two", png((2, 2, 2)))
            with self.assertRaisesRegex(ValueError, "already has 2 memes"):
                self.lib.add(GUILD, "three", png((3, 3, 3)))
        finally:
            memes_module.SERVER_MAX = old

    def test_new_files_show_up(self):
        self.assertEqual(len(self.lib.names(GUILD)), 2)
        (self.shared / "new-one.webp").write_bytes(b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 10)
        os.utime(self.shared, ns=(1, 1))  # some file systems have coarse timestamps: force the folder's modification time to change
        self.assertIn("new-one", self.lib.names(GUILD))

    def test_filenames_that_are_not_utf8_are_skipped(self):
        raw_name = os.path.join(os.fsencode(self.shared), b"\xd0\xdc\xc3\xa8.png")  # a GBK file name, as unpacked from a zip made on Chinese Windows
        with open(raw_name, "wb") as fh:
            fh.write(png())
        os.utime(self.shared, ns=(2, 2))
        with self.assertLogs("dsbot.memes", "WARNING"):
            names = self.lib.names(GUILD)
        self.assertEqual(names, ["Doge", "facepalm-cat"])
        for name in names:
            name.encode("utf-8")  # can go into the prompt without errors

    def test_rescans_after_a_while(self):
        import dsbot.memes as memes_module

        self.assertEqual(len(self.lib.names(GUILD)), 2)
        stamp = self.shared.stat().st_mtime_ns
        (self.shared / "slow-copy.png").write_bytes(png())
        os.utime(self.shared, ns=(stamp, stamp))  # the folder's modification time hasn't changed (that's how it is once a copy finishes)
        self.assertEqual(len(self.lib.names(GUILD)), 2)
        old = memes_module.RESCAN_SECONDS
        memes_module.RESCAN_SECONDS = 0
        try:
            self.assertIn("slow-copy", self.lib.names(GUILD))
        finally:
            memes_module.RESCAN_SECONDS = old

    def test_clean_name(self):
        self.assertEqual(clean_name("a/b\\c:d*e?f\"g<h>i|j"), "a b c d e f g h i j")
        self.assertEqual(clean_name("..hidden.."), "hidden")
        self.assertEqual(len(clean_name("long" * 25)), 40)

    def test_missing_folders(self):
        lib = MemeLibrary(Path(self.tmp.name) / "missing", Path(self.tmp.name) / "also-missing")
        self.assertEqual((lib.names(GUILD), lib.counts(GUILD)), ([], (0, 0)))


# ---------------------------------------------------------------------------
# Parsing, rendering and the prompt
# ---------------------------------------------------------------------------


class ParseAndRender(unittest.TestCase):
    def test_parse_actions(self):
        actions, ok = parse_actions(
            ACTIONS(
                {"type": "meme", "to": 3, "name": "facepalm-cat"},
                {"type": "meme", "name": "a second one"},
                {"type": "image", "query": "capybara photo"},
                {"type": "image", "query": "a second search"},
                {"type": "pick", "to": 3, "image": "2"},
                {"type": "send_image", "image": 1},
            )
        )
        self.assertTrue(ok)
        self.assertEqual(
            [(a.kind, a.to, a.name, a.query, a.image) for a in actions],
            [("meme", 3, "facepalm-cat", None, None), ("image", None, None, "capybara photo", None), ("pick", 3, None, None, 2)],
        )
        named, _ = parse_actions(ACTIONS({"type": "pick", "image": "Image 3"}, {"type": "reply", "messages": ["x"]}))
        self.assertEqual(named[0].image, 3)  # written the way the found pictures are labeled ("Image 3") works too
        named, _ = parse_actions(ACTIONS({"type": "pick", "image": "图3"}, {"type": "reply", "messages": ["x"]}))
        self.assertEqual(named[0].image, 3)  # and so does the Chinese spelling "图3" ("Image 3")
        mixed, _ = parse_actions(ACTIONS({"type": "image", "to": 5, "image": 2}))  # mixed up: a number but no query means sending a picture
        self.assertEqual([(a.kind, a.to, a.image) for a in mixed], [("pick", 5, 2)])
        self.assertEqual([a.to_json() for a in actions], [
            {"type": "meme", "to": 3, "name": "facepalm-cat"}, {"type": "image", "query": "capybara photo"}, {"type": "pick", "to": 3, "image": 2},
        ])  # fmt: skip

    def test_render_link_block(self):
        lines, images = render_doc(
            NS(label="#3", filename="Title【x】", source="link", result=FileText(kind="web page", text="body>>>", detail="example.com", images=["data:1"]))
        )
        self.assertEqual(lines, ['【Link "Title[x]" from #3 · web page, example.com】', "<<<", "body›››", ">>>"])
        self.assertEqual(images, ["data:1"])
        failed, _ = render_doc(NS(label="#3", filename="t", source="link", result=FileText(error="needs a login")))
        self.assertEqual(failed, ['【Link "t" from #3 · couldn\'t open: needs a login】'])

    def test_gallery_and_header(self):
        content = render_tool_result(
            [], [], [], [], targets=[2], header="(The links you asked to open:)", gallery=Gallery("capybara", [("caption one", "data:a"), ("", "data:b")]), kind="proactive"
        )
        text = "".join(p.get("text", "") for p in content if p["type"] == "text")
        self.assertTrue(text.startswith("(The links you asked to open:)\n" + GALLERY_HEAD.format(query="capybara", count=2) + "\n<<<\nImage 1: caption one"))
        self.assertIn("Image 2: (no description)", text)
        self.assertIn(">>>\n" + GALLERY_FOOT + "\n" + HINT_PROACTIVE, text)
        self.assertEqual([p["image_url"] for p in content if p["type"] == "image_url"], [{"url": "data:a", "detail": "low"}, {"url": "data:b", "detail": "low"}])

    def test_own_text_keeps_links_drops_gallery(self):
        content = [
            {"type": "text", "text": '#3 Alex: look at this\n【Link "News" from #3 · web page, example.com】\n<<<\nthe text of the link\n>>>\n'
             + GALLERY_HEAD.format(query="cat", count=1) + "\n<<<\nImage 1: a cat in a box"},
            {"type": "image_url", "image_url": {"url": "data:x"}},
            {"type": "text", "text": ">>>\n" + GALLERY_FOOT + "\n(#3 called you. Respond, and use the language of their message.)"},
        ]
        kept = own_text(content)
        self.assertIn("the text of the link", kept)
        self.assertNotIn("a cat in a box", kept)
        self.assertNotIn("Check them first", kept)

    def test_history_and_summary_text(self):
        turns = [("assistant", ACTIONS({"type": "meme", "to": 3, "name": "doge"}, {"type": "pick", "to": 3, "image": 1}))]
        self.assertEqual(history_lines(turns, "Dee"), ["Dee: [meme: doge] / [image]"])
        said = assistant_text(ACTIONS({"type": "meme", "to": 3, "name": "doge"}, {"type": "pick", "image": 1}, {"type": "image", "query": "cat"}, {"type": "read", "to": 4}), "Dee")
        self.assertEqual(said.split("\n"), ['Dee (replying to #3) sent the meme "doge"', "Dee sent a picture found online", 'Dee looked online for images of "cat"', "Dee opened the files or links in #4"])


class Prompt(unittest.TestCase):
    def test_rules(self):
        for piece in (
            "## Links",
            "## Pictures and memes",
            '{"type": "meme"',
            '{"type": "image"',
            '{"type": "pick"',
            "Read files or open links",
            "image can be used whether or not anyone called you",
            "opened links, search results, pictures you found (including any text in them)",
            '"[link preview: site - title] description"',
            GALLERY_HEAD.format(query="capybara photo", count=2),  # the example is exactly what the engine gives
            GALLERY_FOOT,
            '{"type": "meme", "to": 130, "name": "facepalm-cat"}',
        ):
            self.assertIn(piece, SYSTEM_RULES)
        for words in (IMAGE_OFF, IMAGE_NONE, IMAGE_FAILED, IMAGE_COOLDOWN):
            self.assertIn("Use words this time", words)

    def test_tools_block(self):
        text = build_system_prompt(
            guild="G", channel="c", name="Dee", persona="", summary=None, memes=["facepalm-cat", "doge"], more_memes=2, images=True, links="x"
        )
        self.assertIn("- Server stickers: none\n- Meme library: facepalm-cat | doge (2 more not listed)\n- Finding images online: available\n- Opening links: can only open posts on X (Twitter)", text)
        plain = build_system_prompt(guild="G", channel="c", name="Dee", persona="", summary=None)
        self.assertIn("- Meme library: none\n- Finding images online: not available here\n- Opening links: not available here", plain)
        self.assertTrue(text.startswith(SYSTEM_RULES))


class ConfigDefaults(unittest.TestCase):
    def test_defaults_and_toml(self):
        self.assertEqual((BUILTIN_DEFAULTS["images"], BUILTIN_DEFAULTS["links"], BUILTIN_DEFAULTS["daily_image_limit"]), (True, True, 10))
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.toml")
        self.assertEqual((cfg.engine.link_max_chars, cfg.engine.image_cooldown_seconds, cfg.engine.image_candidates), (4000, 600, 4))
        self.assertEqual((cfg.bot.meme_dir, cfg.bot.server_meme_dir), ("data/memes", "data/server-memes"))
        self.assertEqual((cfg.defaults["images"], cfg.defaults["links"], cfg.defaults["daily_image_limit"]), (True, True, 10))

    def test_slash_command_choices_fit(self):
        owner_set = OWNER_KEYS + [k for k in ADMIN_KEYS if k != "persona"]
        self.assertLessEqual(len(owner_set), 25)  # Discord allows at most 25 choices per option
        self.assertIn("images", ADMIN_KEYS)
        self.assertTrue({"links", "daily_image_limit"} <= set(OWNER_KEYS))


class Stats(unittest.TestCase):
    def test_link_and_image_rows_are_not_calls(self):
        store = Store(":memory:")
        for kind, outcome in (("mention", "reply"), ("link", "error"), ("image", "image"), ("search", "search")):
            store.record_usage(ts=T0, day="d", guild_id=1, channel_id=2, kind=kind, outcome=outcome)
        stats = store.day_stats("d", guild_id=1)
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["outcomes"], {"reply": 1, "search": 1})
        self.assertEqual(store.count_usage("d", guild_id=1, kind="link"), 1)


# ---------------------------------------------------------------------------
# Converting messages: link previews in full, and noting the links
# ---------------------------------------------------------------------------


def fake_message(**kw):
    base = dict(
        id=501,
        channel=NS(id=CHANNEL),
        guild=NS(id=GUILD),
        author=NS(id=11, display_name="Alex", bot=False),
        clean_content="",
        mentions=[],
        role_mentions=[],
        reference=None,
        attachments=[],
        stickers=[],
        embeds=[],
    )
    base.update(kw)
    return NS(**base)


def embed(etype, *, url=None, title=None, description=None, author=None, provider=None, footer=None, thumb=None, image=None):
    return NS(
        type=etype,
        url=url,
        title=title,
        description=description,
        author=NS(name=author),
        provider=NS(name=provider),
        footer=NS(text=footer),
        # Discord gives preview images a URL on its own image proxy (the bot only downloads from there)
        thumbnail=NS(url=thumb, proxy_url=thumb and "https://images-ext-1.discordapp.net/external/" + thumb.rsplit("/", 1)[-1]),
        image=NS(url=image, proxy_url=image and "https://images-ext-1.discordapp.net/external/" + image.rsplit("/", 1)[-1]),
    )


class ConvertLinks(unittest.IsolatedAsyncioTestCase):
    def conv(self, message):
        async def fetch(url):
            return b"img"

        return convert_message(message, me_id=999, aliases=[], is_ours=lambda mid: False, fetch_url=fetch)

    async def test_tweet_preview_text_once_and_one_picture(self):
        text_url = "https://x.com/someone/status/1234567890?s=20"
        embed_url = "https://twitter.com/someone/status/1234567890"
        m = self.conv(fake_message(
            clean_content=f"look {text_url}",
            embeds=[
                embed("rich", url=embed_url, description="Official: coffee is buy one get one free\nfor real?", author="Coffee Intel (@someone)", footer="X", image="https://pbs.twimg.com/1.jpg"),
                embed("rich", url=embed_url, image="https://pbs.twimg.com/2.jpg"),  # the second image of the same post
            ],
        ))  # fmt: skip
        self.assertEqual(m.text, f"look {text_url} [link preview: X - Coffee Intel (@someone)] Official: coffee is buy one get one free for real?")
        self.assertEqual([i.kind for i in m.images], ["preview"])
        self.assertEqual(
            [(link.url, link.title, link.site, link.preview_image) for link in m.links],
            [(text_url, "Coffee Intel (@someone)", "X", True)],  # a different spelling is still recognized as the same post
        )

    async def test_which_links_are_kept(self):
        m = self.conv(fake_message(
            clean_content="https://example.com/a.png https://tenor.com/view/x https://discord.com/channels/1/2/3 http://192.168.1.1/ " + PAGE,
        ))  # fmt: skip
        self.assertEqual([link.url for link in m.links], [PAGE])
        self.assertFalse(m.links[0].preview_image)

    async def test_forwarded_links_and_bot_cards(self):
        ref = NS(message_id=400, channel_id=CHANNEL, resolved=None, type=NS(value=1))
        m = self.conv(fake_message(reference=ref, message_snapshots=[NS(content=f"passing this along {PAGE}")]))
        self.assertEqual([link.url for link in m.links], [PAGE])
        card = self.conv(fake_message(clean_content="", embeds=[embed("rich", title="Today's fortune", description="Excellent luck")]))
        self.assertEqual(card.text, "[card: Today's fortune] Excellent luck")  # a card sent by another bot (its link isn't in the text)


# ---------------------------------------------------------------------------
# Engine: reading links
# ---------------------------------------------------------------------------


class EngineLinks(unittest.IsolatedAsyncioTestCase):
    async def test_called_with_a_link_opens_it_first(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "read it"))]), link_reader=reader)
        await engine.handle_message(link_msg(501, f"@Dee is this real {PAGE}", [PAGE], mentions_bot=True))
        await engine.drain()
        self.assertEqual(reader.calls, [(PAGE, 4000, 1024)])
        text = user_text(llm.calls[0])
        self.assertIn(f'#1 Alex: @Dee is this real {PAGE} 〔@you〕\n【Link "Page title" from #1 · web page, example.com】\n<<<\ntext of {PAGE}\n>>>', text)
        self.assertEqual(len(llm.calls), 1)  # attached right away, no need to ask a second time
        day = engine.ledger.day(clock())
        self.assertEqual(engine.store.count_usage(day, guild_id=GUILD, kind="link"), 1)
        # when it's called again, the same link isn't opened again
        clock.advance(30)
        await engine.handle_message(msg(502, "@Dee what else?", mentions_bot=True, reply_to_id=501))
        await engine.drain()
        self.assertEqual(len(reader.calls), 1)

    async def test_not_opened_in_proactive_rounds(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(lambda call: '{"actions": []}', link_reader=reader, defaults={"proactive": True})
        await engine.handle_message(link_msg(501, PAGE, [PAGE]))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(reader.calls, [])

    async def test_reply_target_and_read_action(self):
        reader = FakeReader()
        outputs = [ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(2, "I had a look"))]
        engine, llm, transport, clock = make_engine(scripted(outputs), link_reader=reader)
        await engine.handle_message(link_msg(501, f"worth a read {PAGE}", [PAGE], author=12, name="Mia"))
        clock.advance(200)  # not just posted by the person calling it: not opened right away
        await engine.handle_message(msg(502, "@Dee what's that link up there about", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertIn('(The links you asked to open:)\n【Link "Page title" from #1 · web page, example.com】', user_text(llm.calls[1]))
        self.assertEqual(transport.sent[0][1], "I had a look")
        # asked to open it again: it's already in the conversation
        outputs[:] = [ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(3, "ok"))]
        clock.advance(30)
        engine.llm.responder = scripted(outputs)
        await engine.handle_message(msg(503, "@Dee look again", mentions_bot=True))
        await engine.drain()
        self.assertIn('(The link "Page title" from #1 was already shown to you earlier; scroll up to find it)', user_text(llm.calls[3]))
        self.assertEqual(len(reader.calls), 1)

    async def test_reply_to_a_link_message_opens_it(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(2, "hmm"))]), link_reader=reader)
        await engine.handle_message(link_msg(501, PAGE, [PAGE], author=12))
        await engine.handle_message(msg(502, "@Dee what about this", mentions_bot=True, reply_to_id=501))
        await engine.drain()
        self.assertEqual([c[0] for c in reader.calls], [PAGE])

    async def test_x_only_mode(self):
        reader = FakeReader(mode="x")
        outputs = [ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(2, "I only saw the preview"))]
        engine, llm, transport, clock = make_engine(scripted(outputs), link_reader=reader)
        await engine.handle_message(link_msg(501, f"{PAGE} {TWEET}", [PAGE, TWEET], author=12))
        clock.advance(200)
        await engine.handle_message(msg(502, "@Dee take a look", mentions_bot=True))
        await engine.drain()
        self.assertEqual([c[0] for c in reader.calls], [TWEET])
        self.assertIn("can't be opened: only posts on X can be opened here", user_text(llm.calls[1]))
        self.assertIn("- Opening links: can only open posts on X (Twitter)", system_text(llm.calls[0]))

    async def test_links_off(self):
        reader = FakeReader()
        outputs = [ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(1, "can't open it"))]
        engine, llm, transport, clock = make_engine(scripted(outputs), link_reader=reader, defaults={"links": False})
        await engine.handle_message(link_msg(501, f"@Dee {PAGE}", [PAGE], mentions_bot=True))
        await engine.drain()
        self.assertEqual(reader.calls, [])
        self.assertIn("Opening links is turned off", user_text(llm.calls[1]))
        self.assertIn("- Opening links: not available here", system_text(llm.calls[0]))

    async def test_limits(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "ok"))]), link_reader=reader)
        engine.ec.max_links_per_round = 1
        urls = [PAGE, "https://b.example.com/x"]
        await engine.handle_message(link_msg(501, "@Dee " + " ".join(urls), urls, mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(reader.calls), 1)
        self.assertIn("couldn't open: at most 1 link can be opened at a time, so this one wasn't", user_text(llm.calls[0]))
        # the daily limit
        engine.ec.max_links_per_round = 3
        engine.ec.daily_link_limit = 1
        clock.advance(30)
        await engine.handle_message(link_msg(502, "@Dee https://c.example.com/y", ["https://c.example.com/y"], mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(reader.calls), 1)
        self.assertIn("this server has used up today's link opens (limit: 1)", user_text(llm.calls[1]))

    async def test_failures(self):
        reader = FakeReader(results={
            PAGE: LinkResult("", FileText(error="the site may need a login, or doesn't let bots in")),
            TWEET: LinkResult("", FileText(error="the post can't be read right now", transient=True)),
        })  # fmt: skip
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "can't open them"))]), link_reader=reader)
        await engine.handle_message(link_msg(501, "@Dee look", [PAGE, TWEET], mentions_bot=True))
        await engine.drain()
        text = user_text(llm.calls[0])
        self.assertIn('【Link "news.example.com" from #1 · couldn\'t open: the site may need a login, or doesn\'t let bots in】', text)
        day = engine.ledger.day(clock())
        stats = engine.store.db.execute("SELECT outcome, COUNT(*) AS n FROM usage WHERE kind = 'link' GROUP BY outcome").fetchall()
        self.assertEqual({r["outcome"]: r["n"] for r in stats}, {"error": 2})
        # links that definitely can't be opened aren't tried automatically again; ones that only failed for now are tried again next time
        clock.advance(30)
        await engine.handle_message(msg(502, "@Dee try again", mentions_bot=True, reply_to_id=501))
        await engine.drain()
        self.assertEqual([c[0] for c in reader.calls], [PAGE, TWEET, TWEET])
        self.assertEqual(engine.store.count_usage(day, guild_id=GUILD, kind="link"), 3)

    async def test_link_cut_short_by_the_round_budget_can_be_opened_again(self):
        reader = FakeReader(results={PAGE: LinkResult("Long read", FileText(kind="web page", text="start" * 160, truncated=True))})
        outputs = [ACTIONS(REPLY(1, "only read the start"))]
        engine, llm, transport, clock = make_engine(scripted(outputs), link_reader=reader)
        engine.ec.file_round_max_chars = 800  # what's left of this round's allowance isn't enough to read it all
        await engine.handle_message(link_msg(501, "@Dee", [PAGE], mentions_bot=True))
        await engine.drain()
        self.assertEqual(reader.calls[0][1], 800)
        clock.advance(30)
        engine.ec.file_round_max_chars = 40000
        engine.llm.responder = scripted([ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(2, "read the whole thing"))])
        await engine.handle_message(msg(502, "@Dee what does the rest say", mentions_bot=True))
        await engine.drain()
        self.assertEqual([c[1] for c in reader.calls], [800, 4000])  # not treated as "already seen"

    async def test_reader_crash_is_contained(self):
        reader = FakeReader(results={PAGE: RuntimeError("boom")})
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "hmm"))]), link_reader=reader)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(link_msg(501, "@Dee", [PAGE], mentions_bot=True))
            await engine.drain()
        self.assertIn("couldn't open: something went wrong", user_text(llm.calls[0]))
        self.assertEqual(transport.sent[0][1], "hmm")

    async def test_preview_image_means_no_second_download(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "hmm"))]), link_reader=reader)
        message = msg(501, "@Dee", mentions_bot=True, links=[LinkRef(TWEET, title="Post", preview_image=True)])
        await engine.handle_message(message)
        await engine.drain()
        self.assertEqual(reader.calls, [(TWEET, 4000, None)])

    async def test_clear_memory_reopens(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "hmm"))]), link_reader=reader)
        await engine.handle_message(link_msg(501, "@Dee", [PAGE], mentions_bot=True))
        await engine.drain()
        engine.reset_channel(CHANNEL)
        clock.advance(30)
        await engine.handle_message(msg(502, "@Dee that link", mentions_bot=True, reply_to_id=501))
        await engine.drain()
        self.assertEqual(len(reader.calls), 2)


# ---------------------------------------------------------------------------
# Engine: finding pictures online, memes
# ---------------------------------------------------------------------------

IMAGE = lambda q="capybara photo": {"type": "image", "query": q}  # noqa: E731
PICK = lambda to, n: {"type": "pick", "to": to, "image": n}  # noqa: E731


class EngineImages(unittest.IsolatedAsyncioTestCase):
    async def test_find_review_and_send(self):
        pictures = FakePictures()
        engine, llm, transport, clock = make_engine(
            scripted([ACTIONS(IMAGE()), ACTIONS(PICK(1, 2), REPLY(1, "looks like this"))]), pictures=pictures
        )
        await engine.handle_message(msg(501, "@Dee what does a capybara look like", mentions_bot=True))
        await engine.drain()
        self.assertEqual(pictures.calls, ["capybara photo"])
        self.assertIn("- Finding images online: available", system_text(llm.calls[0]))
        second = llm.calls[1]
        self.assertIn(GALLERY_HEAD.format(query="capybara photo", count=2) + "\n<<<\nImage 1: a capybara", user_text(second))
        self.assertIn("Image 2: capybara meme", user_text(second))
        self.assertEqual([p["image_url"]["url"] for p in image_parts(second)], ["data:image/jpeg;base64,1", "data:image/jpeg;base64,2"])
        self.assertEqual(transport.files_sent, [(CHANNEL, pictures.pictures[1].data, "image.png", 501, True)])  # the original is what gets sent
        self.assertEqual(transport.sent, [(CHANNEL, "looks like this", 501, False)])  # the same message is only pinged once
        st = engine.states[CHANNEL]
        self.assertEqual(st.gallery, [])  # the candidates are only valid within this round
        self.assertEqual(engine.store.count_usage(engine.ledger.day(clock()), guild_id=GUILD, kind="image"), 1)
        self.assertIn("said: looks like this", st.last_decision)

    async def test_rejecting_all_candidates(self):
        engine, llm, transport, clock = make_engine(
            scripted([ACTIONS(IMAGE()), ACTIONS(REPLY(1, "none of the pictures were any good, never mind"))]), pictures=FakePictures()
        )
        await engine.handle_message(msg(501, "@Dee send a picture", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.sent[0][1], "none of the pictures were any good, never mind")

    async def test_bad_pick_is_skipped(self):
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(PICK(1, 9))]), pictures=FakePictures())
        await engine.handle_message(msg(501, "@Dee send a picture", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "👍")])  # it was called, so it owes some response

    async def test_pick_without_finding_first(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(PICK(1, 1), REPLY(1, "hmm")), pictures=FakePictures())
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.sent[0][1], "hmm")

    async def test_off_without_tavily_or_setting(self):
        for kw in ({"pictures": None}, {"pictures": FakePictures(), "defaults": {"images": False}}, {"pictures": FakePictures(), "defaults": {"daily_image_limit": 0}}):
            engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(REPLY(1, "can't look for one"))]), **kw)
            await engine.handle_message(msg(501, "@Dee send a picture", mentions_bot=True))
            await engine.drain()
            self.assertIn("- Finding images online: not available here", system_text(llm.calls[0]))
            self.assertIn(IMAGE_OFF.format(query="capybara photo"), user_text(llm.calls[1]))
            if kw.get("pictures") is not None:
                self.assertEqual(kw["pictures"].calls, [])

    async def test_daily_limit_cooldown_and_errors(self):
        pictures = FakePictures()
        outputs = [ACTIONS(IMAGE()), ACTIONS(REPLY(1, "a"))]
        engine, llm, transport, clock = make_engine(lambda call: outputs[(len(llm.calls) - 1) % 2], pictures=pictures, defaults={"daily_image_limit": 2})
        await engine.handle_message(msg(501, "@Dee picture", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(pictures.calls), 1)
        clock.advance(60)  # cooling down
        await engine.handle_message(msg(502, "@Dee another one", mentions_bot=True))
        await engine.drain()
        self.assertIn(IMAGE_COOLDOWN.format(query="capybara photo"), user_text(llm.calls[3]))
        self.assertEqual(len(pictures.calls), 1)
        clock.advance(600)
        pictures.error = TavilyError("Tavily error", transient=True)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(503, "@Dee another one", mentions_bot=True))
            await engine.drain()
        self.assertIn(IMAGE_FAILED.format(query="capybara photo"), user_text(llm.calls[5]))
        clock.advance(600)
        await engine.handle_message(msg(504, "@Dee another one", mentions_bot=True))
        await engine.drain()
        self.assertIn(IMAGE_LIMIT.format(query="capybara photo", limit=2), user_text(llm.calls[7]))  # the failed one counts too
        self.assertEqual(len(pictures.calls), 2)

    async def test_nothing_found(self):
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(REPLY(1, "found nothing"))]), pictures=FakePictures([]))
        await engine.handle_message(msg(501, "@Dee picture", mentions_bot=True))
        await engine.drain()
        self.assertIn(IMAGE_NONE.format(query="capybara photo"), user_text(llm.calls[1]))

    async def test_proactive_image(self):
        pictures = FakePictures()
        engine, llm, transport, clock = make_engine(
            scripted([ACTIONS(IMAGE("facepalm cat")), ACTIONS(PICK(1, 1))]), pictures=pictures, defaults={"proactive": True}
        )
        await engine.handle_message(msg(501, "spilled coffee on my keyboard again"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(pictures.calls, ["facepalm cat"])
        self.assertTrue(user_text(llm.calls[1]).endswith(HINT_PROACTIVE))
        self.assertEqual(llm.calls[1]["thinking"], "low")  # the thinking level for proactive checks
        self.assertEqual(transport.files_sent, [(CHANNEL, pictures.pictures[0].data, "image.png", 501, False)])
        st = engine.states[CHANNEL]
        self.assertAlmostEqual(st.bucket.tokens, 2.0)  # uses up 1 from the quota, just like speaking
        self.assertEqual(st.last_speak_at, clock())
        self.assertEqual(transport.typing_calls, 1)  # only the once, before sending the picture

    async def test_proactive_image_not_available(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(IMAGE()), defaults={"proactive": True})
        await engine.handle_message(msg(501, "hahaha"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # it can't find pictures: no second call just for that
        self.assertEqual(transport.files_sent, [])

    async def test_proactive_image_during_cooldown(self):
        pictures = FakePictures()
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(IMAGE()), pictures=pictures, defaults={"proactive": True})
        engine.states.clear()
        st = engine.state(CHANNEL, GUILD)
        st.last_image_at = clock() - 60  # it just searched a minute ago
        await engine.handle_message(msg(501, "hahaha"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # cooling down: no second call for the image search, and no search either
        self.assertEqual(pictures.calls, [])

    async def test_gallery_dropped_when_images_are_rejected(self):
        state = {"n": 0}

        def responder(call):
            state["n"] += 1
            if state["n"] == 1:
                return ACTIONS(IMAGE())
            if image_parts(call):
                return LLMError("bad image", status=400)
            return ACTIONS(PICK(1, 1), REPLY(1, "can't see the pictures"))

        engine, llm, transport, clock = make_engine(responder, pictures=FakePictures())
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@Dee picture", mentions_bot=True))
            await engine.drain()
        self.assertIn("(The pictures found couldn't be shown to you, so no picture this time.)", user_text(llm.calls[2]))
        self.assertEqual(transport.files_sent, [])  # pictures it hasn't seen aren't sent
        self.assertEqual(transport.sent[0][1], "can't see the pictures")


class MediaSendFailures(unittest.IsolatedAsyncioTestCase):
    async def test_failed_upload_gets_a_reaction_and_does_not_pause_the_channel(self):
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(PICK(1, 1))]), pictures=FakePictures())
        transport.can_send = False  # e.g. the "Attach Files" permission is missing
        await engine.handle_message(msg(501, "@Dee picture", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "👍")])
        self.assertEqual(engine.states[CHANNEL].send_failures, 0)  # only the picture couldn't be sent: text still goes through, so proactive chat doesn't need to stop

    async def test_no_extra_reaction_when_the_model_reacted(self):
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(PICK(1, 1), {"type": "react", "to": 1, "emoji": "😂"})]), pictures=FakePictures())
        transport.can_send = False
        await engine.handle_message(msg(501, "@Dee picture", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "😂")])

    async def test_two_failed_uploads_pause_images_for_a_while(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shared = Path(tmp.name) / "memes"
        shared.mkdir()
        (shared / "doge.png").write_bytes(png())
        lib = MemeLibrary(shared, Path(tmp.name) / "server")
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS({"type": "meme", "name": "doge"}), memes=lib)
        transport.can_send = False
        for i in range(3):
            await engine.handle_message(msg(501 + i, "@Dee meme battle", mentions_bot=True))
            await engine.drain()
            clock.advance(30)
        st = engine.states[CHANNEL]
        self.assertGreater(st.media_blocked_until, clock())
        self.assertEqual(len(transport.reactions), 3)  # some response every time it's called
        transport.can_send = True
        with self.assertLogs("dsbot.engine", "INFO"):
            await engine.handle_message(msg(510, "@Dee another one", mentions_bot=True))
            await engine.drain()
        self.assertEqual(transport.files_sent, [])  # no pictures while it's paused
        clock.advance(1801)
        await engine.handle_message(msg(511, "@Dee another one", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(transport.files_sent), 1)

    async def test_no_image_search_while_paused(self):
        pictures = FakePictures()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(REPLY(1, "can't send pictures, so I'll describe it"))]), pictures=pictures)
        await engine.handle_message(msg(500, "anyone around?"))
        st = engine.states[CHANNEL]
        st.media_blocked_until = clock() + 600
        status = engine.status(GUILD, CHANNEL, None)
        self.assertGreater(status["media_blocked_for"], 0)
        await engine.handle_message(msg(501, "@Dee send a picture of a capybara", mentions_bot=True))
        await engine.drain()
        self.assertEqual(pictures.calls, [])  # it couldn't be sent even if found: no credits spent
        self.assertIn(IMAGE_BLOCKED.format(query="capybara photo"), user_text(llm.calls[-1]))
        self.assertEqual(engine.store.count_usage(engine.ledger.day(clock()), guild_id=GUILD, kind="image"), 0)
        self.assertEqual(st.last_image_at, 0.0)  # it doesn't count toward the cooldown either


class EngineMemes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shared = Path(self.tmp.name) / "memes"
        shared.mkdir()
        (shared / "facepalm-cat.png").write_bytes(png())
        (shared / "【new】confetti.png").write_bytes(png((0, 255, 0)))
        self.lib = MemeLibrary(shared, Path(self.tmp.name) / "server")

    def tearDown(self):
        self.tmp.cleanup()

    async def test_listed_and_sent(self):
        engine, llm, transport, clock = make_engine(
            lambda call: ACTIONS({"type": "meme", "to": 1, "name": "facepalm-cat"}), memes=self.lib, defaults={"proactive": True}
        )
        await engine.handle_message(msg(501, "spilled coffee on my keyboard again"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertIn("- Meme library: facepalm-cat | [new]confetti", system_text(llm.calls[0]))  # sorted, and the marker symbols are neutralized too
        self.assertEqual([(f[2], f[3], f[4]) for f in transport.files_sent], [("meme.png", 501, False)])
        st = engine.states[CHANNEL]
        self.assertAlmostEqual(st.bucket.tokens, 2.0)
        self.assertIn("sent a meme: facepalm-cat", st.last_decision)

    async def test_label_written_back(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS({"type": "meme", "name": "[new]confetti"}), memes=self.lib)
        await engine.handle_message(msg(501, "@Dee let's celebrate", mentions_bot=True))
        await engine.drain()
        self.assertEqual([(f[3], f[4]) for f in transport.files_sent], [(501, True)])

    async def test_unknown_or_off(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS({"type": "meme", "name": "no-such-meme"}), memes=self.lib)
        await engine.handle_message(msg(501, "@Dee lol", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "👍")])
        engine, llm, transport, clock = make_engine(
            lambda call: ACTIONS({"type": "meme", "name": "facepalm-cat"}), memes=self.lib, defaults={"images": False}
        )
        await engine.handle_message(msg(501, "@Dee lol", mentions_bot=True))
        await engine.drain()
        self.assertIn("- Meme library: none", system_text(llm.calls[0]))
        self.assertEqual(transport.files_sent, [])

    async def test_list_limit(self):
        engine, llm, transport, clock = make_engine(lambda call: '{"actions": []}', memes=self.lib)
        engine.ec.meme_list_limit = 1
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertIn("- Meme library: facepalm-cat (1 more not listed)", system_text(llm.calls[0]))


class DiscordLayer(unittest.TestCase):
    def test_bot_does_not_replace_discord_clients_own_attributes(self):
        """If attributes discord.Client uses itself get overwritten (http is its client for talking to Discord), it can't log in. Checked by reading the source, so it works without discord.py installed."""
        import ast

        source = (Path(__file__).resolve().parents[1] / "dsbot" / "discord_app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        bot = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DSBot")
        assigned = {
            target.attr
            for node in ast.walk(bot)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
        }
        reserved = {"http", "ws", "loop", "user", "_connection", "_closed", "_ready", "shard_id", "shard_count", "intents", "application_id", "application"}
        self.assertEqual(assigned & reserved, set())


class Status(unittest.IsolatedAsyncioTestCase):
    async def test_status_fields(self):
        engine, llm, transport, clock = make_engine(link_reader=FakeReader(mode="x"), pictures=FakePictures())
        st = engine.status(GUILD, CHANNEL, None)
        self.assertEqual((st["links_mode"], st["can_find_images"], st["memes_on"], st["meme_counts"]), ("x", True, False, (0, 0)))
        self.assertEqual((st["images_found"], st["links_opened"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
