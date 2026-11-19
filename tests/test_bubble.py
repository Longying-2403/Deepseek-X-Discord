"""2.11.0 冒泡：频道安静了很久，它自己来说点什么。

- 只在开了冒泡的频道（只认频道自己这一级的设置）；安静 bubble_hours～1.5 倍小时（随机）以后；只在白天（8～23 点）；
  每天最多 3 次；上次冒的没人理，就等有人说话再冒。
- 大多数时候（八成）替它找一条新鲜事：先单独问一次模型“这群人对什么感兴趣”，程序从里面（和记着的兴趣里）挑一个最近
  没搜过的话题去搜，限定最近 3 天，七成先只搜 X 上的、没搜到再搜全网；搜到的写在这一轮里，它挑一条用自己的话说。
  其余时候、不能搜、看不出兴趣、没搜到时，只打个招呼。
- 搜过的话题记进这个频道的兴趣；管理员不让搜的不搜；发过的链接记下来，下次告诉它别再发。
- 冒泡以后有人接话，照“跟着聊”的规矩接（没开主动发言也接）。"""

from __future__ import annotations

import json
import random
import unittest

from helpers import CHANNEL, GUILD, FakeSearcher, make_engine, msg
from test_engine import REPLY

from dsbot.models import ChannelPlace
from dsbot.prompts import BUBBLE_GREET, BUBBLE_HEAD, HINT_FOLLOW
from dsbot.render import review_text
from dsbot.search import SearchResult

PASS = '{"actions": []}'
HOUR = 3600
X_QUERY = "原神 新版本 site:x.com after:2026-10-05"
NEWS_QUERY = "原神 新版本 after:2026-10-05"
LINK = "https://x.com/Genshin_7/status/1"
SHARE = [json.dumps({"topics": ["原神 新版本"]}, ensure_ascii=False), REPLY(None, f"原神新版本的预告出了 {LINK}")]


def TOPICS(*names):
    return json.dumps({"topics": list(names)}, ensure_ascii=False)


class Fixed(random.Random):
    """随机的间隔取最短的那头；roll：掷出来的数（0.8 以上只打招呼；小于 0.7 先搜 X，否则搜全网）。"""

    roll = 0.0

    def uniform(self, a, b):
        return a

    def random(self):
        return self.roll

    def choice(self, seq):
        return seq[0]


def setup(answers, *, searcher=None, bubble=True, **defaults):
    left = list(answers)
    searcher = searcher if searcher is not None else FakeSearcher()
    engine, llm, transport, clock = make_engine(lambda call: left.pop(0) if left else PASS, searcher=searcher, defaults=defaults)
    engine.rng = Fixed()
    if bubble:
        engine.settings.set("channel", CHANNEL, "bubble", True)
    clock.advance(14 * HOUR)  # 测试的钟从周三 20:00 开始：拨到周四上午 10 点
    return engine, llm, transport, clock, searcher


def text(call):
    content = call["messages"][-1]["content"]
    return content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)


def asked_for_topics(call):
    return "找话题" in call["messages"][0]["content"]


class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(setattr, Fixed, "roll", 0.0)

    async def say(self, engine, mid, words="最近原神新版本好玩吗", author=11, **kw):
        await engine.handle_message(msg(mid, words, author=author, **kw))

    async def wait(self, engine, clock, seconds):
        clock.advance(seconds)
        await engine.tick()
        await engine.drain()

    async def bubbled(self, answers=None, **kw):
        """有人说了一句，然后安静了 4 小时：它冒了个泡。"""
        engine, llm, transport, clock, searcher = setup(SHARE if answers is None else answers, **kw)
        await self.say(engine, 501)
        await self.wait(engine, clock, 4 * HOUR - 60)
        self.assertEqual(llm.calls, [])  # 还没安静够
        await self.wait(engine, clock, 61)
        return engine, llm, transport, clock, searcher

    async def again(self, engine, clock):
        """有人理了上一次冒的泡，然后又安静了 4 小时。"""
        st = engine.states[CHANNEL]
        self.next_id = getattr(self, "next_id", 700) + 1
        await self.say(engine, self.next_id, "哈哈", author=12)
        st.following = False  # （这里不看“跟着聊”）
        await self.wait(engine, clock, 4 * HOUR + 1)


