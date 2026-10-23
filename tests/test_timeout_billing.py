"""超时、响应传到一半断了：对方多半照样算了钱，按估算记进用量记录和账本（每日预算拦得住）。"""

import asyncio
import copy
import json
import unittest
from unittest import mock

from helpers import CHANNEL, GUILD, FakeSearcher, make_engine, msg
from test_memory import ACTIONS, REPLY, chat, is_review, stores, three_rounds

from dsbot.config import DeepSeekConfig
from dsbot.ledger import Ledger
from dsbot.llm import LLMError
from dsbot.models import Usage
from dsbot.prompts import TOOL_FAILED
from dsbot.render import content_tokens, estimate_tokens
from dsbot.search import SearchError, WebSearch, add_usage

try:
    import aiohttp  # 只有测 WebSearch._post 时要用（discord.py 自带）
except ImportError:  # pragma: no cover
    aiohttp = None

# 新加的名字（estimate_usage、estimate_search 等）在用到的测试里再导入：修复之前的代码也能载入这个文件，逐个看哪些测试不过


def billed(message="请求超时（90 秒）", usage=None, *, timeout=True):
    """超时（timeout=False 是传到一半断了）的 LLMError，带着估出来的用量。
    用量另外设：修复之前的 LLMError 不认 usage 参数，这样那边也能跑到断言。"""
    exc = LLMError(message, timeout=timeout)
    exc.usage = usage
    return exc


def search_timeout(message="搜索超时（60 秒）"):
    """_post 超时（对方可能照样算了钱）。maybe_billed 另外设，理由同上。"""
    exc = SearchError(message)
    exc.maybe_billed = True
    return exc


def search_client(responses, tavily_key=None):
    """WebSearch，_post 换成按顺序给出 responses（是 Exception 的就抛出）。"""
    client = WebSearch(api_key="sk-test", url=DeepSeekConfig().anthropic_url, model="deepseek-flash", tavily_key=tavily_key)
    sent = []

    async def fake_post(url, payload, headers):
        sent.append((url, json.loads(json.dumps(payload)), headers))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client._post = fake_post
    return client, sent


def rows(engine, where="1"):
    return engine.store.db.execute(f"SELECT * FROM usage WHERE {where} ORDER BY id").fetchall()


class _BrokenSession:
    """假的 aiohttp 会话：连接时（at="enter"）或读响应时（at="read"）抛出 exc。"""

    closed = False

    def __init__(self, exc, at="enter", status=200):
        self.exc, self.at, self.status = exc, at, status

    def post(self, url, **kw):
        return self

    async def __aenter__(self):
        if self.at == "enter":
            raise self.exc
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        raise self.exc


# ---------------------------------------------------------------------------
# llm：估算用量
# ---------------------------------------------------------------------------


