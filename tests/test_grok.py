"""Grok 版（xAI）和 DeepSeek 版不一样的部分：xAI 客户端、价格、默认值、命令名，以及和别的 bot 待在同一个频道。"""

import json
import unittest
from pathlib import Path
from unittest import mock

from helpers import CHANNEL, FakeSearcher, make_engine, msg

from dsbot.config import BUILTIN_DEFAULTS, SPECS, XAIConfig, load_config, parse_value
from dsbot.llm import (
    LLMError,
    XAIClient,
    billed_output,
    build_payload,
    classify_status,
    effort_for,
    error_message,
    parse_completion,
)
from dsbot.models import SearchResult, Usage
from dsbot.pricing import Pricing
from dsbot.prompts import DEFAULT_PERSONA, SYSTEM_RULES

ROOT = Path(__file__).resolve().parent.parent
REPLY = lambda to, *texts: json.dumps({"actions": [{"type": "reply", "to": to, "messages": list(texts)}]}, ensure_ascii=False)  # noqa: E731
SEARCH = lambda q: json.dumps({"actions": [{"type": "search", "query": q}]}, ensure_ascii=False)  # noqa: E731
OK_BODY = {
    "choices": [{"message": {"content": '{"actions": []}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
}


# ---------------------------------------------------------------------------
# 请求参数
# ---------------------------------------------------------------------------


class Payload(unittest.TestCase):
    def test_bot_calls(self):
        p = build_payload("grok-4.3", [], thinking="off", json_mode=True, max_tokens=100, user_id="guild-1")
        self.assertEqual(p["reasoning_effort"], "none")  # grok-4.3 能完全不推理
        self.assertEqual(p["response_format"], {"type": "json_object"})
        self.assertEqual((p["max_completion_tokens"], p["user"]), (100, "guild-1"))
        for deepseek_only in ("thinking", "max_tokens", "user_id"):
            self.assertNotIn(deepseek_only, p)
        p = build_payload("grok-4.3", [], thinking="medium")
        self.assertEqual(p["reasoning_effort"], "medium")
        self.assertNotIn("response_format", p)

    def test_models_that_always_reason(self):
        for model in ("grok-4.5", "grok-4.6", "grok-4.7", "grok-4.7-latest"):
            self.assertEqual(effort_for(model, "off"), "low")  # 关不掉：退到最低
            self.assertEqual(effort_for(model, "high"), "high")
        self.assertEqual(effort_for("grok-4.3", "off"), "none")
        self.assertNotIn("reasoning_effort", build_payload("grok-4.3", [], effort=""))  # 模型不收这个参数时不发

    def test_thinking_settings_accept_medium(self):
        self.assertEqual(parse_value(SPECS["mention_thinking"], "Medium"), "medium")
        with self.assertRaises(ValueError):
            parse_value(SPECS["mention_thinking"], "xhigh")


# ---------------------------------------------------------------------------
# 用量和花费
# ---------------------------------------------------------------------------


class UsageAndCost(unittest.TestCase):
    def test_reasoning_is_billed_as_output(self):
        # xAI 的 completion_tokens 不含推理：total = prompt + completion + reasoning
        c = parse_completion(
            {
                "choices": [{"message": {"content": "{}", "reasoning_content": "嗯"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1170,
                    "prompt_tokens_details": {"cached_tokens": 900},
                    "completion_tokens_details": {"reasoning_tokens": 120},
                    "cost_in_usd_ticks": 4_500_000,
                },
            }
        )
        self.assertEqual((c.content, c.reasoning, c.finish_reason), ("{}", "嗯", "stop"))
        self.assertEqual(c.usage, Usage(1000, 900, 100, 170, 120, cost_usd=0.00045))

    def test_completion_that_already_includes_reasoning_is_not_counted_twice(self):
        self.assertEqual(billed_output(1000, 150, 120, 1150), 150)
        self.assertEqual(billed_output(1000, 30, 120, 1150), 150)
        self.assertEqual(billed_output(1000, 30, 120, None), 150)  # 没有 total：照 xAI 文档的例子算
        self.assertEqual(billed_output(1000, 30, 0, 1030), 30)

    def test_minimal_usage(self):
        c = parse_completion({"choices": [{"message": {"content": None}}], "usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}})
        self.assertEqual((c.content, c.usage.hit, c.usage.miss, c.usage.cost_usd), ("", 10, 0, None))
        c = parse_completion({"choices": [{"message": {"content": "x"}}], "usage": {"cost_in_usd_ticks": True}})
        self.assertIsNone(c.usage.cost_usd)  # 不是数字的不认

    def test_price_uses_what_xai_charged(self):
        p = Pricing()
        self.assertEqual(p.cost(Usage(prompt_tokens=10_000, hit=9_000, miss=1_000, output=200, cost_usd=0.0123)), 0.0123)

    def test_price_estimate(self):
        p = Pricing()
        usage = Usage(prompt_tokens=30000, hit=28500, miss=1500, output=300)
        self.assertAlmostEqual(p.cost(usage), (28500 * 0.20 + 1500 * 1.25 + 300 * 2.50) / 1e6)
        self.assertAlmostEqual(p.cost(usage, 1_791_374_400.0), p.cost(usage))  # 没有高峰、非高峰
        big = Usage(prompt_tokens=200_000, hit=0, miss=200_000, output=100)
        self.assertAlmostEqual(p.cost(big), 2 * (200_000 * 1.25 + 100 * 2.5) / 1e6)  # 长上下文整次翻倍
        searched = Usage(prompt_tokens=1000, miss=1000, web_searches=3)
        self.assertAlmostEqual(p.cost(searched), 1000 * 1.25 / 1e6 + 3 * 0.005)

    def test_shipped_pricing(self):
        cfg = load_config(ROOT / "config.toml")
        self.assertEqual(cfg.pricing, Pricing())
        self.assertEqual(cfg.unknown, [])


# ---------------------------------------------------------------------------
# 出错
# ---------------------------------------------------------------------------


class Errors(unittest.TestCase):
    def test_error_shapes(self):
        self.assertEqual(error_message({"code": "Some code", "error": "Your team has no credits"}), "Your team has no credits")
        self.assertEqual(error_message({"error": {"message": "bad"}}), "bad")
        self.assertEqual(error_message("plain text"), "plain text")

    def test_classify(self):
        no_credits = "Your newly created teams doesn't have any credits yet. You can purchase credits on https://console.x.ai/team/x"
        self.assertEqual(classify_status(403, no_credits), 402)
        self.assertEqual(classify_status(429, "Your team has reached its monthly spending limit"), 402)
        self.assertEqual(classify_status(429, "Too many requests, slow down"), 429)  # 普通限流：过一会儿重试
        self.assertEqual(classify_status(400, "Incorrect API key provided: xa***yz"), 401)
        self.assertEqual(classify_status(401, "invalid token"), 401)
        self.assertEqual(classify_status(404, "The model grok-9 does not exist or your team does not have access to it"), 404)
        self.assertEqual(classify_status(400, "This model's maximum prompt length is 1000000"), 400)


class Client(unittest.IsolatedAsyncioTestCase):
    def _client(self, responses, model="grok-4.3", max_retries=2):
        client = XAIClient(api_key="xai-test", base_url="https://api.x.ai/v1/", model=model, max_retries=max_retries)
        sent = []

        async def fake_post(payload, headers, timeout):
            sent.append((json.loads(json.dumps(payload)), dict(headers), timeout))
            return responses.pop(0)

        client._post = fake_post
        return client, sent

    async def test_request_and_cache_routing(self):
        client, sent = self._client([(200, OK_BODY)])
        self.assertEqual(client.url, "https://api.x.ai/v1/chat/completions")
        c = await client.complete([{"role": "user", "content": "hi"}], json_mode=True, max_tokens=50, user_id="guild-9")
        self.assertEqual(c.content, '{"actions": []}')
        payload, headers, timeout = sent[0]
        self.assertEqual(headers, {"x-grok-conv-id": "guild-9"})  # 同一个服务器送到同一台机器，缓存才命中
        self.assertEqual((payload["reasoning_effort"], payload["max_completion_tokens"], timeout), ("none", 50, 90.0))

    async def test_model_that_rejects_none_falls_back_to_low(self):
        rejected = (422, {"code": "Invalid argument", "error": "Model grok-4.20 does not support reasoning_effort `none`"})
        client, sent = self._client([rejected, (200, OK_BODY), (200, OK_BODY)])
        await client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual([p["reasoning_effort"] for p, _, _ in sent], ["none", "low"])
        await client.complete([{"role": "user", "content": "again"}])
        self.assertEqual(sent[-1][0]["reasoning_effort"], "low")  # 记住了，不再先试一次 none
        self.assertTrue(client.notes)

    async def test_model_without_reasoning_effort(self):
        rejected = (400, {"error": "Argument not supported on this model: reasoning_effort"})
        client, sent = self._client([rejected, (200, OK_BODY)])
        client.no_none = True
        await client.complete([{"role": "user", "content": "hi"}], thinking="high")
        self.assertNotIn("reasoning_effort", sent[-1][0])
        self.assertTrue(client.no_effort)

    async def test_json_mode_rejected(self):
        client, sent = self._client([(400, {"error": "response_format json_object is not supported"}), (200, OK_BODY)])
        await client.complete([{"role": "user", "content": "hi"}], json_mode=True)
        self.assertNotIn("response_format", sent[-1][0])
        self.assertTrue(client.no_json)

    async def test_unrelated_bad_request_is_not_retried(self):
        client, sent = self._client([(400, {"error": "Failed to deserialize the JSON body: messages[3]: invalid type"})])
        with self.assertRaises(LLMError) as ctx:
            await client.complete([{"role": "user", "content": "hi"}], json_mode=True)
        self.assertEqual((ctx.exception.status, len(sent)), (400, 1))
        self.assertFalse(client.no_json)  # 和 JSON 模式无关的错误不会把它关掉

    async def test_no_credits_pauses_without_retrying(self):
        client, sent = self._client([(403, {"code": "Forbidden", "error": "Your team doesn't have any credits yet."})])
        with self.assertRaises(LLMError) as ctx:
            await client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual((ctx.exception.status, len(sent)), (402, 1))
        self.assertIn("HTTP 403", str(ctx.exception))

    async def test_rate_limit_is_retried(self):
        client, sent = self._client([(429, {"error": "Too many requests"}), (503, "busy"), (200, OK_BODY)])
        with mock.patch("dsbot.llm.asyncio.sleep", new=mock.AsyncMock()) as slept:
            c = await client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual((c.content, len(sent), slept.await_count), ('{"actions": []}', 3, 2))


# ---------------------------------------------------------------------------
# 默认值、配置、命令名
# ---------------------------------------------------------------------------


class Defaults(unittest.TestCase):
    def test_model_and_endpoints(self):
        cfg = XAIConfig()
        self.assertEqual((cfg.model, cfg.responses_url, cfg.search_max_uses), ("grok-4.3", "https://api.x.ai/v1/responses", 2))

    def test_judges_later_than_the_deepseek_edition(self):
        # DeepSeek 版是 30 / 180：错开一点，两个 bot 在同一个频道时它能先看到对方说了什么
        self.assertEqual((BUILTIN_DEFAULTS["quiet_seconds"], BUILTIN_DEFAULTS["max_wait_seconds"]), (60, 240))
        cfg = load_config(ROOT / "config.toml")
        self.assertEqual((cfg.defaults["quiet_seconds"], cfg.defaults["max_wait_seconds"]), (60, 240))
        self.assertTrue(cfg.engine.yield_to_bots)

    def test_own_database_files(self):
        cfg = load_config(ROOT / "config.toml")
        files = {cfg.bot.database, cfg.bot.cards_database, cfg.bot.memory_database}
        self.assertEqual(files, {"data/xaibot.sqlite3", "data/xai-cards.sqlite3", "data/xai-memory.sqlite3"})
        # 和 DeepSeek 版的文件名都不一样：就算两个 bot 指到同一个 data 文件夹，数据也不会混
        self.assertFalse(files & {"data/dsbot.sqlite3", "data/cards.sqlite3", "data/memory.sqlite3"})

    def test_compose_names_differ_from_the_deepseek_edition(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("container_name: xaibot", compose)
        self.assertIn("name: xai-discord-bot", compose)
        self.assertNotIn("container_name: dsbot", compose)
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("XAI_API_KEY=", env)
        self.assertNotIn("DEEPSEEK_API_KEY", env)

    def test_prompt(self):
        self.assertNotIn("dsowner", SYSTEM_RULES)
        self.assertNotIn("/ds ", SYSTEM_RULES)
        self.assertNotIn("@小D", SYSTEM_RULES)
        self.assertIn("「〔xai-owner〕」表示发言的是 xai-owner", SYSTEM_RULES)
        self.assertIn("告诉他用 /xai-rp", SYSTEM_RULES)
        self.assertIn("群里可能还有别的机器人（标着〔bot〕）", SYSTEM_RULES)
        self.assertIn("你的知识大约只到 2025 年底", SYSTEM_RULES)
        self.assertIn("说话有点皮", DEFAULT_PERSONA)


class CommandNames(unittest.TestCase):
    def test_all_commands_start_with_xai(self):
        try:
            import discord
        except ImportError:
            self.skipTest("没装 discord.py")
        from dsbot.config import Secrets
        from dsbot.discord_app import DSBot
        from dsbot.ledger import Ledger
        from dsbot.memory import CardStore, MemoryStore
        from dsbot.settings import Settings
        from dsbot.store import Store

        cfg = load_config(ROOT / "config.toml")
        store, cards, memory = Store(":memory:"), CardStore(":memory:"), MemoryStore(":memory:")
        try:
            bot = DSBot(
                cfg,
                Secrets("x", "x", 1, control_guild_id=5),
                store,
                Settings(cfg.defaults, store),
                Ledger(store, cfg.bot.timezone),
                XAIClient(api_key="x", base_url=cfg.xai.base_url, model=cfg.xai.model),
                cards=cards,
                memory=memory,
            )
            public = bot.tree.get_commands()
            owner = bot.tree.get_commands(guild=discord.Object(id=5))
            self.assertEqual(sorted(c.name for c in public), ["xai", "xai-rp"])
            self.assertEqual([c.name for c in owner], ["xai-owner"])
            for command in public + owner:
                payload = command.to_dict(bot.tree)  # Discord 对名字、描述的限制都过得了
                self.assertLessEqual(len(payload["description"]), 100)
            by_name = {c.name: c for c in public + owner}
            self.assertIn("Grok", by_name["xai"].description)
            self.assertIn("Grok", by_name["xai-owner"].description)
        finally:
            for db in (store, cards, memory):
                db.close()


# ---------------------------------------------------------------------------
# 和别的 bot（比如 DeepSeek 版）待在同一个频道
# ---------------------------------------------------------------------------


def other_bot(message_id, text, **kw):
    return msg(message_id, text, author=99, name="DeepSeek群友", is_bot=True, **kw)


class SharingAChannel(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_pile_on_after_another_bot(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True})
        await engine.handle_message(msg(501, "有人知道这个怎么修吗"))
        clock.advance(5)
        await engine.handle_message(other_bot(502, "试试重启"))  # 别的 bot 先接了话
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])  # 不插嘴，也不花钱判断
        self.assertEqual(len(engine.states[CHANNEL].pending), 2)  # 都留着当上下文

        await engine.handle_message(msg(503, "重启也没用"))  # 有人接着说话了：照常判断
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        content = llm.calls[0]["messages"][-1]["content"]
        self.assertIn("DeepSeek群友 〔bot〕：试试重启", content)  # 看得到对方说了什么

    async def test_can_be_turned_off(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True}, engine_cfg={"yield_to_bots": False})
        await engine.handle_message(msg(501, "有人知道这个怎么修吗"))
        await engine.handle_message(other_bot(502, "试试重启"))
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)

    async def test_still_answers_when_called(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "在"), defaults={"proactive": True})
        await engine.handle_message(msg(501, "@小G 在吗", mentions_bot=True))
        await engine.handle_message(other_bot(502, "我在"))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(transport.sent[0][1], "在")

    async def test_other_bot_cannot_call_it_or_refill_quota(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True, "refill_turns": 1, "quota_capacity": 1})
        st = engine.state(CHANNEL, 1)
        st.bucket.tokens = 0.0
        for i in range(5):  # 别的 bot @ 它、回复它，说再多轮也不算
            await engine.handle_message(other_bot(510 + i, f"@小G 第{i}句", mentions_bot=True, reply_to_bot=True))
            clock.advance(30)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertEqual(st.bucket.tokens, 0.0)


class PausesOnAccountProblems(unittest.IsolatedAsyncioTestCase):
    async def _fail(self, status):
        engine, llm, transport, clock = make_engine(lambda call: LLMError(f"HTTP {status}：x", status=status))
        await engine.handle_message(msg(501, "@小G 在吗", mentions_bot=True))
        await engine.drain()
        return engine.paused_until - clock()

    async def test_pauses(self):
        self.assertEqual(await self._fail(402), 600)  # 没钱了：10 分钟后再试
        self.assertEqual(await self._fail(401), 3600)  # key 不对
        self.assertEqual(await self._fail(404), 3600)  # 模型名不对 / 没权限：每次都会一样失败
        self.assertEqual(await self._fail(403), 3600)
        self.assertLessEqual(await self._fail(500), 0)  # 对方临时出问题：不全局暂停


class SearchCost(unittest.IsolatedAsyncioTestCase):
    async def test_what_xai_charged_goes_into_the_budget(self):
        result = SearchResult(summary="结果", sources=[], usage=Usage(prompt_tokens=8000, miss=8000, output=300, cost_usd=0.021, web_searches=2), provider="xAI")
        replies = iter([SEARCH("台北 天气"), REPLY(1, "晴天")])
        engine, llm, transport, clock = make_engine(lambda call: next(replies), searcher=FakeSearcher({"台北 天气": result}))
        await engine.handle_message(msg(501, "@小G 台北今天天气", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.sent[0][1], "晴天")
        row = engine.store.db.execute("SELECT cost FROM usage WHERE kind = 'search'").fetchone()
        self.assertAlmostEqual(row["cost"], 0.021)


if __name__ == "__main__":
    unittest.main()