class WhenItBubbles(Case):
    async def test_after_quiet_hours_it_finds_something_on_x_and_shares_it(self):
        engine, llm, transport, clock, searcher = await self.bubbled()
        self.assertEqual(len(llm.calls), 2)
        ask, round_ = llm.calls
        self.assertTrue(asked_for_topics(ask))  # 先单独问一次：这群人对什么感兴趣
        self.assertIn("阿明：最近原神新版本好玩吗", text(ask))
        self.assertEqual(searcher.calls, [X_QUERY])  # 话题是从它答的里面挑的；“只要 X 上的、最近 3 天的”是这边加的
        shown = text(round_)
        self.assertIn("#1 阿明：最近原神新版本好玩吗", shown)  # 之前那句还没进过记录：这一轮带上，当背景
        self.assertIn(f"关于「{X_QUERY}」搜到的内容", shown)
        hint = shown.split("\n")[-1]
        self.assertTrue(hint.startswith(BUBBLE_HEAD + "这里已经安静了 4 个小时，没人叫你。"), hint)
        self.assertIn("上面是刚搜到的「原神 新版本」最近的消息", hint)
        self.assertEqual([(c, t) for c, t, *_ in transport.sent], [(CHANNEL, f"原神新版本的预告出了 {LINK}")])
        st = engine.states[CHANNEL]
        day = engine.ledger.day(clock())
        self.assertTrue(st.bubble_waiting)
        self.assertEqual((st.bubble_count, st.bubble_day), (1, day))
        self.assertEqual(engine.store.interests(CHANNEL), [("原神 新版本", "auto", clock(), 1)])
        self.assertEqual(engine.store.bubble_links(CHANNEL), [LINK])
        self.assertEqual(engine.store.count_usage(day, guild_id=GUILD, kind="search"), 1)
        self.assertEqual(engine.store.count_usage(day, guild_id=GUILD, kind="bubble"), 1)  # 问兴趣的那一次也记了账
        self.assertEqual(engine.store.bubble_state(CHANNEL), (clock(), True, day, 1))  # 重启以后也记得
        self.assertEqual(len(engine.states[CHANNEL].conversation.turns), 2)  # 问兴趣的那次不进聊天记录

    async def test_waits_until_someone_speaks_before_the_next_one(self):
        Fixed.roll = 0.9
        engine, llm, transport, clock, searcher = await self.bubbled([REPLY(None, "下午好"), REPLY(None, "又是我")])
        self.assertEqual(len(llm.calls), 1)
        await self.wait(engine, clock, 7 * HOUR)  # 没人理：一直不再冒
        self.assertEqual(len(llm.calls), 1)
        clock.advance(15 * HOUR)  # 第二天中午
        await self.say(engine, 502, "哈哈", author=12)
        st = engine.states[CHANNEL]
        self.assertFalse(st.bubble_waiting)  # 有人说话了
        st.following = False  # （这里不看“跟着聊”，见下面）
        st.pending.clear()
        await self.wait(engine, clock, 4 * HOUR + 1)
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(st.bubble_count, 1)  # 新的一天重新数

    async def test_not_at_night(self):
        Fixed.roll = 0.9
        engine, llm, transport, clock, searcher = setup([REPLY(None, "早")])
        clock.advance(9 * HOUR)  # 晚上 7 点
        await self.say(engine, 501)
        await self.wait(engine, clock, 4 * HOUR + 1)  # 晚上 11 点：不冒
        self.assertEqual(llm.calls, [])
        await self.wait(engine, clock, 8 * HOUR + 58 * 60)  # 早上 7:59
        self.assertEqual(llm.calls, [])
        await self.wait(engine, clock, 2 * 60)  # 8 点以后
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(transport.sent[0][1], "早")

    async def test_daily_limit(self):
        Fixed.roll = 0.9
        engine, llm, transport, clock, searcher = setup([REPLY(None, "嗨")])
        await self.say(engine, 501)
        st = engine.states[CHANNEL]
        st.bubble_loaded, st.bubble_day, st.bubble_count = True, engine.ledger.day(clock()), engine.ec.bubble_daily_limit
        await self.wait(engine, clock, 4 * HOUR + 1)
        self.assertEqual(llm.calls, [])  # 今天冒够了
        st.bubble_count -= 1
        await self.wait(engine, clock, 5)
        self.assertEqual(len(llm.calls), 1)

    async def test_staying_silent_is_not_a_bubble(self):
        Fixed.roll = 0.9
        engine, llm, transport, clock, searcher = await self.bubbled([PASS, REPLY(None, "嗨")])
        self.assertEqual(len(llm.calls), 1)
        st = engine.states[CHANNEL]
        self.assertEqual((transport.sent, st.bubble_waiting, st.bubble_count), ([], False, 0))  # 没说话：不算一次，也不用等人理
        await self.wait(engine, clock, 4 * HOUR - 60)
        self.assertEqual(len(llm.calls), 1)  # 但要再等一个间隔
        await self.wait(engine, clock, 61)
        self.assertEqual(len(llm.calls), 2)

    async def test_only_where_the_channel_itself_turned_it_on(self):
        Fixed.roll = 0.9
        engine, llm, transport, clock, searcher = setup([REPLY(None, "嗨")], bubble=False)
        engine.settings.set("guild", GUILD, "bubble", True)  # 开在全服务器上不算
        await self.say(engine, 501)
        await self.wait(engine, clock, 6 * HOUR)
        self.assertEqual(llm.calls, [])
        engine.settings.set("channel", CHANNEL, "bubble", True)  # 这时候才开始算
        await self.wait(engine, clock, 5)
        await self.wait(engine, clock, 4 * HOUR - 60)
        self.assertEqual(llm.calls, [])
        await self.wait(engine, clock, 61)
        self.assertEqual(len(llm.calls), 1)

    async def test_a_channel_nobody_spoke_in_since_restart(self):
        Fixed.roll = 0.9
        engine, llm, transport, clock, searcher = setup([REPLY(None, "有人吗")])
        transport.places[CHANNEL] = ChannelPlace(guild_id=GUILD, name="闲聊", public=True)
        self.assertEqual(engine.states, {})
        await self.wait(engine, clock, 5)  # 开着冒泡的频道：先建好状态，从现在算起
        self.assertIn(CHANNEL, engine.states)
        await self.wait(engine, clock, 4 * HOUR + 1)
        self.assertEqual(len(llm.calls), 1)
        self.assertIn("这里已经安静了好一阵", text(llm.calls[0]))  # 不知道上次有人说话是什么时候

    async def test_talks_on_once_someone_answers(self):
        Fixed.roll = 0.9
        engine, llm, transport, clock, searcher = await self.bubbled([REPLY(None, "下午好"), REPLY(None, "是啊")])
        st = engine.states[CHANNEL]
        self.assertTrue(st.following)  # 没开主动发言也跟着：冒了泡有人接话，要能接着聊
        await self.say(engine, 502, "今天好闲", author=12)
        await self.wait(engine, clock, 46)
        self.assertEqual(len(llm.calls), 2)
        self.assertTrue(text(llm.calls[1]).endswith(HINT_FOLLOW))
        self.assertFalse(st.bubble_waiting)
        await self.wait(engine, clock, 400)  # 没人说话了：回到平时（这里没开主动发言，平时不看）
        await self.say(engine, 503, "还在吗", author=13)
        await self.wait(engine, clock, 600)
        self.assertEqual(len(llm.calls), 2)


