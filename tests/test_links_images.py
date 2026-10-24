"""1.4.0：读链接（推文走 FxTwitter、网页走 Tavily）、上网找图（模型先审核再发）、表情包库。"""

import asyncio
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
        raise unittest.SkipTest("需要 Pillow")
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
            return "摘要"
        out = outputs[min(state["i"], len(outputs) - 1)]
        state["i"] += 1
        return out

    return responder


def link_msg(message_id, text, urls, **kw):
    return msg(message_id, text, links=[LinkRef(u) for u in urls], **kw)


class FakeReader:
    """假的读链接：mode=all 时什么都能打开，mode=x 时只能打开推文。"""

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
            out = LinkResult("网页标题", FileText(kind="网页", text=f"{url} 的正文", detail="example.com"))
        return out


class FakePictures:
    def __init__(self, pictures=None, error=None):
        self.pictures = pictures if pictures is not None else [pic(1, "一只水豚"), pic(2, "水豚表情包")]
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
    """假的 HttpClient：api() 按网址返回 (状态码, JSON)，fetch() 按网址返回内容。"""

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
# 只连公网
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
        """aiohttp 会按 IDNA 把全角数字、全角句号换成普通写法：换完是内网地址的一样不连。"""
        for url in (
            "http://１２７.０.０.１/x.jpg", "http://127.0.0。1/x.jpg", "http://192.168.1．1/cam.jpg",
            "http://192.168.1.１/cam.jpg", "http://ｌｏｃａｌｈｏｓｔ/x", "http://nas．local/x", "http://［::1］/x",
        ):  # fmt: skip
            self.assertIsNotNone(check_url(url), url)
        self.assertIsNone(check_url("https://例子.台湾/a.png"))  # 正常的中文域名照样能连

    def test_yarl_view_when_available(self):
        try:
            import yarl  # noqa: F401
        except ImportError:
            self.skipTest("没装 aiohttp / yarl")
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
# 链接：找出来、认推文、能不能打开
# ---------------------------------------------------------------------------


