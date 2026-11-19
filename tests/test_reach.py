"""2.12.0 主动私信（/ds-dm 主动）和冒泡的档位。

- 主动私信：每个人只管自己的，默认关。低 = 6～10 小时没聊、一天最多 1～2 次；中 = 3～5 小时、3～4 次；高 = 1.5～3 小时、
  5～7 次。时段自己定（不填 = 全天，可以跨夜）。连着 3 次没人回就停，等他说话了再继续。
- 说什么它自己挑（问候、接着以前聊过的事）；三成的时候程序先替它搜一条他感兴趣的新鲜事。
- 发不出去（他关了私信、屏蔽了它）连着两次：替他关掉。花费算进他今天的私信额度，到了上限不再主动找他。
- 频道的冒泡也按档位：低 / 中 / 高。"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, OWNER, FakeSearcher, make_engine, msg
from test_bubble import HOUR, LINK, PASS, Fixed, asked_for_topics, text
from test_dm import DM, BotCase, FakeGuild, FakeResponse
from test_engine import REPLY

from dsbot.config import BUBBLE_LEVELS, REACH_LEVELS, SPECS, format_value, level_of, load_config, parse_value
from dsbot.engine import _in_hours
from dsbot.models import ChannelPlace
from dsbot.prompts import BUBBLE_HEAD, REACH_FREE
from dsbot.render import review_text

USER = 11


def dm(message_id, words, **kw):
    return msg(message_id, words, author=USER, name="阿明", channel_id=DM, mentions_bot=True, private=True, **kw)


def setup(answers, *, level="mid", window=(None, None), searcher=None, **engine_cfg):
    left = list(answers)
    searcher = searcher if searcher is not None else FakeSearcher()
    engine, llm, transport, clock = make_engine(lambda call: left.pop(0) if left else PASS, searcher=searcher, engine_cfg=engine_cfg)
    engine.rng = Fixed()
    transport.places[DM] = ChannelPlace(guild_id=GUILD, name="私信", dm=True)
    transport.dm_with[DM] = "阿明"
    transport.dm_users[DM] = USER
    engine.store.set_dm_home(USER, GUILD, DM)
    if level is not None:
        engine.store.set_dm_reach(USER, level, *window)

    async def sleep(seconds):  # 私信要等对方说完：时间跟着走
        clock.advance(seconds)

    engine.sleep = sleep
    clock.advance(14 * HOUR)  # 测试的钟从周三 20:00 开始：拨到周四上午 10 点
    return engine, llm, transport, clock, searcher


class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Fixed.roll = 0.9  # 默认不去搜
        self.addCleanup(setattr, Fixed, "roll", 0.0)
        self.addCleanup(setattr, Fixed, "rolls", [])

    async def chat(self, engine, llm, mid, words="在吗"):
        """他私信了一句，它回了。"""
        await engine.handle_message(dm(mid, words))
        await engine.drain()

    async def wait(self, engine, clock, seconds):
        clock.advance(seconds)
        await engine.tick()
        await engine.drain()


class WhenItReachesOut(Case):
    async def test_after_a_quiet_while_it_writes_first(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢"), REPLY(None, "午饭吃了吗")])
        await self.chat(engine, llm, 1)
        self.assertEqual(len(llm.calls), 1)
        await self.wait(engine, clock, 3 * HOUR - 120)
        self.assertEqual(len(llm.calls), 1)  # 中档：还没安静够 3 小时
        await self.wait(engine, clock, 121)
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(transport.sent[-1][:2], (DM, "午饭吃了吗"))
        hint = text(llm.calls[1]).split("\n")[-1]
        self.assertTrue(hint.startswith(BUBBLE_HEAD + "「阿明」已经 3 个小时没找你了"), hint)
        self.assertIn(REACH_FREE, hint)
        self.assertFalse(any(asked_for_topics(c) for c in llm.calls))
        self.assertFalse(any(BUBBLE_HEAD in line for line in review_text(text(llm.calls[1]))))  # 整理记忆时那句说明不算聊天

    async def test_off_by_default_and_each_person_decides(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢")], level=None)
        await self.chat(engine, llm, 1)
        await self.wait(engine, clock, 30 * HOUR)
        self.assertEqual(len(llm.calls), 1)
        engine.store.set_dm_home(USER + 1, GUILD, DM + 1)
        engine.store.set_dm_reach(USER + 1, "high")  # 别人开了：不关他的事
        engine.reach_changed()
        self.assertFalse(engine._bubble_on(engine.states[DM]))
        await self.wait(engine, clock, 30 * HOUR)
        self.assertEqual(len(llm.calls), 1)

    async def test_levels(self):
        self.assertEqual(REACH_LEVELS, {"low": (6, 10, 1, 2), "mid": (3, 5, 3, 4), "high": (1.5, 3, 5, 7)})
        for level, hours in (("low", 6), ("high", 1.5)):
            engine, llm, transport, clock, _ = setup([REPLY(1, "在呢"), REPLY(None, "嗨")], level=level)
            await self.chat(engine, llm, 1)
            await self.wait(engine, clock, hours * HOUR - 120)
            self.assertEqual(len(llm.calls), 1, level)
            await self.wait(engine, clock, 121)
            self.assertEqual(len(llm.calls), 2, level)

    async def test_three_unanswered_then_it_waits_for_them(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢")] + [REPLY(None, f"第{i}次") for i in range(1, 6)], level="high")
        await self.chat(engine, llm, 1)
        for n in (2, 3, 4):
            await self.wait(engine, clock, 1.5 * HOUR + 1)
            self.assertEqual(len(llm.calls), n)
        st = engine.states[DM]
        self.assertEqual(st.bubble_waiting, 3)
        await self.wait(engine, clock, 20 * HOUR)
        self.assertEqual(len(llm.calls), 4)  # 连着三次没人回：停下
        self.assertTrue(engine.status(GUILD, DM, None)["bubble"]["waiting"])
        await self.chat(engine, llm, 2, "刚在忙")  # 他说话了：这一句照常回，钟重新走
        self.assertEqual((len(llm.calls), st.bubble_waiting), (5, 0))
        await self.wait(engine, clock, 1.5 * HOUR + 1)
        self.assertEqual(len(llm.calls), 6)

    async def test_strikes_come_from_config(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢"), REPLY(None, "嗨"), REPLY(None, "嗨")], level="high", reach_max_unanswered=1)
        await self.chat(engine, llm, 1)
        await self.wait(engine, clock, 1.5 * HOUR + 1)
        await self.wait(engine, clock, 20 * HOUR)
        self.assertEqual(len(llm.calls), 2)

    async def test_daily_cap_is_rolled_within_the_range(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢")] + [REPLY(None, "嗨")] * 9, level="mid")
        await self.chat(engine, llm, 1)
        st = engine.states[DM]
        st.bubble_loaded, st.bubble_day, st.bubble_count = True, engine.ledger.day(clock()), 4  # 中档一天 3～4 次（这里掷到 4）
        await self.wait(engine, clock, 3 * HOUR + 1)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(st.bubble_cap, 4)
        st.bubble_count = 3
        await self.wait(engine, clock, 5)
        self.assertEqual(len(llm.calls), 2)

    async def test_any_hour_unless_they_set_a_window(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢"), REPLY(None, "还没睡？")], level="high")
        clock.advance(15 * HOUR)  # 周五凌晨 1 点
        await self.chat(engine, llm, 1)
        await self.wait(engine, clock, 1.5 * HOUR + 1)  # 凌晨 2 点半：没定时段就照发
        self.assertEqual(len(llm.calls), 2)

    async def test_window_is_respected_and_may_cross_midnight(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢"), REPLY(None, "早")], level="high", window=(9, 23))
        clock.advance(15 * HOUR)  # 周五凌晨 1 点
        await self.chat(engine, llm, 1)
        await self.wait(engine, clock, 1.5 * HOUR + 1)  # 凌晨 2 点半：不在时段里
        self.assertEqual(len(llm.calls), 1)
        await self.wait(engine, clock, 6 * HOUR)  # 8 点半
        self.assertEqual(len(llm.calls), 1)
        await self.wait(engine, clock, 1.6 * HOUR)  # 过了 9 点（挪到 9 点以后，随机的那一小时取最短）
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual([_in_hours(h, 22, 2) for h in (21, 22, 23, 0, 1, 2, 12)], [False, True, True, True, True, False, False])
        self.assertTrue(all(_in_hours(h, None, None) and _in_hours(h, 0, 0) for h in range(24)))
        self.assertEqual([_in_hours(h, 8, 23) for h in (7, 8, 22, 23)], [False, True, True, False])

    async def test_silence_from_it_is_not_a_strike(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢"), PASS, REPLY(None, "嗨")], level="high")
        await self.chat(engine, llm, 1)
        await self.wait(engine, clock, 1.5 * HOUR + 1)
        st = engine.states[DM]
        self.assertEqual((len(llm.calls), st.bubble_waiting, st.bubble_count), (2, 0, 0))  # 它决定不发：不算一次
        await self.wait(engine, clock, 1.5 * HOUR + 1)
        self.assertEqual((len(llm.calls), st.bubble_waiting, st.bubble_count), (3, 1, 1))

    async def test_after_restart_the_clock_still_runs(self):
        engine, llm, transport, clock, _ = setup([REPLY(None, "好久不见")], level="high")
        await self.wait(engine, clock, 1)  # 重启以后他还没说过话：tick 替他建好状态
        self.assertIn(DM, engine.states)
        self.assertTrue(engine.states[DM].private)
        await self.wait(engine, clock, 1.5 * HOUR + 1)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(transport.sent[-1][:2], (DM, "好久不见"))

    async def test_needs_a_dm_channel_and_an_open_server(self):
        engine, llm, transport, clock, _ = setup([REPLY(None, "嗨")], level="high")
        engine.settings.set("guild", GUILD, "dm", False)  # 那个服务器关掉了私信
        await self.wait(engine, clock, 1)
        await self.wait(engine, clock, 5 * HOUR)
        self.assertEqual(llm.calls, [])
        self.assertEqual(engine.store.dm_reach_channels(), {DM: (USER, "high", None, None)})
        engine.store.set_dm_reach(USER + 5, "high")  # 开了、但从没私信过它：不知道往哪发
        self.assertNotIn(USER + 5, [v[0] for v in engine.store.dm_reach_channels().values()])

    async def test_their_daily_dm_budget_also_stops_it(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢"), REPLY(None, "嗨")], level="high")
        await self.chat(engine, llm, 1)
        now = clock()
        engine.store.record_usage(
            ts=now, day=engine.ledger.day(now + 2 * HOUR), guild_id=GUILD, channel_id=DM, kind="mention", outcome="reply", hit=0, miss=0, output=0, cost=0.5
        )
        await self.wait(engine, clock, 1.5 * HOUR + 1)
        self.assertEqual(len(llm.calls), 1)  # 今天私信花的到了每人的上限（0.2 美元）

    async def test_turned_off_when_it_cannot_be_delivered(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢")] + [REPLY(None, "嗨")] * 4, level="high")
        await self.chat(engine, llm, 1)
        transport.can_send = False  # 他关了私信 / 屏蔽了它
        await self.wait(engine, clock, 1.5 * HOUR + 1)
        self.assertIsNotNone(engine.store.dm_reach(USER))  # 一次发不出去：可能只是网络
        await self.wait(engine, clock, 1.5 * HOUR + 1)
        self.assertIsNone(engine.store.dm_reach(USER))  # 连着两次：替他关掉
        calls = len(llm.calls)
        await self.wait(engine, clock, 30 * HOUR)
        self.assertEqual(len(llm.calls), calls)

    async def test_changing_the_setting_takes_effect_at_once(self):
        engine, llm, transport, clock, _ = setup([REPLY(1, "在呢"), REPLY(None, "嗨")], level="low")
        await self.chat(engine, llm, 1)
        engine.store.set_dm_reach(USER, None)
        engine.reach_changed()
        self.assertFalse(engine._bubble_on(engine.states[DM]))


class WhatItSays(Case):
    async def test_some_of_the_time_it_brings_news(self):
        Fixed.roll = 0.2  # 三成的时候先替它搜
        answers = [REPLY(1, "原神新版本什么时候出"), json.dumps({"topics": ["原神 新版本"]}, ensure_ascii=False), REPLY(None, f"预告出了 {LINK}")]
        engine, llm, transport, clock, searcher = setup(answers, level="mid")
        await self.chat(engine, llm, 1, "原神新版本什么时候出")
        await self.wait(engine, clock, 3 * HOUR + 1)
        scout = next(c for c in llm.calls if asked_for_topics(c))
        self.assertIn("这是它和一个人的私信", scout["messages"][0]["content"])  # 只记能公开搜的兴趣，不记私事
        self.assertEqual(searcher.calls, ["原神 新版本 site:x.com after:2026-10-05"])
        last = text(llm.calls[-1])
        self.assertIn("刚搜到的「原神 新版本」最近的消息（他爱聊这个）", last.split("\n")[-1])
        self.assertEqual(transport.sent[-1][:2], (DM, f"预告出了 {LINK}"))
        self.assertEqual(engine.store.bubble_links(DM), [LINK])
        self.assertEqual(engine.ec.reach_search_share, 0.3)

    async def test_most_of_the_time_it_decides_by_itself(self):
        Fixed.roll = 0.3  # 不在先搜的那三成里
        engine, llm, transport, clock, searcher = setup([REPLY(1, "在呢"), REPLY(None, "嗨")], level="mid")
        await self.chat(engine, llm, 1)
        await self.wait(engine, clock, 3 * HOUR + 1)
        self.assertEqual((len(llm.calls), searcher.calls), (2, []))

    async def test_group_bubble_may_bring_up_remembered_things(self):
        from dsbot.prompts import BUBBLE_GREET

        self.assertIn("长期记忆里记着的事", BUBBLE_GREET)
        self.assertIn("自己挑", BUBBLE_GREET)


class GroupLevels(Case):
    async def run_level(self, level, hours, per_day):
        answers = [REPLY(None, "嗨")] * 3
        left = list(answers)
        engine, llm, transport, clock = make_engine(lambda call: left.pop(0) if left else PASS)
        engine.rng = Fixed()
        engine.settings.set("channel", CHANNEL, "bubble", level)
        clock.advance(14 * HOUR)
        await engine.handle_message(msg(501, "大家好"))
        await self.wait(engine, clock, hours * HOUR - 60)
        self.assertEqual(llm.calls, [], level)
        await self.wait(engine, clock, 61)
        self.assertEqual(len(llm.calls), 1, level)
        self.assertEqual(engine.status(GUILD, CHANNEL, None)["bubble"]["limit"], per_day)

    async def test_low_mid_high(self):
        self.assertEqual(BUBBLE_LEVELS, {"low": (8, 12, 1, 1), "mid": (4, 6, 3, 3), "high": (2, 3, 5, 5)})
        await self.run_level("低", 8, 1)
        await self.run_level("中", 4, 3)
        await self.run_level("high", 2, 5)

    async def test_off_and_the_old_switch(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "嗨"))
        engine.rng = Fixed()
        engine.settings.set("channel", CHANNEL, "bubble", "关")
        clock.advance(14 * HOUR)
        await engine.handle_message(msg(501, "大家好"))
        await self.wait(engine, clock, 13 * HOUR)
        self.assertEqual(llm.calls, [])
        engine.settings._overrides[("channel", CHANNEL)] = {"bubble": True}  # 2.11.0 存下的“开”：算中档
        self.assertEqual(engine._bubble_profile(engine.states[CHANNEL]).low, 4)

    def test_values(self):
        spec = SPECS["bubble"]
        self.assertEqual([parse_value(spec, v) for v in ("关", "低", "中", "高", "开", True, False, "MID", "off")],
                         ["off", "low", "mid", "high", "mid", "mid", "off", "mid", "off"])
        self.assertEqual([format_value(spec, v) for v in ("off", "low", "mid", "high", True, False)], ["关", "低", "中", "高", "中", "关"])
        with self.assertRaises(ValueError):
            parse_value(spec, "很高")
        with self.assertRaises(ValueError):
            level_of("5")

    def test_old_config_lines_are_ignored(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("[defaults]\nbubble = false\nbubble_hours = 4\n[engine]\nbubble_daily_limit = 3\n")
            cfg = load_config(path)
        self.assertEqual((cfg.unknown, cfg.defaults["bubble"]), ([], "off"))  # 旧的 config.toml 照样能用，不提醒


class FakeEngine(NS):
    pass


class ReachCommand(BotCase):
    """/ds-dm 主动：档位、时段；只管自己的。"""

    def commands(self, bot):
        from dsbot.commands import register_dm_commands
        from dsbot.ledger import Ledger

        added = []
        bot.tree = NS(add_command=added.append)
        bot.ledger = Ledger(bot.store, "Asia/Taipei")
        bot.secrets = NS(owner_id=OWNER)
        bot.engine.changed = 0

        def changed():
            bot.engine.changed += 1

        bot.engine.reach_changed = changed
        register_dm_commands(bot)
        return added[0].get_command("主动").callback, added[0].get_command("状态").callback

    def interaction(self, *, in_guild=None):
        self.sent = []

        async def followup(words, ephemeral=False):
            self.sent.append((words, ephemeral))

        return NS(user=NS(id=USER), guild_id=in_guild, channel_id=CHANNEL if in_guild is not None else DM,
                  response=FakeResponse(self.sent), followup=NS(send=followup))

    async def test_set_level_and_window(self):
        bot = self.make([FakeGuild(GUILD, "A服", {USER})])
        reach, status = self.commands(bot)
        bot.store.set_dm_home(USER, GUILD, DM)
        await reach(self.interaction())
        self.assertIn("主动私信：关", self.sent[0][0])
        await reach(self.interaction(), NS(value="high"))
        said = self.sent[0][0]
        self.assertIn("主动私信：高 · 你 1.5～3 小时没找它，它会来找你说句话，一天最多 5～7 次 · 时段：全天 · 连着 3 次你都没回就先停下", said)
        self.assertIn("/ds-dm 主动 档位:关", said)
        self.assertTrue(self.sent[0][1])  # 只有自己看得到
        self.assertEqual((bot.store.dm_reach(USER), bot.engine.changed), (("high", None, None), 1))
        await reach(self.interaction(in_guild=GUILD), None, 22, 2)  # 只改时段：档位不动；在服务器里也能用
        self.assertIn("时段：22 点到第二天 2 点", self.sent[0][0])
        self.assertEqual(bot.store.dm_reach(USER), ("high", 22, 2))
        await reach(self.interaction(), NS(value="low"))  # 只改档位：时段不动
        self.assertEqual(bot.store.dm_reach(USER), ("low", 22, 2))
        await reach(self.interaction(), None, 0, 0)  # 都填 0：全天
        self.assertEqual(bot.store.dm_reach(USER), ("low", None, None))
        await status(self.interaction())
        self.assertIn("主动私信：低 · 你 6～10 小时没找它", self.sent[0][0])

    async def test_half_a_window_or_no_level(self):
        bot = self.make([FakeGuild(GUILD, "A服", {USER})])
        reach, _ = self.commands(bot)
        await reach(self.interaction(), NS(value="mid"), 8, None)
        self.assertIn("要一起填", self.sent[0][0])
        await reach(self.interaction(), None, 8, 23)
        self.assertIn("先选一个档位", self.sent[0][0])
        self.assertIsNone(bot.store.dm_reach(USER))

    async def test_never_dmed_it_yet(self):
        bot = self.make([FakeGuild(GUILD, "A服", {USER})])
        reach, _ = self.commands(bot)
        await reach(self.interaction(in_guild=GUILD), NS(value="mid"))
        self.assertIn("⚠️ 你还没私信过它", self.sent[0][0])
        self.assertEqual(bot.store.dm_reach_channels(), {})

    async def test_off_forgets_what_it_noted(self):
        bot = self.make([FakeGuild(GUILD, "A服", {USER})])
        reach, _ = self.commands(bot)
        bot.store.set_dm_home(USER, GUILD, DM)
        await reach(self.interaction(), NS(value="mid"), 8, 23)
        bot.store.note_interests(DM, ["原神 新版本"], 100.0, 20)
        bot.store.add_bubble_links(DM, [LINK], 100.0)
        bot.store.note_interests(CHANNEL, ["频道的"], 100.0, 20)
        await reach(self.interaction(), NS(value="off"))
        self.assertIn("主动私信：关", self.sent[0][0])
        self.assertEqual((bot.store.dm_reach(USER), bot.store.interests(DM), bot.store.bubble_links(DM)), (None, [], []))
        self.assertEqual(len(bot.store.interests(CHANNEL)), 1)  # 频道的兴趣不动

    async def test_server_closed_dms(self):
        bot = self.make([FakeGuild(GUILD, "A服", {USER})])
        reach, _ = self.commands(bot)
        bot.store.set_dm_home(USER, GUILD, DM)
        bot.settings.set("guild", GUILD, "dm", False)
        await reach(self.interaction(), NS(value="mid"))
        self.assertIn("关掉了私信，它发不了", self.sent[0][0])
