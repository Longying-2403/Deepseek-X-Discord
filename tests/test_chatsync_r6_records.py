"""2.8.0 连通的频道，第六轮评审修掉的问题：组散了以后的删除、整理记忆排着队的那几批、清空，和连通、断开、清空时等的锁。

- 组散了，压缩前挑出来、交给不在组里的一份接着整理的那批：散了以后才删的消息也认得出，不拿删掉的消息写人设卡。
- 频道自己那份安静时整理正在问的那一段，问的时候压缩掉了一部分（原话只在那一段里了）：这时候频道连进组又断开（或者组散了），
  问到一半的那次作废，压掉的那几条交给新的那份整理，只记一次；那一段已经问完了的话，压掉的那几条不再交出去（不然记两次）。
- 频道自己那份压缩前挑出来的那批：断开时交给新的那份的，只看每一段自己整理过没有，不看整理进度（又压缩了一次、那个服务器今天
  的预算用完了，整理进度就记过了它，它却还没整理过）。
- 清空一个频道的记忆：每份记录（组还在的也算）排着队的、正在整理的那几批里它的那几段都不整理了，不靠组的摘要记着它（组里别的
  频道清空时摘要整个删了）；正在问的那一段问完也不照着改。别的频道的那几段照样整理完。
- 连通、断开、清空等锁：拿到以后照现在的情况再算一遍要等哪几份记录（等的时候这几个频道连通、断开了，有组的记录里留下了它说的），
  变了就放开、照现在的重新等，不在没拿着锁的记录上动手（那里正在跑的一轮不会被作废，跨服务器的记录里不会留下只该原来那边看的
  人设卡）；一直在变就不办（TimeoutError，什么都不改）。没写进去的删改照现在用的那几份记录写进去（等的时候换成了新的状态的，
  要改的在新的那份上）。
- /dsadmin 清空记忆 等完以后，照那时的情况说这个频道还连通不连通。"""

import asyncio
import json
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from helpers import CHANNEL, GUILD, OWNER, msg
from test_chatsync import HALL, LOBBY, connect, disconnect, replies, say, text_of, world
from test_chatsync_lifecycle import Deferred, answering, hold, sent, spin
from test_chatsync_privacy import history_of, record
from test_chatsync_r2_records import compact, hold_reviews, times_reviewed
from test_chatsync_r2_review import quiet, summarized
from test_chatsync_r3_review import noted_each_time, say_in_order
from test_chatsync_records import three_linked
from test_chatsync_review import remembering
from test_dm import OTHER
from test_memory import is_review, stores

from dsbot.models import ChannelPlace

HEAVY = "很长的旧事" * 300  # 比后面几轮加起来还长：压缩正好压掉它那一轮（连同它前面的）
LONG = "\n".join(["很长的旧事" * 100] * 3)  # 整理时三行各截成 500 字：一次最多给它看 2000 字的话，一轮正好是一段
LONGER = "\n".join(["很长的旧事" * 300] * 3)  # 整理时一样截短（也是一段）；压缩时比 LONG 重得多
CARD = "主服的阿明：住在幸福路三号"  # 阿明在主服的人设卡（人设卡各服务器一张）
HER_CARD = "小红说她家住在幸福路三号"  # 整理时照小红那条消息给她写的卡
SQUARE = 703  # 别的服 #广场
GARDEN = 704  # 别的服 #花园


def notes(table):
    """整理记忆时，给它看的聊天里有 table 的键就记那一条长期记忆（没有就不记）。"""
    return remembering(lambda text: next((note for words, note in table.items() if words in text), None))


def writing_her_card():
    """整理到小红说“我家住在幸福路三号”的那一段：照那条消息的编号给她写人设卡；聊天、压缩摘要照 replies() 回。"""
    others = replies()

    def responder(call):
        if is_review(call):
            hit = re.search(r"#(\d+) 小红：[^\n]*我家住在幸福路三号", text_of(call["messages"][-1]["content"]))
            cards = [{"to": int(hit.group(1)), "text": HER_CARD}] if hit else []
            return json.dumps({"cards": cards, "memory": []}, ensure_ascii=False)
        return others(call)

    return responder