class FindLinks(unittest.TestCase):
    def test_find_urls(self):
        text = (
            "看这个 https://example.com/a。还有 <https://example.com/b> 和 [文章](https://example.com/c) "
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
# 推文、网页、找图的解析
# ---------------------------------------------------------------------------

STATUS = {
    "type": "status",
    "text": "官方宣布：下个月起所有咖啡买一送一",
    "created_timestamp": T0,
    "author": {"name": "咖啡情报站", "screen_name": "someone"},
    "likes": 12000,
    "reposts": 3000,
    "replies": 50,
    "views": 1_500_000,
    "replying_to": {"screen_name": "boss"},
    "media": {
        "photos": [
            {"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg", "altText": "一杯咖啡"},
            {"type": "photo", "url": "https://evil.example.com/b.jpg"},
        ],
        "videos": [{"type": "video", "duration": 75.4}, {"type": "gif", "duration": 3}],
    },
    "poll": {"choices": [{"label": "信", "percentage": 30}, {"label": "不信", "percentage": 70}], "total_votes": 1234},
    "card": {"title": "活动页", "description": "详情请看", "domain": "coffee.example.com"},
    "quote": {"type": "status", "text": "被引用的推文", "author": {"name": "老板", "screen_name": "boss"}},
    "community_note": {"text": "这不是官方账号"},
    "article": {"title": "长文标题", "content": {"blocks": [{"text": "第一段"}, {"text": "第二段"}]}},
}


class Tweets(unittest.TestCase):
    def test_format(self):
        title, text = format_tweet(STATUS, "Asia/Taipei")
        self.assertEqual(title, "@someone 的推文")
        self.assertTrue(text.startswith("咖啡情报站（@someone） 2026-10-07 20:00 发的推文："))
        for piece in (
            "（这是在回复 @boss）",
            "官方宣布：下个月起所有咖啡买一送一",
            "[长文] 《长文标题》\n第一段\n第二段",
            "[图片 ×2] 图片说明：一杯咖啡",
            "[视频 1:15]",
            "[GIF]",
            "[投票（共 1234 票）] 信 30% / 不信 70%",
            "[链接卡片] 活动页：详情请看（coffee.example.com）",
            "[引用 老板（@boss）] 被引用的推文",
            "[社区笔记] 这不是官方账号",
            "（赞 1.2万 · 转发 3000 · 回复 50 · 浏览 150.0万）",
        ):
            self.assertIn(piece, text)
        self.assertEqual(tweet_photos(STATUS), ["https://pbs.twimg.com/media/a.jpg"])  # 只从 X 的图片服务器下载

    def test_tombstone_quote_and_missing_fields(self):
        title, text = format_tweet({"text": "x", "quote": {"type": "tombstone", "message": "已删除"}, "author": "坏数据"})
        self.assertEqual(title, "推文")
        self.assertIn("[引用的推文看不到：已删除]", text)


class PagesAndImages(unittest.TestCase):
    def test_clean_markdown(self):
        raw = "# 标题\n\n![图](https://x/a.png)\n正文有[链接](https://x/b)。\n\n\n\n| --- |\n<div>结尾</div>"
        cleaned = clean_markdown(raw)
        self.assertEqual(cleaned, "# 标题\n\n正文有链接。\n\n结尾")
        self.assertEqual(page_title(cleaned), "标题")
        self.assertEqual(page_title("没有标题"), "")

    def test_embedded_images_and_long_urls_are_dropped_but_text_stays(self):
        blob = "QUJD" * 300
        self.assertEqual(clean_markdown(f"前文![图](data:image/png;base64,{blob})后文"), "前文后文")
        cleaned = clean_markdown(f"前文 data:image/svg+xml;charset=utf-8;base64,{blob} 后文")
        self.assertNotIn("QUJD", cleaned)
        self.assertIn("后文", cleaned)
        tracking = "https://example.com/r?" + "utm_source=x&" * 40
        # 中文句子之间没有空格：网址后面的正文不能一起删掉
        self.assertEqual(clean_markdown(f"详见[这里]({tracking})。今天我们来聊聊咖啡"), "详见这里。今天我们来聊聊咖啡")
        self.assertEqual(clean_markdown(f"原文：{tracking}，今天我们来聊聊咖啡"), "原文：，今天我们来聊聊咖啡")
        self.assertEqual(clean_markdown("Metadata:" + "x" * 300), "Metadata:" + "x" * 300)  # 不是 data URI

    def test_clean_markdown_is_fast_on_hostile_pages(self):
        import time

        for pattern in ("[", "![", "![](", "[a](", "data:,", "https://a"):
            raw = pattern * (300_000 // len(pattern))
            started = time.monotonic()
            clean_markdown(raw, 4000)
            clean_markdown(raw[:64_000])
            self.assertLess(time.monotonic() - started, 2.0, pattern)

    def test_parse_extract(self):
        self.assertEqual(parse_extract({"results": [{"url": PAGE, "raw_content": "正文"}]}, PAGE), "正文")
        with self.assertRaisesRegex(TavilyError, "打不开（403 Forbidden）"):
            parse_extract({"results": [], "failed_results": [{"url": PAGE, "error": "403 Forbidden"}]}, PAGE)
        with self.assertRaises(TavilyError):
            parse_extract("坏数据", PAGE)

    def test_parse_images(self):
        data = {
            "images": [
                "https://a.example/1.jpg",
                {"url": "https://a.example/2.png", "description": "  水豚\n泡温泉 "},
                {"url": "https://a.example/1.jpg"},
                {"url": "https://a.example/logo.svg"},
                {"url": "javascript:alert(1)"},
                {"url": "https://a.example/3.jpg\nfake"},
            ],
            "results": [{"images": [{"url": "https://b.example/4.jpg", "description": "页面里的图"}]}, "坏数据"],
        }
        self.assertEqual(
            parse_images(data),
            [("https://a.example/1.jpg", ""), ("https://a.example/2.png", "水豚 泡温泉"), ("https://b.example/4.jpg", "页面里的图")],
        )
        self.assertEqual(parse_images(None), [])


class Readers(unittest.IsolatedAsyncioTestCase):
    async def test_tweet(self):
        api = "https://api.fxtwitter.com/2/status/1234567890"
        http = FakeHttp({api: (200, {"code": 200, "status": STATUS})}, {"https://pbs.twimg.com/media/a.jpg": png()})
        got = await LinkReader(http, None, tz="Asia/Taipei").read(TWEET, max_chars=4000, image_side=512)
        self.assertEqual((got.title, got.result.kind, got.result.error), ("@someone 的推文", "X 推文", ""))
        self.assertIn("官方宣布", got.result.text)
        self.assertEqual(len(got.result.images), 1)
        self.assertTrue(got.result.images[0].startswith("data:image/jpeg;base64,"))
        self.assertEqual(http.fetches, [("https://pbs.twimg.com/media/a.jpg", frozenset({"pbs.twimg.com"}))])

    async def test_tweet_without_images_sensitive_and_truncated(self):
        api = "https://api.fxtwitter.com/2/status/1234567890"
        http = FakeHttp({api: (200, {"code": 200, "status": dict(STATUS, possibly_sensitive=True)})})
        reader = LinkReader(http, None)
        got = await reader.read(TWEET, max_chars=20, image_side=512)
        self.assertEqual(http.fetches, [])  # 可能是敏感内容：不下载图
        self.assertIn("可能是敏感内容，没附图", got.result.detail)
        self.assertTrue(got.result.truncated)
        self.assertEqual(len(got.result.text), 20)
        await reader.read(TWEET, max_chars=4000, image_side=None)
        self.assertEqual(http.fetches, [])  # 预览里已经有图：不再下载

    async def test_tweet_errors(self):
        api = "https://api.fxtwitter.com/2/status/1234567890"
        cases = [
            ((404, {"code": 404, "status": None}), "看不到", False),
            ((200, {"code": 401, "status": None}), "看不到", False),
            ((200, {"code": 200, "status": {"type": "tombstone", "message": "账号被封了"}}), "账号被封了", False),
            ((500, {"code": 500}), "暂时读不到", True),
            (FetchError("超时", transient=True), "暂时读不到", True),
        ]
        for response, words, transient in cases:
            got = await LinkReader(FakeHttp({api: response}), None).read(TWEET, max_chars=100)
            self.assertIn(words, got.result.error)
            self.assertEqual(got.result.transient, transient)

    async def test_page_through_tavily(self):
        extract = "https://api.tavily.com/extract"
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "# 大新闻\n\n内容" * 3}]})})
        reader = LinkReader(http, Tavily(http, "tvly-key"))
        self.assertEqual(reader.mode(), "all")
        got = await reader.read(PAGE, max_chars=4000)
        self.assertEqual((got.title, got.result.kind, got.result.detail), ("大新闻", "网页", "news.example.com"))
        method, url, payload, headers = http.api_calls[0]
        self.assertEqual((method, url, payload["urls"], payload["extract_depth"]), ("POST", extract, [PAGE], "basic"))
        self.assertEqual(headers, {"Authorization": "Bearer tvly-key"})

    async def test_page_failures(self):
        extract = "https://api.tavily.com/extract"
        cases = [
            ((200, {"results": [], "failed_results": [{"url": PAGE, "error": "login required"}]}), "网站可能要登录", False, "INFO"),
            # key 不对：要你处理；不算链接打不开（修好以后还能再打开）
            ((401, {"detail": {"error": "bad key"}}), "读网页的服务这边出了问题", True, "WARNING"),
            ((500, {}), "暂时出错", True, "INFO"),
        ]
        for response, words, transient, level in cases:
            http = FakeHttp({extract: response})
            with self.assertLogs("dsbot.links", level):
                got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=100)
            self.assertIn(words, got.result.error)
            self.assertEqual(got.result.transient, transient)
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "![x](y)"}]})})
        got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=100)
        self.assertIn("没有能读的文字", got.result.error)

    async def test_long_raw_page_counts_as_truncated(self):
        extract = "https://api.tavily.com/extract"
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "![x](y)" * 2000 + "正文"}]})})
        got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=500)
        self.assertTrue(got.result.transient)  # 只整理了开头、开头又全是标记：之后额度大了还能再试
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "内容" * 3000}]})})
        got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=500)
        self.assertTrue(got.result.truncated)

    async def test_inline_images_do_not_use_up_the_budget(self):
        extract = "https://api.tavily.com/extract"
        cover = "![封面](data:image/png;base64," + "QUJD" * 9000 + ")"
        http = FakeHttp({extract: (200, {"results": [{"url": PAGE, "raw_content": "# 标题\n" + cover + "\n" + "正文" * 1500}]})})
        got = await LinkReader(http, Tavily(http, "k")).read(PAGE, max_chars=4000)
        self.assertEqual(got.result.text.count("正文"), 1500)
        self.assertFalse(got.result.truncated)

    async def test_without_tavily_only_tweets(self):
        reader = LinkReader(FakeHttp(), None)
        self.assertEqual(reader.mode(), "x")
        self.assertTrue(reader.readable(TWEET))
        self.assertFalse(reader.readable(PAGE))
        self.assertIn("只能看 Discord 的预览", (await reader.read(PAGE, max_chars=100)).result.error)

    async def test_tavily_images_drop_safe_search_when_rejected(self):
        search = "https://api.tavily.com/search"

        def respond(payload):
            if "safe_search" in payload:
                return (400, {"detail": {"error": "safe_search is not available on your plan"}})
            return (200, {"images": [{"url": "https://a.example/1.jpg", "description": "水豚"}]})

        http = FakeHttp({search: respond})
        tavily = Tavily(http, "k")
        with self.assertLogs("dsbot.tavily", "WARNING"):
            self.assertEqual(await tavily.images("水豚"), [("https://a.example/1.jpg", "水豚")])
        self.assertEqual(await tavily.images("水豚"), [("https://a.example/1.jpg", "水豚")])
        self.assertEqual(["safe_search" in c[2] for c in http.api_calls], [True, False, False])  # 以后都不带
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
            self.assertEqual(await Tavily(http, "k").images("水豚"), [("https://a.example/1.jpg", "")])

    async def test_other_bad_requests_keep_safe_search(self):
        search = "https://api.tavily.com/search"
        http = FakeHttp({search: (400, {"detail": {"error": "Query contains unsafe content"}})})
        tavily = Tavily(http, "k")
        with self.assertRaises(TavilyError):
            await tavily.images("x" * 500)
        self.assertEqual(len(http.api_calls), 1)  # 和 safe_search 无关的错误：不去掉它重试
        self.assertTrue(tavily._safe_search)

    async def test_tavily_errors(self):
        search = "https://api.tavily.com/search"
        for status, words, transient in ((401, "key 不对", False), (432, "额度用完", False), (429, "HTTP 429", True)):
            with self.assertRaisesRegex(TavilyError, words) as ctx:
                await Tavily(FakeHttp({search: (status, {})}), "k").images("x")
            self.assertEqual(ctx.exception.transient, transient)

    async def test_picture_finder(self):
        a, b = png((1, 2, 3)), png((4, 5, 6))
        urls = [f"https://img{i}.example.com/{i}.png" for i in range(8)]
        files = {urls[0]: a, urls[1]: FetchError("403"), urls[2]: b"<html>not an image", urls[3]: a, urls[4]: b}
        http = FakeHttp({"https://api.tavily.com/search": (200, {"images": [{"url": u, "description": f"说明{i}"} for i, u in enumerate(urls)]})}, files)
        found = await PictureFinder(http, Tavily(http, "k"), keep=4).find("水豚")
        self.assertEqual([(p.description, p.host, p.mime) for p in found], [("说明0", "img0.example.com", "image/png"), ("说明4", "img4.example.com", "image/png")])
        self.assertEqual(found[0].data, a)  # 发出去的是原图
        self.assertTrue(found[0].uri.startswith("data:image/jpeg;base64,"))  # 给模型看的是缩小版
        self.assertEqual(found[0].filename, "image.png")
        self.assertEqual(len(http.fetches), 8)  # keep=4：最多试 8 张

    async def test_huge_pictures_are_skipped_before_decoding(self):
        if Image is None:
            self.skipTest("需要 Pillow")
        huge = io.BytesIO()
        Image.new("1", (5000, 5000)).save(huge, format="PNG")  # 文件很小，解码却要很多内存
        url = "https://a.example/huge.png"
        http = FakeHttp({"https://api.tavily.com/search": (200, {"images": [url]})}, {url: huge.getvalue()})
        self.assertEqual(await PictureFinder(http, Tavily(http, "k")).find("x"), [])

    async def test_animated_pictures_are_skipped(self):
        if Image is None:
            self.skipTest("需要 Pillow")
        frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 255, 0))]
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
        still = io.BytesIO()
        frames[0].save(still, format="GIF")
        urls = ["https://a.example/anim.gif", "https://a.example/still.gif"]
        http = FakeHttp({"https://api.tavily.com/search": (200, {"images": urls})}, {urls[0]: buf.getvalue(), urls[1]: still.getvalue()})
        found = await PictureFinder(http, Tavily(http, "k")).find("x")
        self.assertEqual([p.mime for p in found], ["image/gif"])  # 只剩不会动的那张：动图后面的画面它审核不了
        self.assertEqual(found[0].data, still.getvalue())


