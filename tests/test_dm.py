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


class BotCase(unittest.IsolatedAsyncioTestCase):
    """真的 DSBot（不连 Discord）：服务器、成员都是假的。"""

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


class Homes(BotCase):
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



class FakeResponse:
    def __init__(self, sent):
        self.sent = sent
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def defer(self, ephemeral=False):
        self.deferred = ephemeral

    async def send_message(self, text, ephemeral=False):
        self.sent.append((text, ephemeral))


class DmCommands(BotCase):
    """/ds-dm：服务器里、私信里都能用（之前只在私信里出现，在服务器里打 / 找不到）。"""

    def setUp(self):
        self.a, self.b = FakeGuild(GUILD, "A服", {11}), FakeGuild(OTHER, "B服", {11})

    def commands(self, bot):
        from dsbot.commands import register_dm_commands
        from dsbot.ledger import Ledger

        added = []
        bot.tree = NS(add_command=added.append)
        bot.ledger = Ledger(bot.store, "Asia/Taipei")
        bot.secrets = NS(owner_id=OWNER)
        bot.engine.reset_channel = lambda cid: bot.engine.resets.append(cid)
        bot.engine.resets = []
        register_dm_commands(bot)
        group = added[0]
        return {name: group.get_command(name).callback for name in ("服务器", "状态", "清空")}, group

    def interaction(self, *, in_guild=None):
        self.sent = []

        async def followup(text, ephemeral=False):
            self.sent.append((text, ephemeral))

        return NS(
            user=NS(id=11),
            guild_id=in_guild,
            channel_id=CHANNEL if in_guild is not None else DM,
            response=FakeResponse(self.sent),
            followup=NS(send=followup),
        )

    async def test_shows_up_in_servers_and_dms(self):
        bot = self.make([self.a])
        _, group = self.commands(bot)
        contexts = group.allowed_contexts
        self.assertTrue(contexts.guild and contexts.dm_channel)
        installs = group.allowed_installs  # 装在自己账号上不行：不然会出现在 bot 不在的服务器里
        self.assertTrue(installs.guild)
        self.assertFalse(installs.user)

    async def test_pick_this_server_from_a_channel(self):
        bot = self.make([self.a, self.b])
        cmds, _ = self.commands(bot)
        await cmds["服务器"](self.interaction(in_guild=OTHER))  # 在 B服 里用，不填 = 这个服务器
        self.assertEqual(bot.store.dm_home(11), (OTHER, None))
        self.assertIn("「B服」", self.sent[0][0])
        self.assertIn("可能提起", self.sent[0][0])
        self.assertTrue(self.sent[0][1])  # 只有自己看得到
        self.assertEqual(bot.engine.moved, [])
        guild, note = await bot.dm_home(NS(id=11), DM)  # 之后第一次私信：就用 B服，私信里也说明一次
        self.assertEqual(guild.id, OTHER)
        self.assertEqual(bot.store.dm_home(11), (OTHER, DM))
        self.assertIn("「B服」", note)
        self.assertIn("保密", note)
        self.assertNotIn("清空", note)
        _, again = await bot.dm_home(NS(id=11), DM)
        self.assertIsNone(again)  # 只说一次

    async def test_switching_from_a_channel_clears_the_dm(self):
        bot = self.make([self.a, self.b])
        bot.store.set_dm_home(11, GUILD, DM)
        cmds, _ = self.commands(bot)
        await cmds["服务器"](self.interaction(in_guild=OTHER))
        self.assertEqual(bot.engine.moved, [(DM, OTHER)])
        self.assertEqual((bot.store.dm_home(11), bot.dm_guild(DM)), ((OTHER, DM), OTHER))
        self.assertIn("清空", self.sent[0][0])

    async def test_server_with_dms_off(self):
        bot = self.make([self.a, self.b])
        bot.settings.set("guild", OTHER, "dm", "关")
        cmds, _ = self.commands(bot)
        await cmds["服务器"](self.interaction(in_guild=OTHER))
        self.assertIn("关掉了私信", self.sent[0][0])
        self.assertIsNone(bot.store.dm_home(11))

    async def test_pick_by_name_in_the_dm(self):
        bot = self.make([self.a, self.b])
        bot.store.set_dm_home(11, GUILD, DM)
        cmds, _ = self.commands(bot)
        await cmds["服务器"](self.interaction(), target=str(OTHER))
        self.assertEqual(bot.engine.moved, [(DM, OTHER)])
        await cmds["服务器"](self.interaction())  # 私信里不填：要他选
        self.assertIn("选一个服务器", self.sent[0][0])

    async def test_status_and_clear_from_a_channel(self):
        bot = self.make([self.a])
        cmds, _ = self.commands(bot)
        await cmds["清空"](self.interaction(in_guild=GUILD))
        self.assertIn("还没私信过", self.sent[0][0])
        bot.store.set_dm_home(11, GUILD, DM)
        await cmds["状态"](self.interaction(in_guild=GUILD))
        self.assertIn("「A服」", self.sent[0][0])
        self.assertIn("$0.0000 / $0.2", self.sent[0][0])
        await cmds["清空"](self.interaction(in_guild=GUILD))
        self.assertEqual(bot.engine.resets, [DM])  # 清的是私信，不是这个频道


    async def test_this_server_without_the_bot(self):
        bot = self.make([self.a])
        cmds, _ = self.commands(bot)
        await cmds["服务器"](self.interaction(in_guild=999))
        self.assertIn("我不在这个服务器里", self.sent[0][0])
        self.assertIsNone(bot.store.dm_home(11))

    async def test_this_server_even_with_a_stale_membership_answer(self):
        # 他刚加入 B服：之前问过 Discord 时还“不在”（记了 10 分钟）。在 B服 里用命令就说明他在
        bot = self.make([self.a, self.b])
        bot._members[(OTHER, 11)] = (False, __import__("time").monotonic())
        cmds, _ = self.commands(bot)
        await cmds["服务器"](self.interaction(in_guild=OTHER))
        self.assertEqual(bot.store.dm_home(11), (OTHER, None))
        guild, _ = await bot.dm_home(NS(id=11), DM)  # 之后私信也认这个服务器，不会因为旧结果又换走
        self.assertEqual(guild.id, OTHER)
        self.assertEqual(self.b.fetched, 0)

    async def test_status_warns_when_they_left_the_server(self):
        bot = self.make([self.a, self.b])
        bot.store.set_dm_home(11, GUILD, DM)
        self.a.members.clear()
        cmds, _ = self.commands(bot)
        await cmds["状态"](self.interaction(in_guild=OTHER))
        self.assertIn("你已经不在那个服务器里了", self.sent[0][0])

    async def test_switch_waits_for_a_dm_being_handled(self):
        # 一条私信正在定服务器（在等 Discord 回答他在不在 A服）的时候，他在 B服 里换了服务器：
        # 换服务器要等那条私信处理完，不能被它用旧的结果盖回 A服
        gate = asyncio.Event()
        a = FakeGuild(GUILD, "A服", {11})
        original = a.fetch_member

        async def slow(uid):
            await gate.wait()
            return await original(uid)

        a.fetch_member = slow
        bot = self.make([a, self.b])
        bot.store.set_dm_home(11, GUILD, DM)
        first = asyncio.create_task(bot.dm_home(NS(id=11), DM))
        await asyncio.sleep(0)
        switch = asyncio.create_task(bot.set_dm_home(11, self.b, None))
        await asyncio.sleep(0)
        self.assertFalse(switch.done())  # 在等那条私信
        gate.set()
        await asyncio.gather(first, switch)
        self.assertEqual((bot.store.dm_home(11), bot.dm_guild(DM)), ((OTHER, DM), OTHER))
        self.assertEqual(bot.engine.moved, [(DM, OTHER)])


    async def test_naming_this_server_with_a_stale_membership_answer(self):
        bot = self.make([self.a, self.b])
        bot._members[(OTHER, 11)] = (False, __import__("time").monotonic())
        cmds, group = self.commands(bot)
        choices = await group.get_command("服务器")._params["target"].autocomplete(self.interaction(in_guild=OTHER), "")
        self.assertIn(str(OTHER), [c.value for c in choices])  # 输入框里列得出这个服务器
        await cmds["服务器"](self.interaction(in_guild=OTHER), target="B服")
        self.assertEqual(bot.store.dm_home(11), (OTHER, None))

    async def test_pick_by_id_checks_that_server(self):
        bot = self.make([self.a, self.b])
        cmds, _ = self.commands(bot)
        await cmds["服务器"](self.interaction(in_guild=GUILD), target=str(OTHER))
        self.assertEqual(bot.store.dm_home(11), (OTHER, None))
        self.b.members.clear()
        bot._members.clear()
        await cmds["服务器"](self.interaction(in_guild=GUILD), target=str(OTHER))
        self.assertIn("找不到这个服务器", self.sent[0][0])
        self.b.members.add(11)
        bot._members.clear()
        self.b.error = _server_error()
        await cmds["服务器"](self.interaction(in_guild=GUILD), target=str(OTHER))
        self.assertIn("过一会儿再试", self.sent[0][0])

    async def test_no_history_means_nothing_cleared(self):
        # 在服务器里定了 A服，还没私信过，又在私信里换成 B服：没有私信记录可清
        bot = self.make([self.a, self.b])
        cmds, _ = self.commands(bot)
        await cmds["服务器"](self.interaction(in_guild=GUILD))
        await cmds["服务器"](self.interaction(), target=str(OTHER))
        self.assertNotIn("清空", self.sent[0][0])
        self.assertEqual(bot.engine.moved, [])
        self.assertEqual(bot.store.dm_home(11), (OTHER, DM))

    async def test_first_dm_after_the_chosen_server_became_unusable(self):
        bot = self.make([self.a, self.b])
        bot.store.set_dm_home(11, OTHER, None)  # 用命令定了 B服，还没私信过
        self.b.members.clear()
        guild, note = await bot.dm_home(NS(id=11), DM)
        self.assertEqual(guild.id, GUILD)
        self.assertIn("「B服」那边私信用不了了", note)
        self.assertNotIn("清空", note)
        self.assertIn("保密", note)
        self.assertEqual(bot.engine.moved, [])

    async def test_status_when_the_home_is_gone(self):
        bot = self.make([self.a, self.b])
        cmds, _ = self.commands(bot)
        bot.store.set_dm_home(11, 999, DM)  # bot 已经不在那个服务器了
        await cmds["状态"](self.interaction(in_guild=GUILD))
        self.assertIn("它已经不在", self.sent[0][0])
        self.assertIn("会改用「A服」，私信记录会清空", self.sent[0][0])
        bot.store.set_dm_home(11, GUILD, DM)
        self.a.members.clear()
        self.b.members.clear()
        bot._members.clear()  # 刚才在 A服 里用过命令，那时他还在
        await cmds["状态"](self.interaction(in_guild=None))
        self.assertIn("你已经不在那个服务器里了", self.sent[0][0])
        self.assertIn("私信暂时聊不了", self.sent[0][0])