def hold_reviews_on(llm, gates):
    """整理记忆问模型时，给它看的聊天里有 gates 的键：那一段停在那里等那个 Event（“模型还在想”）。"""

    async def hook(call):
        if is_review(call):
            text = text_of(call["messages"][-1]["content"])
            for words, gate in gates.items():
                if words in text:
                    await gate.wait()
                    return

    llm.hook = hook


def on_other_server(transport, *channels):
    """别的服里另外几个公开频道：(频道, 名字)。"""
    for cid, name in channels:
        transport.places[cid] = ChannelPlace(guild_id=OTHER, name=name, public=True)
        transport.names[cid] = name


async def link_like_the_command(engine, channel, to):
    """像 /dsowner chatsync 连通（do_sync_link）那样：要等哪几个频道在点确认的时候就算好了（这两个频道，连同 to 所在的组），
    等它们正在跑的一轮跑完再连、换记录。返回连通组。"""
    before = engine.store.sync_of(to) or engine.store.sync_of(channel)
    involved = [channel, to] + (engine.store.sync_members(before) if before is not None else [])
    async with engine.sync_guard(involved):
        return connect(engine, channel, to)


async def unlink_like_the_command(engine, channel):
    """像 /dsowner chatsync 断开（do_sync_unlink）那样：先等组里每个频道正在跑的一轮跑完，再断开、换记录。"""
    async with engine.sync_guard(engine.store.sync_members(engine.store.sync_of(channel))):
        return disconnect(engine, channel)


class WithCommands(unittest.IsolatedAsyncioTestCase):
    """用到斜杠命令的测试：没装 discord.py 就跳过（在事件循环外面先 import 好）。"""

    def setUp(self):
        try:
            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")


def clear_command(engine):
    """/dsadmin 清空记忆（commands.reset_cmd，像 test_chatsync_lifecycle.ClearCommand 那样注册）：返回 run(频道) -> 它最后回的那句。"""
    from dsbot.commands import register_commands

    added = []
    tree = NS(add_command=lambda c, **kw: added.append(c), command=lambda **kw: (lambda f: f), error=lambda f: f)
    bot = NS(store=engine.store, engine=engine, settings=engine.settings, cfg=engine.cfg, cards=None, memory=None, tree=tree)
    bot.secrets = NS(owner_id=OWNER, control_guild_ids=())
    register_commands(bot)
    callback = next(c for c in added if c.name == "dsadmin").get_command("清空记忆").callback

    async def run(channel):
        response = Deferred()

        async def followup(text, ephemeral=False):
            response.said.append(text)

        guild = engine.transport.locate(channel).guild_id
        await callback(NS(channel_id=channel, guild_id=guild, user=NS(id=OWNER), response=response, followup=NS(send=followup)))
        return response.said[-1]

    return run


class DeletesAfterTheGroupDissolved(unittest.IsolatedAsyncioTestCase):
    async def test_a_message_deleted_while_the_dissolved_groups_review_asks_is_not_written_into_her_card(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(writing_her_card(), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1000})
        connect(engine)  # 主服 #闲聊 + 别的服 #大厅：断开一个，组就散了
        await say(engine, clock, 1, f"@小D 我家住在幸福路三号{HEAVY}", channel=HALL, author=21, name="小红")
        for i in range(3):
            await say(engine, clock, 2 + i, f"@小D 主服最近{i}")
        gate = asyncio.Event()
        hold_reviews(llm, "我家住在幸福路三号", gate)  # 整理小红那一段的时候模型回得慢
        await compact(engine, CHANNEL)  # 压掉小红那一轮：挑出来一批，正在问
        await spin()
        self.assertEqual(disconnect(engine, CHANNEL), ([CHANNEL, HALL], []))  # 组散了：那批交给不在组里的一份接着整理
        await spin()
        self.assertEqual(times_reviewed(llm, "我家住在幸福路三号"), 2)  # 对照：接着整理的那份又在问那一段
        engine.handle_delete(OTHER, HALL, [1])  # 这时候小红删了那条
        gate.set()
        await engine.drain()
        llm.hook = None
        self.assertIsNone(cards.get(OTHER, 21))  # 回来的结果是照她删掉的那条写的卡：不写