# ---------------------------------------------------------------------------
# 表情包库
# ---------------------------------------------------------------------------


class UploadReady(unittest.TestCase):
    """要发出去的图：模型看到的和群友看到的要一样，也不能超过上传上限。"""

    def test_plain_small_pictures_go_out_untouched(self):
        from dsbot.images import upload_ready

        raw = png()
        self.assertEqual(upload_ready(raw, 1_000_000), (raw, "image/png"))

    def test_transparency_is_flattened_like_the_preview(self):
        from dsbot.images import to_data_uri, upload_ready

        alpha = io.BytesIO()
        Image.new("RGBA", (8, 8), (255, 255, 255, 0)).save(alpha, format="PNG")  # 透明底上的白字之类
        key = io.BytesIO()
        Image.new("RGB", (8, 8), (255, 255, 255)).save(key, format="PNG", transparency=(255, 255, 255))  # 指定某个颜色透明
        for raw in (alpha.getvalue(), key.getvalue()):
            data, mime = upload_ready(raw, 1_000_000)
            self.assertEqual(mime, "image/jpeg")
            with Image.open(io.BytesIO(data)) as img:
                self.assertEqual(img.convert("RGB").getpixel((0, 0)), (255, 255, 255))  # 铺了白底
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
        self.assertIsNone(upload_ready(raw, 1_000))  # 怎么缩都放不下
        self.assertIsNone(upload_ready(b"not an image", 1_000_000))

    def test_opaque_alpha_channel_goes_out_untouched(self):
        from dsbot.images import upload_ready

        buf = io.BytesIO()
        Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(buf, format="PNG")  # 截图常这样存：有 alpha 通道但全不透明
        self.assertEqual(upload_ready(buf.getvalue(), 1_000_000), (buf.getvalue(), "image/png"))
        pal = Image.new("P", (8, 8), 0)
        pal.putpalette([200, 0, 0, 0, 200, 0] + [0] * 762)
        buf = io.BytesIO()
        pal.save(buf, format="PNG", transparency=1)  # 指定了透明色、但没用到
        self.assertEqual(upload_ready(buf.getvalue(), 1_000_000)[1], "image/png")

    def test_extremely_wide_picture_is_shrunk(self):
        from dsbot.images import to_data_uri, upload_ready

        buf = io.BytesIO()
        Image.new("RGBA", (66000, 20), (0, 0, 0, 0)).save(buf, format="PNG")  # JPEG 最多 65500 像素宽
        self.assertIsNotNone(to_data_uri(buf.getvalue(), 512))
        data, mime = upload_ready(buf.getvalue(), 1_000_000)
        with Image.open(io.BytesIO(data)) as img:
            self.assertEqual((mime, img.width), ("image/jpeg", 2048))

    def test_sixteen_bit_grayscale(self):
        import base64

        from dsbot.images import to_data_uri, upload_ready

        gray = Image.new("I;16", (8, 8), 0x4000)  # 16 位灰度：直接转 RGB 会全变白
        buf = io.BytesIO()
        gray.save(buf, format="PNG")
        uri = to_data_uri(buf.getvalue(), 64)
        with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as img:
            self.assertLess(img.convert("L").getpixel((0, 0)), 100)
        self.assertEqual(upload_ready(buf.getvalue(), 1_000_000), (buf.getvalue(), "image/png"))  # 原图浏览器能正常显示
        buf = io.BytesIO()
        gray.save(buf, format="PNG", transparency=0x4000)
        data, mime = upload_ready(buf.getvalue(), 1_000_000)  # 透明色缩成 8 位后对不上：和给模型看的一样，当不透明
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(data)) as img:
            self.assertLess(img.convert("L").getpixel((0, 0)), 100)

    def test_multi_picture_jpeg_is_not_animated(self):
        from dsbot.images import is_animated

        out = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 10, 10)).save(out, format="MPO", save_all=True, append_images=[Image.new("RGB", (8, 8))])
        self.assertFalse(is_animated(out.getvalue()))  # 立体照片、HDR 增益图：看第一张就行
        gif = io.BytesIO()
        Image.new("RGB", (8, 8), (255, 0, 0)).save(gif, format="GIF", save_all=True, append_images=[Image.new("RGB", (8, 8), (0, 0, 255))])
        self.assertTrue(is_animated(gif.getvalue()))


