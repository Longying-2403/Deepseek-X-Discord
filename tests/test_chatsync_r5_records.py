"""2.8.0 连通的频道，第五轮评审修掉的问题：清空、断开、散组时内存里的那几份记录，和还没整理完的记忆。

- 还没有频道状态的频道（刚断开、重启以后还没人说过话、暂停着的）清空记忆：它那份载入了内存的记录也换成新的，接着聊的时候
  看不到清空前的聊天。
- 记录换成新的状态（清空一个连通的频道、断开）：新的那份用原来那把锁。等着原来那份的锁的连通、清空，等的就是现在这份上
  正在跑的那一轮：不会趁它跑到一半连通（跨服务器的记录里不会留下只该原来那边看的人设卡），也不会趁它跑到一半清空（那一轮的
  回话不会被作废）。
- 组散了，压缩前挑出来、还没整理完的那几批照样整理完；这时清空一个频道的记忆，那几批里它的那几段不再整理（正在问的那一段
  问完作废），别的频道的接着整理；接着再清空另一个频道，它那几段也不再整理。整理完了就不再留着。
- 频道连进组之前就在整理它自己那份压缩前挑出来的那批（压掉的原话只在那批里）：断开（或者组散了）时交给它新的那份接着整理。
  已经整理完的那几段（频道自己那份按整理进度算）不再问；正在问的那一段问完作废，新的那份再问一次，只记一次。组里的记录照旧
  不按整理进度算：清空一个频道时交出去的那批里，进度记过了、却还没整理过的那段照样整理。"""

import asyncio
import unittest

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, connect, disconnect, replies, say, world
from test_chatsync_lifecycle import answering, hold, sent, spin
from test_chatsync_privacy import history_of, record
from test_chatsync_r2_records import compact, hold_reviews, times_reviewed
from test_chatsync_r2_review import summarized
from test_chatsync_r3_review import noted_each_time, say_in_order
from test_chatsync_records import three_linked
from test_chatsync_review import remembering
from test_dm import OTHER
from test_memory import stores

HEAVY = "很长的旧事" * 300  # 比后面几轮加起来还长：压缩正好压掉它那一轮（连同它前面的）
CARD = "主服的阿明：住在幸福路三号"  # 阿明在主服的人设卡（人设卡各服务器一张）


def notes(table):
    """整理记忆时，给它看的聊天里有 table 的键就记那一条长期记忆（没有就不记）。"""
    return remembering(lambda text: next((note for words, note in table.items() if words in text), None))


async def link_like_the_command(engine, channel, to):
    """像 /dsowner chatsync 连通（do_sync_link）那样：先等这几个频道（连同 to 所在的组）正在跑的一轮跑完，再连、换记录。"""
    before = engine.store.sync_of(to) or engine.store.sync_of(channel)
    involved = [channel, to] + (engine.store.sync_members(before) if before is not None else [])
    async with engine.sync_guard(involved):
        connect(engine, channel, to)


async def unlink_like_the_command(engine, channel):
    """像 /dsowner chatsync 断开（do_sync_unlink）那样：先等组里每个频道正在跑的一轮跑完，再断开、换记录。"""
    async with engine.sync_guard(engine.store.sync_members(engine.store.sync_of(channel))):
        return disconnect(engine, channel)


class ClearingWithoutAChannelState(unittest.IsolatedAsyncioTestCase):
    async def test_clearing_a_channel_nobody_spoke_in_since_a_restart_forgets_what_it_took_back(self):
        engine, llm, transport, clock = world(replies())
        connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        await say(engine, clock, 1, "@小D 我家住在幸福路三号", channel=HALL, author=21, name="小红")
        engine.states.clear()
        engine.convs.clear()  # 重启了：内存里什么都没有，之后 #大厅 一直没人说话（暂停着的频道也是这样）
        self.assertEqual(disconnect(engine, CHANNEL), ([CHANNEL, HALL], []))  # 组散了：#大厅拿回它那部分，载入了内存
        # 别的服 #大厅 /ds 清空记忆：它已经不在组里，也没有组还留着它说过的话，直接清它自己那份
        self.assertFalse(engine.store.origin_syncs(HALL))
        engine.reset_channel(HALL)
        self.assertEqual(engine.store.load_conversation(HALL, OTHER).turns, [])  # 对照：库里清空了
        await say(engine, clock, 2, "@小D 我刚才说了什么", channel=HALL, author=21, name="小红")
        self.assertNotIn("幸福路三号", history_of(llm))  # 接着聊的时候也看不到（不再用清空前载入的那份）


