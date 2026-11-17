"""2.8.0 连通的频道，第二轮评审修掉的问题：清空记忆、断开、/ds 状态，还有没连通时服务器连组、退组附过的人设卡。

- 没连通的频道（默认模式，和 2.7.1 一样）：服务器连组、退组时人设卡挪进组里、复制回来，版本号不变，对话里附过的那份照样算，
  不再附一遍；连组以后他没有卡了，告诉它之前附过的那份删掉了。
- 在一个连通的频道里清空记忆、断开让组散了：别的频道压缩前挑出来、还在排队（或者正在问模型）的整理照样整理完，记进它们的
  服务器（原话已经压掉了，不整理就没了）；别的频道还没附上的“改成了什么”照样附上。
- 断开时等着的那会儿组里又连进来频道：照样断开，跟 bot 主人照实说办成了，说明里也列上新连进来的。
- 它自己那份在连通期间被旧版本写过（断开时拿不回连通期间的聊天）：确认框、频道里的说明、结果都照实说，不许诺拿回去。
- /ds 状态：和别的服务器的频道连通时，“能翻记录的频道”一个也不列（这里翻不了）。"""

import asyncio
import unittest
from types import SimpleNamespace as NS

import test_chatsync
from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, say, text_channel, text_of, world
from test_chatsync_lifecycle import spin
from test_chatsync_privacy import last_user
from test_chatsync_review import remembering
from test_dm import OTHER
from test_dm_memory import setup
from test_memory import ACTIONS, REPLY, chat, is_review, stores, user_text
from test_server_groups import talk

from dsbot.groups import link, unlink
from dsbot.prompts import EDIT_NOTE
from dsbot.render import CARD_DELETED

BANTER = 102  # 主服的 #杂谈（断开等着锁的时候连进来的）


class GroupChangeWithoutLinks(unittest.IsolatedAsyncioTestCase):
    """没连通的频道（默认模式）：和 2.7.1 一样。"""

    async def test_a_card_attached_before_joining_or_leaving_a_group_is_not_attached_again(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        engine.cards.put(GUILD, 11, "阿明", "自称猫娘", via="command", now=clock() - 100)
        await talk(engine, clock, 1)
        self.assertIn("自称猫娘", user_text(chats(llm)[-1]))  # 附过一次
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())  # 连组：组里没有他的卡，挪过去（版本号不变）
        engine.groups_changed([GUILD, OTHER], left=False)
        self.assertEqual(engine.cards.get(engine.store.group_of(GUILD), 11).text, "自称猫娘")
        await talk(engine, clock, 2)
        self.assertNotIn("自称猫娘", user_text(chats(llm)[-1]))  # 对话里已经有这一版：不再附
        unlink(engine.store, engine.cards, memory, GUILD, now=clock())  # 退组：复制回来，版本号也不变
        engine.groups_changed([GUILD, OTHER], left=True)
        self.assertEqual(engine.cards.get(GUILD, 11).text, "自称猫娘")
        await talk(engine, clock, 3)
        self.assertNotIn("自称猫娘", user_text(chats(llm)[-1]))

    async def test_a_card_lost_by_joining_a_group_is_called_off(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        t = clock()
        engine.cards.put(GUILD, 11, "阿明", "自称猫娘", via="command", now=t - 300)
        await talk(engine, clock, 1)
        self.assertIn("自称猫娘", user_text(chats(llm)[-1]))
        # 别的服里他的卡后来删掉了（比这里的卡晚）：合并时删除算数，连组以后组里没有他的卡
        engine.cards.put(OTHER, 11, "阿明", "别的服的卡", via="command", now=t - 200)
        engine.cards.delete(OTHER, 11, now=t - 100)
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())
        engine.groups_changed([GUILD, OTHER], left=False)
        self.assertIsNone(engine.cards.get(engine.store.group_of(GUILD), 11))
        await talk(engine, clock, 2)
        self.assertIn(CARD_DELETED, user_text(chats(llm)[-1]))  # 告诉它记录里附过的那份删掉了