class Switching(unittest.IsolatedAsyncioTestCase):
    async def test_engine_follows_a_switch_it_missed(self):
        # 引擎里这段私信还挂在原来的服务器（比如换的时候它还没载入）：新来的消息说是 B服，就按换了服务器处理
        engine, llm, transport, clock = dm_engine()
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        transport.places[DM] = ChannelPlace(guild_id=OTHER, name="私信", dm=True)
        await engine.handle_message(dm(2, "换了", guild_id=OTHER))
        await engine.drain()
        self.assertEqual(engine.states[DM].guild_id, OTHER)
        self.assertNotIn("在吗", json.dumps(chats(llm)[-1]["messages"], ensure_ascii=False))

    async def test_no_memory_writes_after_a_switch_mid_round(self):
        # 一轮里要记两条，记第一条加 📝 的时候他换了服务器：第二条不能写进新的服务器
        cards, memory = stores()
        out = ACTIONS({"type": "memory", "op": "add", "text": "周五打副本"}, {"type": "memory", "op": "add", "text": "周六团建"}, REPLY(1, "记下了"))
        engine, llm, transport, clock = dm_engine(lambda call: out, cards=cards, memory=memory)

        async def react(channel_id, message_id, emoji):
            transport.reactions.append((channel_id, message_id, emoji))
            if emoji == "📝":
                engine.move_dm(DM, OTHER)
            return True

        transport.react = react
        await engine.handle_message(dm(1, "帮我记住：周五打副本，周六团建"))
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["周五打副本"])
        self.assertEqual(memory.entries(OTHER), [])

    async def test_clearing_the_record_mid_round_keeps_the_memory_writes(self):
        # 只是清空聊天记录（没换服务器）：长期记忆不受影响，这一轮要记的照样记全
        cards, memory = stores()
        out = ACTIONS({"type": "memory", "op": "add", "text": "周五打副本"}, {"type": "memory", "op": "add", "text": "周六团建"}, REPLY(1, "记下了"))
        engine, llm, transport, clock = dm_engine(lambda call: out, cards=cards, memory=memory)

        async def react(channel_id, message_id, emoji):
            if emoji == "📝":
                engine.reset_channel(DM)
            return True

        transport.react = react
        await engine.handle_message(dm(1, "帮我记住：周五打副本，周六团建"))
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["周五打副本", "周六团建"])

    async def test_reply_written_for_the_old_server_is_not_sent(self):
        engine, llm, transport, clock = dm_engine()
        sleep = engine.sleep

        async def typing_then_switch(seconds):
            await sleep(seconds)
            if chats(llm):  # 问完模型、假装打字的时候换了服务器
                engine.move_dm(DM, OTHER)

        engine.sleep = typing_then_switch
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertEqual(transport.sent, [])

    async def test_unanswered_dms_survive_a_switch(self):
        engine, llm, transport, clock = dm_engine()
        await engine.handle_message(dm(1, "在吗"))  # 还在等他说完，就换了服务器
        transport.places[DM] = ChannelPlace(guild_id=OTHER, name="私信", dm=True)
        engine.move_dm(DM, OTHER)
        await engine.drain()
        self.assertEqual(len(chats(llm)), 1)
        self.assertIn("在吗", user_text(chats(llm)[0]))
        self.assertEqual(engine.states[DM].guild_id, OTHER)
        self.assertEqual(engine.store.conversation_channels(OTHER), [DM])