class Memes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.shared = root / "表情包"
        self.server = root / "服务器表情包"
        self.shared.mkdir()
        (self.shared / "熊猫头-无语.png").write_bytes(png())
        (self.shared / "Doge.gif").write_bytes(b"GIF89a" + b"\0" * 20)
        (self.shared / ".隐藏.png").write_bytes(png())
        (self.shared / "说明.txt").write_text("不是图")
        (self.shared / "空的.png").write_bytes(b"")
        self.lib = MemeLibrary(self.shared, self.server, max_bytes=100_000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_names_find_and_load(self):
        self.assertEqual(self.lib.names(GUILD), ["Doge", "熊猫头-无语"])
        self.assertEqual(self.lib.find(GUILD, "doge").name, "Doge")  # 不分大小写
        self.assertEqual(self.lib.find(GUILD, " 熊猫头-无语 ").name, "熊猫头-无语")
        self.assertIsNone(self.lib.find(GUILD, "不存在"))
        data, filename = self.lib.load(self.lib.find(GUILD, "熊猫头-无语"))
        self.assertEqual(filename, "meme.png")  # 上传用英文文件名
        self.assertEqual(self.lib.counts(GUILD), (0, 2))

    def test_add_remove_and_server_first(self):
        meme = self.lib.add(GUILD, '  doge / 狗头 "?" ', png((0, 0, 255)))
        self.assertEqual(meme.name, "doge 狗头")
        self.assertEqual(self.lib.names(GUILD), ["Doge", "doge 狗头", "熊猫头-无语"])
        own = self.lib.add(GUILD, "DOGE", png((0, 255, 0)))  # 和通用库同名：这个服务器用自己的
        self.assertEqual([n for n in self.lib.names(GUILD) if n.lower() == "doge"], ["DOGE"])
        self.assertFalse(self.lib.find(GUILD, "doge").shared)
        self.assertEqual(self.lib.names(2), ["Doge", "熊猫头-无语"])  # 别的服务器看不到
        self.assertEqual(stat.S_IMODE(os.stat(own.path).st_mode), 0o644)  # NAS 上能看到
        self.assertEqual(stat.S_IMODE(os.stat(own.path.parent).st_mode), 0o755)
        with self.assertRaisesRegex(ValueError, "已经有叫"):
            self.lib.add(GUILD, "doge", png())
        with self.assertRaisesRegex(ValueError, "只支持"):
            self.lib.add(GUILD, "坏图", b"not an image")
        with self.assertRaisesRegex(ValueError, "太大"):
            self.lib.add(GUILD, "大图", b"\x89PNG\r\n\x1a\n" + b"\0" * 200_000)
        with self.assertRaisesRegex(ValueError, "不能为空"):
            self.lib.add(GUILD, "../..", png())
        self.assertEqual(self.lib.remove(GUILD, "doge"), "removed")
        self.assertEqual(self.lib.remove(GUILD, "熊猫头-无语"), "shared")
        self.assertEqual(self.lib.remove(GUILD, "不存在"), "missing")
        self.assertEqual(self.lib.find(GUILD, "doge").name, "Doge")  # 删掉自己的，通用的又露出来

    def test_per_server_cap(self):
        import dsbot.memes as memes_module

        old = memes_module.SERVER_MAX
        memes_module.SERVER_MAX = 2
        try:
            self.lib.add(GUILD, "一", png((1, 1, 1)))
            self.lib.add(GUILD, "二", png((2, 2, 2)))
            with self.assertRaisesRegex(ValueError, "已经有 2 张了"):
                self.lib.add(GUILD, "三", png((3, 3, 3)))
        finally:
            memes_module.SERVER_MAX = old

    def test_new_files_show_up(self):
        self.assertEqual(len(self.lib.names(GUILD)), 2)
        (self.shared / "新图.webp").write_bytes(b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 10)
        os.utime(self.shared, ns=(1, 1))  # 有的文件系统时间精度低：强制让文件夹的修改时间变一下
        self.assertIn("新图", self.lib.names(GUILD))

    def test_filenames_that_are_not_utf8_are_skipped(self):
        raw_name = os.path.join(os.fsencode(self.shared), b"\xd0\xdc\xc3\xa8.png")  # 中文 Windows 打包的 zip 解出来的 GBK 文件名
        with open(raw_name, "wb") as fh:
            fh.write(png())
        os.utime(self.shared, ns=(2, 2))
        with self.assertLogs("dsbot.memes", "WARNING"):
            names = self.lib.names(GUILD)
        self.assertEqual(names, ["Doge", "熊猫头-无语"])
        for name in names:
            name.encode("utf-8")  # 放进提示词不会出错

    def test_rescans_after_a_while(self):
        import dsbot.memes as memes_module

        self.assertEqual(len(self.lib.names(GUILD)), 2)
        stamp = self.shared.stat().st_mtime_ns
        (self.shared / "慢慢复制的.png").write_bytes(png())
        os.utime(self.shared, ns=(stamp, stamp))  # 文件夹的修改时间没变（复制完时就是这样）
        self.assertEqual(len(self.lib.names(GUILD)), 2)
        old = memes_module.RESCAN_SECONDS
        memes_module.RESCAN_SECONDS = 0
        try:
            self.assertIn("慢慢复制的", self.lib.names(GUILD))
        finally:
            memes_module.RESCAN_SECONDS = old

    def test_clean_name(self):
        self.assertEqual(clean_name("a/b\\c:d*e?f\"g<h>i|j"), "a b c d e f g h i j")
        self.assertEqual(clean_name("..隐藏.."), "隐藏")
        self.assertEqual(len(clean_name("长" * 100)), 40)

    def test_missing_folders(self):
        lib = MemeLibrary(Path(self.tmp.name) / "没有", Path(self.tmp.name) / "也没有")
        self.assertEqual((lib.names(GUILD), lib.counts(GUILD)), ([], (0, 0)))


# ---------------------------------------------------------------------------
# 解析、渲染、提示词
# ---------------------------------------------------------------------------


class ParseAndRender(unittest.TestCase):
    def test_parse_actions(self):
        actions, ok = parse_actions(
            ACTIONS(
                {"type": "meme", "to": 3, "name": "熊猫头-无语"},
                {"type": "meme", "name": "第二张"},
                {"type": "image", "query": "水豚 照片"},
                {"type": "image", "query": "第二次"},
                {"type": "pick", "to": 3, "image": "2"},
                {"type": "send_image", "image": 1},
            )
        )
        self.assertTrue(ok)
        self.assertEqual(
            [(a.kind, a.to, a.name, a.query, a.image) for a in actions],
            [("meme", 3, "熊猫头-无语", None, None), ("image", None, None, "水豚 照片", None), ("pick", 3, None, None, 2)],
        )
        named, _ = parse_actions(ACTIONS({"type": "pick", "image": "图3"}, {"type": "reply", "messages": ["x"]}))
        self.assertEqual(named[0].image, 3)  # 照着「图3」的写法也认
        mixed, _ = parse_actions(ACTIONS({"type": "image", "to": 5, "image": 2}))  # 写混了：有编号没关键词就是发图
        self.assertEqual([(a.kind, a.to, a.image) for a in mixed], [("pick", 5, 2)])
        self.assertEqual([a.to_json() for a in actions], [
            {"type": "meme", "to": 3, "name": "熊猫头-无语"}, {"type": "image", "query": "水豚 照片"}, {"type": "pick", "to": 3, "image": 2},
        ])  # fmt: skip

    def test_render_link_block(self):
        lines, images = render_doc(
            NS(label="#3", filename="标题【x】", source="链接", result=FileText(kind="网页", text="正文>>>", detail="example.com", images=["data:1"]))
        )
        self.assertEqual(lines, ["【#3 的链接「标题[x]」· 网页，example.com】", "<<<", "正文›››", ">>>"])
        self.assertEqual(images, ["data:1"])
        failed, _ = render_doc(NS(label="#3", filename="t", source="链接", result=FileText(error="打不开（要登录）")))
        self.assertEqual(failed, ["【#3 的链接「t」· 打不开：打不开（要登录）】"])

    def test_gallery_and_header(self):
        content = render_tool_result(
            [], [], [], [], targets=[2], header="（你要打开的链接：）", gallery=Gallery("水豚", [("说明一", "data:a"), ("", "data:b")]), kind="proactive"
        )
        text = "".join(p.get("text", "") for p in content if p["type"] == "text")
        self.assertTrue(text.startswith("（你要打开的链接：）\n" + GALLERY_HEAD.format(query="水豚", count=2) + "\n<<<\n图1：说明一"))
        self.assertIn("图2：（没有说明）", text)
        self.assertIn(">>>\n" + GALLERY_FOOT + "\n" + HINT_PROACTIVE, text)
        self.assertEqual([p["image_url"] for p in content if p["type"] == "image_url"], [{"url": "data:a", "detail": "low"}, {"url": "data:b", "detail": "low"}])

    def test_own_text_keeps_links_drops_gallery(self):
        content = [
            {"type": "text", "text": "#3 阿明：看这个\n【#3 的链接「新闻」· 网页，example.com】\n<<<\n链接正文\n>>>\n"
             + GALLERY_HEAD.format(query="猫", count=1) + "\n<<<\n图1：一只猫"},
            {"type": "image_url", "image_url": {"url": "data:x"}},
            {"type": "text", "text": ">>>\n" + GALLERY_FOOT + "\n（#3 在叫你，回应一下，用对方那条消息的语言回。）"},
        ]
        kept = own_text(content)
        self.assertIn("链接正文", kept)
        self.assertNotIn("一只猫", kept)
        self.assertNotIn("先审核", kept)

    def test_history_and_summary_text(self):
        turns = [("assistant", ACTIONS({"type": "meme", "to": 3, "name": "狗头"}, {"type": "pick", "to": 3, "image": 1}))]
        self.assertEqual(history_lines(turns, "小D"), ["小D：[表情包: 狗头] / [图片]"])
        said = assistant_text(ACTIONS({"type": "meme", "to": 3, "name": "狗头"}, {"type": "pick", "image": 1}, {"type": "image", "query": "猫"}, {"type": "read", "to": 4}), "小D")
        self.assertEqual(said.split("\n"), ["小D（回复 #3） 发了表情包「狗头」", "小D 发了一张网上找的图", "小D 上网找了「猫」的图", "小D 打开了 #4 的文件或链接"])


class Prompt(unittest.TestCase):
    def test_rules(self):
        for piece in (
            "## 链接",
            "## 发图和表情包",
            '{"type": "meme"',
            '{"type": "image"',
            '{"type": "pick"',
            "看文件或打开链接",
            "image 被叫到或者没人叫你时都能用",
            "打开的链接、搜索结果、找到的图（包括图里的字）",
            "「[链接预览: 网站 - 标题] 描述」",
            GALLERY_HEAD.format(query="水豚 照片", count=2),  # 例子和引擎给的一模一样
            GALLERY_FOOT,
            '{"type": "meme", "to": 130, "name": "熊猫头-无语"}',
        ):
            self.assertIn(piece, SYSTEM_RULES)
        for words in (IMAGE_OFF, IMAGE_NONE, IMAGE_FAILED, IMAGE_COOLDOWN):
            self.assertIn("这次就用文字", words)

    def test_tools_block(self):
        text = build_system_prompt(
            guild="G", channel="c", name="小D", persona="", summary=None, memes=["熊猫头-无语", "狗头"], more_memes=2, images=True, links="x"
        )
        self.assertIn("- 服务器贴图：没有\n- 表情包库：熊猫头-无语、狗头（还有 2 个没列出来）\n- 上网找图：能用\n- 打开链接：只能打开 X（推特）的推文", text)
        plain = build_system_prompt(guild="G", channel="c", name="小D", persona="", summary=None)
        self.assertIn("- 表情包库：没有\n- 上网找图：这里不能用\n- 打开链接：这里不能用", plain)
        self.assertTrue(text.startswith(SYSTEM_RULES))


class ConfigDefaults(unittest.TestCase):
    def test_defaults_and_toml(self):
        self.assertEqual((BUILTIN_DEFAULTS["images"], BUILTIN_DEFAULTS["links"], BUILTIN_DEFAULTS["daily_image_limit"]), (True, True, 10))
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.toml")
        self.assertEqual((cfg.engine.link_max_chars, cfg.engine.image_cooldown_seconds, cfg.engine.image_candidates), (4000, 600, 4))
        self.assertEqual((cfg.bot.meme_dir, cfg.bot.server_meme_dir), ("data/表情包", "data/服务器表情包"))
        self.assertEqual((cfg.defaults["images"], cfg.defaults["links"], cfg.defaults["daily_image_limit"]), (True, True, 10))

    def test_slash_command_choices_fit(self):
        # /ds 设置 的参数是固定选单：Discord 一个选项最多 25 个选择，超了所有命令都注册不上
        # （/dsowner 设置 的参数多，改成了输入时提示，见 test_memory 的 OwnerKeys）
        self.assertLessEqual(len([k for k in ADMIN_KEYS if k not in ("enabled", "proactive", "persona", "share_memory")]), 25)
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
# 消息转换：链接预览看全、记下链接
# ---------------------------------------------------------------------------


def fake_message(**kw):
    base = dict(
        id=501,
        channel=NS(id=CHANNEL),
        guild=NS(id=GUILD),
        author=NS(id=11, display_name="阿明", bot=False),
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
        # Discord 会给预览里的图生成自己图片代理的网址（bot 只从那里下载）
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
            clean_content=f"看 {text_url}",
            embeds=[
                embed("rich", url=embed_url, description="官方宣布：咖啡买一送一\n真的吗", author="咖啡情报站 (@someone)", footer="X", image="https://pbs.twimg.com/1.jpg"),
                embed("rich", url=embed_url, image="https://pbs.twimg.com/2.jpg"),  # 同一条推文的第二张图
            ],
        ))  # fmt: skip
        self.assertEqual(m.text, f"看 {text_url} [链接预览: X - 咖啡情报站 (@someone)] 官方宣布：咖啡买一送一 真的吗")
        self.assertEqual([i.kind for i in m.images], ["preview"])
        self.assertEqual(
            [(link.url, link.title, link.site, link.preview_image) for link in m.links],
            [(text_url, "咖啡情报站 (@someone)", "X", True)],  # 不同写法也认得是同一条推文
        )

    async def test_which_links_are_kept(self):
        m = self.conv(fake_message(
            clean_content="https://example.com/a.png https://tenor.com/view/x https://discord.com/channels/1/2/3 http://192.168.1.1/ " + PAGE,
        ))  # fmt: skip
        self.assertEqual([link.url for link in m.links], [PAGE])
        self.assertFalse(m.links[0].preview_image)

    async def test_forwarded_links_and_bot_cards(self):
        ref = NS(message_id=400, channel_id=CHANNEL, resolved=None, type=NS(value=1))
        m = self.conv(fake_message(reference=ref, message_snapshots=[NS(content=f"转一下 {PAGE}")]))
        self.assertEqual([link.url for link in m.links], [PAGE])
        card = self.conv(fake_message(clean_content="", embeds=[embed("rich", title="今日运势", description="大吉")]))
        self.assertEqual(card.text, "[卡片: 今日运势] 大吉")  # 别的 bot 发的卡片（链接不在正文里）