class ClearingKeepsTheOthersWork(unittest.IsolatedAsyncioTestCase):
    async def compacted_while_reviewing(self):
        """主服 #闲聊、别的服 #大厅 连通；别的服说了几句以后共用的记录压缩了：压掉的那几句排队整理，正停在等模型。"""
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "别的服的约定" if "别的服" in text else "主服的约定"),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1000},
        )
        gate = asyncio.Event()

        async def hook(call):
            if is_review(call) and "别的服" in text_of(call["messages"][-1]["content"]):
                await gate.wait()

        llm.hook = hook
        connect(engine)
        for i in range(4):
            await say(engine, clock, 10 + i, f"@小D 别的服第{i}句", channel=HALL, author=21, name="小红")
        st = engine.states[HALL]
        await engine._compact(st, engine._conversation(st), transport.channel_info(HALL), force=True)
        await spin()
        self.assertTrue(any(is_review(c) for c in llm.calls))  # 别的服压掉的那几句正在等模型
        return engine, memory, gate

    async def test_clearing_one_channel_still_reviews_the_others_compacted_chat(self):
        engine, memory, gate = await self.compacted_while_reviewing()
        await engine.reset_linked(CHANNEL)  # 主服的管理员清空 #闲聊 的记忆：别的服的不该动
        gate.set()
        await engine.drain()
        # 压掉的原话已经不在记录里了：这次不整理就再也整理不到
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["别的服的约定"])

    async def test_dissolving_the_group_still_reviews_the_compacted_chat(self):
        engine, memory, gate = await self.compacted_while_reviewing()
        disconnect(engine, CHANNEL)  # 只有两个频道：组散了，共用的记录删掉
        self.assertIsNone(engine.store.sync_of(HALL))
        gate.set()
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["别的服的约定"])  # 压掉的不在记录里，拿不回去：照样整理完

    async def test_clearing_one_channel_keeps_the_others_pending_edit_note(self):
        engine, llm, transport, clock = world()
        connect(engine)
        await say(engine, clock, 50, "@小D 别的服很早的一句", channel=HALL, author=21, name="小红")
        for i in range(6):  # 记录往后走：那条成了很早的消息（被改了不改旧记录，下一轮附一句）
            await say(engine, clock, 60 + i, f"@小D 别的服后来的第{i}句", channel=HALL, author=21, name="小红")
        engine.handle_edit(msg(50, "别的服改过的内容", channel_id=HALL, guild_id=OTHER, author=21, name="小红"), edited=True)
        await engine.drain()
        await engine.reset_linked(CHANNEL)  # 别的服下一轮之前，主服的管理员清空了 #闲聊 的记忆
        await say(engine, clock, 80, "@小D 还记得我最早说的吗", channel=HALL, author=21, name="小红")
        # 别的服改的和主服无关：照样附上（不然共用的记录一直是改之前的）
        self.assertIn(EDIT_NOTE.format(sid=1, name="小红", text="别的服改过的内容"), last_user(llm))


