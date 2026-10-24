"""私信：每段私信挂在一个服务器上（用那里的人设、长期记忆、人设卡、预算），等对方说完再回，有每人每天的花费上限。"""

import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from helpers import CHANNEL, GUILD, OWNER, make_engine, msg
from test_convert import fake_message
from test_memory import ACTIONS, REPLY, chat, is_review, stores, system_text, user_text

from dsbot.convert import convert_message
from dsbot.engine import DM_MAX_WAIT, DM_SETTLE, DM_TYPING_HOLD
from dsbot.models import ChannelPlace
from dsbot.prompts import DM_LIMIT_TEXT, REVIEW_DM
from dsbot.store import Store

DM = 5000  # 和阿明的私信频道
OTHER = 2  # 另一个服务器


def dm(message_id, text, *, author=11, name="阿明", **kw):
    kw.setdefault("channel_id", DM)
    return msg(message_id, text, author=author, name=name, mentions_bot=True, private=True, **kw)


def dm_engine(responder=None, **kw):
    engine, llm, transport, clock = make_engine(responder or (lambda call: ACTIONS(REPLY(1, "在呢"))), **kw)
    transport.places[DM] = ChannelPlace(guild_id=GUILD, name="私信", dm=True)
    transport.dm_with[DM] = "阿明"

    async def sleep(seconds):  # 私信要等对方说完：时间跟着走
        waited(engine, llm, seconds)
        clock.advance(seconds)

    engine.sleep = sleep
    engine.settle = []
    return engine, llm, transport, clock


def waited(engine, llm, seconds):
    """记下等对方说完的那几次（第一次问模型之前的；之后的是发消息前假装打字）。"""
    if not llm.calls:
        engine.settle.append(seconds)


def chats(llm):
    return [c for c in llm.calls if c["json_mode"] and not is_review(c)]


class Basics(unittest.IsolatedAsyncioTestCase):
    async def test_every_message_is_for_it_and_prompt_says_dm(self):
        engine, llm, transport, clock = dm_engine()
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertEqual(len(chats(llm)), 1)
        system = system_text(llm.calls[0])
        self.assertIn("你在 Discord 私信里和「阿明」单独聊天", system)
        self.assertIn("服务器「测试服」", system)
        self.assertNotIn("的 #", system.split("## 你是谁")[1].split("##")[0])  # 不说自己在哪个频道
        self.assertIn("- 点歌：这里不能用", system)
        self.assertEqual(transport.sent[0][:2], (DM, "在呢"))

    async def test_burst_is_answered_once_after_they_stop(self):
        engine, llm, transport, clock = dm_engine()
        later = [dm(2, "今天"), dm(3, "抽到金了")]

        async def sleep(seconds):  # 等的时候他又发了一条（等了 1 秒的时候）
            waited(engine, llm, seconds)
            if later:
                clock.advance(1)
                await engine.handle_message(later.pop(0))
                clock.advance(seconds - 1)
            else:
                clock.advance(seconds)

        engine.sleep = sleep
        await engine.handle_message(dm(1, "我跟你说"))
        await engine.drain()
        self.assertEqual(len(chats(llm)), 1)  # 三条一起回
        text = user_text(llm.calls[0])
        for part in ("我跟你说", "今天", "抽到金了"):
            self.assertIn(part, text)
        # 第 2 条在 1 秒时到、第 3 条在 4 秒时到：最后一条之后安静了 DM_SETTLE 秒才问
        self.assertEqual(engine.settle, [DM_SETTLE, 1.0, DM_SETTLE])

    async def test_tick_does_not_cut_the_wait_short(self):
        engine, llm, transport, clock = dm_engine()
        await engine.handle_message(dm(1, "我跟你说"))
        await engine.tick()  # 定时检查看到“有人叫它”：私信还在等他说完，不能马上开始
        self.assertEqual(len([t for t in engine._tasks if not t.done()]), 1)  # 只有在等他说完的那个
        await engine.drain()
        self.assertEqual((len(chats(llm)), engine.settle), (1, [DM_SETTLE]))

    async def test_typing_after_a_message_waits_longer(self):
        engine, llm, transport, clock = dm_engine()
        await engine.handle_message(dm(1, "等下"))
        clock.advance(1)
        engine.handle_typing(DM)  # 发完又开始打字
        await engine.drain()
        self.assertEqual(engine.settle, [DM_TYPING_HOLD])  # 从开始打字算
        self.assertEqual(len(chats(llm)), 1)

    async def test_typing_forever_still_gets_an_answer(self):
        engine, llm, transport, clock = dm_engine()

        async def sleep(seconds):
            waited(engine, llm, seconds)
            clock.advance(seconds)
            engine.handle_typing(DM)  # 一直显示在打字

        engine.sleep = sleep
        await engine.handle_message(dm(1, "嗯……"))
        await engine.drain()
        self.assertEqual(len(chats(llm)), 1)
        self.assertEqual(sum(engine.settle), DM_MAX_WAIT)

    async def test_not_limited_by_mentions_per_minute(self):
        engine, llm, transport, clock = dm_engine(defaults={"mention_rate_limit": 1})
        for i in range(3):
            await engine.handle_message(dm(1 + i, f"第{i}句"))
            await engine.drain()
            clock.advance(5)
        self.assertEqual(len(chats(llm)), 3)

    async def test_server_turned_dms_off(self):
        engine, llm, transport, clock = dm_engine()
        engine.settings.set("guild", GUILD, "dm", "关")
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertEqual(llm.calls, [])

    async def test_channel_whitelist_does_not_block_dms(self):
        # 服务器只在白名单频道里回应（允许回应默认关）：私信只看“私信”这一项
        engine, llm, transport, clock = dm_engine(defaults={"enabled": False})
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertEqual(len(chats(llm)), 1)