class GuardsAfterARecordIsReplaced(unittest.IsolatedAsyncioTestCase):
    async def test_a_link_queued_behind_a_clear_waits_for_a_round_on_the_fresh_group_record(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            answering({"第一轮": "好", "别的服在忙": "嗯", "第三轮": "在", "他是谁": "不知道"}), cards=cards, memory=memory
        )
        cards.put(GUILD, 11, "阿明", CARD, via="command")
        sync = connect(engine, CHANNEL, LOBBY)  # 主服的两个频道连通：照常附卡
        gates = {words: asyncio.Event() for words in ("第一轮", "别的服在忙", "第三轮")}
        hold(llm, gates)
        await engine.handle_message(msg(1, "@小D 第一轮", mentions_bot=True))  # #闲聊这一轮拿着组的锁，在等模型回话
        # 别的服 #大厅（还没连通）也在回话：它自己那份的锁被占着
        await engine.handle_message(msg(2, "@小D 别的服在忙", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        clearing = asyncio.create_task(engine.reset_linked(LOBBY))  # 主服 #大厅 /ds 清空记忆：等组的锁
        await spin()
        linking = asyncio.create_task(link_like_the_command(engine, HALL, CHANNEL))  # 把别的服 #大厅连进来：排在清空后面
        await spin()
        await engine.handle_message(msg(3, "@小D 第三轮", mentions_bot=True))  # 阿明又叫它：排在第一轮后面
        await spin()
        gates["第一轮"].set()  # 先清空（组的记录换成新的状态），叫它的那一轮接着在新的那份上跑
        await spin()
        gates["别的服在忙"].set()  # 别的服 #大厅那一轮也回完了
        await spin()
        await linking
        gates["第三轮"].set()
        await engine.drain()
        await clearing
        self.assertTrue(engine.channels_cross(engine.store.sync_members(sync)))  # 对照：组跨服务器了
        # 连通等第三轮写完了才连（那一轮没赶在跨服务器之前附上主服的卡、又在之后写进来）
        self.assertNotIn("住在幸福路三号", record(engine, sync))
        await say(engine, clock, 4, "@小D 他是谁", channel=HALL, author=21, name="小红")
        self.assertNotIn("住在幸福路三号", history_of(llm))  # 别的服 #大厅被叫到时看不到主服的卡

    async def test_a_clear_queued_behind_an_unlink_waits_for_a_round_on_the_channels_fresh_own_record(self):
        engine, llm, transport, clock = world(answering({"第一轮": "好", "第二轮": "在"}))
        three_linked(engine)  # 别的服 #大厅断开以后组还在
        gates = {words: asyncio.Event() for words in ("第一轮", "第二轮")}
        hold(llm, gates)
        await engine.handle_message(msg(1, "@小D 第一轮", mentions_bot=True))  # #闲聊这一轮拿着组的锁，在等模型回话
        await spin()
        # 小红在别的服 #大厅叫它：排在这一轮后面
        await engine.handle_message(msg(2, "@小D 第二轮", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        unlinking = asyncio.create_task(unlink_like_the_command(engine, HALL))  # 断开别的服 #大厅：等组的锁
        await spin()
        clearing = asyncio.create_task(engine.reset_linked(HALL))  # 别的服 #大厅 /ds 清空记忆：也等组的锁，排在断开后面
        await spin()
        gates["第一轮"].set()  # 先断开（#大厅自己那份换成新的状态），#大厅叫它的那一轮接着在新的那份上跑
        await spin()
        gates["第二轮"].set()
        self.assertEqual(await unlinking, ([HALL], [CHANNEL, LOBBY]))
        await clearing
        await engine.drain()
        self.assertIn((HALL, "在"), sent(transport))  # 清空等那一轮回完了才清：回话没被作废


class ReviewsLeftByADissolvedGroup(unittest.IsolatedAsyncioTestCase):
    async def dissolved_while_asking(self):
        """主服 #闲聊、别的服 #大厅各说了一段很长的旧事。压缩共用的记录挑出来一批（两段：主服的在前，别的服的在后），正在问
        主服那段、模型还没回的时候组散了：那批交给不在组里的一份接着整理（又在问主服那段）。返回 (engine, llm, memory, gate)。"""
        cards, memory = stores()
        engine, llm, transport, clock = world(
            notes({"主服旧事": "阿明说过主服旧事", "我养了一只猫": "小红养了一只猫"}),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1000},  # 不做安静时整理：只有压缩前挑出来的那批在整理
        )
        connect(engine)  # 只有两个频道：断开一个，组就散了
        rounds = [(CHANNEL, f"主服旧事{HEAVY}"), (HALL, f"我养了一只猫{HEAVY}")] + [(CHANNEL, f"主服最近{i}") for i in range(3)]
        await say_in_order(engine, clock, rounds)
        gate = asyncio.Event()
        hold_reviews(llm, "主服旧事", gate)  # 整理主服那段的时候模型回得慢
        await compact(engine, CHANNEL)  # 压掉两边的旧事
        self.assertIn("我养了一只猫", summarized(llm))
        await spin()
        self.assertEqual(disconnect(engine, CHANNEL), ([CHANNEL, HALL], []))  # 组散了
        await spin()
        # 两边都不在组里了，也没有组还留着它们说过的话（散组时删了）：/ds 清空记忆直接清它自己那份
        self.assertFalse(engine.store.origin_syncs(HALL) or engine.store.origin_syncs(CHANNEL))
        return engine, llm, memory, gate

    async def test_clearing_a_channel_after_its_group_dissolved_drops_its_part_of_the_pending_review(self):
        engine, llm, memory, gate = await self.dissolved_while_asking()
        engine.reset_channel(HALL)  # 别的服 #大厅 /ds 清空记忆
        gate.set()
        await engine.drain()
        llm.hook = None
        self.assertEqual([m.text for m in memory.entries(OTHER)], [])  # 清空了的那段没记进别的服的长期记忆
        self.assertEqual(times_reviewed(llm, "我养了一只猫"), 0)  # 也不再拿去问模型
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明说过主服旧事"])  # 主服那段照样整理完
        self.assertEqual(getattr(engine, "_orphans", []), [])  # 整理完了，不再留着

    async def test_clearing_the_other_channel_afterwards_drops_its_part_too(self):
        engine, llm, memory, gate = await self.dissolved_while_asking()
        engine.reset_channel(HALL)  # 别的服 #大厅 /ds 清空记忆：主服那段交给新的一份接着整理
        await spin()  # 新的那份又在问主服那段
        engine.reset_channel(CHANNEL)  # 主服 #闲聊也清空了记忆
        gate.set()
        await engine.drain()
        llm.hook = None
        self.assertEqual([m.text for m in memory.entries(GUILD)], [])  # 接着整理的那份也找得到：主服那段不再记进长期记忆
        self.assertEqual([m.text for m in memory.entries(OTHER)], [])
        self.assertEqual(getattr(engine, "_orphans", []), [])


class OwnReviewWhenLeaving(unittest.IsolatedAsyncioTestCase):
    async def test_a_review_of_its_own_compacted_chat_running_when_it_leaves_is_finished_once_in_its_fresh_record(self):
        cards, memory = stores()
        cat, sea = noted_each_time("我养了一只猫", "小红养了一只猫"), noted_each_time("我家住在海边", "小红家住在海边")
        engine, llm, transport, clock = world(
            remembering(lambda text: cat(text) or sea(text)),
            cards=cards,
            memory=memory,
            # 不做安静时整理；一次给它看最多 2000 字：下面两轮长的各是一段
            engine_cfg={"review_min_rounds": 1000, "review_max_chars": 2000},
        )
        connect(engine, LOBBY, CHANNEL)  # 主服 #闲聊 + #大厅：别的服 #大厅连进来又断开，组还在
        long = "\n".join(["很长的旧事" * 100] * 3)
        rounds = [(HALL, f"我养了一只猫\n{long}"), (HALL, f"我家住在海边\n{long}")] + [(HALL, f"别的服闲聊{i}") for i in range(3)]
        await say_in_order(engine, clock, rounds)
        gate = asyncio.Event()
        hold_reviews(llm, "我家住在海边", gate)  # 第一段（猫）马上整理完；问第二段（海边）的时候模型回得慢
        await compact(engine, HALL)  # 别的服 #大厅还没连通：压的是它自己那份，压掉的两轮挑出来一批（两段）
        self.assertIn("我家住在海边", summarized(llm))
        await spin()
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第1次问到）"])  # 对照：第一段整理完了
        connect(engine, HALL, CHANNEL)  # 正在问第二段的时候，bot 主人把别的服 #大厅连进组里……
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))  # ……又断开了（连错了）：它自己那份换成新的状态
        await spin()  # 新的那份接着问第二段：也在等模型回话
        gate.set()
        await engine.drain()
        llm.hook = None
        self.assertEqual(times_reviewed(llm, "我养了一只猫"), 1)  # 整理完的那段不再问
        # 问到一半的那次（第 1 次）作废，新的那份再问的那次记下了：海边那段没丢，也只记一次
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第1次问到）", "小红家住在海边（第2次问到）"])

    async def test_control_a_group_records_queued_chunk_is_handed_on_even_if_the_progress_passed_it(self):
        # 对照（control）：只有频道自己那份按整理进度算整理过。组里的记录不是：又压缩了一次、压掉的那部分不用整理，整理进度
        # 就记过了排在后面的那段，它却还没整理过；这时清空一个频道，交给新的那份接着整理的那批里照样有它
        cards, memory = stores()
        engine, llm, transport, clock = world(
            notes({"闲聊这边约好": "阿明约好周日爬山", "大厅这边约好": "小雨约好周六唱歌"}),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1000},
        )
        engine.settings.set("guild", OTHER, "cards", False)  # 别的服没开人设卡、长期记忆：它那部分不用整理
        engine.settings.set("guild", OTHER, "long_memory", False)
        three_linked(engine)
        big, mid = "很长的旧事" * 600, "很长的旧事" * 300  # 头两轮最长：第一次压缩压掉它们；别的服那两轮第二次压掉
        await say(engine, clock, 1, f"@小D 闲聊这边约好周日爬山{big}")
        await say(engine, clock, 2, f"@小D 大厅这边约好周六唱歌{big}", channel=LOBBY, author=12, name="小雨")
        await say_in_order(engine, clock, [(HALL, f"别的服闲聊一{mid}"), (HALL, f"别的服闲聊二{mid}"), (CHANNEL, "主服最近一句")], 3)
        gate = asyncio.Event()
        hold_reviews(llm, "闲聊这边约好", gate)  # 整理 #闲聊 那段的时候模型回得慢
        await compact(engine, CHANNEL)  # 压掉主服两个频道的旧事：挑出来一批，两段（#闲聊 的、主服 #大厅 的）
        await spin()
        await compact(engine, CHANNEL)  # 又压缩了一次：压掉的只有别的服的闲聊，整理进度记过了主服 #大厅 那段
        self.assertIn("别的服闲聊二", summarized(llm))
        await engine.reset_linked(CHANNEL)  # 主服 #闲聊 /ds 清空记忆：那批交给新的那份接着整理（#闲聊 那段不整理了）
        gate.set()
        await engine.drain()
        llm.hook = None
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["小雨约好周六唱歌"])  # 主服 #大厅那段照样整理了


if __name__ == "__main__":
    unittest.main()