class UnlinkCommand(unittest.IsolatedAsyncioTestCase):
    """/dsowner chatsync 断开（借 test_chatsync.OwnerCommand 的假 Discord）。"""

    setUp = test_chatsync.OwnerCommand.setUp
    interaction = test_chatsync.OwnerCommand.interaction

    async def test_unlink_is_reported_done_when_the_group_grew_while_it_waited(self):
        from dsbot.commands import SYNC_NOTICE_LEFT, SYNC_NOTICE_LEFT_REST, sync_command

        engine, transport = self.engine, self.transport
        banter = text_channel(self.bot.get_guild(GUILD), BANTER, "杂谈", 2)
        known = self.bot.get_channel
        self.bot.get_channel = lambda cid: banter if cid == BANTER else known(cid)
        gate = asyncio.Event()

        async def hook(call):  # 别的服这一轮停在等模型（拿着共用记录的锁）
            if call["json_mode"] and not is_review(call) and "别的服在问" in text_of(call["messages"][-1]["content"]):
                await gate.wait()

        engine.llm.hook = hook
        connect(engine)
        connect(engine, LOBBY, CHANNEL)  # 主服 #闲聊、别的服 #大厅、主服 #大厅
        await engine.handle_message(msg(1, "@小D 别的服在问", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        # bot 主人先确认“把 #杂谈 连进来”，再确认“让主服 #大厅 断开”：两个都等着锁，连通先办成
        ask_link, ask_unlink = self.interaction(), self.interaction()
        await sync_command(self.bot, ask_link, "link", str(BANTER), str(CHANNEL))
        await sync_command(self.bot, ask_unlink, "unlink", str(LOBBY), None)
        click_link, click_unlink = self.interaction(), self.interaction()
        linking = asyncio.create_task(ask_link.response.sent[0][1].confirm(click_link))
        await spin()
        unlinking = asyncio.create_task(ask_unlink.response.sent[0][1].confirm(click_unlink))
        await spin()
        gate.set()
        await linking
        await unlinking
        await engine.drain()
        self.assertIsNone(engine.store.sync_of(LOBBY))  # 断开办成了
        # 等锁的时候 #杂谈 连进来了
        self.assertEqual(engine.store.sync_members(engine.store.sync_of(CHANNEL)), [HALL, CHANNEL, BANTER])
        result = click_unlink.done[-1]
        self.assertIn("「主服」#大厅已经断开连通组 1", result)  # 照实说办成了（不是“出错了”）
        self.assertIn("「主服」#杂谈", result)  # 组里还有的也列上刚连进来的
        # 断开的频道收到的说明照断开时的组员写（连同刚连进来的）
        others = "「别的服」#大厅、「主服」#闲聊、「主服」#杂谈"
        left_notice = [t for c, t, _, _ in transport.sent if c == LOBBY][-1]
        self.assertEqual(left_notice, SYNC_NOTICE_LEFT.format(others=others, rest=SYNC_NOTICE_LEFT_REST))

    async def test_unlinking_a_stale_channel_does_not_promise_to_take_its_chat_back(self):
        from dsbot import commands
        from dsbot.commands import SYNC_NOTICE_LEFT, sync_command

        engine = self.engine
        await say(engine, self.clock, 1, "@小D 连通之前主服说的")
        connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        await say(engine, self.clock, 2, "@小D 连通期间主服说的")
        # 换回旧版本跑过一阵：主服自己那份在连通期间又被写过（编号往后走了），断开时拿不回连通期间的聊天
        engine.store.db.execute("UPDATE conversations SET next_short_id = next_short_id + 50 WHERE channel_id = ?", (CHANNEL,))
        self.assertEqual(engine.store.stale_syncs(), [CHANNEL])
        ask = self.interaction()
        await sync_command(self.bot, ask, "unlink", str(CHANNEL), None)
        preview = ask.response.sent[0][0]
        click = self.interaction()
        before = len(self.transport.sent)
        with self.assertLogs("dsbot.store", "WARNING"):
            await ask.response.sent[0][1].confirm(click)
        notices = {c: t for c, t, _, _ in self.transport.sent[before:]}
        own = [text_of(t.content) for t in engine.store.load_conversation(CHANNEL, GUILD).turns]
        self.assertFalse(any("连通期间主服说的" in t for t in own))  # 真的没拿回来（组散了，跟着共用的记录删掉了）
        self.assertNotIn("「主服」#闲聊把共用记录里自己这次连通期间的聊天", preview)  # 确认框不许诺拿回去
        self.assertIn("「主服」#闲聊自己那份记录在连通期间被旧版本写过", preview)  # 照实说拿不回
        stale_notice = getattr(commands, "SYNC_NOTICE_LEFT_STALE", None)  # 第二轮加的说明
        self.assertIsNotNone(stale_notice, "没有“连通期间的聊天没能拿回这里”的说明")
        self.assertEqual(notices[CHANNEL], stale_notice.format(others="「别的服」#大厅", rest=""))
        self.assertEqual(notices[HALL], SYNC_NOTICE_LEFT.format(others="「主服」#闲聊", rest=""))  # 对照：别的服照常拿回了
        self.assertIn("「主服」#闲聊自己那份记录在连通期间被旧版本写过，没拿回连通期间的聊天", click.done[-1])


class StatusCommand(unittest.IsolatedAsyncioTestCase):
    """/ds 状态（借 test_chatsync.OwnerCommand 的假 Discord）。"""

    setUp = test_chatsync.OwnerCommand.setUp
    interaction = test_chatsync.OwnerCommand.interaction

    async def test_status_lists_no_recall_sources_when_linked_across_servers(self):
        from dsbot.commands import status_text

        engine = self.engine
        engine.settings.set("guild", GUILD, "share_memory", True)
        await say(engine, self.clock, 1, "@小D 大厅这边的事", channel=LOBBY)  # 主服 #大厅：开了共享、跟它聊过
        bot = NS(**vars(self.bot), settings=engine.settings, transport=self.transport, music_problem="没装")
        here = NS(guild_id=GUILD, channel_id=CHANNEL, channel=NS(category=None, category_id=None))

        def sources():
            return next(line for line in status_text(bot, here).split("\n") if line.startswith("能翻记录的频道："))

        self.assertIn(f"<#{LOBBY}>", sources())  # 对照：没连通的频道照常列出来
        connect(engine)  # 和别的服的 #大厅 连通：跨服务器，这里翻不了别的频道
        self.assertNotIn("<#", sources())  # 一个也不列


if __name__ == "__main__":
    unittest.main()