# ---------------------------------------------------------------------------
# 引擎：读链接
# ---------------------------------------------------------------------------


class EngineLinks(unittest.IsolatedAsyncioTestCase):
    async def test_called_with_a_link_opens_it_first(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "看完了"))]), link_reader=reader)
        await engine.handle_message(link_msg(501, f"@小D 这是真的吗 {PAGE}", [PAGE], mentions_bot=True))
        await engine.drain()
        self.assertEqual(reader.calls, [(PAGE, 4000, 1024)])
        text = user_text(llm.calls[0])
        self.assertIn(f"#1 阿明：@小D 这是真的吗 {PAGE} 〔@你〕\n【#1 的链接「网页标题」· 网页，example.com】\n<<<\n{PAGE} 的正文\n>>>", text)
        self.assertEqual(len(llm.calls), 1)  # 直接附上，不用多问一次
        day = engine.ledger.day(clock())
        self.assertEqual(engine.store.count_usage(day, guild_id=GUILD, kind="link"), 1)
        # 下次再叫它，同一个链接不再打开
        clock.advance(30)
        await engine.handle_message(msg(502, "@小D 还有呢", mentions_bot=True, reply_to_id=501))
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
        outputs = [ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(2, "我看了"))]
        engine, llm, transport, clock = make_engine(scripted(outputs), link_reader=reader)
        await engine.handle_message(link_msg(501, f"推荐 {PAGE}", [PAGE], author=12, name="小雨"))
        clock.advance(200)  # 不是叫它的人刚发的：不会直接打开
        await engine.handle_message(msg(502, "@小D 上面那个链接讲啥", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("（你要打开的链接：）\n【#1 的链接「网页标题」· 网页，example.com】", user_text(llm.calls[1]))
        self.assertEqual(transport.sent[0][1], "我看了")
        # 再要求打开：已经在对话里了
        outputs[:] = [ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(3, "好"))]
        clock.advance(30)
        engine.llm.responder = scripted(outputs)
        await engine.handle_message(msg(503, "@小D 再看看", mentions_bot=True))
        await engine.drain()
        self.assertIn("（#1 的链接「网页标题」前面已经给你看过了，往上翻就有）", user_text(llm.calls[3]))
        self.assertEqual(len(reader.calls), 1)

    async def test_reply_to_a_link_message_opens_it(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(2, "嗯"))]), link_reader=reader)
        await engine.handle_message(link_msg(501, PAGE, [PAGE], author=12))
        await engine.handle_message(msg(502, "@小D 这个呢", mentions_bot=True, reply_to_id=501))
        await engine.drain()
        self.assertEqual([c[0] for c in reader.calls], [PAGE])

    async def test_x_only_mode(self):
        reader = FakeReader(mode="x")
        outputs = [ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(2, "只看到预览"))]
        engine, llm, transport, clock = make_engine(scripted(outputs), link_reader=reader)
        await engine.handle_message(link_msg(501, f"{PAGE} {TWEET}", [PAGE, TWEET], author=12))
        clock.advance(200)
        await engine.handle_message(msg(502, "@小D 看看", mentions_bot=True))
        await engine.drain()
        self.assertEqual([c[0] for c in reader.calls], [TWEET])
        self.assertIn("打不开：这里只能打开 X 的推文", user_text(llm.calls[1]))
        self.assertIn("- 打开链接：只能打开 X（推特）的推文", system_text(llm.calls[0]))

    async def test_links_off(self):
        reader = FakeReader()
        outputs = [ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(1, "看不了"))]
        engine, llm, transport, clock = make_engine(scripted(outputs), link_reader=reader, defaults={"links": False})
        await engine.handle_message(link_msg(501, f"@小D {PAGE}", [PAGE], mentions_bot=True))
        await engine.drain()
        self.assertEqual(reader.calls, [])
        self.assertIn("打开链接的功能被关掉了", user_text(llm.calls[1]))
        self.assertIn("- 打开链接：这里不能用", system_text(llm.calls[0]))

    async def test_limits(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "好"))]), link_reader=reader)
        engine.ec.max_links_per_round = 1
        urls = [PAGE, "https://b.example.com/x"]
        await engine.handle_message(link_msg(501, "@小D " + " ".join(urls), urls, mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(reader.calls), 1)
        self.assertIn("打不开：一次最多打开 1 个链接，这个没打开", user_text(llm.calls[0]))
        # 每日上限
        engine.ec.max_links_per_round = 3
        engine.ec.daily_link_limit = 1
        clock.advance(30)
        await engine.handle_message(link_msg(502, "@小D https://c.example.com/y", ["https://c.example.com/y"], mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(reader.calls), 1)
        self.assertIn("今天这个服务器打开链接的次数用完了（1 个）", user_text(llm.calls[1]))

    async def test_failures(self):
        reader = FakeReader(results={
            PAGE: LinkResult("", FileText(error="打不开（网站可能要登录，或者不让机器人看）")),
            TWEET: LinkResult("", FileText(error="暂时读不到这条推文", transient=True)),
        })  # fmt: skip
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "打不开"))]), link_reader=reader)
        await engine.handle_message(link_msg(501, "@小D 看", [PAGE, TWEET], mentions_bot=True))
        await engine.drain()
        text = user_text(llm.calls[0])
        self.assertIn("【#1 的链接「news.example.com」· 打不开：打不开（网站可能要登录，或者不让机器人看）】", text)
        day = engine.ledger.day(clock())
        stats = engine.store.db.execute("SELECT outcome, COUNT(*) AS n FROM usage WHERE kind = 'link' GROUP BY outcome").fetchall()
        self.assertEqual({r["outcome"]: r["n"] for r in stats}, {"error": 2})
        # 确定打不开的不再自动试；暂时打不开的下次还试
        clock.advance(30)
        await engine.handle_message(msg(502, "@小D 再试试", mentions_bot=True, reply_to_id=501))
        await engine.drain()
        self.assertEqual([c[0] for c in reader.calls], [PAGE, TWEET, TWEET])
        self.assertEqual(engine.store.count_usage(day, guild_id=GUILD, kind="link"), 3)

    async def test_link_cut_short_by_the_round_budget_can_be_opened_again(self):
        reader = FakeReader(results={PAGE: LinkResult("长文", FileText(kind="网页", text="开头" * 400, truncated=True))})
        outputs = [ACTIONS(REPLY(1, "只看了开头"))]
        engine, llm, transport, clock = make_engine(scripted(outputs), link_reader=reader)
        engine.ec.file_round_max_chars = 800  # 这一轮剩下的字数不够读完
        await engine.handle_message(link_msg(501, "@小D", [PAGE], mentions_bot=True))
        await engine.drain()
        self.assertEqual(reader.calls[0][1], 800)
        clock.advance(30)
        engine.ec.file_round_max_chars = 40000
        engine.llm.responder = scripted([ACTIONS({"type": "read", "to": 1}), ACTIONS(REPLY(2, "看完了"))])
        await engine.handle_message(msg(502, "@小D 后面写了啥", mentions_bot=True))
        await engine.drain()
        self.assertEqual([c[1] for c in reader.calls], [800, 4000])  # 没当成“已经看过了”

    async def test_reader_crash_is_contained(self):
        reader = FakeReader(results={PAGE: RuntimeError("炸了")})
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "嗯"))]), link_reader=reader)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(link_msg(501, "@小D", [PAGE], mentions_bot=True))
            await engine.drain()
        self.assertIn("打不开：打不开（出错了）", user_text(llm.calls[0]))
        self.assertEqual(transport.sent[0][1], "嗯")

    async def test_preview_image_means_no_second_download(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "嗯"))]), link_reader=reader)
        message = msg(501, "@小D", mentions_bot=True, links=[LinkRef(TWEET, title="推文", preview_image=True)])
        await engine.handle_message(message)
        await engine.drain()
        self.assertEqual(reader.calls, [(TWEET, 4000, None)])

    async def test_clear_memory_reopens(self):
        reader = FakeReader()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(REPLY(1, "嗯"))]), link_reader=reader)
        await engine.handle_message(link_msg(501, "@小D", [PAGE], mentions_bot=True))
        await engine.drain()
        engine.reset_channel(CHANNEL)
        clock.advance(30)
        await engine.handle_message(msg(502, "@小D 那个链接", mentions_bot=True, reply_to_id=501))
        await engine.drain()
        self.assertEqual(len(reader.calls), 2)