class OwnQuietReviewCompactedWhileAsking(unittest.IsolatedAsyncioTestCase):
    async def test_the_compacted_part_of_the_chunk_being_asked_is_reviewed_once_after_linking_and_unlinking(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(noted_each_time("我养了一只猫", "小红养了一只猫")),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        connect(engine, LOBBY, CHANNEL)  # 主服 #闲聊 + #大厅：别的服 #大厅连进来又断开，组还在
        await say_in_order(engine, clock, [(HALL, f"我养了一只猫{HEAVY}")] + [(HALL, f"别的服闲聊{i}") for i in range(3)])
        gate = asyncio.Event()
        hold_reviews(llm, "我养了一只猫", gate)
        clock.advance(700)  # 别的服 #大厅（还没连通）安静了一阵：整理它自己那份，只有一段，猫那一轮在最前面
        await engine.tick()
        await spin()
        self.assertEqual(times_reviewed(llm, "我养了一只猫"), 1)  # 正在问模型
        await compact(engine, HALL)  # 这时候它那份压缩了：猫那一轮压掉了，原话只在正在问的那一段里
        self.assertIn("我养了一只猫", summarized(llm))
        connect(engine, HALL, CHANNEL)  # bot 主人把别的服 #大厅连进组里……
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))  # ……又断开了：它自己那份换成新的状态
        await spin()
        gate.set()
        await engine.drain()
        llm.hook = None
        await quiet(engine, clock, times=3)
        # 问到一半的那次（第 1 次）作废；压掉的那几条交给新的那份再问（第 2 次）：猫没丢，也只记一次
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第2次问到）"])

    async def test_control_the_compacted_part_of_a_chunk_already_answered_is_not_handed_over(self):
        # 对照（control）：问完了的那一段里压掉的那几条，断开时不再交给新的那份（不然记两次）；问到一半的下一段照样作废，
        # 新的那份再问一次
        cards, memory = stores()
        cat, sea = noted_each_time("我养了一只猫", "小红养了一只猫"), noted_each_time("我家住在海边", "小红家住在海边")
        engine, llm, transport, clock = world(
            remembering(lambda text: cat(text) or sea(text)),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1, "review_max_chars": 2000},  # 一次给它看最多 2000 字：两轮长的各是一段
        )
        connect(engine, LOBBY, CHANNEL)  # 主服 #闲聊 + #大厅：别的服 #大厅连进来又断开，组还在
        rounds = [(HALL, f"我养了一只猫\n{LONGER}"), (HALL, f"我家住在海边\n{LONG}")] + [(HALL, f"别的服闲聊{i}") for i in range(3)]
        await say_in_order(engine, clock, rounds)
        gates = {"我养了一只猫": asyncio.Event(), "我家住在海边": asyncio.Event()}
        hold_reviews_on(llm, gates)
        clock.advance(700)
        await engine.tick()
        await spin()  # 安静时整理它自己那份：正在问第一段（猫）
        await compact(engine, HALL)  # 压掉的只有猫那一轮（它最重）：在正在问的那一段里
        self.assertNotIn("我家住在海边", summarized(llm))
        gates["我养了一只猫"].set()
        await spin()  # 第一段问完、记下了；接着问第二段（海边）
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第1次问到）"])
        connect(engine, HALL, CHANNEL)
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
        gates["我家住在海边"].set()
        await engine.drain()
        llm.hook = None
        await quiet(engine, clock, times=3)
        self.assertEqual(times_reviewed(llm, "我养了一只猫"), 1)  # 压掉的那几条没交给新的那份再问
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第1次问到）", "小红家住在海边（第2次问到）"])