class EstimateUsage(unittest.TestCase):
    def test_prefix_is_a_hit_and_the_last_two_are_a_miss(self):
        from dsbot.llm import estimate_usage

        messages = [
            {"role": "system", "content": "系统" * 100},
            {"role": "user", "content": "早" * 50},
            {"role": "assistant", "content": '{"actions": []}'},
            {"role": "user", "content": [{"type": "text", "text": "在吗" * 20}, {"type": "image_url", "image_url": {"url": "data:x", "detail": "low"}}]},
        ]
        hit = sum(content_tokens(m["content"]) for m in messages[:2])
        miss = sum(content_tokens(m["content"]) for m in messages[2:])
        self.assertEqual(estimate_usage(messages, 2000), Usage(prompt_tokens=hit + miss, hit=hit, miss=miss, output=1000))
        self.assertGreaterEqual(miss, 200)  # 图按 200 算（low）

    def test_edge_cases(self):
        from dsbot.llm import DEFAULT_MAX_TOKENS, estimate_usage

        two = [{"role": "system", "content": "整理"}, {"role": "user", "content": "聊天记录" * 100}]
        total = sum(content_tokens(m["content"]) for m in two)
        u = estimate_usage(two, 6000)
        self.assertEqual((u.hit, u.miss, u.prompt_tokens, u.output, u.reasoning), (0, total, total, 3000, 0))  # 只有两条：全按没命中
        one = estimate_usage([{"role": "user", "content": None}], None)
        self.assertEqual((one.hit, one.miss, one.output), (0, 4, DEFAULT_MAX_TOKENS // 2))  # 没写 max_tokens：按默认的一半
        self.assertEqual(estimate_usage([], 100), Usage(output=50))
        image = estimate_usage([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}], 10)
        self.assertEqual(image.miss, 4 + 800)  # 没写 detail 的图按 800 算
        self.assertEqual(estimate_usage([{"role": "user", "content": "hi", "name": "x"}], 0).output, DEFAULT_MAX_TOKENS // 2)


# ---------------------------------------------------------------------------
# llm：DeepSeekClient 什么时候带估算
# ---------------------------------------------------------------------------


class Client(unittest.IsolatedAsyncioTestCase):
    MESSAGES = [
        {"role": "system", "content": "系统" * 100},
        {"role": "user", "content": "早" * 50},
        {"role": "assistant", "content": '{"actions": []}'},
        {"role": "user", "content": [{"type": "text", "text": "在吗" * 20}, {"type": "image_url", "image_url": {"url": "data:x", "detail": "low"}}]},
    ]

    def _want(self):
        """MESSAGES、max_tokens=2000 该估出来的：前两条命中，后两条没命中，输出 1000。"""
        hit = sum(content_tokens(m["content"]) for m in self.MESSAGES[:2])
        miss = sum(content_tokens(m["content"]) for m in self.MESSAGES[2:])
        return Usage(prompt_tokens=hit + miss, hit=hit, miss=miss, output=1000)

    async def test_timeout_and_cut_off_carry_an_estimate(self):
        from dsbot.llm import DeepSeekClient, _CutOff

        messages, want = self.MESSAGES, self._want()
        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m", max_retries=2)
        for error, timed_out in ((asyncio.TimeoutError(), True), (_CutOff("payload"), False)):
            with self.subTest(timeout=timed_out):
                calls = []

                async def post(payload, timeout, error=error):
                    calls.append(timeout)
                    raise error

                client._post = post
                with self.assertRaises(LLMError) as ctx:
                    await client.complete(messages, max_tokens=2000)
                exc = ctx.exception
                self.assertEqual((len(calls), exc.timeout, exc.status), (1, timed_out, None))  # 不重试；传到一半断了不算超时
                self.assertEqual(getattr(exc, "usage", None), want)

    async def test_unbilled_errors_have_no_estimate(self):
        from dsbot.llm import DeepSeekClient

        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m", max_retries=0)
        outcomes = (
            OSError("connection refused"),
            (500, {"error": {"message": "busy"}}),
            (400, {"error": {"message": "bad"}}),
            (402, {"error": {"message": "Insufficient Balance"}}),
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome):

                async def post(payload, timeout, outcome=outcome):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                client._post = post
                with self.assertRaises(LLMError) as ctx:
                    await client.complete([{"role": "user", "content": "hi"}], max_tokens=100)
                self.assertIsNone(getattr(ctx.exception, "usage", None))
                self.assertFalse(ctx.exception.timeout)
        # 原来的写法照样能用
        exc = LLMError("Server Error", 500)
        self.assertEqual((exc.status, exc.timeout, getattr(exc, "usage", None)), (500, False, None))

    async def test_unusable_200_is_billed(self):
        """HTTP 200 但结果用不了：对方已经跑完、算了钱。带了用量就照记，没有就估。"""
        from dsbot.llm import DeepSeekClient, estimate_usage

        messages = [{"role": "user", "content": "hi"}]
        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m", max_retries=2)
        usage = {"prompt_tokens": 900, "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 100, "completion_tokens": 50}
        cases = (
            ((200, {"oops": 1}), estimate_usage(messages, 100)),
            ((200, "不是 JSON"), estimate_usage(messages, 100)),
            ((200, {"choices": [], "usage": usage}), Usage(prompt_tokens=900, hit=800, miss=100, output=50)),
        )
        for outcome, want in cases:
            with self.subTest(outcome=outcome):
                calls = []

                async def post(payload, timeout, outcome=outcome):
                    calls.append(1)
                    return outcome

                client._post = post
                with self.assertRaises(LLMError) as ctx:
                    await client.complete(messages, max_tokens=100)
                self.assertEqual((len(calls), ctx.exception.usage), (1, want))  # 不重试（再发一次又要算钱）

    async def test_connect_timeout_is_not_billed_and_is_retried(self):
        """连接阶段就超时（连不上、代理卡住）：请求没发出去，不算钱，可以重试。"""
        from dsbot.llm import DeepSeekClient, _NotSent

        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m", max_retries=2)
        outcomes = [_NotSent("ConnectionTimeoutError()"), (200, {"choices": [{"message": {"content": "好"}}], "usage": {}})]

        async def post(payload, timeout):
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        client._post = post
        with mock.patch("dsbot.llm.asyncio.sleep", new=mock.AsyncMock()):
            completion = await client.complete([{"role": "user", "content": "hi"}], max_tokens=100)
        self.assertEqual(completion.content, "好")
        outcomes[:] = [_NotSent("ConnectionTimeoutError()")] * 3
        with mock.patch("dsbot.llm.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(LLMError) as ctx:
                await client.complete([{"role": "user", "content": "hi"}], max_tokens=100)
        self.assertEqual((ctx.exception.usage, ctx.exception.timeout), (None, False))
        self.assertIn("连不上", str(ctx.exception))

    @unittest.skipIf(aiohttp is None, "没装 aiohttp")
    async def test_post_tells_what_was_sent(self):
        """_post：连接阶段超时 → 没发出去；错误响应传到一半断了、读超时 → 照常按状态码处理（不算钱）；200 传到一半断了 → 算钱。"""
        from dsbot.llm import DeepSeekClient, _CutOff, _NotSent

        not_connected = getattr(aiohttp, "ConnectionTimeoutError", aiohttp.ServerTimeoutError)
        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m")
        cases = (
            (not_connected("connect"), "enter", 200, _NotSent),
            (aiohttp.ClientPayloadError("cut"), "read", 200, _CutOff),
            (aiohttp.ClientPayloadError("cut"), "read", 503, (503, "")),
            (asyncio.TimeoutError(), "read", 429, (429, "")),
            (asyncio.TimeoutError(), "read", 200, asyncio.TimeoutError),
        )
        for exc, at, status, want in cases:
            with self.subTest(exc=type(exc).__name__, status=status):
                client._session = _BrokenSession(exc, at, status)
                if isinstance(want, tuple):
                    self.assertEqual(await client._post({}, 30), want)
                else:
                    with self.assertRaises(want):
                        await client._post({}, 30)

    @unittest.skipIf(aiohttp is None, "没装 aiohttp")
    async def test_connect_deadline_is_well_before_the_total(self):
        """连接的时限要比总时限早一截：aiohttp 把时限凑成整秒，挨得太近就总是总时限先到，连不上的也被当成“发出去了”。"""
        from dsbot.llm import DeepSeekClient
        from dsbot.search import WebSearch as Search

        class Recorder:
            closed = False
            status = 200

            def __init__(self):
                self.timeouts = []

            def post(self, url, **kw):
                self.timeouts.append(kw["timeout"])
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def json(self, content_type=None):
                return {}

        for total in (2.0, 6.0, 16.0, 90.0):
            with self.subTest(total=total):
                rec = Recorder()
                client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m", timeout=total)
                client._session = rec
                await client._post({}, total)
                search = Search(api_key="sk-test", url="https://api.example", model="m", timeout=total)
                search._session = rec
                await search._post("https://api.example", {}, {})
                for limits in rec.timeouts:
                    self.assertLessEqual(limits.connect, limits.total / 2)

    @unittest.skipIf(aiohttp is None, "没装 aiohttp")
    async def test_non_utf8_200_is_still_a_response(self):
        """200 但正文不是 UTF-8：照样当成拿到了响应（算钱、不重试），和别的读不懂的 200 一样。"""
        from dsbot.llm import DeepSeekClient

        class Garbled:
            closed = False
            status = 200

            def post(self, url, **kw):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def json(self, content_type=None):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

            async def text(self, errors="strict"):
                if errors == "strict":
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
                return "�"

        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m")
        client._session = Garbled()
        self.assertEqual(await client._post({}, 30), (200, "�"))

    async def test_200_with_a_bad_message_is_billed_and_marked(self):
        """200 但 message 不是对象：也是“响应格式不对”（LLMError，状态 200，带着用量），整理记忆会当成模型那边的问题。"""
        from dsbot.llm import DeepSeekClient

        usage = {"prompt_tokens": 900, "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 100, "completion_tokens": 50}
        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m", max_retries=2)
        for body in ({"choices": [{"message": "oops"}], "usage": usage}, {"choices": [{"message": {"content": 5}}], "usage": usage}, {"usage": usage}):
            with self.subTest(body=body):

                async def post(payload, timeout, body=body):
                    return 200, body

                client._post = post
                with self.assertRaises(LLMError) as ctx:
                    await client.complete([{"role": "user", "content": "hi"}], max_tokens=100)
                self.assertEqual((ctx.exception.status, ctx.exception.usage), (200, Usage(prompt_tokens=900, hit=800, miss=100, output=50)))

    async def test_network_error_then_timeout_is_estimated_once(self):
        from dsbot.llm import DeepSeekClient

        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m", max_retries=2)
        outcomes = [OSError("connection reset"), asyncio.TimeoutError()]
        calls = []

        async def post(payload, timeout):
            calls.append(timeout)
            raise outcomes.pop(0)

        slept = []

        async def no_sleep(seconds):
            slept.append(seconds)

        client._post = post
        with mock.patch("dsbot.llm.asyncio.sleep", no_sleep):
            with self.assertRaises(LLMError) as ctx:
                await client.complete(self.MESSAGES, max_tokens=2000)
        self.assertEqual((len(calls), len(slept), ctx.exception.timeout), (2, 1, True))  # 连不上的重试了，超时的不再重试
        self.assertEqual(getattr(ctx.exception, "usage", None), self._want())  # 只估超时的那一次（连不上的没算钱）


# ---------------------------------------------------------------------------
# engine：聊天的调用失败
# ---------------------------------------------------------------------------


class EngineBilling(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_billed_by_estimate(self):
        guess = Usage(prompt_tokens=30000, hit=28000, miss=2000, output=1000)
        engine, llm, transport, clock = make_engine(lambda call: billed(usage=guess))
        with self.assertLogs("dsbot.engine", "INFO") as logs:
            await engine.handle_message(msg(501, "@小D", mentions_bot=True))
            await engine.drain()
        self.assertTrue(any("按估算记了" in line for line in logs.output))
        cost = engine.pricing.cost(guess, clock())
        self.assertGreater(cost, 0)
        got = rows(engine)
        self.assertEqual([(r["kind"], r["outcome"], r["hit"], r["miss"], r["output"]) for r in got], [("mention", "error", 28000, 2000, 1000)])
        self.assertAlmostEqual(got[0]["cost"], cost)
        self.assertEqual(got[0]["day"], engine.ledger.day(clock()))
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), cost)
        self.assertAlmostEqual(engine.ledger.total_spent(clock()), cost)
        self.assertAlmostEqual(Ledger(engine.store, engine.tz).guild_spent(GUILD, clock()), cost)  # 重启后从数据库读也一样
        stats = engine.store.day_stats(engine.ledger.day(clock()), guild_id=GUILD)
        self.assertEqual((stats["outcomes"], stats["hit_rate"], stats["calls"]), ({"error": 1}, None, 1))  # 估出来的不算命中率
        self.assertAlmostEqual(stats["cost"], cost)
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "⚠️")])
        self.assertEqual(len(engine.states[CHANNEL].pending), 1)  # 消息照样留着下一轮
        self.assertTrue(engine.states[CHANNEL].last_decision.startswith("出错：请求超时"))

    async def test_cut_off_is_billed_too(self):
        guess = Usage(prompt_tokens=8000, hit=6000, miss=2000, output=1000)
        engine, llm, transport, clock = make_engine(lambda call: billed("响应传到一半连接断了：payload", guess, timeout=False))
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@小D", mentions_bot=True))
            await engine.drain()
        got = rows(engine)
        self.assertEqual([(r["outcome"], r["hit"], r["miss"], r["output"]) for r in got], [("error", 6000, 2000, 1000)])
        self.assertAlmostEqual(got[0]["cost"], engine.pricing.cost(guess, clock()))
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), got[0]["cost"])
        self.assertGreater(engine.states[CHANNEL].error_until, clock())  # 其余照旧：和连不上一样冷却一会儿

    async def test_estimated_spend_counts_toward_the_budget(self):
        guess = Usage(prompt_tokens=10000, miss=10000, output=1000)  # 非高峰 $0.0021：超过预算 0.001 的 2 倍
        outputs = [billed(usage=guess), ACTIONS(REPLY(2, "在"))]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1], defaults={"daily_budget_usd": 0.001})
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@小D", mentions_bot=True))
            await engine.drain()
        self.assertGreater(engine.ledger.guild_spent(GUILD, clock()), 0.002)
        clock.advance(60)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(502, "@小D 在吗", mentions_bot=True))
            await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # 没有这次修复的话会回第二次
        self.assertEqual(transport.sent, [])
        self.assertIn((CHANNEL, 502, "⚠️"), transport.reactions)

    async def test_empty_reply_retry_that_times_out_is_charged_once(self):
        guess = Usage(prompt_tokens=5000, hit=4000, miss=1000, output=1000)
        outputs = ["", billed(usage=guess)]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@小D", mentions_bot=True))
            await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        got = rows(engine)
        self.assertEqual([r["outcome"] for r in got], ["empty", "error"])  # 第一次有自己的一笔，重试超时只多一笔
        self.assertGreater(got[0]["cost"], 0)
        self.assertAlmostEqual(got[1]["cost"], engine.pricing.cost(guess, clock()))
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), got[0]["cost"] + got[1]["cost"])

    async def test_timed_out_tool_step_is_charged_once(self):
        guess = Usage(prompt_tokens=6000, hit=5000, miss=1000, output=1000)
        outputs = iter([ACTIONS({"type": "search", "query": "明天天气"}), billed(usage=guess)])
        engine, llm, transport, clock = make_engine(lambda call: next(outputs), searcher=FakeSearcher())
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@小D 明天天气怎样", mentions_bot=True))
            await engine.drain()
        got = rows(engine)
        errors = [r for r in got if r["outcome"] == "error"]
        self.assertEqual([(r["kind"], r["outcome"]) for r in errors], [("mention", "error")])  # 只记一笔
        self.assertAlmostEqual(errors[0]["cost"], engine.pricing.cost(guess, clock()))
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), sum(r["cost"] for r in got))
        self.assertEqual(engine.states[CHANNEL].conversation.turns[2].content, TOOL_FAILED)
        self.assertIn((CHANNEL, 501, "⚠️"), transport.reactions)

    async def test_server_error_costs_nothing(self):
        engine, llm, transport, clock = make_engine(lambda call: LLMError("Server Error", status=500))
        with self.assertLogs("dsbot.engine", "WARNING") as logs:
            await engine.handle_message(msg(501, "@小D", mentions_bot=True))
            await engine.drain()
        self.assertFalse(any("按估算" in line for line in logs.output))
        got = rows(engine)
        self.assertEqual([(r["outcome"], r["hit"], r["miss"], r["output"], r["cost"]) for r in got], [("error", 0, 0, 0, 0.0)])
        self.assertEqual(engine.ledger.guild_spent(GUILD, clock()), 0.0)
        self.assertNotIn(GUILD, engine.ledger.all_guilds(clock()))  # 没算钱的失败照旧不进账本

    async def test_estimate_is_priced_and_dated_when_the_call_ends(self):
        guess = Usage(prompt_tokens=30000, hit=28000, miss=2000, output=1000)
        engine, llm, transport, clock = make_engine(lambda call: billed(usage=guess))
        start_day = engine.ledger.day(clock())

        async def slow(call):
            clock.advance(14 * 3600)  # 等着等着过了午夜（台北时间），还进了高峰时段

        llm.hook = slow
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@小D", mentions_bot=True))
            await engine.drain()
        now = clock()
        day = engine.ledger.day(now)
        self.assertNotEqual(day, start_day)
        self.assertTrue(engine.pricing.is_peak(now))
        cost = engine.pricing.cost(guess, now)
        got = rows(engine)
        self.assertEqual((len(got), got[0]["day"]), (1, day))  # 记在花钱的那一天
        self.assertAlmostEqual(got[0]["cost"], cost)  # 按那时的价格（高峰）
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, now), cost)  # 跨天重读账本时没算两遍
        self.assertAlmostEqual(Ledger(engine.store, engine.tz).guild_spent(GUILD, now), cost)

    async def test_real_client_estimate_reaches_the_row(self):
        from dsbot.llm import DeepSeekClient

        engine, llm, transport, clock = make_engine()
        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m", max_retries=0)
        ok = {
            "choices": [{"message": {"content": ACTIONS(REPLY(1, "早"))}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100, "completion_tokens": 30},
        }
        sent = []

        async def post(payload, timeout):
            sent.append(copy.deepcopy(payload))
            if len(sent) == 1:
                return 200, ok
            raise asyncio.TimeoutError

        client._post = post
        engine.llm = client
        await engine.handle_message(msg(501, "@小D 早", mentions_bot=True))
        await engine.drain()
        clock.advance(30)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(502, "@小D 在吗", mentions_bot=True))
            await engine.drain()
        messages = sent[1]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        hit = sum(content_tokens(m["content"]) for m in messages[:2])  # system + 上一轮的消息：上次发过的前缀
        miss = sum(content_tokens(m["content"]) for m in messages[2:])  # 上一次的回复 + 这次新加的
        got = rows(engine)
        self.assertEqual([r["outcome"] for r in got], ["reply", "error"])
        self.assertEqual((got[1]["hit"], got[1]["miss"], got[1]["output"]), (hit, miss, sent[1]["max_tokens"] // 2))
        self.assertGreater(got[1]["cost"], 0)
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), got[0]["cost"] + got[1]["cost"])

    async def test_card_quote_retry_that_times_out_is_billed(self):
        card = "自称猫娘奶糖，每天早上都要喝一杯热牛奶才肯起床"
        cards, memory = stores()
        cards.put(GUILD, 11, "阿明", card, via="command")
        guess = Usage(prompt_tokens=5000, hit=4000, miss=1000, output=1000)
        engine, llm, transport, clock = make_engine(chat([ACTIONS(REPLY(1, card)), billed(usage=guess)]), cards=cards, memory=memory)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@小D 早", mentions_bot=True))
            await engine.drain()
        self.assertEqual(len(llm.calls), 2)  # 照念了卡：让它重说，重说的时候超时
        self.assertEqual(transport.sent, [])
        self.assertIn((CHANNEL, 501, "🤐"), transport.reactions)
        self.assertNotIn((CHANNEL, 501, "⚠️"), transport.reactions)  # 不走 _on_error
        got = rows(engine)
        errors = [r for r in got if r["outcome"] == "error"]
        self.assertEqual([(r["kind"], r["hit"], r["miss"], r["output"]) for r in errors], [("mention", 4000, 1000, 1000)])
        self.assertAlmostEqual(errors[0]["cost"], engine.pricing.cost(guess, clock()))
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), sum(r["cost"] for r in got))


