"""3.0.0 主动搜索：没人叫它、它自己想接话时，碰到不懂的可以先上网搜一下再说（以前只有被叫到时才能搜）。

- 主人的参数「主动搜索」，默认开；要开着联网搜索。关掉就和以前一样：没人叫它时写了 search 也不搜。
- 另外记数，不占被叫到时的每日搜索上限；「每日主动搜索上限」默认 0 = 不限。花费照样算进预算，超预算就不搜。
- 一轮只搜一次；只开搜索，打开链接、翻别的频道这些还是只在被叫到时用。
- 冒泡自己会搜，不走这条；私信里每句都算叫它，不受影响。"""

from __future__ import annotations

import json
import unittest

from helpers import CHANNEL, GUILD, FakeSearcher, make_engine, msg
from test_proactive import PASS, Case, Shortest, text

from dsbot.config import SPECS, load_config
from dsbot.engine import AUTO_SEARCH
from dsbot.models import ChannelPlace
from dsbot.prompts import build_system_prompt

DM = 5000


def acts(*items):
    return json.dumps({"actions": list(items)}, ensure_ascii=False)


def search(query="星穹铁道 3.8 版本 新角色"):
    return {"type": "search", "query": query}


def say(words, to=None):
    return {"type": "reply", "to": to, "messages": [words]}


def setup(answers, **defaults):
    left = list(answers)
    searcher = FakeSearcher()
    engine, llm, transport, clock = make_engine(
        lambda call: left.pop(0) if left else PASS, defaults={"proactive": True, **defaults}, searcher=searcher
    )
    engine.rng = Shortest()
    return engine, llm, transport, clock, searcher


def count(engine, kind):
    return engine.store.count_usage(engine.ledger.day(engine.clock()), guild_id=GUILD, kind=kind)


class Unasked(Case):
    async def chat(self, engine, clock, mid=501, words="3.8 那个新角色强不强啊"):
        await self.say(engine, mid, words)
        await self.look(engine, clock, 31)

    async def test_searches_then_speaks(self):
        engine, llm, transport, clock, searcher = setup([acts(search()), acts(say("搜了一下，新角色是火属性的输出"))])
        await self.chat(engine, clock)
        self.assertEqual(searcher.calls, ["星穹铁道 3.8 版本 新角色"])
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("关于「星穹铁道 3.8 版本 新角色」搜到的内容", text(llm.calls[1]))  # 结果给它看了再说
        self.assertEqual([s[1] for s in transport.sent], ["搜了一下，新角色是火属性的输出"])
        self.assertEqual((count(engine, AUTO_SEARCH), count(engine, "search")), (1, 0))  # 另外记数

    async def test_may_stay_silent_after_searching(self):
        engine, llm, transport, clock, searcher = setup([acts(search()), PASS])
        await self.chat(engine, clock)
        self.assertEqual(len(searcher.calls), 1)
        self.assertEqual(transport.sent, [])

    async def test_off_means_no_search(self):
        engine, llm, transport, clock, searcher = setup([acts(search()), acts(say("不该有第二步"))], proactive_search=False)
        await self.chat(engine, clock)
        self.assertEqual(searcher.calls, [])
        self.assertEqual(len(llm.calls), 1)  # 写了 search 也当没写，不为它再问一次
        self.assertEqual(transport.sent, [])
        self.assertEqual(count(engine, AUTO_SEARCH), 0)

    async def test_needs_web_search_on(self):
        engine, llm, transport, clock, searcher = setup([acts(search())], web_search=False)
        await self.chat(engine, clock)
        self.assertEqual((searcher.calls, len(llm.calls)), ([], 1))

    async def test_one_search_per_round(self):
        engine, llm, transport, clock, searcher = setup([acts(search("甲"), search("乙")), PASS])
        await self.chat(engine, clock)
        self.assertEqual(searcher.calls, ["甲"])

    async def test_only_search_is_opened_up(self):
        engine, llm, transport, clock, searcher = setup(
            [acts({"type": "recall", "channel": "别的频道"}, {"type": "read", "to": 1}), acts(say("不该有第二步"))]
        )
        await self.chat(engine, clock)
        self.assertEqual((searcher.calls, len(llm.calls), transport.sent), ([], 1, []))

    async def test_no_cap_by_default(self):
        engine, llm, transport, clock, searcher = setup([], daily_search_limit=2)
        llm.responder = lambda call: acts(search()) if len(llm.calls) % 2 else PASS
        for i in range(5):
            clock.advance(10 * 60)  # 过了“下一眼”的时间
            await self.say(engine, 600 + i, "又一个不懂的", author=12 + i)
            await self.look(engine, clock, 31)
        self.assertEqual(len(searcher.calls), 5)  # 默认不限，也不受“每日搜索上限”（2 次）管
        self.assertEqual((count(engine, AUTO_SEARCH), count(engine, "search")), (5, 0))

    async def test_does_not_use_up_the_asked_quota(self):
        engine, llm, transport, clock, searcher = setup([acts(search()), PASS, acts(search("明天天气")), acts(say("晴", 2))], daily_search_limit=1)
        await self.chat(engine, clock)
        self.assertEqual(count(engine, AUTO_SEARCH), 1)
        clock.advance(60)
        await engine.handle_message(msg(502, "@小D 明天天气怎样", mentions_bot=True))
        await engine.drain()
        self.assertEqual(searcher.calls[-1], "明天天气")  # 被叫到时的那 1 次还在
        self.assertEqual(count(engine, "search"), 1)

    async def test_own_cap(self):
        engine, llm, transport, clock, searcher = setup([], daily_proactive_search_limit=2)
        llm.responder = lambda call: acts(search())
        for i in range(4):
            clock.advance(10 * 60)  # 过了“下一眼”的时间
            await self.say(engine, 600 + i, "又一个不懂的", author=12 + i)
            await self.look(engine, clock, 31)
        self.assertEqual(len(searcher.calls), 2)  # 到了上限：写了 search 也不搜
        self.assertEqual(len(llm.calls), 6)  # 前两轮各问两次（搜完再问），到了上限的两轮只问一次：不为搜不了的再问
        self.assertEqual(count(engine, AUTO_SEARCH), 2)

    async def test_over_budget_means_no_search(self):
        engine, llm, transport, clock, searcher = setup([PASS])
        await self.chat(engine, clock)
        st, view = engine.states[CHANNEL], engine.settings.view(GUILD, CHANNEL)
        self.assertTrue(engine._auto_search_ready(st, view, clock()))
        engine._over_budget = lambda st, view, now: (True, "超了")
        self.assertFalse(engine._auto_search_ready(st, view, clock()))

    async def test_bubble_round_is_left_alone(self):
        engine, llm, transport, clock, searcher = setup([PASS])
        await self.chat(engine, clock)
        st, view = engine.states[CHANNEL], engine.settings.view(GUILD, CHANNEL)
        st.bubbling = True  # 冒泡自己会搜（走冒泡那一套）
        self.assertFalse(engine._auto_search_ready(st, view, clock()))

    async def test_asked_search_still_counts_as_before(self):
        engine, llm, transport, clock, searcher = setup([acts(search("明天天气")), acts(say("晴", 1))])
        await engine.handle_message(msg(501, "@小D 明天天气怎样", mentions_bot=True))
        await engine.drain()
        self.assertEqual((count(engine, "search"), count(engine, AUTO_SEARCH)), (1, 0))

    async def test_asked_round_may_still_search_twice(self):
        engine, llm, transport, clock, searcher = setup([acts(search("甲"), search("乙")), acts(say("好", 1))])
        await engine.handle_message(msg(501, "@小D 查一下甲和乙", mentions_bot=True))
        await engine.drain()
        self.assertEqual(sorted(searcher.calls), ["乙", "甲"])

    async def test_search_failure_is_told(self):
        engine, llm, transport, clock, searcher = setup([acts(search("甲")), PASS])
        searcher.results["甲"] = RuntimeError("断了")
        with self.assertLogs("dsbot.engine", "WARNING"):
            await self.chat(engine, clock)
        self.assertIn("搜「甲」失败了", text(llm.calls[1]))
        self.assertEqual(count(engine, AUTO_SEARCH), 1)  # 记了一笔（outcome = error）