class Budget(unittest.IsolatedAsyncioTestCase):
    def spend(self, engine, clock, cost, channel=DM):
        now = clock()
        engine.store.record_usage(
            ts=now, day=engine.ledger.day(now), guild_id=GUILD, channel_id=channel, kind="mention", outcome="reply", cost=cost
        )
        engine.ledger._day = None  # 账本下次从数据库重新读

    async def test_per_person_daily_cap(self):
        engine, llm, transport, clock = dm_engine(defaults={"dm_daily_budget_usd": 0.1})
        self.spend(engine, clock, 0.1)
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        await engine.handle_message(dm(2, "人呢"))
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertEqual([t for _, t, _, _ in transport.sent], [DM_LIMIT_TEXT])  # 只说一次
        self.assertEqual(transport.reactions, [(DM, 2, "💤")])

    async def test_cap_is_per_person(self):
        engine, llm, transport, clock = dm_engine(defaults={"dm_daily_budget_usd": 0.1})
        self.spend(engine, clock, 0.5, channel=DM + 1)  # 别人私信花的
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertEqual(len(chats(llm)), 1)

    async def test_server_over_budget_keeps_the_rest_for_mentions(self):
        engine, llm, transport, clock = dm_engine()
        self.spend(engine, clock, 1.5, channel=CHANNEL)  # 服务器今天的预算（1 美元）用完了
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertEqual(llm.calls, [])
        await engine.handle_message(msg(2, "@小D 在吗", mentions_bot=True))  # 群里点名照样回
        await engine.drain()
        self.assertEqual(len(chats(llm)), 1)

    async def test_owner_is_not_capped(self):
        engine, llm, transport, clock = dm_engine(defaults={"dm_daily_budget_usd": 0.1})
        self.spend(engine, clock, 1.5)
        await engine.handle_message(dm(1, "在吗", author=OWNER, name="主人", from_owner=True))
        await engine.drain()
        self.assertEqual(len(chats(llm)), 1)

    async def test_owner_still_stops_at_the_hard_limit(self):
        engine, llm, transport, clock = dm_engine()
        self.spend(engine, clock, 2.0)  # 服务器预算的两倍
        await engine.handle_message(dm(1, "在吗", author=OWNER, name="主人"))
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertEqual(transport.reactions, [(DM, 1, "⚠️")])


