"""2.13.0 私信记忆的第三类：可共享。

- 本人用 /ds-dm 记忆 操作:设为可共享 标的（只有本人能标；敏感的标不了）。
- 标了以后，他私信挂的那个服务器（和它连成一组的服务器）的频道里，它看得到「谁：什么事」，主动说话、冒泡时可以自己提起；
  不用等他叫它。别的服务器、私信里、没开私信记忆的服务器不列。
- 取消共享、改成敏感、删掉、它改了那条的内容：下一轮就不列了（不写进频道记录，没有要撤回的）。"""

import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, msg
from test_dm import DM, OTHER, BotCase, FakeGuild, FakeResponse, chats, dm
from test_dm_memory import HALL, setup
from test_memory import ACTIONS, REPLY, chat, system_text

from dsbot.memory import MemoryStore
from dsbot.prompts import DM_MEMORY_BLOCK, SHARED_NOTES_BLOCK

HEAD = "## 群友愿意让这里知道的事"


class Store(unittest.TestCase):
    def test_share_and_unshare(self):
        memory = MemoryStore(":memory:")
        a = memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat", now=100.0)
        b = memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat", now=101.0)
        self.assertTrue(memory.dm_share(11, a, "阿明"))
        self.assertFalse(memory.dm_share(11, b, "阿明"))  # 敏感的标不了
        self.assertFalse(memory.dm_share(11, 99, "阿明"))
        self.assertEqual([(u, e.text, e.shared_as) for u, e in memory.dm_shared()], [(11, "最近在学吉他", "阿明")])
        self.assertEqual(memory.dm_get(11, a).updated_at, 100.0)  # 标一下不算改了内容
        self.assertTrue(memory.dm_share(11, a, None))
        self.assertEqual(memory.dm_shared(), [])

    def test_sensitive_or_changed_text_drops_the_mark(self):
        memory = MemoryStore(":memory:")
        a = memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_share(11, a, "阿明")
        memory.dm_edit(11, a, None, sensitive=True, via="command")  # 改成敏感：不再共享
        self.assertIsNone(memory.dm_get(11, a).shared_as)
        memory.dm_edit(11, a, None, sensitive=False, via="command")
        self.assertIsNone(memory.dm_get(11, a).shared_as)  # 改回不敏感也不会自己变回可共享
        memory.dm_share(11, a, "阿明")
        memory.dm_edit(11, a, "最近在学吉他，也在学贝斯", via="review")  # 它整理时改了内容：要本人再标一次
        self.assertIsNone(memory.dm_get(11, a).shared_as)
        memory.dm_share(11, a, "阿明")
        memory.db.execute("UPDATE dm_memories SET sensitive = 1 WHERE user_id = 11 AND no = ?", (a,))  # 万一两个标记同时在
        self.assertEqual(memory.dm_shared(), [])  # 敏感的无论如何不列
        memory.db.execute("UPDATE dm_memories SET sensitive = 0 WHERE user_id = 11 AND no = ?", (a,))
        self.assertEqual(len(memory.dm_shared()), 1)
        memory.dm_delete(11, a)
        self.assertEqual(memory.dm_shared(), [])

    def test_old_database_gets_the_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.sqlite3")
            first = MemoryStore(path)
            first.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
            first.close()
            db = sqlite3.connect(path)
            db.execute("ALTER TABLE dm_memories DROP COLUMN shared_as")  # 2.12.0 以前建的表没有这一列
            db.commit()
            db.close()
            again = MemoryStore(path)
            self.assertEqual([(e.text, e.shared_as) for e in again.dm_entries(11)], [("最近在学吉他", None)])
            self.assertTrue(again.dm_share(11, 1, "阿明"))
            again.close()