# ---------------------------------------------------------------------------
# engine：压缩摘要、整理记忆
# ---------------------------------------------------------------------------


class Background(unittest.IsolatedAsyncioTestCase):
    async def test_summary_billed_only_when_there_is_an_estimate(self):
        guess = Usage(prompt_tokens=6000, miss=6000, output=1000)
        cases = (
            ("timeout", billed(usage=guess), True),
            ("cut off", billed("响应传到一半连接断了：payload", guess, timeout=False), True),
            ("5xx", LLMError("Server Error", status=500), False),
            ("timeout without estimate", LLMError("请求超时（90 秒）", timeout=True), False),
        )
        for name, error, charged in cases:
            with self.subTest(name):

                def responder(call, error=error):
                    return error if not call["json_mode"] else ACTIONS(REPLY(None, "嗯嗯"))

                engine, llm, transport, clock = make_engine(responder, defaults={"context_budget_tokens": 4000})
                llm.prompt_tokens = 5000
                with self.assertLogs("dsbot.engine", "WARNING"):
                    for i in range(14):
                        await engine.handle_message(msg(501 + i, f"@小D 第{i}轮", mentions_bot=True))
                        await engine.drain()
                        clock.advance(30)
                tries = [c for c in llm.calls if not c["json_mode"]]
                self.assertGreaterEqual(len(tries), 1)
                self.assertFalse(engine.states[CHANNEL].conversation.summary)  # 没压成
                got = rows(engine, "kind = 'summary'")
                if charged:
                    self.assertEqual([(r["outcome"], r["miss"], r["output"]) for r in got], [("error", 6000, 1000)] * len(tries))
                    for r in got:
                        self.assertAlmostEqual(r["cost"], engine.pricing.cost(guess, clock()))  # 一直是非高峰
                else:
                    self.assertEqual(got, [])  # 没算钱的失败照旧不记
                day = engine.ledger.day(clock())
                self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), engine.store.spend_by_guild(day)[GUILD])
                self.assertEqual(engine.store.day_stats(day)["outcomes"].get("error", 0), len(tries) if charged else 0)

    async def test_review_timeout_is_billed(self):
        cards, memory = stores()
        guess = Usage(prompt_tokens=8000, miss=8000, output=3000)
        engine, llm, transport, clock = make_engine(chat([ACTIONS()], review=billed("请求超时（180 秒）", guess)), cards=cards, memory=memory)
        await three_rounds(engine, clock)
        clock.advance(601)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.tick()
            await engine.drain()
        self.assertEqual(len([c for c in llm.calls if is_review(c)]), 1)
        got = rows(engine, "kind = 'review'")
        self.assertEqual([(r["outcome"], r["miss"], r["output"]) for r in got], [("error", 8000, 3000)])
        self.assertAlmostEqual(got[0]["cost"], engine.pricing.cost(guess, clock()))
        day = engine.ledger.day(clock())
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), engine.store.spend_by_guild(day)[GUILD])
        self.assertEqual(engine.states[CHANNEL].review_bad, 1)  # 超时照旧算“模型那边的问题”

    async def test_review_cut_off_is_billed_and_still_retried(self):
        cards, memory = stores()
        guess = Usage(prompt_tokens=8000, miss=8000, output=3000)
        answers = [billed("响应传到一半连接断了：payload", guess, timeout=False), '{"cards": [], "memory": []}']

        def review(call):
            return answers.pop(0) if len(answers) > 1 else answers[0]

        engine, llm, transport, clock = make_engine(chat([ACTIONS()], review=review), cards=cards, memory=memory)
        await three_rounds(engine, clock)
        clock.advance(601)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.tick()
            await engine.drain()
        st = engine.states[CHANNEL]
        self.assertEqual((st.review_failures, st.review_bad, st.conversation.reviewed_upto), (1, 0, 0))  # 传到一半断了：等会儿再试
        clock.advance(1201)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len([c for c in llm.calls if is_review(c)]), 2)
        got = rows(engine, "kind = 'review'")
        self.assertEqual([r["outcome"] for r in got], ["error", "review"])  # 每次请求各记各的
        self.assertAlmostEqual(got[0]["cost"], engine.pricing.cost(guess, clock()))
        self.assertEqual(st.conversation.reviewed_upto, st.conversation.turns[-1].id)
        day = engine.ledger.day(clock())
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), engine.store.spend_by_guild(day)[GUILD])