class TransportDM(unittest.IsolatedAsyncioTestCase):
    """discord.py 不把私信频道放进缓存：“正在输入”、发消息、对方的名字都不能靠 get_channel。"""

    def make(self):
        try:
            import discord

            from dsbot.discord_app import DiscordTransport
        except ImportError:
            self.skipTest("没装 discord.py")
        typed, made = [], []

        class Partial:
            def __init__(self, cid):
                self.id, self.guild = cid, None

            def typing(self):
                outer = self

                class Ctx:
                    async def __aenter__(self):
                        typed.append(outer.id)

                    async def __aexit__(self, *exc):
                        return False

                return Ctx()

        def partial(cid, type=None):
            made.append((cid, type))
            return Partial(cid)

        guild = NS(id=GUILD, name="测试服", me=NS(display_name="小D"), emojis=[], stickers=[], get_member=lambda uid: None)
        client = NS(
            get_channel=lambda cid: None,  # 私信频道不在缓存里
            get_partial_messageable=partial,
            get_guild=lambda gid: guild if gid == GUILD else None,
            user=NS(display_name="小D"),
            dm_channels={DM: (11, GUILD)},
            dm_names={DM: "阿明"},
        )
        client.dm_guild = lambda cid: client.dm_channels.get(cid, (None, None))[1]
        client.dm_user = lambda cid: None  # 用户缓存里没有了
        return DiscordTransport(client), typed, made, discord

    async def test_typing_shows_in_dms(self):
        transport, typed, made, discord = self.make()
        async with transport.typing(DM):
            pass
        self.assertEqual(typed, [DM])
        self.assertEqual(made, [(DM, discord.ChannelType.private)])
        async with transport.typing(CHANNEL):  # 不是私信、又不在缓存里：不显示，也不出错
            pass
        self.assertEqual(typed, [DM])

    async def test_dm_partner_name_without_the_user_cache(self):
        transport, *_ = self.make()
        info = transport.channel_info(DM)
        self.assertEqual((info.dm_with, info.guild_name, info.channel_name), ("阿明", "测试服", "私信"))


if __name__ == "__main__":
    unittest.main()
