"""2.8.0 连通的频道，第四轮评审修掉的问题：共用记录的整理记忆。

压缩前挑出来、排着队的那几段，只有真的整理过了才跳过（整理完的那一段自己记着；安静时整理正在问的那一段里压掉的那几条，
那一段问完了才算）：整理进度记过了它们不算数（又压缩了一次、压掉的那部分不用整理，进度就记过去了；问到一半那个频道断开了的
那段是最后一段，进度也记过去了）。安静时整理正在问的那一段要压掉的时候：只有压掉的那几条另外分段排进压缩前挑出来的那批，
而且只在那个频道还在组里、还是那次连进来的时候排；已经断开了的（断开又连回来的也算），它那部分断开时拿回去了，只在它自己那份
里整理一次。没压掉的那些还在记录里，组散了跟着拿回去，也只在它自己那份里整理一次。断开时没能拿回自己那部分的频道（它自己那份
在连通期间被旧版本写过）又连回同一个组：留在组里的那部分不再算（那段时间的不再整理），以后照常断开时，这次连通期间说的只在它
自己那份里整理一次；别的频道连进这个组、它连进别的组，留在组里的那部分照旧。"""

import unittest

import test_chatsync_r3_review
from helpers import CHANNEL, GUILD
from test_chatsync import HALL, LOBBY, connect, disconnect, say, text_of, world
from test_chatsync_privacy import ARENA, THIRD
from test_chatsync_r2_commands import BANTER
from test_chatsync_r2_records import times_reviewed
from test_chatsync_r2_review import compact, quiet, summarized
from test_chatsync_r3_review import noted_each_time, say_in_order
from test_chatsync_records import three_linked
from test_chatsync_review import remembering
from test_dm import OTHER
from test_memory import is_review, stores

HEAVY = "很长的旧事" * 300  # 比后面几轮加起来还长：压缩正好压掉它那一轮（连同它前面的）


def asked_about(call, words):
    """这次问模型是整理记忆，给它看的聊天里有 words。"""
    return is_review(call) and words in text_of(call["messages"][-1]["content"])


class QueuedChunks(unittest.IsolatedAsyncioTestCase):
    async def test_a_queued_chunk_is_still_reviewed_after_a_compaction_with_nothing_to_review(self):
        cards, memory = stores()
        noted = {"闲聊这边约好": "阿明约好周日爬山", "大厅这边约好": "小雨约好周六唱歌"}
        engine, llm, transport, clock = world(
            remembering(lambda text: next((note for words, note in noted.items() if words in text), None)),
            cards=cards,
            memory=memory,
        )
        engine.settings.set("guild", OTHER, "cards", False)  # 别的服没开人设卡、长期记忆：它那部分不用整理
        engine.settings.set("guild", OTHER, "long_memory", False)
        three_linked(engine)
        big, mid = "很长的旧事" * 600, "很长的旧事" * 300  # 头两轮最长：第一次压缩压掉它们；别的服那两轮第二次压掉
        await say(engine, clock, 1, f"@小D 闲聊这边约好周日爬山{big}")
        await say(engine, clock, 2, f"@小D 大厅这边约好周六唱歌{big}", channel=LOBBY, author=12, name="小雨")
        await say_in_order(engine, clock, [(HALL, f"别的服闲聊一{mid}"), (HALL, f"别的服闲聊二{mid}"), (CHANNEL, "主服最近一句")], 3)
        state = {}

        async def hook(call):
            if asked_about(call, "闲聊这边约好") and not state:
                # 那批正在问第一段（#闲聊 的）：又压缩了一次，压掉的只有别的服的闲聊。不用整理，整理进度就记到了那里，
                # 排在后面的主服 #大厅 那段也在这之前
                await compact(engine)
                state["second"] = summarized(llm)

        llm.hook = hook
        await compact(engine)  # 压掉主服两个频道的旧事：挑出来一批，两段（#闲聊 的、主服 #大厅 的）
        first = summarized(llm)
        await engine.drain()
        llm.hook = None
        self.assertIn("大厅这边约好", first)
        self.assertNotIn("别的服闲聊一", first)
        self.assertIn("别的服闲聊二", state["second"])
        self.assertNotIn("主服最近一句", state["second"])
        self.assertEqual(times_reviewed(llm, "大厅这边约好"), 1)  # 排在后面的那段照样问了（没整理过，进度记过了它不算数）
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明约好周日爬山", "小雨约好周六唱歌"])

    async def test_a_compacted_last_chunk_whose_channel_left_while_it_was_asked_is_still_reviewed(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "小红养了一只猫" if "我养了一只猫" in text else None),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        three_linked(engine)  # 三个频道：别的服 #大厅断开以后组还在
        # 主服先说：安静时整理先问主服那段，别的服那段最后问
        await say_in_order(engine, clock, [(CHANNEL, "主服第一句"), (HALL, f"我养了一只猫{HEAVY}"), (CHANNEL, "主服第二句")])
        state = {}

        async def hook(call):
            if asked_about(call, "我养了一只猫") and not state:
                await compact(engine)  # 问别的服那段的时候压缩：那一轮压掉了（排进压缩前挑出来的那批）
                state["summary"] = summarized(llm)
                state["left"] = disconnect(engine, HALL)  # 然后别的服 #大厅断开了：那一轮已经压掉了，没什么可拿回去的

        llm.hook = hook
        await quiet(engine, clock, 700)
        llm.hook = None
        self.assertIn("我养了一只猫", state["summary"])
        self.assertNotIn("主服第二句", state["summary"])
        self.assertEqual(state["left"], ([HALL], [CHANNEL, LOBBY]))
        await quiet(engine, clock, times=2)
        # 问到一半的那次不照着改（它断开了）；这段是最后一段，整理进度记过了它，压缩前挑出来的那批照样整理它
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫"])