# ---------------------------------------------------------------------------
# search：联网搜索超时
# ---------------------------------------------------------------------------


class SearchBilling(unittest.IsolatedAsyncioTestCase):
    def test_estimate_search(self):
        from dsbot.search import SEARCH_USE_TOKENS, estimate_search

        payload = {
            "model": "deepseek-flash",
            "max_tokens": 1500,
            "system": "你是一个搜索助手。今天是 2026-10-07",
            "messages": [{"role": "user", "content": "台北 明天 天气"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        }
        u = estimate_search(payload, 3)
        self.assertEqual((u.hit, u.prompt_tokens, u.output, u.reasoning), (0, u.miss, 750, 0))  # 全按没命中；输出按上限的一半
        none = estimate_search(payload, 0)
        self.assertEqual(u.miss - none.miss, 3 * SEARCH_USE_TOKENS)  # 每搜一次塞进去的网页
        self.assertGreaterEqual(none.miss, estimate_tokens(payload["system"]) + estimate_tokens("台北 明天 天气"))
        self.assertEqual(estimate_search(payload, -2), none)
        self.assertEqual(estimate_search({**payload, "metadata": {"user_id": "guild-1"}, "tools": []}, 3), u)  # 只看发出去的内容
        resent = {**payload, "messages": [*payload["messages"], {"role": "assistant", "content": [{"type": "text", "text": "搜到了" * 200}]}]}
        self.assertGreater(estimate_search(resent, 3).miss, u.miss + 300)  # pause_turn 之后发回去的也算
        self.assertEqual(estimate_search({"messages": []}, 1).output, 0)

    @unittest.skipIf(aiohttp is None, "没装 aiohttp")
    async def test_post_flags_what_may_be_billed(self):
        client = WebSearch(api_key="sk-test", url="https://api.example", model="m", timeout=60)
        not_connected = getattr(aiohttp, "ConnectionTimeoutError", aiohttp.ServerTimeoutError)
        cases = (
            (asyncio.TimeoutError(), "enter", 200, True, "搜索超时（60 秒）"),
            (aiohttp.ClientPayloadError("Response payload is not completed"), "read", 200, True, "传到一半"),
            (aiohttp.ClientPayloadError("Response payload is not completed"), "read", 503, False, "传到一半"),  # 错误响应
            (asyncio.TimeoutError(), "read", 429, False, "搜索超时"),
            (not_connected("connect"), "enter", 200, False, "连不上"),  # 连都没连上：请求没发出去
            (aiohttp.ClientConnectionError("refused"), "enter", 200, False, "网络错误"),
            (OSError("unreachable"), "enter", 200, False, "网络错误"),
        )
        for exc, at, status, maybe, text in cases:
            with self.subTest(f"{type(exc).__name__} {status}"):
                client._session = _BrokenSession(exc, at, status)
                with self.assertRaises(SearchError) as ctx:
                    await client._post("https://api.example", {}, {})
                self.assertIs(getattr(ctx.exception, "maybe_billed", False), maybe)
                self.assertIn(text, str(ctx.exception))
                self.assertIsNone(ctx.exception.usage)  # _post 只做标记，不估（Tavily 也走这里）
        self.assertFalse(getattr(SearchError("x", Usage(miss=1)), "maybe_billed", False))  # 原来的写法照样能用

    async def test_timeout_is_billed_by_estimate(self):
        client, sent = search_client([search_timeout()])
        with self.assertLogs("dsbot.search", "WARNING"):
            with self.assertRaises(SearchError) as ctx:
                await client.search("q", today="d", max_chars=500)
        self.assertIn("搜索超时", str(ctx.exception))
        got = ctx.exception.usage
        self.assertGreater(got.miss, 0)  # 超时也记一笔

        from dsbot.search import SEARCH_USE_TOKENS, estimate_search

        self.assertEqual(got, estimate_search(sent[0][1], 3))
        self.assertEqual((got.hit, got.output), (0, 750))
        self.assertGreaterEqual(got.miss, 3 * SEARCH_USE_TOKENS)

    async def test_unbilled_transport_error_has_no_estimate(self):
        client, sent = search_client([SearchError("网络错误：ClientOSError()")])
        with self.assertLogs("dsbot.search", "WARNING"):
            with self.assertRaises(SearchError) as ctx:
                await client.search("q", today="d", max_chars=500)
        self.assertEqual(ctx.exception.usage, Usage())

    async def test_pause_turn_usage_survives_a_later_error(self):
        paused = {"stop_reason": "pause_turn", "content": [{"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}}],
                  "usage": {"input_tokens": 4000, "output_tokens": 20}}
        first = Usage(prompt_tokens=4000, miss=4000, output=20)
        client, sent = search_client([(200, paused), SearchError("网络错误：ClientOSError()")])
        with self.assertLogs("dsbot.search", "WARNING"):
            with self.assertRaises(SearchError) as ctx:
                await client.search("q", today="d", max_chars=500)
        self.assertEqual(ctx.exception.usage, first)  # 前面那段已经算的钱不丢，也不多估
        client, sent = search_client([(200, paused), search_timeout()])
        with self.assertLogs("dsbot.search", "WARNING"):
            with self.assertRaises(SearchError) as ctx:
                await client.search("q", today="d", max_chars=500)
        self.assertEqual(sent[1][1]["messages"][-1], {"role": "assistant", "content": paused["content"]})

        from dsbot.search import estimate_search

        self.assertEqual(ctx.exception.usage, add_usage(first, estimate_search(sent[1][1], 3)))  # 估的是第二次发出去的（带着发回去的内容）

    async def test_timeout_then_tavily_keeps_the_estimate(self):
        tavily = {"results": [{"title": "标题", "url": "https://t.example", "content": "内容"}]}
        client, sent = search_client([search_timeout(), (200, tavily)], tavily_key="tvly-1")
        with self.assertLogs("dsbot.search", "WARNING"):
            result = await client.search("q", today="d", max_chars=500)
        self.assertEqual(result.provider, "Tavily")
        self.assertGreater(result.usage.miss, 0)  # DeepSeek 那边超时估的那笔也带上

        from dsbot.search import estimate_search

        self.assertEqual(result.usage, estimate_search(sent[0][1], 3))

    async def test_tavily_timeout_is_not_billed(self):
        client, sent = search_client([(401, {"error": {"message": "bad key"}}), search_timeout()], tavily_key="tvly-1")
        with self.assertLogs("dsbot.search", "WARNING"):
            with self.assertRaises(SearchError) as ctx:
                await client.search("q", today="d", max_chars=500)
        self.assertEqual(sent[1][0], "https://api.tavily.com/search")
        self.assertEqual(ctx.exception.usage, Usage())  # Tavily 的超时不是 DeepSeek 的钱

    async def test_search_timeout_reaches_the_ledger(self):
        searcher, sent = search_client([search_timeout()])
        outputs = [ACTIONS({"type": "search", "query": "明天天气"}), ACTIONS(REPLY(1, "没搜到"))]
        engine, llm, transport, clock = make_engine(chat(outputs), searcher=searcher)
        with self.assertLogs("dsbot", "WARNING"):
            await engine.handle_message(msg(501, "@小D 明天天气怎样", mentions_bot=True))
            await engine.drain()
        self.assertEqual(transport.sent[-1][1], "没搜到")
        got = rows(engine, "kind = 'search'")
        self.assertEqual([r["outcome"] for r in got], ["error"])
        self.assertGreater(got[0]["cost"], 0)
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), sum(r["cost"] for r in rows(engine)))

        from dsbot.search import estimate_search

        want = estimate_search(sent[0][1], searcher.max_uses)
        self.assertEqual((got[0]["hit"], got[0]["miss"], got[0]["output"]), (0, want.miss, want.output))
        self.assertAlmostEqual(got[0]["cost"], engine.pricing.cost(want, clock()))

    async def test_search_across_midnight_is_dated_when_it_ends(self):
        """搜的时候跨过了午夜：这笔记在搜完的那天，账本和数据库一致（重启后从库里读的预算也对得上）。"""
        guess = Usage(prompt_tokens=15000, miss=15000, output=750)
        box = {}

        class SlowSearcher(FakeSearcher):
            async def search(self, query, **kw):
                box["clock"].advance(90)  # 搜了 90 秒，跨过了台北的午夜
                raise SearchError("搜索超时（60 秒）", guess)

        outputs = [ACTIONS({"type": "search", "query": "明天天气"}), ACTIONS(REPLY(1, "没搜到"))]
        engine, llm, transport, clock = make_engine(chat(outputs), searcher=SlowSearcher())
        box["clock"] = clock
        clock.t += 4 * 3600 - 30  # T0 是台北 20:00：差 30 秒到午夜
        start_day = engine.ledger.day(clock())
        with self.assertLogs("dsbot", "WARNING"):
            await engine.handle_message(msg(501, "@小D 明天天气怎样", mentions_bot=True))
            await engine.drain()
        day = engine.ledger.day(clock())
        self.assertNotEqual(day, start_day)
        row = rows(engine, "kind = 'search'")[0]
        self.assertEqual(row["day"], day)
        self.assertAlmostEqual(row["cost"], engine.pricing.cost(guess, row["ts"]))
        self.assertAlmostEqual(engine.ledger.guild_spent(GUILD, clock()), engine.store.spend_by_guild(day)[GUILD])
        self.assertAlmostEqual(Ledger(engine.store, engine.tz).guild_spent(GUILD, clock()), engine.ledger.guild_spent(GUILD, clock()))


# ---------------------------------------------------------------------------
# store：/ds 状态 的统计
# ---------------------------------------------------------------------------


class Stats(unittest.TestCase):
    def test_hit_rate_ignores_error_rows(self):
        engine, *_ = make_engine()
        store = engine.store
        store.record_usage(ts=0, day="d", guild_id=1, channel_id=1, kind="mention", outcome="reply", hit=900, miss=100, cost=0.001)
        store.record_usage(ts=0, day="d", guild_id=1, channel_id=1, kind="mention", outcome="error", miss=20000, output=1000, cost=0.01)
        store.record_usage(ts=0, day="d", guild_id=1, channel_id=1, kind="proactive", outcome="error", hit=50000, cost=0.002)
        stats = store.day_stats("d")
        self.assertAlmostEqual(stats["hit_rate"], 0.9)  # 超时按估算记的用量不算进命中率
        self.assertEqual((stats["hit"], stats["miss"]), (900, 100))
        self.assertAlmostEqual(stats["cost"], 0.013)  # 花费照算
        self.assertEqual((stats["calls"], stats["output"]), (3, 1000))
        self.assertEqual(stats["outcomes"], {"reply": 1, "error": 2})
        store.record_usage(ts=0, day="e", guild_id=1, channel_id=1, kind="mention", outcome="error", hit=28000, miss=2000, cost=0.002)
        self.assertIsNone(store.day_stats("e")["hit_rate"])  # 只有出错的：没有命中率
        self.assertAlmostEqual(store.spend_by_guild("e")[1], 0.002)


if __name__ == "__main__":
    unittest.main()