class WhatItShares(Case):
    async def test_falls_back_to_the_whole_web_when_x_has_nothing(self):
        searcher = FakeSearcher({X_QUERY: SearchResult(summary="", sources=[], provider="DeepSeek", paid=0)})
        engine, llm, transport, clock, _ = await self.bubbled(searcher=searcher)
        self.assertEqual(searcher.calls, [X_QUERY, NEWS_QUERY])
        self.assertIn(f"关于「{NEWS_QUERY}」搜到的内容", text(llm.calls[1]))
        self.assertEqual(engine.store.count_usage(engine.ledger.day(clock()), guild_id=GUILD, kind="search"), 2)  # 两次都记上

    async def test_some_of_the_time_it_searches_the_news(self):
        Fixed.roll = 0.75  # 不是只打招呼的那两成（0.8 以上），也不在先搜 X 的那七成里
        engine, llm, transport, clock, searcher = await self.bubbled()
        self.assertEqual(searcher.calls, [NEWS_QUERY])
        self.assertEqual((engine.ec.bubble_x_share, engine.ec.bubble_greet_share), (0.7, 0.2))

    async def test_now_and_then_it_just_says_hello(self):
        Fixed.roll = 0.9  # 两成的时候不找新鲜事
        engine, llm, transport, clock, searcher = await self.bubbled([REPLY(None, "下午好")])
        self.assertEqual(len(llm.calls), 1)  # 不问兴趣、不搜
        self.assertFalse(asked_for_topics(llm.calls[0]))
        hint = text(llm.calls[0]).split("\n")[-1]
        self.assertEqual(hint, BUBBLE_HEAD + "这里已经安静了 4 个小时，没人叫你。你像群友闲下来随手发一条那样冒个泡。" + BUBBLE_GREET + "）")
        self.assertEqual(searcher.calls, [])
        self.assertEqual(transport.sent[0][1], "下午好")

    async def test_nothing_found_means_just_hello(self):
        searcher = FakeSearcher({q: SearchResult(summary="", sources=[], provider="DeepSeek", paid=0) for q in (X_QUERY, NEWS_QUERY)})
        engine, llm, transport, clock, _ = await self.bubbled([TOPICS("原神 新版本"), REPLY(None, "下午好")], searcher=searcher)
        self.assertEqual(searcher.calls, [X_QUERY, NEWS_QUERY])
        hint = text(llm.calls[1]).split("\n")[-1]
        self.assertIn(BUBBLE_GREET, hint)
        self.assertNotIn("搜到", text(llm.calls[1]))

    async def test_no_interests_means_just_hello(self):
        engine, llm, transport, clock, searcher = await self.bubbled([TOPICS(), REPLY(None, "下午好")])
        self.assertEqual(searcher.calls, [])
        self.assertIn(BUBBLE_GREET, text(llm.calls[1]).split("\n")[-1])
        engine2, llm2, *_ = await self.bubbled(["不是 JSON", REPLY(None, "下午好")])  # 问兴趣那次答坏了：一样
        self.assertIn(BUBBLE_GREET, text(llm2.calls[1]).split("\n")[-1])

    async def test_topics_are_cleaned_up(self):
        answers = [TOPICS("  原神   新版本 site:reddit.com after:2020-01-01 ", "原神 新版本"), REPLY(None, "嗨")]
        engine, llm, transport, clock, searcher = await self.bubbled(answers)
        self.assertEqual(searcher.calls, [X_QUERY])  # 它自己写的限定去掉；重复的只算一个
        self.assertEqual([t for t, *_ in engine.store.interests(CHANNEL)], ["原神 新版本"])

    async def test_blocked_topics_are_not_searched(self):
        engine, llm, transport, clock, searcher = setup([TOPICS("原神 新版本", "F1"), REPLY(None, "下午好")])
        engine.store.set_interest(CHANNEL, "原神 新版本", "blocked", clock())
        await self.say(engine, 501)
        await self.wait(engine, clock, 4 * HOUR + 1)
        self.assertEqual(searcher.calls, ["F1 site:x.com after:2026-10-05"])  # 不让搜的跳过，挑下一个
        self.assertEqual({t: k for t, k, _, _ in engine.store.interests(CHANNEL)}, {"原神 新版本": "blocked", "F1": "auto"})
        engine, llm, transport, clock, searcher = setup([TOPICS("原神 新版本"), REPLY(None, "下午好")])
        engine.store.set_interest(CHANNEL, "原神 新版本", "blocked", clock())
        await self.say(engine, 501)
        await self.wait(engine, clock, 4 * HOUR + 1)
        self.assertEqual(searcher.calls, [])  # 只看出这一个，又不让搜：打个招呼
        self.assertIn(BUBBLE_GREET, text(llm.calls[1]).split("\n")[-1])

    async def test_does_not_keep_on_about_the_same_thing(self):
        answers = [*SHARE, TOPICS("原神 新版本"), REPLY(None, "下午好"), TOPICS("原神 新版本", "显卡"), REPLY(None, f"显卡降价了 {LINK}2。")]
        engine, llm, transport, clock, searcher = await self.bubbled(answers)
        await self.again(engine, clock)
        self.assertEqual(len(searcher.calls), 1)  # 两天内搜过的话题不再搜：这次只打招呼
        self.assertIn(BUBBLE_GREET, text(llm.calls[3]).split("\n")[-1])
        clock.advance(24 * HOUR - 4 * HOUR)  # 第二天同一时间
        await self.again(engine, clock)
        self.assertEqual(searcher.calls[1], "显卡 site:x.com after:2026-10-06")  # 换一个话题
        self.assertIn(f"这些链接以前发过，别再发：{LINK}。", text(llm.calls[5]).split("\n")[-1])
        self.assertEqual(engine.store.bubble_links(CHANNEL), [LINK + "2", LINK])  # 链接后面的句号不算进去
        self.assertIn("已经记下的：原神 新版本。", llm.calls[4]["messages"][0]["content"])  # 问兴趣时把记着的也给它看

    async def test_manual_interests_are_used_too(self):
        engine, llm, transport, clock, searcher = setup([TOPICS(), REPLY(None, "嗨")])
        engine.store.set_interest(CHANNEL, "显卡价格", "manual", clock())
        await self.say(engine, 501)
        await self.wait(engine, clock, 4 * HOUR + 1)
        self.assertEqual(searcher.calls, ["显卡价格 site:x.com after:2026-10-05"])  # 这次没看出新的：用管理员加的
        self.assertEqual(engine.store.interests(CHANNEL), [("显卡价格", "manual", clock(), 1)])  # 还是管理员加的那一种

    async def test_without_search_it_can_still_say_hello(self):
        engine, llm, transport, clock, searcher = setup([REPLY(None, "下午好")], web_search=False)
        await self.say(engine, 501)
        await self.wait(engine, clock, 4 * HOUR + 1)
        self.assertEqual(len(llm.calls), 1)  # 这里不能搜：不问兴趣
        self.assertIn(BUBBLE_GREET, text(llm.calls[0]).split("\n")[-1])
        self.assertEqual(searcher.calls, [])

    async def test_auto_topics_are_capped(self):
        engine, llm, transport, clock, searcher = setup([])
        engine.ec.bubble_max_topics = 3
        engine.store.set_interest(CHANNEL, "管理员加的", "manual", clock())
        for i in range(5):
            engine.store.use_interest(CHANNEL, f"话题{i}", clock() + i, engine.ec.bubble_max_topics)
        kept = {t: k for t, k, _, _ in engine.store.interests(CHANNEL)}
        self.assertEqual(kept, {"管理员加的": "manual", "话题2": "auto", "话题3": "auto", "话题4": "auto"})  # 管理员加的不挤掉


