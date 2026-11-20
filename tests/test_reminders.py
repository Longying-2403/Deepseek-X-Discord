"""2.14.0 提醒：群友被叫到时口头说（"明天九点提醒我开会"），模型写一个 remind 动作，程序发一条带按钮的确认消息，
本人点了“确认”才设上；到点由程序直接发（不再问模型）。

- 在频道里说的：到点在那个频道里 @ 他（可以 @ 别人，最多 10 个）；说了私信提醒的：私信一个人，发不出去就回频道里说。
- 私信里说的：只提醒他自己，发在私信里。
- 每人最多 15 条，每个服务器（到点在频道里说的）最多 30 条；满了直接告诉他。
- bot 不在线错过了：晚了不到 6 小时补发并说晚了多久；再晚就不发，列表里标“错过了”。
- 提醒是程序办的：设好了、取消了、到点说过了，下一轮在聊天记录里告诉它一声。"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, T0, make_engine, msg
from test_bubble import text
from test_dm import DM, BotCase, FakeGuild, FakeResponse
from test_engine import REPLY

from dsbot import reminders as rm
from dsbot.actions import parse_actions
from dsbot.config import SPECS, load_config
from dsbot.models import ChannelPlace
from dsbot.reminders import Reminder

TZ = "Asia/Taipei"
HOUR = 3600.0
NINE = T0 + 13 * HOUR  # 测试的钟从 2026-10-07 周三 20:00（台北）开始：第二天早上九点
PASS = '{"actions": []}'


def acts(*items):
    return json.dumps({"actions": list(items)}, ensure_ascii=False)


def say(to, words):
    return {"type": "reply", "to": to, "messages": [words]}


def remind(to=1, **kw):
    kw.setdefault("at", "2026-10-08 09:00")
    kw.setdefault("text", "开会")
    return {"type": "remind", "to": to, **kw}


def call_me(mid, words="明天九点提醒我开会", **kw):
    return msg(mid, "@小D " + words, mentions_bot=True, **kw)


def setup(answers, **kw):
    left = list(answers)
    engine, llm, transport, clock = make_engine(lambda call: left.pop(0) if left else PASS, **kw)
    transport.cards = []
    transport.dms = []
    transport.blocked = set()
    return engine, llm, transport, clock


class Case(unittest.IsolatedAsyncioTestCase):
    async def ask(self, engine, message):
        await engine.handle_message(message)
        await engine.drain()

    async def wait(self, engine, clock, until):
        clock.t = until
        await engine.tick()
        await engine.drain()

    async def set_one(self, engine, transport, mid=1, **kw):
        """叫它设一条，再点确认。返回提醒的编号。"""
        await self.ask(engine, call_me(mid, **kw))
        rid = transport.cards[-1][2]
        self.assertIsNotNone(rid, transport.cards[-1])
        said, replace = engine.remind_decide(rid, kw.get("author", 11), True)
        self.assertTrue(replace and said.startswith("⏰ 设好了"), said)
        return rid


class Times(unittest.TestCase):
    def test_full_date(self):
        self.assertEqual(rm.parse_when("2026-10-08 09:00", None, T0, TZ), NINE)
        self.assertEqual(rm.parse_when("2026/10/08 9:00", None, T0, TZ), NINE)
        self.assertEqual(rm.parse_when("2026-10-08T09:00:00", None, T0, TZ), NINE)
        self.assertEqual(rm.parse_when("2026年10月8日 9点", None, T0, TZ), NINE)

    def test_no_year_and_clock_only_take_the_next_one(self):
        self.assertEqual(rm.parse_when("10-08 09:00", None, T0, TZ), NINE)
        self.assertEqual(rm.parse_when("09:00", None, T0, TZ), NINE)  # 今天九点过了：明天
        self.assertEqual(rm.parse_when("21:30", None, T0, TZ), T0 + 1.5 * HOUR)  # 今天还没到
        next_year = rm.parse_when("10-01 09:00", None, T0, TZ)  # 今年的过了：明年
        self.assertGreater(next_year, T0 + 300 * 86400)

    def test_minutes_from_now(self):
        self.assertEqual(rm.parse_when(None, 30, T0, TZ), T0 + 1800)
        self.assertEqual(rm.parse_when("2026-10-08 09:00", "90", T0, TZ), T0 + 5400)  # 写了多少分钟以后就按它
        self.assertIsNone(rm.parse_when(None, -5, T0, TZ))
        self.assertIsNone(rm.parse_when(None, True, T0, TZ))

    def test_nonsense_is_refused(self):
        for raw in ("明天", "", "2026-13-01 09:00", "2026-02-30 09:00", "25:00", "soon", None, 9):
            self.assertIsNone(rm.parse_when(raw, None, T0, TZ), raw)

    def test_range(self):
        self.assertIsNone(rm.check_when(T0 + 60, T0))
        self.assertEqual(rm.check_when(None, T0), "时间没看懂")
        self.assertEqual(rm.check_when(T0 - 60, T0), "这个时间已经过了")
        self.assertEqual(rm.check_when(T0 + 10, T0), "这个时间已经过了")
        self.assertEqual(rm.check_when(T0 + 400 * 86400, T0), "最远只能设到一年以后")

    def test_shown_with_weekday_and_how_long(self):
        self.assertEqual(rm.when_text(NINE, T0, TZ), "10-08 周四 09:00（13 小时后）")
        self.assertEqual(rm.when_text(T0 + 1800, T0, TZ), "10-07 周三 20:30（30 分钟后）")
        self.assertTrue(rm.when_text(T0 + 200 * 86400, T0, TZ).startswith("2027-"))
        self.assertEqual(rm.when_text(T0 - 60, T0, TZ), "10-07 周三 19:59")
        self.assertEqual([rm.span_text(s) for s in (45, 720, 3 * 3600 + 300, 2 * 86400 + 4 * 3600)], ["45 秒", "12 分钟", "3 小时 5 分钟", "2 天 4 小时"])

    def test_buttons(self):
        self.assertEqual(rm.parse_button(rm.button_id(True, 12)), (True, 12))
        self.assertEqual(rm.parse_button(rm.button_id(False, 3)), (False, 3))
        for raw in (None, "", "dsr:ok", "dsr:yes:1", "other:ok:1", "dsr:ok:x", "dsr:ok:1:2", "dsr:ok:①"):
            self.assertIsNone(rm.parse_button(raw), raw)

    def test_cleaning(self):
        self.assertEqual(rm.clean("  开会\n别迟到 <@123> <@&9> ", 50), "开会 别迟到")
        self.assertNotIn("@everyone", rm.clean("@everyone 开会", 50))
        self.assertEqual(len(rm.clean("长" * 500, 200)), 200)
        self.assertEqual(rm.clean(None, 50), "")


class Parsing(unittest.TestCase):
    def one(self, item):
        actions, ok = parse_actions(acts(item))
        self.assertTrue(ok)
        return actions

    def test_add(self):
        (a,) = self.one(remind(3, who=["@阿明", "小红", "阿明"], say="该开会啦", dm=True))
        self.assertEqual((a.kind, a.op, a.to, a.at, a.minutes, a.text), ("remind", "add", 3, "2026-10-08 09:00", None, "开会"))
        self.assertEqual((a.who, a.dm, a.say), (["阿明", "小红"], True, "该开会啦"))
        self.assertEqual(a.to_json(), {"type": "remind", "op": "add", "to": 3, "at": "2026-10-08 09:00", "text": "开会", "who": ["阿明", "小红"], "dm": True, "say": "该开会啦"})

    def test_minutes_win_over_a_clock_time(self):
        (a,) = self.one({"type": "remind", "in_minutes": 30, "at": "20:30", "text": "关火"})
        self.assertEqual((a.minutes, a.at), (30.0, None))
        self.assertEqual(a.to_json(), {"type": "remind", "op": "add", "in_minutes": 30.0, "text": "关火"})

    def test_needs_a_time_and_a_text(self):
        self.assertEqual(self.one({"type": "remind", "text": "开会"}), [])
        self.assertEqual(self.one({"type": "remind", "at": "09:00"}), [])
        self.assertEqual(self.one({"type": "remind", "op": "add", "at": "09:00", "text": "  "}), [])

    def test_cancel_and_list(self):
        (a,) = self.one({"type": "remind", "op": "cancel", "text": "开会"})
        self.assertEqual((a.op, a.text), ("cancel", "开会"))
        (b,) = self.one({"type": "remind", "op": "list"})
        self.assertEqual(b.op, "list")
        self.assertEqual(b.to_json(), {"type": "remind", "op": "list"})

    def test_at_most_three_in_a_round(self):
        actions, _ = parse_actions(acts(*[remind(1, text=f"事{i}") for i in range(5)]))
        self.assertEqual(len(actions), 3)


class InAChannel(Case):
    async def test_asks_first_then_fires_on_time_without_the_model(self):
        engine, llm, transport, clock = setup([acts(say(1, "记下了，点一下确认"), remind(say="该去开会啦"))])
        await self.ask(engine, call_me(1))
        self.assertEqual(transport.sent[-1][1], "记下了，点一下确认")
        channel, card, rid = transport.cards[-1]
        self.assertEqual(channel, CHANNEL)
        self.assertIn("10-08 周四 09:00（13 小时后） 在这个频道提醒你：开会", card)
        self.assertIn("阿明，时间对的话点“确认”", card)
        self.assertEqual(engine.store.reminder(rid).status, "pending")
        clock.t = T0 + 60
        self.assertEqual(engine.remind_decide(rid, 99, True), ("这条提醒是 阿明 设的，只有他本人能点。", False))
        self.assertEqual(engine.store.reminder(rid).status, "pending")
        said, replace = engine.remind_decide(rid, 11, True)
        self.assertTrue(replace)
        self.assertIn("⏰ 设好了：10-08 周四 09:00", said)
        self.assertIn("/ds-dm 提醒", said)
        self.assertEqual(engine.remind_decide(rid, 11, True)[0], "⏰ 这条确认已经过期了，要设的话再跟我说一次。")  # 点第二次

        await self.wait(engine, clock, NINE - 10)
        self.assertEqual(len(transport.sent), 1)
        await self.wait(engine, clock, NINE + 2)
        self.assertEqual(transport.sent[-1], (CHANNEL, "⏰ <@11> 该去开会啦", None, False))
        self.assertEqual(transport.mentions[-1], (11,))
        self.assertEqual(len(llm.calls), 1)  # 到点不问模型
        self.assertEqual(engine.store.reminder(rid).status, "done")
        await self.wait(engine, clock, NINE + 20)
        self.assertEqual(len(transport.sent), 2)  # 只发一次

    async def test_it_is_told_what_happened_next_round(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind(say="开会啦")), REPLY(2, "嗯"), REPLY(3, "不客气")])
        rid = await self.set_one(engine, transport)
        await self.ask(engine, call_me(2, "谢了"))
        seen = text(llm.calls[1])
        self.assertIn("（阿明 点了确认，提醒设好了：10-08 周四 09:00", seen)
        self.assertIn("在这个频道提醒阿明：开会", seen)
        await self.wait(engine, clock, NINE + 1)
        await self.ask(engine, call_me(3, "收到"))
        seen = text(llm.calls[2])
        self.assertIn("（到点了，你已经照设好的提醒在这里发了：⏰ <@11> 开会啦）", seen)
        self.assertNotIn("提醒设好了", seen)  # 上一轮说过的不再说
        self.assertEqual(engine.store.reminder(rid).status, "done")

    async def test_never_confirmed_never_fires(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind())])
        await self.ask(engine, call_me(1))
        rid = transport.cards[-1][2]
        await self.wait(engine, clock, NINE + 60)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(engine.store.reminder(rid).status, "cancelled")  # 一直没点：作废
        self.assertEqual(engine.remind_decide(rid, 11, True), ("⏰ 这条确认已经过期了，要设的话再跟我说一次。", True))

    async def test_without_a_line_of_its_own_it_says_the_plain_thing(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind())])
        await self.set_one(engine, transport)
        await self.wait(engine, clock, NINE)
        self.assertEqual(transport.sent[-1][1], "⏰ <@11> 提醒：开会")

    async def test_cancel_button(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind()), REPLY(2, "嗯")])
        await self.ask(engine, call_me(1))
        rid = transport.cards[-1][2]
        self.assertEqual(engine.remind_decide(rid, 11, False), ("⏰ 没有设：开会（已取消）", True))
        await self.wait(engine, clock, NINE + 1)
        self.assertEqual(len(transport.sent), 1)
        await self.ask(engine, call_me(2, "算了"))
        self.assertIn("（阿明 点了取消，「开会」那条提醒没有设。）", text(llm.calls[1]))

    async def test_confirming_too_late(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind())])
        await self.ask(engine, call_me(1))
        rid = transport.cards[-1][2]
        clock.t = T0 + 601
        said, replace = engine.remind_decide(rid, 11, True)
        self.assertEqual((said, replace), ("⏰ 阿明，提醒没设成：确认得太晚了，再跟我说一次。", True))
        self.assertEqual(engine.store.reminder(rid).status, "cancelled")

    async def test_confirm_after_the_time_has_passed(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind(at=None, in_minutes=2))])
        await self.ask(engine, call_me(1, "两分钟后提醒我"))
        rid = transport.cards[-1][2]
        clock.t = T0 + 170
        self.assertIn("这个时间已经过了", engine.remind_decide(rid, 11, True)[0])

    async def test_bad_times_are_said_right_away(self):
        for bad, why in (({"at": "2026-10-07 08:00"}, "这个时间已经过了"), ({"at": "明天"}, "时间没看懂"), ({"at": "2028-01-01 09:00"}, "最远只能设到一年以后")):
            engine, llm, transport, clock = setup([acts(say(1, "好"), remind(**bad)), REPLY(2, "嗯")])
            await self.ask(engine, call_me(1))
            self.assertEqual(transport.cards, [(CHANNEL, f"⏰ 阿明，提醒没设成：{why}。", None)])
            self.assertEqual(engine.store.reminders_of(11), [])
            await self.ask(engine, call_me(2, "？"))
            self.assertIn(f"（阿明 要的提醒没设成：{why}。已经告诉他了。）", text(llm.calls[1]))

    async def test_in_minutes(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind(at=None, in_minutes=30, text="关火"))])
        await self.set_one(engine, transport, words="半小时后提醒我关火")
        self.assertIn("10-07 周三 20:30（30 分钟后） 在这个频道提醒你：关火", transport.cards[-1][1])
        await self.wait(engine, clock, T0 + 1799)
        self.assertEqual(len(transport.sent), 1)
        await self.wait(engine, clock, T0 + 1801)
        self.assertEqual(transport.sent[-1][1], "⏰ <@11> 提醒：关火")

    async def test_counts_for_whoever_called_it(self):
        engine, llm, transport, clock = setup([acts(say(2, "好"), remind(to=1))])  # to 写的不是这一轮叫它的消息
        await self.ask(engine, msg(1, "明天九点要开会", author=22, name="小红"))
        await self.ask(engine, call_me(2))
        rid = transport.cards[-1][2]
        self.assertEqual(engine.store.reminder(rid).owner_id, 11)

    async def test_only_when_called(self):
        engine, llm, transport, clock = setup([acts(say(None, "我来提醒"), remind(to=None))], defaults={"proactive": True})
        await engine.handle_message(msg(1, "明天九点要开会"))
        clock.advance(40)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(transport.cards, [])
        self.assertIsNone(engine.store.reminder(1))

    async def test_turned_off_here(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind())], defaults={"reminders": False})
        await self.ask(engine, call_me(1))
        self.assertEqual(transport.cards, [(CHANNEL, "⏰ 阿明，提醒没设成：这里没开提醒。", None)])
        self.assertIn("- 提醒：这里没开", llm.calls[0]["messages"][0]["content"])

    async def test_prompt_says_it_can(self):
        engine, llm, transport, clock = setup([REPLY(1, "在")])
        await self.ask(engine, call_me(1, "在吗"))
        system = llm.calls[0]["messages"][0]["content"]
        self.assertIn("- 提醒：能用", system)
        self.assertIn('15. 提醒：{"type": "remind"', system)
        self.assertIn("他本人点了才算设上", system)

    async def test_no_buttons_no_reminder(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind())])
        transport.can_send = False
        await self.ask(engine, call_me(1))
        self.assertEqual([r.status for r in [engine.store.reminder(1)]], ["cancelled"])


class OtherPeople(Case):
    async def people(self, engine):
        await self.ask(engine, msg(1, "我也在", author=22, name="小红"))
        await self.ask(engine, msg(2, "来了", author=33, name="老王"))

    async def test_several_people_in_the_channel(self):
        engine, llm, transport, clock = setup([acts(say(3, "好"), remind(to=3, who=["小红", "老王", "阿明"], text="集合", say="集合啦"))])
        await self.people(engine)
        rid = await self.set_one(engine, transport, mid=3, words="明天九点提醒我们仨集合")
        self.assertIn("在这个频道提醒小红、老王、你：集合", transport.cards[-1][1])
        self.assertEqual(engine.store.reminder(rid).targets, [(22, "小红"), (33, "老王"), (11, "阿明")])
        await self.wait(engine, clock, NINE)
        self.assertEqual(transport.sent[-1][1], "⏰ <@22> <@33> <@11> 集合啦（阿明 让我提醒的）")
        self.assertEqual(transport.mentions[-1], (22, 33, 11))

    async def test_someone_it_cannot_find(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind(who=["不存在的人"]))])
        await self.ask(engine, call_me(1))
        self.assertEqual(transport.cards[-1][2], None)
        self.assertIn("提醒没设成：找不到「不存在的人」是谁", transport.cards[-1][1])
        self.assertEqual(engine.store.reminders_of(11), [])

    async def test_two_people_with_the_same_name(self):
        engine, llm, transport, clock = setup([acts(say(3, "好"), remind(to=3, who=["小红"]))])
        await self.ask(engine, msg(1, "我是小红", author=22, name="小红"))
        await self.ask(engine, msg(2, "我也是小红", author=23, name="小红"))
        await self.ask(engine, call_me(3))
        self.assertIn("叫「小红」的不止一个", transport.cards[-1][1])

    async def test_too_many_people(self):
        engine, llm, transport, clock = setup([acts(say(3, "好"), remind(to=3, who=["小红", "老王", "阿明"]))], engine_cfg={"remind_max_targets": 2})
        await self.people(engine)
        await self.ask(engine, call_me(3))
        self.assertIn("提醒没设成：一条提醒最多 @ 2 个人", transport.cards[-1][1])

    async def test_dm_to_one_person(self):
        engine, llm, transport, clock = setup([acts(say(3, "好"), remind(to=3, who=["小红"], dm=True, text="交报告"))])
        await self.people(engine)
        rid = await self.set_one(engine, transport, mid=3, words="明天九点私信提醒小红交报告")
        self.assertIn("发私信提醒小红：交报告", transport.cards[-1][1])
        await self.wait(engine, clock, NINE)
        self.assertEqual(transport.dms, [(22, "⏰ 提醒：交报告（阿明 在「测试服」让我提醒你的）")])
        self.assertEqual(len(transport.sent), 1)  # 频道里不说
        self.assertEqual(engine.store.reminder(rid).status, "done")

    async def test_dm_to_myself_from_a_channel(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind(dm=True, say="开会啦")), REPLY(2, "嗯")])
        await self.set_one(engine, transport, words="明天九点私信提醒我开会")
        await self.wait(engine, clock, NINE)
        self.assertEqual(transport.dms, [(11, "⏰ 开会啦（你在「测试服」让我提醒的）")])
        await self.ask(engine, call_me(2, "谢了"))
        self.assertIn("（到点了，你已经照这里设好的提醒私信了阿明：开会）", text(llm.calls[1]))

    async def test_dm_only_one_person(self):
        engine, llm, transport, clock = setup([acts(say(3, "好"), remind(to=3, who=["小红", "老王"], dm=True))])
        await self.people(engine)
        await self.ask(engine, call_me(3))
        self.assertIn("提醒没设成：私信提醒一次只能给一个人", transport.cards[-1][1])

    async def test_dm_that_cannot_be_delivered_falls_back_to_the_channel(self):
        engine, llm, transport, clock = setup([acts(say(3, "好"), remind(to=3, who=["小红"], dm=True, text="交报告"))])
        await self.people(engine)
        await self.set_one(engine, transport, mid=3)
        transport.blocked.add(22)
        await self.wait(engine, clock, NINE)
        self.assertEqual(transport.dms, [])
        self.assertEqual(transport.sent[-1][1], "⏰ <@22> 提醒：交报告（阿明 让我提醒的）（私信发不出去，在这里说）")
        self.assertEqual(transport.mentions[-1], (22,))

    async def test_nothing_gets_out(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind())])
        rid = await self.set_one(engine, transport)
        transport.can_send = False
        await self.wait(engine, clock, NINE)
        self.assertEqual(engine.store.reminder(rid).status, "failed")
        transport.can_send = True
        await self.wait(engine, clock, NINE + 60)
        self.assertEqual(len(transport.sent), 1)  # 不再试


class InDM(Case):
    def dm_setup(self, answers, **kw):
        engine, llm, transport, clock = setup(answers, **kw)
        transport.places[DM] = ChannelPlace(guild_id=GUILD, name="私信", dm=True)
        transport.dm_with[DM] = "阿明"
        transport.dm_users[DM] = 11
        engine.store.set_dm_home(11, GUILD, DM)

        async def sleep(seconds):
            clock.advance(seconds)

        engine.sleep = sleep
        return engine, llm, transport, clock

    async def test_only_me_and_in_the_dm(self):
        engine, llm, transport, clock = self.dm_setup([acts(say(1, "好"), remind(who=["小红"], say="开会啦"))])
        await self.ask(engine, msg(1, "明天九点提醒我开会", channel_id=DM, mentions_bot=True, private=True))
        channel, card, rid = transport.cards[-1]
        self.assertEqual(channel, DM)
        self.assertIn("发私信提醒你：开会", card)
        engine.remind_decide(rid, 11, True)
        r = engine.store.reminder(rid)
        self.assertEqual((r.private, r.dm, r.targets), (True, True, [(11, "阿明")]))
        sent = len(transport.sent)
        await self.wait(engine, clock, NINE)
        self.assertEqual(transport.sent[sent:], [(DM, "⏰ 开会啦", None, False)])
        self.assertEqual(transport.mentions[-1], ())
        self.assertEqual(transport.dms, [])


class Limits(Case):
    async def test_per_person(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind()), acts(say(2, "好"), remind(to=2, text="吃药"))], engine_cfg={"remind_max_per_person": 1})
        await self.set_one(engine, transport)
        await self.ask(engine, call_me(2, "再提醒我吃药"))
        self.assertEqual(transport.cards[-1], (CHANNEL, "⏰ 阿明，提醒没设成：你已经有 1 条提醒了（最多 1 条），先用 `/ds-dm 提醒` 取消几条。", None))
        self.assertEqual(len(engine.store.reminders_of(11)), 1)

    async def test_per_server_counts_only_channel_ones(self):
        answers = [acts(say(1, "好"), remind(dm=True)), acts(say(2, "好"), remind(to=2, text="吃药")), acts(say(3, "好"), remind(to=3, text="浇花")), acts(say(4, "好"), remind(to=4, text="遛狗", dm=True))]
        engine, llm, transport, clock = setup(answers, engine_cfg={"remind_max_per_server": 1})
        await self.set_one(engine, transport)  # 私信的不占服务器的名额
        await self.set_one(engine, transport, mid=2)
        await self.ask(engine, call_me(3, author=22, name="小红"))
        self.assertIn("小红，提醒没设成：这个服务器的提醒满了（最多 1 条），请管理员用 `/dsadmin 提醒` 取消几条", transport.cards[-1][1])
        await self.set_one(engine, transport, mid=4, author=22, name="小红")  # 私信提醒还能设

    async def test_limit_is_checked_again_on_confirm(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind()), acts(say(2, "好"), remind(to=2, text="吃药"))], engine_cfg={"remind_max_per_person": 1})
        await self.ask(engine, call_me(1))
        await self.ask(engine, call_me(2))
        first, second = transport.cards[0][2], transport.cards[1][2]
        self.assertTrue(engine.remind_decide(first, 11, True)[0].startswith("⏰ 设好了"))
        self.assertIn("你已经有 1 条提醒了", engine.remind_decide(second, 11, True)[0])

    def test_config(self):
        cfg = load_config("../config.toml")
        self.assertEqual((cfg.engine.remind_max_per_person, cfg.engine.remind_max_per_server, cfg.engine.remind_max_targets, cfg.engine.remind_late_hours), (15, 30, 10, 6.0))
        self.assertIs(cfg.defaults["reminders"], True)
        self.assertEqual((SPECS["reminders"].label, SPECS["reminders"].level), ("提醒", "admin"))


class Late(Case):
    async def test_a_bit_late_still_goes_out_and_says_so(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind())])
        await self.set_one(engine, transport)
        await self.wait(engine, clock, NINE + 2 * HOUR)
        self.assertEqual(transport.sent[-1][1], "⏰ <@11> 提醒：开会（晚了 2 小时，刚才我不在线）")

    async def test_much_too_late_is_marked_missed(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind()), REPLY(2, "抱歉")])
        rid = await self.set_one(engine, transport)
        await self.wait(engine, clock, NINE + 6 * HOUR + 60)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(engine.store.reminder(rid).status, "missed")
        self.assertEqual(engine.store.reminders_of(11), [])  # 不占名额
        self.assertEqual([r.id for r in engine.store.reminders_of(11, missed_since=clock.t - rm.KEEP_SECONDS)], [rid])
        await self.ask(engine, call_me(2, "怎么没提醒我"))
        self.assertIn("（阿明 设的提醒「开会」到点时你不在线，晚了太久，没有发。）", text(llm.calls[1]))

    async def test_old_ones_are_tidied_away(self):
        engine, llm, transport, clock = setup([acts(say(1, "好"), remind()), acts(say(2, "好"), remind(to=2, text="吃药"))])
        done = await self.set_one(engine, transport)
        await self.ask(engine, call_me(2))
        pending = transport.cards[-1][2]
        await self.wait(engine, clock, NINE)
        engine.store.tidy_reminders(clock.t - rm.PENDING_SECONDS, clock.t - 7 * 86400)
        self.assertEqual(engine.store.reminder(pending).status, "cancelled")  # 一直没点确认
        self.assertEqual(engine.store.reminder(done).status, "done")
        engine.store.tidy_reminders(clock.t + 8 * 86400, clock.t + 8 * 86400)
        self.assertIsNone(engine.store.reminder(done))


class ByVoice(Case):
    async def test_cancel_by_a_few_words(self):
        answers = [acts(say(1, "好"), remind()), acts(say(2, "好"), remind(to=2, text="吃药")), acts(say(3, "取消了"), {"type": "remind", "op": "cancel", "text": "开会"}), REPLY(4, "嗯")]
        engine, llm, transport, clock = setup(answers)
        first = await self.set_one(engine, transport)
        await self.set_one(engine, transport, mid=2)
        await self.ask(engine, call_me(3, "开会那条不用提醒了"))
        self.assertEqual(transport.cards[-1], (CHANNEL, "⏰ 阿明，取消了：10-08 周四 09:00（13 小时后） 在这个频道提醒你：开会", None))
        self.assertEqual(engine.store.reminder(first).status, "cancelled")
        clock.advance(61)  # 每人每分钟最多叫它 3 次
        await self.ask(engine, call_me(4, "好"))
        self.assertIn("（阿明 的提醒取消了：10-08 周四 09:00", text(llm.calls[3]))
        await self.wait(engine, clock, NINE)
        self.assertEqual([s[1] for s in transport.sent if s[1].startswith("⏰")], ["⏰ <@11> 提醒：吃药"])

    async def test_cannot_tell_which_one(self):
        answers = [acts(say(1, "好"), remind()), acts(say(2, "好"), remind(to=2, text="开会前吃药")), acts(say(3, "哪条"), {"type": "remind", "op": "cancel", "text": "开会"})]
        engine, llm, transport, clock = setup(answers)
        await self.set_one(engine, transport)
        await self.set_one(engine, transport, mid=2)
        await self.ask(engine, call_me(3, "取消开会的提醒"))
        card = transport.cards[-1][1]
        self.assertIn("分不出要取消哪一条", card)
        self.assertIn("(1) ", card)
        self.assertIn("(2) ", card)
        self.assertEqual(len(engine.store.reminders_of(11)), 2)

    async def test_list_shows_only_what_belongs_here(self):
        answers = [acts(say(1, "好"), remind()), acts(say(2, "好"), remind(to=2, text="看病", dm=True)), acts(say(3, "这些"), {"type": "remind", "op": "list"}), acts(say(4, "没有"), {"type": "remind", "op": "list"})]
        engine, llm, transport, clock = setup(answers)
        await self.set_one(engine, transport)
        await self.set_one(engine, transport, mid=2)
        await self.ask(engine, call_me(3, "我有哪些提醒"))
        card = transport.cards[-1][1]
        self.assertIn("(1) 10-08 周四 09:00（13 小时后） 在这个频道提醒你：开会", card)
        self.assertNotIn("看病", card)  # 要私信的那条不在频道里露出来
        await self.ask(engine, call_me(4, "我有哪些提醒", author=22, name="小红"))
        self.assertIn("小红，你在这里没有设着的提醒", transport.cards[-1][1])

    async def test_cancel_only_my_own(self):
        answers = [acts(say(1, "好"), remind()), acts(say(2, "好"), {"type": "remind", "op": "cancel", "text": "开会"})]
        engine, llm, transport, clock = setup(answers)
        rid = await self.set_one(engine, transport)
        await self.ask(engine, call_me(2, "取消开会的提醒", author=22, name="小红"))
        self.assertEqual(engine.store.reminder(rid).status, "set")


class Cancelling(Case):
    async def test_owner_and_admin_reach(self):
        answers = [acts(say(1, "好"), remind()), acts(say(2, "好"), remind(to=2, text="看病", dm=True))]
        engine, llm, transport, clock = setup(answers)
        here = await self.set_one(engine, transport)
        private = await self.set_one(engine, transport, mid=2)
        self.assertIsNone(engine.remind_cancel(here, owner_id=22))  # 别人的取消不了
        self.assertIsNone(engine.remind_cancel(private, guild_id=GUILD))  # 管理员管不到发私信的
        self.assertIsNone(engine.remind_cancel(here, guild_id=GUILD + 1))
        self.assertEqual([r.id for r in engine.store.guild_reminders(GUILD)], [here])
        self.assertEqual(engine.remind_cancel(here, guild_id=GUILD).id, here)
        self.assertIsNone(engine.remind_cancel(here, guild_id=GUILD))  # 已经取消了
        self.assertEqual(engine.remind_cancel(private, owner_id=11).id, private)
        await self.wait(engine, clock, NINE)
        self.assertEqual((len(transport.sent), transport.dms), (2, []))

    async def test_store_round_trip(self):
        engine, llm, transport, clock = setup([])
        r = Reminder(0, 11, "阿明", GUILD, CHANNEL, False, False, [(11, "阿明"), (22, "小红")], "开会", "开会啦", NINE, T0, "set")
        rid = engine.store.add_reminder(r)
        back = engine.store.reminder(rid)
        self.assertEqual((back.targets, back.say, back.due_at, back.dm, back.private), ([(11, "阿明"), (22, "小红")], "开会啦", NINE, False, False))
        self.assertFalse(engine.store.move_reminder(rid, "pending", "set"))
        self.assertTrue(engine.store.move_reminder(rid, "set", "done"))
        self.assertEqual(engine.store.reminders_due(NINE + 1), [])
        self.assertEqual(engine.store.drop_reminders(owner_id=11), 1)


class Commands(BotCase):
    def commands(self, engine):
        from dsbot.commands import register_dm_commands
        from dsbot.ledger import Ledger

        bot = self.make([FakeGuild(GUILD, "A服", {11})], store=engine.store)
        added = []
        bot.tree = NS(add_command=added.append)
        bot.ledger = Ledger(bot.store, TZ)
        bot.secrets = NS(owner_id=0)
        bot.memory = None
        bot.cfg = NS(engine=NS(dm_memory_max_entries=30, remind_max_per_person=15, remind_max_per_server=30))
        bot.engine = engine
        register_dm_commands(bot)
        group = added[0].get_command("提醒")
        return bot, group.get_command("查看").callback, group.get_command("取消").callback

    def interaction(self, user=11):
        self.sent = []

        async def followup(text, ephemeral=False):
            self.sent.append((text, ephemeral))

        return NS(user=NS(id=user, display_name="阿明"), guild_id=None, channel_id=DM, response=FakeResponse(self.sent), followup=NS(send=followup))

    async def test_view_and_cancel(self):
        from dsbot.commands import guild_reminders_text, reminder_choices

        engine, llm, transport, clock = setup([])
        bot, view, cancel = self.commands(engine)
        await view(self.interaction())
        self.assertIn("**你的提醒**（0 / 15 条）\n还没有。", self.sent[0][0])
        self.assertIn("guild_reminders" and "现在没有", guild_reminders_text(bot, GUILD))
        a = engine.store.add_reminder(Reminder(0, 11, "阿明", GUILD, CHANNEL, False, False, [(11, "阿明"), (22, "小红")], "开会", None, NINE, T0, "set"))
        b = engine.store.add_reminder(Reminder(0, 11, "阿明", GUILD, DM, True, True, [(11, "阿明")], "吃药", None, NINE + HOUR, T0, "set"))
        engine.store.add_reminder(Reminder(0, 11, "阿明", GUILD, CHANNEL, False, False, [(11, "阿明")], "浇花", None, T0 - HOUR, T0 - 2 * HOUR, "missed"))
        engine.store.add_reminder(Reminder(0, 22, "小红", GUILD, CHANNEL, False, False, [(22, "小红")], "别人的", None, NINE, T0, "set"))
        await view(self.interaction())
        listing, private = self.sent[0]
        self.assertTrue(private)
        self.assertIn("**你的提醒**（2 / 15 条）", listing)
        self.assertIn(f"(1) 10-07 周三 19:00 · <#{CHANNEL}> · 提醒你：浇花〔错过了：到点时它不在线〕", listing)
        self.assertIn(f"(2) 10-08 周四 09:00（13 小时后） · <#{CHANNEL}> · 提醒你、小红：开会", listing)
        self.assertIn("(3) 10-08 周四 10:00（14 小时后） · 私信 · 提醒你：吃药", listing)
        self.assertNotIn("别人的", listing)

        mine = engine.store.reminders_of(11, missed_since=clock.t - rm.KEEP_SECONDS)
        choices = reminder_choices(bot, mine, "", you=11)
        self.assertEqual([c.value for c in choices][1:], [a, b])
        self.assertEqual([c.value for c in reminder_choices(bot, mine, "吃药", you=11)], [b])

        admin = guild_reminders_text(bot, GUILD)
        self.assertIn("**这个服务器的提醒**（2 / 30 条", admin)
        self.assertIn("阿明 设的 · 提醒阿明、小红：开会", admin)
        self.assertNotIn("吃药", admin)  # 私信里设的是各人自己的

        await cancel(self.interaction(user=22), a)
        self.assertIn("没有这一条", self.sent[0][0])
        await cancel(self.interaction(), a)
        self.assertIn("已取消：10-08 周四 09:00", self.sent[0][0])
        self.assertEqual(engine.store.reminder(a).status, "cancelled")
        await cancel(self.interaction(), a)
        self.assertIn("没有这一条", self.sent[0][0])


if __name__ == "__main__":
    unittest.main()