class Memory(unittest.IsolatedAsyncioTestCase):
    async def test_dm_is_reviewed_into_the_server(self):
        cards, memory = stores()
        review = json.dumps({"cards": [{"to": 1, "text": "阿明喜欢猫"}], "memory": [{"add": "阿明在准备考试"}]}, ensure_ascii=False)
        engine, llm, transport, clock = dm_engine(chat([ACTIONS(REPLY(1, "好"))], review=review), cards=cards, memory=memory)
        for i in range(3):
            await engine.handle_message(dm(1 + i, f"第{i}句：我喜欢猫"))
            await engine.drain()
            clock.advance(30)
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        reviews = [c for c in llm.calls if is_review(c)]
        self.assertEqual(len(reviews), 1)
        self.assertIn(REVIEW_DM.strip(), system_text(reviews[0]))
        self.assertEqual(cards.get(GUILD, 11).text, "阿明喜欢猫")
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明在准备考试"])

    async def test_dm_uses_the_server_memory(self):
        cards, memory = stores()
        memory.add(GUILD, "周五打副本", via="command")
        engine, llm, transport, clock = dm_engine(cards=cards, memory=memory)
        await engine.handle_message(dm(1, "周五干嘛来着"))
        await engine.drain()
        self.assertIn("周五打副本", system_text(llm.calls[0]))

    async def test_own_dm_follows_them_into_the_channel(self):
        # 互通：他在服务器的频道里叫它时，带上他刚才私信里聊的（不用开记忆共享）
        engine, llm, transport, clock = dm_engine(chat([ACTIONS(REPLY(1, "收到"))]))
        await engine.handle_message(dm(1, "我在准备期末考试"))
        await engine.drain()
        clock.advance(60)
        await engine.handle_message(msg(2, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        text = user_text(chats(llm)[-1])
        self.assertIn("（阿明 刚才在私信里跟你聊过：", text)
        self.assertIn("准备期末考试", text)
        clock.advance(60)
        await engine.handle_message(msg(3, "@小D 还在吗", mentions_bot=True))
        await engine.drain()
        self.assertNotIn("准备期末考试", user_text(chats(llm)[-1]))  # 带过一次就不再带

    async def test_dm_of_one_person_is_not_shown_for_another(self):
        engine, llm, transport, clock = dm_engine(chat([ACTIONS(REPLY(1, "收到"))]), defaults={"share_memory": True})
        await engine.handle_message(dm(1, "我的秘密是喜欢小雨"))
        await engine.handle_message(dm(2, "我在准备期末考试"))
        await engine.drain()
        await engine.handle_message(msg(3, "@小D 阿明最近在干嘛", author=12, name="小雨", mentions_bot=True))
        await engine.drain()
        self.assertNotIn("期末考试", user_text(chats(llm)[-1]))
        # 别的频道也翻不到私信的记录
        self.assertFalse(engine.shares_out(DM, transport.places[DM]))
        self.assertNotIn(DM, [cid for cid, _ in engine.shared_sources(GUILD, CHANNEL)])

    async def test_secret_dm_stays_in_the_dm(self):
        engine, llm, transport, clock = dm_engine(chat([ACTIONS(REPLY(1, "好，我不说"))]))
        await engine.handle_message(dm(1, "跟你说个事，别告诉别人：我喜欢小雨"))
        await engine.drain()
        await engine.handle_message(msg(2, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        text = user_text(chats(llm)[-1])
        self.assertNotIn("小雨", text)
        self.assertNotIn("私信里跟你聊过", text)

    async def test_group_chat_is_brought_into_the_dm(self):
        engine, llm, transport, clock = dm_engine(chat([ACTIONS(REPLY(1, "收到"))]))
        transport.places[300] = ChannelPlace(guild_id=GUILD, name="秘密基地", public=False)
        await engine.handle_message(msg(1, "@小D 晚上一起打游戏吗", mentions_bot=True))
        await engine.drain()
        await engine.handle_message(msg(2, "@小D 这里的事别外传", channel_id=300, mentions_bot=True))
        await engine.drain()
        clock.advance(60)
        await engine.handle_message(dm(3, "刚才说的那个"))
        await engine.drain()
        text = user_text(chats(llm)[-1])
        self.assertIn("在 #闲聊 跟你聊过：", text)  # 公开频道的带进私信（不用开记忆共享）
        self.assertIn("晚上一起打游戏吗", text)
        self.assertNotIn("别外传", text)  # 私密频道的不带

    async def test_moving_to_another_server_clears_the_dm(self):
        engine, llm, transport, clock = dm_engine()
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        engine.move_dm(DM, OTHER)
        self.assertEqual(engine.states[DM].guild_id, OTHER)
        self.assertEqual(engine.store.load_conversation(DM, OTHER).turns, [])
        transport.places[DM] = ChannelPlace(guild_id=OTHER, name="私信", dm=True)
        await engine.handle_message(dm(2, "换了", guild_id=OTHER))
        await engine.drain()
        self.assertNotIn("在吗", json.dumps(chats(llm)[-1]["messages"], ensure_ascii=False))  # 原来的记录没带过去
        self.assertEqual(engine.store.conversation_channels(OTHER), [DM])
        self.assertNotIn(DM, engine.store.conversation_channels(GUILD))


class StoreDM(unittest.TestCase):
    def test_homes_and_spend(self):
        store = Store(":memory:")
        self.assertIsNone(store.dm_home(11))
        store.set_dm_home(11, GUILD, DM)
        store.set_dm_home(11, OTHER, None)  # 换服务器、不知道私信频道：频道留着
        self.assertEqual(store.dm_home(11), (OTHER, DM))
        self.assertEqual(store.dm_homes(), {DM: (11, OTHER)})
        store.record_usage(ts=1, day="2026-10-24", guild_id=GUILD, channel_id=DM, kind="mention", outcome="reply", cost=0.25)
        store.record_usage(ts=1, day="2026-10-23", guild_id=GUILD, channel_id=DM, kind="mention", outcome="reply", cost=1)
        self.assertEqual(store.channel_spent("2026-10-24", DM), 0.25)
        self.assertEqual(store.channel_spent("2026-10-24", DM + 1), 0.0)


class ConvertDM(unittest.TestCase):
    def test_dm_message(self):
        message = fake_message(guild=None, channel=NS(id=DM), clean_content="随便说说")
        m = convert_message(message, me_id=999, aliases=[], is_ours=lambda mid: False, dm_guild=GUILD)
        self.assertEqual((m.guild_id, m.channel_id, m.private), (GUILD, DM, True))
        self.assertTrue(m.addressed)


def _not_found():
    import discord

    return discord.NotFound(NS(status=404, reason="Not Found"), "Unknown Member")


def _server_error():
    import discord

    return discord.HTTPException(NS(status=500, reason="Server Error"), "boom")


class FakeGuild:
    def __init__(self, gid, name, members=(), error=None):
        self.id, self.name, self.members, self.error = gid, name, set(members), error
        self.fetched = 0

    def get_member(self, uid):
        return None  # 没开成员 intent：缓存里多半没有

    async def fetch_member(self, uid):
        self.fetched += 1
        if self.error is not None:
            raise self.error
        if uid not in self.members:
            raise _not_found()
        return NS(id=uid)


class Homes(unittest.IsolatedAsyncioTestCase):
    def make(self, guilds, store=None):
        try:
            from dsbot.discord_app import DSBot
        except ImportError:
            self.skipTest("没装 discord.py")
        from dsbot.settings import Settings
        from dsbot.config import BUILTIN_DEFAULTS

        bot = DSBot.__new__(DSBot)
        bot.store = store or Store(":memory:")
        bot.settings = Settings(dict(BUILTIN_DEFAULTS), bot.store)
        bot.engine = NS(interactions={}, moved=[])
        bot.engine.move_dm = lambda cid, gid: bot.engine.moved.append((cid, gid))
        bot.dm_channels, bot._members, bot._dm_locks, bot._dm_noticed = {}, {}, {}, {}
        by_id = {g.id: g for g in guilds}
        bot.get_guild = by_id.get
        patcher = mock.patch.object(type(bot), "guilds", new_callable=mock.PropertyMock, return_value=list(guilds))
        patcher.start()
        self.addCleanup(patcher.stop)
        return bot

    async def test_no_common_server(self):
        bot = self.make([FakeGuild(GUILD, "测试服")])
        guild, note = await bot.dm_home(NS(id=11), DM)
        self.assertIsNone(guild)
        self.assertIn("没有共同的服务器", note)

    async def test_only_one_server(self):
        bot = self.make([FakeGuild(GUILD, "测试服", {11}), FakeGuild(OTHER, "别的服")])
        guild, note = await bot.dm_home(NS(id=11), DM)
        self.assertEqual(guild.id, GUILD)
        self.assertIn("「测试服」", note)
        self.assertNotIn("/ds-dm", note)
        self.assertEqual((bot.store.dm_home(11), bot.dm_guild(DM)), ((GUILD, DM), GUILD))
        again, note = await bot.dm_home(NS(id=11), DM)
        self.assertEqual((again.id, note), (GUILD, None))  # 定下来以后不再说

    async def test_prefers_the_server_where_they_talked_recently(self):
        bot = self.make([FakeGuild(GUILD, "A服", {11}), FakeGuild(OTHER, "B服", {11})])
        bot.engine.interactions[(OTHER, 11)] = [NS(at=100.0)]
        guild, note = await bot.dm_home(NS(id=11), DM)
        self.assertEqual(guild.id, OTHER)
        self.assertIn("/ds-dm 服务器", note)

    async def test_skips_servers_that_turned_dms_off(self):
        bot = self.make([FakeGuild(GUILD, "A服", {11}), FakeGuild(OTHER, "B服", {11})])
        bot.settings.set("guild", GUILD, "dm", "关")
        guild, _ = await bot.dm_home(NS(id=11), DM)
        self.assertEqual(guild.id, OTHER)

    async def test_left_the_server_moves_and_clears(self):
        a, b = FakeGuild(GUILD, "A服", {11}), FakeGuild(OTHER, "B服", {11})
        bot = self.make([a, b])
        bot.store.set_dm_home(11, GUILD, DM)
        a.members.clear()  # 退出了 A 服
        guild, note = await bot.dm_home(NS(id=11), DM)
        self.assertEqual(guild.id, OTHER)
        self.assertEqual(bot.engine.moved, [(DM, OTHER)])
        self.assertIn("清空", note)
        self.assertIn("「A服」", note)

    async def test_membership_is_cached(self):
        a = FakeGuild(GUILD, "A服", {11})
        bot = self.make([a])
        for _ in range(3):
            await bot.dm_home(NS(id=11), DM)
        self.assertEqual(a.fetched, 1)

    async def test_discord_error_does_not_move(self):
        a = FakeGuild(GUILD, "A服", {11})
        bot = self.make([a, FakeGuild(OTHER, "B服", {11})])
        bot.store.set_dm_home(11, GUILD, DM)
        a.error = _server_error()
        self.assertEqual(await bot.dm_home(NS(id=11), DM), (None, None))
        self.assertEqual((bot.engine.moved, bot.store.dm_home(11)), ([], (GUILD, DM)))

    async def test_parallel_messages_announce_once(self):
        bot = self.make([FakeGuild(GUILD, "A服", {11})])
        results = await asyncio.gather(*(bot.dm_home(NS(id=11), DM) for _ in range(3)))
        self.assertEqual(sum(note is not None for _, note in results), 1)


if __name__ == "__main__":
    unittest.main()