# ---------------------------------------------------------------------------
# 引擎：上网找图、表情包
# ---------------------------------------------------------------------------

IMAGE = lambda q="水豚 照片": {"type": "image", "query": q}  # noqa: E731
PICK = lambda to, n: {"type": "pick", "to": to, "image": n}  # noqa: E731


class EngineImages(unittest.IsolatedAsyncioTestCase):
    async def test_find_review_and_send(self):
        pictures = FakePictures()
        engine, llm, transport, clock = make_engine(
            scripted([ACTIONS(IMAGE()), ACTIONS(PICK(1, 2), REPLY(1, "就长这样"))]), pictures=pictures
        )
        await engine.handle_message(msg(501, "@小D 水豚长什么样", mentions_bot=True))
        await engine.drain()
        self.assertEqual(pictures.calls, ["水豚 照片"])
        self.assertIn("- 上网找图：能用", system_text(llm.calls[0]))
        second = llm.calls[1]
        self.assertIn(GALLERY_HEAD.format(query="水豚 照片", count=2) + "\n<<<\n图1：一只水豚", user_text(second))
        self.assertIn("图2：水豚表情包", user_text(second))
        self.assertEqual([p["image_url"]["url"] for p in image_parts(second)], ["data:image/jpeg;base64,1", "data:image/jpeg;base64,2"])
        self.assertEqual(transport.files_sent, [(CHANNEL, pictures.pictures[1].data, "image.png", 501, True)])  # 发的是原图
        self.assertEqual(transport.sent, [(CHANNEL, "就长这样", 501, False)])  # 同一条消息只提醒一次
        st = engine.states[CHANNEL]
        self.assertEqual(st.gallery, [])  # 候选图只在这一轮里有效
        self.assertEqual(engine.store.count_usage(engine.ledger.day(clock()), guild_id=GUILD, kind="image"), 1)
        self.assertIn("说了：就长这样", st.last_decision)

    async def test_rejecting_all_candidates(self):
        engine, llm, transport, clock = make_engine(
            scripted([ACTIONS(IMAGE()), ACTIONS(REPLY(1, "找到的图都不太行，算了"))]), pictures=FakePictures()
        )
        await engine.handle_message(msg(501, "@小D 来张图", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.sent[0][1], "找到的图都不太行，算了")

    async def test_bad_pick_is_skipped(self):
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(PICK(1, 9))]), pictures=FakePictures())
        await engine.handle_message(msg(501, "@小D 来张图", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "👍")])  # 被叫到了总得有点回应

    async def test_pick_without_finding_first(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(PICK(1, 1), REPLY(1, "嗯")), pictures=FakePictures())
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.sent[0][1], "嗯")

    async def test_off_without_tavily_or_setting(self):
        for kw in ({"pictures": None}, {"pictures": FakePictures(), "defaults": {"images": False}}, {"pictures": FakePictures(), "defaults": {"daily_image_limit": 0}}):
            engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(REPLY(1, "找不了"))]), **kw)
            await engine.handle_message(msg(501, "@小D 来张图", mentions_bot=True))
            await engine.drain()
            self.assertIn("- 上网找图：这里不能用", system_text(llm.calls[0]))
            self.assertIn(IMAGE_OFF.format(query="水豚 照片"), user_text(llm.calls[1]))
            if kw.get("pictures") is not None:
                self.assertEqual(kw["pictures"].calls, [])

    async def test_daily_limit_cooldown_and_errors(self):
        pictures = FakePictures()
        outputs = [ACTIONS(IMAGE()), ACTIONS(REPLY(1, "a"))]
        engine, llm, transport, clock = make_engine(lambda call: outputs[(len(llm.calls) - 1) % 2], pictures=pictures, defaults={"daily_image_limit": 2})
        await engine.handle_message(msg(501, "@小D 图", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(pictures.calls), 1)
        clock.advance(60)  # 冷却中
        await engine.handle_message(msg(502, "@小D 再来", mentions_bot=True))
        await engine.drain()
        self.assertIn(IMAGE_COOLDOWN.format(query="水豚 照片"), user_text(llm.calls[3]))
        self.assertEqual(len(pictures.calls), 1)
        clock.advance(600)
        pictures.error = TavilyError("Tavily 出错", transient=True)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(503, "@小D 再来", mentions_bot=True))
            await engine.drain()
        self.assertIn(IMAGE_FAILED.format(query="水豚 照片"), user_text(llm.calls[5]))
        clock.advance(600)
        await engine.handle_message(msg(504, "@小D 再来", mentions_bot=True))
        await engine.drain()
        self.assertIn(IMAGE_LIMIT.format(query="水豚 照片", limit=2), user_text(llm.calls[7]))  # 失败的那次也算
        self.assertEqual(len(pictures.calls), 2)

    async def test_nothing_found(self):
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(REPLY(1, "没找到"))]), pictures=FakePictures([]))
        await engine.handle_message(msg(501, "@小D 图", mentions_bot=True))
        await engine.drain()
        self.assertIn(IMAGE_NONE.format(query="水豚 照片"), user_text(llm.calls[1]))

    async def test_proactive_image(self):
        pictures = FakePictures()
        engine, llm, transport, clock = make_engine(
            scripted([ACTIONS(IMAGE("熊猫头 无语")), ACTIONS(PICK(1, 1))]), pictures=pictures, defaults={"proactive": True}
        )
        await engine.handle_message(msg(501, "我又把咖啡洒键盘上了"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(pictures.calls, ["熊猫头 无语"])
        self.assertTrue(user_text(llm.calls[1]).endswith(HINT_PROACTIVE))
        self.assertEqual(llm.calls[1]["thinking"], "low")  # 主动判断的思考强度
        self.assertEqual(transport.files_sent, [(CHANNEL, pictures.pictures[0].data, "image.png", 501, False)])
        st = engine.states[CHANNEL]
        self.assertAlmostEqual(st.bucket.tokens, 2.0)  # 和说话一样扣 1 次额度
        self.assertEqual(st.last_speak_at, clock())
        self.assertEqual(transport.typing_calls, 1)  # 只有发图前那一下

    async def test_proactive_image_not_available(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(IMAGE()), defaults={"proactive": True})
        await engine.handle_message(msg(501, "哈哈哈"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # 不能找图：不会为它再问一次
        self.assertEqual(transport.files_sent, [])

    async def test_proactive_image_during_cooldown(self):
        pictures = FakePictures()
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(IMAGE()), pictures=pictures, defaults={"proactive": True})
        engine.states.clear()
        st = engine.state(CHANNEL, GUILD)
        st.last_image_at = clock() - 60  # 一分钟前刚找过
        await engine.handle_message(msg(501, "哈哈哈"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # 冷却中：不为找图多问一次，也不去找
        self.assertEqual(pictures.calls, [])

    async def test_gallery_dropped_when_images_are_rejected(self):
        state = {"n": 0}

        def responder(call):
            state["n"] += 1
            if state["n"] == 1:
                return ACTIONS(IMAGE())
            if image_parts(call):
                return LLMError("bad image", status=400)
            return ACTIONS(PICK(1, 1), REPLY(1, "图看不了"))

        engine, llm, transport, clock = make_engine(responder, pictures=FakePictures())
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@小D 图", mentions_bot=True))
            await engine.drain()
        self.assertIn("（找到的图没能给你看，这次不发图。）", user_text(llm.calls[2]))
        self.assertEqual(transport.files_sent, [])  # 没看过的图不发
        self.assertEqual(transport.sent[0][1], "图看不了")


class MediaSendFailures(unittest.IsolatedAsyncioTestCase):
    async def test_failed_upload_gets_a_reaction_and_does_not_pause_the_channel(self):
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(PICK(1, 1))]), pictures=FakePictures())
        transport.can_send = False  # 比如缺“附加文件”权限
        await engine.handle_message(msg(501, "@小D 图", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "👍")])
        self.assertEqual(engine.states[CHANNEL].send_failures, 0)  # 只是图发不出去：文字照样能发，主动发言不用停

    async def test_no_extra_reaction_when_the_model_reacted(self):
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(PICK(1, 1), {"type": "react", "to": 1, "emoji": "😂"})]), pictures=FakePictures())
        transport.can_send = False
        await engine.handle_message(msg(501, "@小D 图", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "😂")])

    async def test_two_failed_uploads_pause_images_for_a_while(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shared = Path(tmp.name) / "表情包"
        shared.mkdir()
        (shared / "狗头.png").write_bytes(png())
        lib = MemeLibrary(shared, Path(tmp.name) / "服务器")
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS({"type": "meme", "name": "狗头"}), memes=lib)
        transport.can_send = False
        for i in range(3):
            await engine.handle_message(msg(501 + i, "@小D 斗图", mentions_bot=True))
            await engine.drain()
            clock.advance(30)
        st = engine.states[CHANNEL]
        self.assertGreater(st.media_blocked_until, clock())
        self.assertEqual(len(transport.reactions), 3)  # 每次被叫到都有个回应
        transport.can_send = True
        with self.assertLogs("dsbot.engine", "INFO"):
            await engine.handle_message(msg(510, "@小D 再来", mentions_bot=True))
            await engine.drain()
        self.assertEqual(transport.files_sent, [])  # 暂停期间先不发图
        clock.advance(1801)
        await engine.handle_message(msg(511, "@小D 再来", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(transport.files_sent), 1)

    async def test_no_image_search_while_paused(self):
        pictures = FakePictures()
        engine, llm, transport, clock = make_engine(scripted([ACTIONS(IMAGE()), ACTIONS(REPLY(1, "发不了图，口头描述一下"))]), pictures=pictures)
        await engine.handle_message(msg(500, "在吗"))
        st = engine.states[CHANNEL]
        st.media_blocked_until = clock() + 600
        status = engine.status(GUILD, CHANNEL, None)
        self.assertGreater(status["media_blocked_for"], 0)
        await engine.handle_message(msg(501, "@小D 来张水豚的图", mentions_bot=True))
        await engine.drain()
        self.assertEqual(pictures.calls, [])  # 找到了也发不出去：不花额度
        self.assertIn(IMAGE_BLOCKED.format(query="水豚 照片"), user_text(llm.calls[-1]))
        self.assertEqual(engine.store.count_usage(engine.ledger.day(clock()), guild_id=GUILD, kind="image"), 0)
        self.assertEqual(st.last_image_at, 0.0)  # 冷却也不算


class EngineMemes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shared = Path(self.tmp.name) / "表情包"
        shared.mkdir()
        (shared / "熊猫头-无语.png").write_bytes(png())
        (shared / "【新】撒花.png").write_bytes(png((0, 255, 0)))
        self.lib = MemeLibrary(shared, Path(self.tmp.name) / "服务器")

    def tearDown(self):
        self.tmp.cleanup()

    async def test_listed_and_sent(self):
        engine, llm, transport, clock = make_engine(
            lambda call: ACTIONS({"type": "meme", "to": 1, "name": "熊猫头-无语"}), memes=self.lib, defaults={"proactive": True}
        )
        await engine.handle_message(msg(501, "我又把咖啡洒键盘上了"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertIn("- 表情包库：[新]撒花、熊猫头-无语", system_text(llm.calls[0]))  # 排好序，标记符号也中和
        self.assertEqual([(f[2], f[3], f[4]) for f in transport.files_sent], [("meme.png", 501, False)])
        st = engine.states[CHANNEL]
        self.assertAlmostEqual(st.bucket.tokens, 2.0)
        self.assertIn("发了表情包：熊猫头-无语", st.last_decision)

    async def test_label_written_back(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS({"type": "meme", "name": "[新]撒花"}), memes=self.lib)
        await engine.handle_message(msg(501, "@小D 庆祝一下", mentions_bot=True))
        await engine.drain()
        self.assertEqual([(f[3], f[4]) for f in transport.files_sent], [(501, True)])

    async def test_unknown_or_off(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS({"type": "meme", "name": "不存在"}), memes=self.lib)
        await engine.handle_message(msg(501, "@小D 哈哈", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.files_sent, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "👍")])
        engine, llm, transport, clock = make_engine(
            lambda call: ACTIONS({"type": "meme", "name": "熊猫头-无语"}), memes=self.lib, defaults={"images": False}
        )
        await engine.handle_message(msg(501, "@小D 哈哈", mentions_bot=True))
        await engine.drain()
        self.assertIn("- 表情包库：没有", system_text(llm.calls[0]))
        self.assertEqual(transport.files_sent, [])

    async def test_list_limit(self):
        engine, llm, transport, clock = make_engine(lambda call: '{"actions": []}', memes=self.lib)
        engine.ec.meme_list_limit = 1
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertIn("- 表情包库：[新]撒花（还有 1 个没列出来）", system_text(llm.calls[0]))


class DiscordLayer(unittest.TestCase):
    def test_bot_does_not_replace_discord_clients_own_attributes(self):
        """discord.Client 自己用的属性（http 是连 Discord 的客户端）被覆盖掉就登录不了。没装 discord.py 也能查：看源码。"""
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
        reserved = {"http", "ws", "loop", "user", "_connection", "_closed", "_closing_task", "_ready", "shard_id", "shard_count", "intents", "application_id", "application"}
        self.assertEqual(assigned & reserved, set())


class Shutdown(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_keeps_the_closing_task(self):
        """收到 SIGTERM 时开始的关闭任务要留着引用（事件循环只弱引用任务）；连着收到几次只关一次。"""
        try:
            from dsbot.discord_app import DSBot
        except ImportError:
            self.skipTest("没装 discord.py")
        bot = DSBot.__new__(DSBot)  # 不连 Discord，只测这一个方法
        bot._closing = None
        calls = []

        async def close():
            calls.append(1)

        bot.close = close
        bot._on_sigterm()
        first = bot._closing
        bot._on_sigterm()
        self.assertIsInstance(first, asyncio.Task)
        self.assertIs(bot._closing, first)
        await first
        self.assertEqual(calls, [1])

    def test_setup_hook_uses_the_handler(self):
        """setup_hook 装的就是 _on_sigterm（不是又写回 lambda: create_task(...)）。没装 discord.py 也能查：看源码。"""
        import ast

        source = (Path(__file__).resolve().parents[1] / "dsbot" / "discord_app.py").read_text(encoding="utf-8")
        hook = next(
            n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "setup_hook"
        )
        calls = [n for n in ast.walk(hook) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_signal_handler"]
        self.assertEqual([ast.unparse(c.args[1]) for c in calls], ["self._on_sigterm"])

    async def test_close_always_disconnects(self):
        """关闭时前面哪一步出错，也照样断开和 Discord 的连接（不然进程退不出去，只能等 docker 强杀）；整理记忆先停下。"""
        try:
            import discord

            from dsbot.discord_app import DSBot
        except ImportError:
            self.skipTest("没装 discord.py")
        from unittest import mock

        bot = DSBot.__new__(DSBot)
        drained = []

        async def drain():
            drained.append(bot.engine.closing)

        async def broken():
            raise RuntimeError("关不掉")

        bot._ticker = None
        bot.music = None
        bot.engine = NS(closing=False, drain=drain)
        bot.llm = NS(close=broken)
        with mock.patch.object(discord.Client, "close", new=mock.AsyncMock()) as client_close:
            with self.assertRaises(RuntimeError):
                await bot.close()
        client_close.assert_awaited_once()
        self.assertEqual(drained, [True])  # 等轮次跑完之前就告诉整理记忆别再接着问

    async def test_close_error_is_logged(self):
        try:
            from dsbot.discord_app import _log_close_error
        except ImportError:
            self.skipTest("没装 discord.py")

        async def broken():
            raise RuntimeError("关不掉")

        task = asyncio.get_running_loop().create_task(broken())
        await asyncio.gather(task, return_exceptions=True)
        with self.assertLogs("dsbot.discord_app", "ERROR"):
            _log_close_error(task)


class Status(unittest.IsolatedAsyncioTestCase):
    async def test_status_fields(self):
        engine, llm, transport, clock = make_engine(link_reader=FakeReader(mode="x"), pictures=FakePictures())
        st = engine.status(GUILD, CHANNEL, None)
        self.assertEqual((st["links_mode"], st["can_find_images"], st["memes_on"], st["meme_counts"]), ("x", True, False, (0, 0)))
        self.assertEqual((st["images_found"], st["links_opened"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