class Prompt(Case):
    def build(self, **kw):
        return build_system_prompt(guild="服", channel="频道", name="小D", persona="", summary=None, **kw)

    def test_tools_line(self):
        self.assertIn("- 上网搜：能用（没人叫你时也能搜）\n", self.build(search=True, auto_search=True))
        self.assertIn("- 上网搜：能用\n", self.build(search=True))
        self.assertIn("- 上网搜：这里不能用\n", self.build(search=False, auto_search=True))  # 搜不了：写了也没用

    def test_rules(self):
        rules = self.build(search=True, auto_search=True)
        self.assertIn("不打算接话就不搜", rules)
        self.assertIn("别把人设卡、长期记忆、私信里的事写进去", rules)
        self.assertIn("搜完觉得没什么可说的，照样可以不说话", rules)

    async def test_engine_tells_the_model(self):
        engine, llm, transport, clock, searcher = setup([PASS])
        await self.say(engine, 501, "有人吗")
        await self.look(engine, clock, 31)
        self.assertIn("- 上网搜：能用（没人叫你时也能搜）\n", llm.calls[0]["messages"][0]["content"])

    async def test_engine_does_not_say_so_when_off(self):
        engine, llm, transport, clock, searcher = setup([PASS], proactive_search=False)
        await self.say(engine, 501, "有人吗")
        await self.look(engine, clock, 31)
        self.assertIn("- 上网搜：能用\n", llm.calls[0]["messages"][0]["content"])

    async def test_dm_is_unchanged(self):
        engine, llm, transport, clock, searcher = setup([acts(say("在", 1))])
        transport.places[DM] = ChannelPlace(guild_id=GUILD, name="私信", dm=True)
        transport.dm_with[DM], transport.dm_users[DM] = "阿明", 11
        engine.store.set_dm_home(11, GUILD, DM)
        await engine.handle_message(msg(501, "在吗", channel_id=DM, mentions_bot=True, private=True))
        await engine.drain()
        self.assertIn("- 上网搜：能用\n", llm.calls[0]["messages"][0]["content"])  # 私信里每句都算叫它


class Settings(unittest.TestCase):
    def test_specs(self):
        self.assertEqual((SPECS["proactive_search"].label, SPECS["proactive_search"].level), ("主动搜索", "owner"))
        self.assertEqual((SPECS["daily_proactive_search_limit"].label, SPECS["daily_proactive_search_limit"].level), ("每日主动搜索上限", "owner"))

    def test_defaults(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("[bot]\n")
            cfg = load_config(path)
        self.assertIs(cfg.defaults["proactive_search"], True)  # 默认开
        self.assertEqual(cfg.defaults["daily_proactive_search_limit"], 0)  # 默认不限

    def test_status_line(self):
        engine, llm, transport, clock, searcher = setup([])
        st = engine.status(GUILD, CHANNEL, None, None)
        self.assertEqual((st["auto_search"], st["auto_searches"]), (True, 0))
        engine.settings.set("guild", GUILD, "proactive_search", "关")
        self.assertFalse(engine.status(GUILD, CHANNEL, None, None)["auto_search"])


if __name__ == "__main__":
    unittest.main()