class OwnCompactionReviewWhenLeaving(unittest.IsolatedAsyncioTestCase):
    async def test_a_chunk_is_handed_over_even_if_another_compaction_moved_the_progress_past_it(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(noted_each_time("我养了一只猫", "小红养了一只猫")),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1000},  # 不做安静时整理：只有压缩前挑出来的那批在整理
        )
        connect(engine, LOBBY, CHANNEL)  # 主服 #闲聊 + #大厅：别的服 #大厅连进来又断开，组还在
        rounds = [(HALL, f"我养了一只猫{HEAVY}")] + [(HALL, f"别的服闲聊{i}{HEAVY if i == 1 else ''}") for i in range(5)]
        await say_in_order(engine, clock, rounds)
        gate = asyncio.Event()
        hold_reviews(llm, "我养了一只猫", gate)
        await compact(engine, HALL)  # 别的服 #大厅还没连通：压掉猫那一轮，挑出来一批（一段），正在问
        await spin()
        engine.ledger.add(OTHER, 1000.0, clock())  # 别的服今天的预算用完了
        await compact(engine, HALL)  # 又压缩了一次：压掉的那部分整理不了，整理进度直接记过去
        self.assertGreater(engine.store.load_conversation(HALL, OTHER).reviewed_upto, 2)  # 对照：记过了猫那一段（记录 1、2）
        connect(engine, HALL, CHANNEL)  # 正在问猫那一段的时候，别的服 #大厅连进组里又断开了
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
        await spin()
        gate.set()
        await engine.drain()
        llm.hook = None
        # 问到一半的那次（第 1 次）作废；那一段还没整理过，交给新的那份再问（第 2 次）：猫没丢
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第2次问到）"])


class ClearingWhatTheGroupsReviewStillHolds(WithCommands):
    async def compacted_then_left(self, words):
        """主服 #闲聊、别的服 #大厅、主服 #大厅 连成一组。阿明在 #闲聊、小红在别的服 #大厅各说了一段很长的旧事（小红说她养了
        一只猫），压缩把两段都压掉了：挑出来一批，两段（主服的在前，小红的在后），问到有 words 的那段时模型回得慢。别的服 #大厅
        断开，组还在（小红说的都压掉了：只有组的摘要还记着她）。返回 (engine, llm, memory, gate, 清空)。"""
        cards, memory = stores()
        engine, llm, transport, clock = world(
            notes({"主服旧事": "阿明说过主服旧事", "我养了一只猫": "小红养了一只猫"}),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1000},  # 不做安静时整理：只有压缩前挑出来的那批在整理
        )
        clear = clear_command(engine)
        sync = three_linked(engine)
        rounds = [(CHANNEL, f"主服旧事{HEAVY}"), (HALL, f"我养了一只猫{HEAVY}")] + [(CHANNEL, f"主服最近{i}") for i in range(3)]
        await say_in_order(engine, clock, rounds)
        gate = asyncio.Event()
        hold_reviews(llm, words, gate)
        await compact(engine, CHANNEL)
        self.assertNotIn(HALL, {t.origin for t in engine.store.load_conversation(sync, 0).turns})  # 小红说的都压掉了
        await spin()
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
        self.assertEqual(engine.store.origin_syncs(HALL), [sync])  # 组的摘要里有它的那部分
        return engine, llm, memory, gate, clear

    async def test_its_chunk_queued_in_the_groups_review_is_not_reviewed_after_it_clears(self):
        engine, llm, memory, gate, clear = await self.compacted_then_left("主服旧事")  # 正在问主服那段
        await clear(LOBBY)  # 主服 #大厅 /dsadmin 清空记忆：组的摘要整个删了（摘要里有别的服 #大厅的那部分也就不记了）
        self.assertEqual(engine.store.origin_syncs(HALL), [])  # 对照：看不出组里还留着它的聊天
        await clear(HALL)  # 别的服 #大厅 /dsadmin 清空记忆：直接清它自己那份
        gate.set()
        await engine.drain()
        llm.hook = None
        self.assertEqual(times_reviewed(llm, "我养了一只猫"), 0)  # 那批里它那段不再拿去问模型
        self.assertEqual(memory.entries(OTHER), [])  # 也没记进别的服的长期记忆
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明说过主服旧事"])  # 主服那段照样整理完

    async def test_its_chunk_being_asked_when_it_clears_is_not_applied(self):
        engine, llm, memory, gate, clear = await self.compacted_then_left("我养了一只猫")  # 主服那段问完了，正在问小红那段
        await clear(LOBBY)  # 那批交给组的新的那份接着整理
        await spin()
        self.assertEqual(times_reviewed(llm, "我养了一只猫"), 2)  # 对照：新的那份又在问小红那段
        await clear(HALL)  # 问着的时候别的服 #大厅 /dsadmin 清空记忆
        gate.set()
        await engine.drain()
        llm.hook = None
        self.assertEqual(memory.entries(OTHER), [])  # 清空以后才回来的那次不照着改
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明说过主服旧事"])