class NotingInterests(Case):
    """聊天停下来不久就看一次大家对什么感兴趣（没开主动发言的频道，没叫它的消息只留 6 小时）。"""

    async def chat(self, engine, clock, n=4, start=600):
        for i in range(n):
            await self.say(engine, start + i, f"原神新版本的第 {i} 件事", author=11 + i % 2)
            clock.advance(20)

    async def test_noted_soon_after_the_chat_stops(self):
        engine, llm, transport, clock, searcher = setup([TOPICS("原神 新版本", "F1")])
        await self.chat(engine, clock)
        await self.wait(engine, clock, 500)
        self.assertEqual(llm.calls, [])  # 聊天刚停：再等等
        await self.wait(engine, clock, 100)
        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(asked_for_topics(llm.calls[0]))
        self.assertIn("阿明：原神新版本的第 0 件事", text(llm.calls[0]))
        self.assertEqual([(t, k, uses) for t, k, _, uses in engine.store.interests(CHANNEL)], [("F1", "auto", 0), ("原神 新版本", "auto", 0)])
        self.assertEqual((transport.sent, searcher.calls), ([], []))  # 只是记下来：不说话、不搜
        self.assertEqual(len(engine.states[CHANNEL].pending), 4)  # 消息还留着（没进聊天记录）

    async def test_still_there_after_the_messages_are_gone(self):
        engine, llm, transport, clock, searcher = setup([TOPICS("原神 新版本"), REPLY(None, f"预告出了 {LINK}")], bubble_hours=8)
        await self.chat(engine, clock)
        await self.wait(engine, clock, 601)
        await self.wait(engine, clock, 8 * HOUR)  # 8 小时以后：攒着的消息早就过期了
        self.assertEqual(engine.states[CHANNEL].pending, [])
        self.assertEqual(len(llm.calls), 2)  # 冒泡时不用再问：记着的兴趣还在
        self.assertEqual(searcher.calls, ["原神 新版本 site:x.com after:2026-10-05"])
        self.assertEqual(transport.sent[0][1], f"预告出了 {LINK}")

    async def test_not_for_a_couple_of_lines_and_not_too_often(self):
        engine, llm, transport, clock, searcher = setup([TOPICS("原神"), TOPICS("F1")])
        await self.chat(engine, clock, n=3)
        await self.wait(engine, clock, 700)
        self.assertEqual(llm.calls, [])  # 才三句：不值得问
        await self.chat(engine, clock, n=1, start=650)
        await self.wait(engine, clock, 601)
        self.assertEqual(len(llm.calls), 1)
        await self.chat(engine, clock, n=5, start=660)
        await self.wait(engine, clock, 601)
        self.assertEqual(len(llm.calls), 1)  # 一小时内只看一次
        await self.wait(engine, clock, 3000)
        self.assertEqual(len(llm.calls), 2)

    async def test_only_where_bubbling_is_on_and_search_works(self):
        engine, llm, transport, clock, searcher = setup([TOPICS("原神")], bubble=False)
        await self.chat(engine, clock)
        await self.wait(engine, clock, 700)
        self.assertEqual(llm.calls, [])
        engine, llm, transport, clock, searcher = setup([TOPICS("原神")], web_search=False)
        await self.chat(engine, clock)
        await self.wait(engine, clock, 700)
        self.assertEqual(llm.calls, [])  # 搜不了：记了也用不上


class Records(Case):
    async def test_bubble_notes_are_left_out_when_tidying_memory(self):
        engine, llm, transport, clock, searcher = await self.bubbled()
        turns = engine.states[CHANNEL].conversation.turns
        lines = [line for t in turns if t.role == "user" for line in review_text(t.content)]
        self.assertTrue(any("最近原神新版本好玩吗" in line for line in lines))
        self.assertFalse(any(line.startswith(BUBBLE_HEAD) for line in lines))
        self.assertFalse(any("搜到的内容" in line for line in lines))  # 搜到的正文也不当成大家说的话

    async def test_logged_as_its_own_kind_of_round(self):
        with self.assertLogs("dsbot.engine", level="INFO") as logs:
            await self.bubbled()
        self.assertTrue(any("冒泡 →" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