class InServers(unittest.IsolatedAsyncioTestCase):
    async def ask(self, engine, llm, mid=1, **kw):
        await engine.handle_message(msg(mid, "@小D 在吗", mentions_bot=True, author=12, name="小雨", **kw))
        await engine.drain()
        return system_text(chats(llm)[-1])

    def notes(self, system):
        return system.split(HEAD)[1].split("\n## ")[0] if HEAD in system else ""

    async def test_listed_in_the_home_server_for_everyone(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))] * 6))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_add(11, "养了一只叫年糕的猫", sensitive=False, via="chat")
        memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")
        system = await self.ask(engine, llm)  # 别人（小雨）叫它：什么都没标的时候没有这一节
        self.assertNotIn(HEAD, system)
        memory.dm_share(11, 1, "阿明")
        clock.advance(60)
        system = await self.ask(engine, llm, 2)
        self.assertIn("- 阿明：最近在学吉他", self.notes(system))
        self.assertNotIn("年糕", system)  # 没标的不列（不敏感的只在他自己叫它时带）
        self.assertNotIn("手术", system)
        self.assertIn("别说是从私信里知道的", self.notes(system))
        memory.dm_share(11, 1, None)  # 取消：下一轮就没有了
        clock.advance(60)
        self.assertNotIn(HEAD, await self.ask(engine, llm, 3))

    async def test_only_where_their_dms_live(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))] * 6))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_share(11, 1, "阿明")
        engine.settings.set("guild", OTHER, "cross_server", True)
        system = await self.ask(engine, llm, 1, channel_id=HALL, guild_id=OTHER)
        self.assertNotIn(HEAD, system)  # 别的服务器：开了跨服务器也不列（那只管他自己叫它时带的）
        engine.store.set_dm_home(11, OTHER, DM)  # 私信改挂到那边
        clock.advance(60)
        self.assertIn("- 阿明：最近在学吉他", await self.ask(engine, llm, 2, channel_id=HALL, guild_id=OTHER))
        clock.advance(60)
        self.assertNotIn(HEAD, await self.ask(engine, llm, 3))  # 原来的服务器不再列

    async def test_not_when_dm_memory_is_off_or_capped_to_zero(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))] * 6))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_share(11, 1, "阿明")
        engine.settings.set("guild", GUILD, "dm_memory", False)
        self.assertNotIn(HEAD, await self.ask(engine, llm, 1))
        engine.settings.set("guild", GUILD, "dm_memory", True)
        engine.ec.dm_shared_prompt_chars = 0
        clock.advance(60)
        self.assertNotIn(HEAD, await self.ask(engine, llm, 2))

    async def test_newest_within_the_cap_and_sorted(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))] * 6), engine_cfg={"dm_shared_prompt_chars": 12})
        engine.store.set_dm_home(13, GUILD, DM + 1)
        memory.dm_add(11, "旧的那件事情", sensitive=False, via="chat", now=100.0)
        memory.dm_add(11, "新的这件事情", sensitive=False, via="chat", now=300.0)
        memory.dm_add(13, "阿凯的一件事", sensitive=False, via="chat", now=200.0)
        memory.dm_share(11, 1, "阿明")
        memory.dm_share(11, 2, "阿明")
        memory.dm_share(13, 1, "阿凯")
        notes = self.notes(await self.ask(engine, llm))
        self.assertIn("- 阿凯：阿凯的一件事\n- 阿明：新的这件事情", notes)  # 最近改过的两条，按名字排
        self.assertNotIn("旧的那件事情", notes)

    async def test_also_in_a_bubble(self):
        from test_bubble import HOUR, Fixed

        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(None, "阿明吉他练得怎么样了"))]))
        engine.rng = Fixed()
        Fixed.roll = 0.9
        self.addCleanup(setattr, Fixed, "roll", 0.0)
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_share(11, 1, "阿明")
        engine.settings.set("channel", CHANNEL, "bubble", "中")
        clock.advance(14 * HOUR)
        await engine.handle_message(msg(501, "大家好", author=12, name="小雨"))
        clock.advance(4 * HOUR + 1)
        await engine.tick()
        await engine.drain()
        self.assertIn("- 阿明：最近在学吉他", system_text(chats(llm)[-1]))  # 冒泡找话题时也看得到
        self.assertEqual(transport.sent[-1][1], "阿明吉他练得怎么样了")

    async def test_dm_shows_the_mark_and_who_may_set_it(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")
        memory.dm_share(11, 1, "阿明")
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        system = system_text(chats(llm)[-1])
        self.assertIn("(1) 最近在学吉他〔可共享〕\n(2) 下个月要做手术〔敏感〕", system)
        self.assertIn("这个标记只有他本人能改", system)
        self.assertNotIn(HEAD, system)  # 私信里不另外列
        self.assertIn("/ds-dm 记忆", DM_MEMORY_BLOCK)
        self.assertIn("{entries}", SHARED_NOTES_BLOCK)


class Commands(BotCase):
    def commands(self, bot):
        from dsbot.commands import register_dm_commands
        from dsbot.ledger import Ledger

        added = []
        bot.tree = NS(add_command=added.append)
        bot.ledger = Ledger(bot.store, "Asia/Taipei")
        bot.secrets = NS(owner_id=0)
        bot.memory = MemoryStore(":memory:")
        bot.cfg = NS(engine=NS(dm_memory_max_entries=30))
        register_dm_commands(bot)
        return added[0].get_command("记忆").callback, added[0].get_command("状态").callback

    def interaction(self):
        self.sent = []

        async def followup(text, ephemeral=False):
            self.sent.append((text, ephemeral))

        return NS(user=NS(id=11, display_name="阿明"), guild_id=None, channel_id=DM, response=FakeResponse(self.sent), followup=NS(send=followup))

    async def test_share_unshare_and_listing(self):
        bot = self.make([FakeGuild(GUILD, "A服", {11})])
        command, status = self.commands(bot)
        bot.store.set_dm_home(11, GUILD, DM)
        bot.memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        bot.memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")
        await command(self.interaction(), NS(value="share"), 1)
        self.assertIn("第 1 条设成了可共享：最近在学吉他", self.sent[0][0])
        self.assertIn("那里的人都看得到", self.sent[0][0])
        self.assertTrue(self.sent[0][1])
        self.assertEqual(bot.memory.dm_get(11, 1).shared_as, "阿明")
        await command(self.interaction(), NS(value="share"), 2)
        self.assertIn("是敏感的，不能共享", self.sent[0][0])
        self.assertIsNone(bot.memory.dm_get(11, 2).shared_as)
        await command(self.interaction(), NS(value="share"), 9)
        self.assertIn("没有第 9 条", self.sent[0][0])
        await command(self.interaction())
        listing = self.sent[0][0]
        self.assertIn("(1) 最近在学吉他〔可共享〕", listing)
        self.assertIn("(2) 下个月要做手术〔敏感〕", listing)
        self.assertIn("操作:设为可共享", listing)
        await status(self.interaction())
        self.assertIn("私信记忆：2 条（1 条敏感，1 条可共享）", self.sent[0][0])
        await command(self.interaction(), NS(value="private"), 1)  # 改成敏感：也就不共享了
        self.assertIsNone(bot.memory.dm_get(11, 1).shared_as)
        await command(self.interaction(), NS(value="public"), 1)
        await command(self.interaction(), NS(value="share"), 1)
        await command(self.interaction(), NS(value="unshare"), 1)
        self.assertIn("第 1 条不再共享", self.sent[0][0])
        self.assertEqual(bot.memory.dm_shared(), [])