class GuardsRecheckAfterWaiting(unittest.IsolatedAsyncioTestCase):
    async def test_an_unlink_queued_behind_a_clear_writes_the_deletes_of_the_fresh_group_record_first(self):
        engine, llm, transport, clock = world(answering({"第一轮": "好", "我刚才说了什么": "不知道"}))
        sync = three_linked(engine)  # 主服 #闲聊、别的服 #大厅、主服 #大厅：别的服 #大厅断开以后组还在
        await say(engine, clock, 1, "@小D 我家住在幸福路三号", channel=HALL, author=21, name="小红")
        gate = asyncio.Event()
        hold(llm, {"第一轮": gate})
        await engine.handle_message(msg(2, "@小D 第一轮", mentions_bot=True))  # #闲聊这一轮拿着组的锁，在等模型回话
        await spin()
        reset = engine.reset_channel

        def reset_then_delete(channel_id):
            reset(channel_id)
            engine.handle_delete(OTHER, HALL, [1])  # 清空刚做完（组的记录换成了新的状态），断开还没接着做：小红删了那条

        engine.reset_channel = reset_then_delete
        clearing = asyncio.create_task(engine.reset_linked(LOBBY))  # 主服 #大厅 /dsadmin 清空记忆：等组的锁
        await spin()
        unlinking = asyncio.create_task(unlink_like_the_command(engine, HALL))  # 断开别的服 #大厅：也等，排在清空后面
        await spin()
        gate.set()
        await clearing
        self.assertEqual(await unlinking, ([HALL], [CHANNEL, LOBBY]))
        await engine.drain()
        self.assertNotIn("幸福路三号", record(engine, sync))  # 对照：组的记录里删掉了
        own = "\n".join(text_of(t.content) for t in engine.store.load_conversation(HALL, OTHER).turns)
        self.assertNotIn("幸福路三号", own)  # 断开时拿回去的也是删掉以后的那行
        await say(engine, clock, 3, "@小D 我刚才说了什么", channel=HALL, author=21, name="小红")
        self.assertNotIn("幸福路三号", history_of(llm))  # 小红接着聊的时候它看不到

    async def test_a_link_queued_before_the_group_existed_waits_for_the_groups_round(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(answering({"先问": "好", "闲聊这边问": "好"}), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", CARD, via="command")
        gates = {words: asyncio.Event() for words in ("大厅这边先问", "别的服先问", "闲聊这边问")}
        hold(llm, gates)
        # 主服 #大厅、别的服 #大厅（都还没连通）在回话：它们自己那份的锁被占着
        await engine.handle_message(msg(1, "@小D 大厅这边先问", channel_id=LOBBY, author=12, name="小雨", mentions_bot=True))
        await engine.handle_message(msg(2, "@小D 别的服先问", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        # bot 主人先确认“主服 #闲聊 + 主服 #大厅”，再确认“主服 #大厅 + 别的服 #大厅”：后一个要等哪几个频道是点确认时算的
        # （那时主服 #大厅 还没连通），两个都在等
        linking = asyncio.create_task(link_like_the_command(engine, CHANNEL, LOBBY))
        await spin()
        joining = asyncio.create_task(link_like_the_command(engine, LOBBY, HALL))
        await spin()
        await engine.handle_message(msg(3, "@小D 闲聊这边问一下", mentions_bot=True))  # 阿明叫它：排在连通后面
        await spin()
        gates["大厅这边先问"].set()  # 主服 #大厅那一轮回完：先连成主服的组，阿明那一轮接着在组的记录上跑
        await spin()
        self.assertIn("幸福路", text_of(llm.calls[-1]["messages"][-1]["content"]))  # 对照：同一个服务器，附上了他主服的卡
        gates["别的服先问"].set()  # 别的服 #大厅那一轮也回完了：后一个连通还要等组里正在跑的阿明那一轮
        await spin()
        gates["闲聊这边问"].set()
        sync = await linking
        await joining
        await engine.drain()
        self.assertEqual(sorted(engine.store.sync_members(sync)), sorted([CHANNEL, LOBBY, HALL]))  # 跨服务器了
        self.assertNotIn("幸福路", record(engine, sync))  # 阿明那一轮写完了才连：附过的主服的卡跟着拿掉了
        await say(engine, clock, 4, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
        self.assertNotIn("幸福路", history_of(llm))  # 别的服 #大厅被叫到时看不到主服的卡

    async def test_a_link_whose_records_keep_changing_while_it_waits_gives_up_and_changes_nothing(self):
        engine, llm, transport, clock = world(answering({"先问": "好"}))
        gates = {"大厅这边先问": asyncio.Event(), "别的服先问": asyncio.Event()}
        hold(llm, gates)
        await engine.handle_message(msg(1, "@小D 大厅这边先问", channel_id=LOBBY, author=12, name="小雨", mentions_bot=True))
        await engine.handle_message(msg(2, "@小D 别的服先问", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        with patch("dsbot.engine.SYNC_GUARD_TRIES", 1, create=True):  # 代替 5：等的时候变了一次，就当成一直在变
            linking = asyncio.create_task(link_like_the_command(engine, CHANNEL, LOBBY))
            await spin()
            joining = asyncio.create_task(link_like_the_command(engine, LOBBY, HALL))
            await spin()
            gates["大厅这边先问"].set()  # 先连成主服的组：后一个连通要等的变了
            await spin()
            gates["别的服先问"].set()
            sync = await linking
            with self.assertRaises(asyncio.TimeoutError):  # 照现在的重新等，次数用完了：不连（命令回“一直在忙”）
                await joining
        await engine.drain()
        self.assertIsNone(engine.store.sync_of(HALL))  # 别的服 #大厅没连进去
        self.assertEqual(sorted(engine.store.sync_members(sync)), sorted([CHANNEL, LOBBY]))

    async def test_a_link_made_while_a_clear_waits_does_not_throw_away_the_new_groups_round(self):
        engine, llm, transport, clock = world(answering({"闲聊这边很忙": "好", "广场有人吗": "有的"}))
        on_other_server(transport, (SQUARE, "广场"))
        sync = three_linked(engine)
        await say(engine, clock, 1, "@小D 大厅这边说一句", channel=LOBBY, author=12, name="小雨")
        disconnect(engine, LOBBY)  # 主服 #大厅断开：组还在，它说过的还留在组的记录里
        gates = {"闲聊这边很忙": asyncio.Event(), "广场有人吗": asyncio.Event()}
        hold(llm, gates)
        await engine.handle_message(msg(10, "@小D 闲聊这边很忙", mentions_bot=True))  # 组里这一轮在等模型回话
        await spin()
        clearing = asyncio.create_task(engine.reset_linked(LOBBY))  # 主服 #大厅 /dsadmin 清空记忆：等组里那一轮
        await spin()
        group = await link_like_the_command(engine, LOBBY, SQUARE)  # 这时候 bot 主人把主服 #大厅和别的服 #广场连通了
        await engine.handle_message(msg(11, "@小D 广场有人吗", channel_id=SQUARE, guild_id=OTHER, author=22, name="小蓝", mentions_bot=True))
        await spin()  # 小蓝这一轮在新的组的记录上等模型回话
        gates["闲聊这边很忙"].set()  # 原来那个组的那一轮回完了：清空接着要等新的组里正在跑的这一轮
        await spin()
        gates["广场有人吗"].set()
        await clearing
        await engine.drain()
        self.assertIn((SQUARE, "有的"), sent(transport))  # 小蓝那一轮没被清空作废
        self.assertIn("广场有人吗", record(engine, group))
        self.assertNotIn("大厅这边说一句", record(engine, sync))  # 对照：清空照样做了

    async def test_a_group_that_came_to_hold_its_chat_while_a_clear_waited_is_waited_for_too(self):
        engine, llm, transport, clock = world(answering({"闲聊这边很忙": "好", "广场有人吗": "有的"}))
        on_other_server(transport, (SQUARE, "广场"), (GARDEN, "花园"))
        sync = three_linked(engine)
        await say(engine, clock, 1, "@小D 大厅这边说一句", channel=LOBBY, author=12, name="小雨")
        disconnect(engine, LOBBY)  # 主服 #大厅断开：组还在，它说过的还留在组的记录里
        there = connect(engine, SQUARE, GARDEN)  # 别的服 #广场 + #花园
        gates = {"闲聊这边很忙": asyncio.Event(), "广场有人吗": asyncio.Event()}
        hold(llm, gates)
        await engine.handle_message(msg(10, "@小D 闲聊这边很忙", mentions_bot=True))
        await spin()
        clearing = asyncio.create_task(engine.reset_linked(LOBBY))  # 主服 #大厅 /dsadmin 清空记忆：等组里那一轮
        await spin()
        # 这时候 bot 主人把主服 #大厅连进别的服那个组，小雨在那边说了一句，又断开了：那个组还在，留着她说的
        await link_like_the_command(engine, LOBBY, SQUARE)
        await engine.handle_message(msg(12, "@小D 广场这边打个招呼", channel_id=LOBBY, author=12, name="小雨", mentions_bot=True))
        await spin()
        await unlink_like_the_command(engine, LOBBY)
        self.assertIsNone(engine.store.sync_of(LOBBY))
        self.assertIn(there, engine.store.origin_syncs(LOBBY))  # 对照：那个组的记录里也有它说的了
        await engine.handle_message(msg(11, "@小D 广场有人吗", channel_id=SQUARE, guild_id=OTHER, author=22, name="小蓝", mentions_bot=True))
        await spin()  # 小蓝这一轮在那个组的记录上等模型回话
        gates["闲聊这边很忙"].set()  # 清空接着要等的也有那个组里正在跑的这一轮
        await spin()
        gates["广场有人吗"].set()
        await clearing
        await engine.drain()
        self.assertIn((SQUARE, "有的"), sent(transport))  # 小蓝那一轮没被清空作废
        self.assertIn("广场有人吗", record(engine, there))
        self.assertNotIn("广场这边打个招呼", record(engine, there))  # 清空照样做了：它在那个组里说的也删了
        self.assertNotIn("大厅这边说一句", record(engine, sync))


class ClearCommandReply(WithCommands):
    async def test_the_reply_says_it_is_no_longer_linked_when_it_was_unlinked_while_the_clear_waited(self):
        engine, llm, transport, clock = world(answering({"闲聊这边很忙": "好"}))
        clear = clear_command(engine)
        sync = three_linked(engine)
        await say(engine, clock, 1, "@小D 大厅这边说一句", channel=LOBBY, author=12, name="小雨")
        gate = asyncio.Event()
        hold(llm, {"闲聊这边很忙": gate})
        await engine.handle_message(msg(10, "@小D 闲聊这边很忙", mentions_bot=True))  # 组里这一轮在等模型回话
        await spin()
        unlinking = asyncio.create_task(unlink_like_the_command(engine, LOBBY))  # bot 主人让主服 #大厅断开：等那一轮
        await spin()
        clearing = asyncio.create_task(clear(LOBBY))  # 主服 #大厅 /dsadmin 清空记忆（这时候还连通着）：排在断开后面
        await spin()
        gate.set()
        await unlinking
        reply = await clearing
        await engine.drain()
        self.assertIsNone(engine.store.sync_of(LOBBY))  # 对照：先断开了
        self.assertNotIn("大厅这边说一句", record(engine, sync))  # 清空照样做了：它留在组里的那部分也删了
        self.assertIn("以前连通时留在别的频道共用记录里的那部分也删掉了", reply)  # 照等完以后的情况说：已经不连通了
        self.assertNotIn("之后照样连通", reply)


if __name__ == "__main__":
    unittest.main()