class AskedChunkTakenBack(unittest.IsolatedAsyncioTestCase):
    async def test_a_chunk_whose_channel_left_before_the_compaction_is_applied_once(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(noted_each_time("我养了一只猫", "小红养了一只猫")),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        three_linked(engine)
        await say_in_order(engine, clock, [(HALL, f"我养了一只猫{HEAVY}"), (CHANNEL, "主服这边")])  # 别的服先说：先问它那段
        state = {}

        async def hook(call):
            if asked_about(call, "我养了一只猫") and not state:
                state["left"] = disconnect(engine, HALL)  # 问别的服那段的时候它断开了（组还在）：那一轮拿回它自己那份
                await compact(engine)  # 然后 #闲聊 聊长了：组里的记录压掉了那一轮
                state["summary"] = summarized(llm)

        llm.hook = hook
        await quiet(engine, clock, 700)
        llm.hook = None
        self.assertEqual(state["left"], ([HALL], [CHANNEL, LOBBY]))
        self.assertIn("我养了一只猫", state["summary"])
        await quiet(engine, clock, times=2)  # 别的服 #大厅自己那份整理拿回去的
        # 组里问到一半的那次（第 1 次）不照着改，压缩前挑出来的那批也不排它（它已经不在组里了）：只在它自己那份里整理、记一次
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第2次问到）"])

    async def test_a_chunk_whose_channel_left_and_linked_again_before_the_compaction_is_applied_once(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(noted_each_time("我养了一只猫", "小红养了一只猫")),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        three_linked(engine)
        await say_in_order(engine, clock, [(HALL, f"我养了一只猫{HEAVY}"), (CHANNEL, "主服这边")])  # 别的服先说：先问它那段
        state = {}

        async def hook(call):
            if asked_about(call, "我养了一只猫") and not state:
                state["left"] = disconnect(engine, HALL)  # 问别的服那段的时候它断开了：那一轮拿回它自己那份
                connect(engine, HALL, CHANNEL)  # 马上又连回同一个组：算新的一次连通（拿回去的那轮不算这次的）
                await compact(engine)  # 然后组里的记录压掉了那一轮
                state["summary"] = summarized(llm)

        llm.hook = hook
        await quiet(engine, clock, 700)
        llm.hook = None
        self.assertEqual(state["left"], ([HALL], [CHANNEL, LOBBY]))
        self.assertIn("我养了一只猫", state["summary"])
        await quiet(engine, clock, times=2)
        # 组里问到一半的那次（第 1 次）不照着改，压缩前挑出来的那批也不排它（不是那次连进来的了）：只在它自己那份里整理、记一次
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第2次问到）"])

    async def test_the_uncompacted_rest_of_the_asked_chunk_is_applied_once_when_the_group_dissolves(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(noted_each_time("我家住在海边", "小红家住在海边")),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        connect(engine)  # 只有两个频道：断开一个，组就散了
        rounds = [(HALL, f"我养了一只猫{HEAVY}"), (CHANNEL, "主服这边"), (HALL, "我家住在海边"), (CHANNEL, "主服又一句")]
        await say_in_order(engine, clock, rounds)  # 别的服的两轮是同一段（先问）
        state = {}

        async def hook(call):
            if asked_about(call, "我养了一只猫") and not state:
                state["one_chunk"] = asked_about(call, "我家住在海边")
                await compact(engine)  # 问那段的时候压缩：只压掉了它很长的头一轮，第二轮还在记录里
                state["summary"] = summarized(llm)
                state["left"] = disconnect(engine, CHANNEL)  # 然后主服 #闲聊断开了：组散了，两边各拿回自己那部分

        llm.hook = hook
        await quiet(engine, clock, 700)
        llm.hook = None
        self.assertTrue(state["one_chunk"])
        self.assertIn("我养了一只猫", state["summary"])
        self.assertNotIn("我家住在海边", state["summary"])
        self.assertEqual(state["left"][1], [])  # 组散了
        await quiet(engine, clock, times=2)
        self.assertEqual(times_reviewed(llm, "我养了一只猫"), 2)  # 压掉的那轮：问到一半作废了，压缩前挑出来的那批又问了一次
        # 那批只整理压掉的那一轮；没压掉的那轮跟着拿回去了，只在它自己那份里整理、记一次
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红家住在海边（第2次问到）"])


class RejoinAfterAStaleLeave(unittest.IsolatedAsyncioTestCase):
    async def test_chat_of_a_later_membership_is_applied_once_after_a_stale_leave_and_a_rejoin(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(noted_each_time("我养了一只猫", "小红养了一只猫")),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        await say(engine, clock, 1, "@小D 连通之前", channel=HALL, author=21, name="小红")  # 别的服 #大厅自己那份里有记录
        three_linked(engine)
        await say_in_order(engine, clock, [(HALL, "第一次连通时说的"), (CHANNEL, "主服这边")], 2)
        # 换回旧版本跑过一阵：别的服 #大厅自己那份在连通期间又被写过，断开时拿不回那次连通期间的聊天（留在组里整理）
        engine.store.db.execute("UPDATE conversations SET next_short_id = next_short_id + 10 WHERE channel_id = ?", (HALL,))
        self.assertEqual(engine.store.stale_syncs(), [HALL])
        with self.assertLogs("dsbot.store", "WARNING"):
            self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
        connect(engine, HALL, CHANNEL)  # 又连回同一个组
        self.assertEqual(engine.store.stale_syncs(), [])
        await say_in_order(engine, clock, [(HALL, "我养了一只猫"), (CHANNEL, "主服又一句")], 4)  # 这次连通期间说的
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))  # 这次照常断开：拿回它自己那份
        await quiet(engine, clock, times=2)
        # 组里不再按没拿回去的那次算它（那段时间的不再整理）：这次连通期间说的只在它自己那份里整理、记一次
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第1次问到）"])


class KeptPartStore(unittest.TestCase):
    def test_control_the_kept_part_stays_until_that_channel_links_into_that_group_again(self):
        # 对照（control）：别的频道连进这个组、它自己连进别的组，断开时没能拿回去的那部分照样留在组里整理
        store, sync = test_chatsync_r3_review.StaleLeaverStore.left_stale(self)  # 别的服 #大厅断开时没能拿回去（组还在）
        store.link_channels((BANTER, GUILD, "杂谈"), (CHANNEL, GUILD, "闲聊"), now=7)  # 主服 #杂谈 连进这个组
        self.assertIn(HALL, store.sync_joined_turns(sync))
        store.link_channels((HALL, OTHER, "大厅"), (ARENA, THIRD, "广场"), now=8)  # 别的服 #大厅 和第三服 #广场 连成另一组
        self.assertIn(HALL, store.sync_joined_turns(sync))


if __name__ == "__main__":
    unittest.main()
