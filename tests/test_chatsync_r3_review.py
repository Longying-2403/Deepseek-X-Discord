"""2.8.0 连通的频道，第三轮评审修掉的问题：共用记录的整理记忆。

压缩前挑出来的那批整理，成没成都不动安静时整理的失败次数：不因为它多等、不提前放弃，它整理成了也不把安静时整理失败的次数
清零。安静时整理正在问的那一段的频道这时候断开了（组还在）：模型的回答不照着改，拿回它自己那份里整理（多问一次，只记一次）。
安静时整理排上了、还没开始，这份记录就被清空换成了新的：不照旧的那份整理（清空了的聊天不整理，别的频道的只由新的那份整理
一次）。安静时整理正在问的那一段被压掉了：连同它一起排进压缩前挑出来的那批，这次没问成（出错、组散了作废）也照样整理，
没压掉的那部分也记成整理过（重启以后不再整理）；安静时整理问完了的，那批里不再问。断开时没能拿回自己那部分的频道（它自己
那份在连通期间被旧版本写过）：那部分照样在组里整理一次（照它那个频道的设置）；它清空记忆、组散了就不再留着。"""

import asyncio
import unittest

from helpers import CHANNEL, GUILD
from test_chatsync import HALL, LOBBY, connect, disconnect, say, text_of, world
from test_chatsync_lifecycle import spin
from test_chatsync_r2_records import hold_reviews, times_reviewed
from test_chatsync_r2_review import compact, failing, quiet, summarized
from test_chatsync_records import three_linked
from test_chatsync_review import remembering, reviews
from test_dm import OTHER
from test_memory import is_review, stores

from dsbot.engine import MAX_REVIEW_FAILURES
from dsbot.llm import LLMError
from dsbot.store import Store


def refused():
    """问模型被拒了：算整理失败（连着几次就放弃）。"""
    return LLMError("Content Exists Risk", 400)


def noted_each_time(words, note):
    """整理到有 words 的聊天时记一条长期记忆，每次问到记的都不一样：“note（第几次问到）”。"""
    asked = []

    def what(text):
        if words not in text:
            return None
        asked.append(text)
        return f"{note}（第{len(asked)}次问到）"

    return what


async def say_in_order(engine, clock, rounds, start=1):
    """rounds：(频道, 说的话)，按先后各叫它一轮（主服 #闲聊是阿明，别的服 #大厅是小红）；消息从 start 往后编。"""
    for i, (channel, words) in enumerate(rounds):
        who = {"author": 21, "name": "小红"} if channel == HALL else {}
        await say(engine, clock, start + i, f"@小D {words}", channel=channel, **who)


class CompactionReviewCounters(unittest.IsolatedAsyncioTestCase):
    async def test_failed_chunks_of_a_compaction_review_do_not_count_against_the_quiet_review(self):
        cards, memory = stores()
        state = {"compacting": True, "refuse": 1}

        def fail(text):
            if state["compacting"]:
                return refused()  # 压缩前挑出来的那批：每段都被拒
            if "周六聚餐" in text and state["refuse"]:
                state["refuse"] -= 1
                return refused()  # 安静时整理被拒一次，再问就好了
            return None

        engine, llm, transport, clock = world(
            failing(remembering(lambda text: "小红周六聚餐" if "周六聚餐" in text else None), fail),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        sync = connect(engine)
        heavy = "很长的旧事" * 300  # 两边的旧事比后面的加起来还长：压缩正好压掉这两轮
        await say(engine, clock, 10, f"@小D 主服旧事{heavy}")
        await say(engine, clock, 11, f"@小D 别的服旧事{heavy}", channel=HALL, author=21, name="小红")
        await say(engine, clock, 20, "@小D 新的约定：周六聚餐", channel=HALL, author=21, name="小红")
        for i in range(3):
            await say(engine, clock, 21 + i, f"@小D 别的服闲聊{i}", channel=HALL, author=21, name="小红")
        cs = engine.convs[sync]
        before = (cs.review_failures, cs.review_bad)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await compact(engine)
            await engine.drain()
        self.assertIn("别的服旧事", summarized(llm))
        self.assertNotIn("周六聚餐", summarized(llm))  # 还在记录里：安静时整理
        self.assertEqual(sum("旧事" in text for _, text in reviews(llm)), 2)  # 压掉的两段都问了，都被拒了
        self.assertEqual((cs.review_failures, cs.review_bad), before)  # 不算进安静时整理的失败次数
        state["compacting"] = False
        with self.assertLogs("dsbot.engine", "WARNING"):
            await quiet(engine, clock, engine.ec.review_idle_seconds + 100)
            self.assertEqual(times_reviewed(llm, "周六聚餐"), 1)  # 安静了 review_idle_seconds 就整理（不多等），被拒了一次
            await quiet(engine, clock, 100_000, times=2)
        self.assertEqual(times_reviewed(llm, "周六聚餐"), 2)  # 被拒一次不放弃：下次再问
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红周六聚餐"])

    async def test_a_compaction_review_that_succeeds_does_not_reset_the_quiet_reviews_failures(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            failing(
                remembering(lambda text: "小红说过旧事" if "别的服旧事" in text else None),
                lambda text: refused() if "主服不能处理的话" in text else None,  # 每次都被拒
            ),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        sync = connect(engine)
        await say_in_order(engine, clock, [(HALL, "别的服旧事"), (CHANNEL, "主服不能处理的话")])
        engine.settings.set("guild", OTHER, "daily_budget_usd", 0.0000001)  # 别的服今天的预算用完了：它那部分先不整理
        with self.assertLogs("dsbot.engine", "WARNING"):
            await quiet(engine, clock, 700)
        self.assertEqual(times_reviewed(llm, "主服不能处理的话"), 1)  # 被拒了一次
        cs = engine.convs[sync]
        failures = (cs.review_failures, cs.review_bad)
        engine.settings.set("guild", OTHER, "daily_budget_usd", 5.0)  # 预算调高了
        await compact(engine)  # 压掉最早的那轮（别的服的）：挑出来整理，整理成了
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红说过旧事"])
        self.assertEqual((cs.review_failures, cs.review_bad), failures)  # 不把安静时整理失败的次数清零
        with self.assertLogs("dsbot.engine", "WARNING"):
            await quiet(engine, clock, 100_000, times=MAX_REVIEW_FAILURES)
        self.assertEqual(times_reviewed(llm, "主服不能处理的话"), MAX_REVIEW_FAILURES)  # 连着被拒几次照样放弃（不多问）


class LeavingWhileAsked(unittest.IsolatedAsyncioTestCase):
    async def test_the_answer_for_a_channel_that_left_while_it_was_asked_is_not_applied(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(noted_each_time("我养了一只猫", "小红养了一只猫")),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        three_linked(engine)
        await say_in_order(engine, clock, [(HALL, "我养了一只猫"), (CHANNEL, "主服的话")])  # 别的服先说：先问它那段
        gate = asyncio.Event()
        hold_reviews(llm, "我养了一只猫", gate)
        clock.advance(700)
        await engine.tick()
        await spin()
        self.assertEqual(times_reviewed(llm, "我养了一只猫"), 1)  # 组里正在问别的服那段
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))  # 这时候别的服 #大厅断开了（组还在）
        gate.set()
        await engine.drain()
        await quiet(engine, clock, times=2)
        # 组里问到一半的那次（第 1 次）的回答不照着改；拿回去的在它自己那份里整理（第 2 次），只记了那次的
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫（第2次问到）"])


class ClearAfterTheQuietReviewWasKicked(unittest.IsolatedAsyncioTestCase):
    async def test_a_quiet_review_kicked_just_before_clearing_does_not_review_the_old_record(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "阿明的手机号是13800001111" if "13800001111" in text else None),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        connect(engine)
        await say_in_order(engine, clock, [(CHANNEL, "我的手机号是 13800001111"), (HALL, "别的服的话")])
        gate = asyncio.Event()
        hold_reviews(llm, "别的服的话", gate)  # 问到别的服那段时模型回得慢
        clock.advance(700)
        await engine.tick()  # 到时间了：安静时整理排上了（还没开始跑）
        # 同一轮里主服 /ds 清空记忆先做完了（锁都空着时 reset_linked 中间不停）：组的记录换成了新的
        engine.reset_channel(CHANNEL)
        await spin()
        await engine.tick()  # 新的那份记录也到时间了
        await spin()
        gate.set()
        await engine.drain()
        self.assertEqual(memory.entries(GUILD), [])  # 清空了的聊天不整理进长期记忆
        self.assertEqual(times_reviewed(llm, "别的服的话"), 1)  # 别的服那部分只由新的那份整理一次


class AskedPartCompacted(unittest.IsolatedAsyncioTestCase):
    # 别的服、主服、别的服各说一轮：先整理别的服那段，问主服那段的时候压缩，压掉了前两轮（连同主服那段整段）
    BEFORE_AND_AFTER = [(HALL, "别的服先说"), (CHANNEL, "主服的约定：周日爬山"), (HALL, "别的服后来说")]

    async def compacted_while_asked(self, rounds, *, down=False, then=None):
        """安静时整理问到有周日爬山的那段（主服的）、模型还没回的时候，主服那边聊长了：压缩旧记录，压掉了那段的头一轮。
        down：这次问模型连不上；then(engine)：压缩以后、模型回话之前又发生的事。这次安静时整理完、压缩前挑出来的那批
        也整理完就返回 (engine, llm, clock, memory)；self.answered：整理成了的那几次给模型看的聊天。"""
        cards, memory = stores()
        state = {"summary": None, "down": down}
        self.answered = []

        def fail(text):
            if "周日爬山" in text and state["down"]:
                state["down"] = False
                return LLMError("连不上", 503)
            return None

        def noted(text):
            self.answered.append(text)
            return "阿明约好周日爬山" if "周日爬山" in text else None

        engine, llm, transport, clock = world(
            failing(remembering(noted), fail),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        connect(engine)
        await say_in_order(engine, clock, rounds)

        async def hook(call):
            if is_review(call) and "周日爬山" in text_of(call["messages"][-1]["content"]) and state["summary"] is None:
                await compact(engine)
                state["summary"] = summarized(llm)
                if then is not None:
                    then(engine)

        llm.hook = hook
        with self.assertLogs("dsbot", "INFO"):  # 压缩时记一条（连不上时还有警告）
            await quiet(engine, clock, 700)
        self.assertIn("周日爬山", state["summary"])  # 正在问的那段压掉了
        return engine, llm, clock, memory

    async def test_a_part_compacted_while_it_was_asked_is_reviewed_even_if_the_group_dissolves(self):
        engine, llm, clock, memory = await self.compacted_while_asked(
            self.BEFORE_AND_AFTER, then=lambda engine: disconnect(engine, CHANNEL)  # 只有两个频道：组散了，问到的作废
        )
        await quiet(engine, clock, times=2)
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明约好周日爬山"])  # 压缩前挑出来的那批照样整理了它

    async def test_a_part_compacted_while_it_was_asked_is_reviewed_even_if_asking_failed(self):
        engine, llm, clock, memory = await self.compacted_while_asked(self.BEFORE_AND_AFTER, down=True)
        await quiet(engine, clock, times=2)
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明约好周日爬山"])  # 没问成：压缩前挑出来的那批整理了它

    async def test_the_uncompacted_rest_of_an_asked_part_is_not_reviewed_again_after_a_restart(self):
        # 主服那段是第一轮和第三轮：压掉的只有第一轮，第三轮还在记录里
        rounds = [(CHANNEL, "主服的约定：周日爬山"), (HALL, "别的服的话"), (CHANNEL, "主服后来说：下周去海边")]
        engine, llm, clock, memory = await self.compacted_while_asked(rounds, down=True)
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明约好周日爬山"])  # 没问成：压缩前挑出来的那批整理了它
        engine.states.clear()
        engine.convs.clear()  # 这时候重启了：内存里什么都没有
        await say(engine, clock, 10, "@小D 重启以后")
        await quiet(engine, clock, 700)
        self.assertTrue(any("重启以后" in text for text in self.answered))  # 重启以后说的照常整理
        # 第三轮和压掉的那轮一起整理过了（库里每条记录上都记着）：重启以后不再整理一遍
        self.assertEqual(sum("下周去海边" in text for text in self.answered), 1)

    async def test_control_a_part_the_quiet_review_finished_is_not_asked_again_by_the_compaction_review(self):
        # 对照（control）：正在问的那段问完了，进度也记过去了：排进压缩前挑出来的那批的它不再问一次
        engine, llm, clock, memory = await self.compacted_while_asked(self.BEFORE_AND_AFTER)
        await quiet(engine, clock, times=2)
        self.assertEqual(times_reviewed(llm, "周日爬山"), 1)
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明约好周日爬山"])


class StaleLeaver(unittest.IsolatedAsyncioTestCase):
    async def test_a_stale_leavers_linked_period_chat_is_still_reviewed_once_in_the_group(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "小红养了一只猫" if "我养了一只猫" in text else None),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        await say(engine, clock, 1, "@小D 连通之前", channel=HALL, author=21, name="小红")  # 别的服 #大厅自己那份里有记录
        three_linked(engine)
        await say_in_order(engine, clock, [(HALL, "我养了一只猫"), (CHANNEL, "主服这边")], start=5)
        # 换回旧版本跑过一阵：别的服 #大厅自己那份在连通期间又被写过，断开时拿不回连通期间的聊天
        engine.store.db.execute("UPDATE conversations SET next_short_id = next_short_id + 10 WHERE channel_id = ?", (HALL,))
        self.assertEqual(engine.store.stale_syncs(), [HALL])
        with self.assertLogs("dsbot.store", "WARNING"):
            self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))  # 组还在
        await quiet(engine, clock, times=2)
        self.assertEqual(times_reviewed(llm, "我养了一只猫"), 1)  # 留在组里的那部分照样在组里整理（只整理一次）
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫"])  # 照它那个频道的设置，记进别的服


class StaleLeaverStore(unittest.TestCase):
    def left_stale(self):
        """「主服」#闲聊、「别的服」#大厅、「主服」#大厅连成一组；#大厅（别的服）在组里说过话，它自己那份在连通期间被
        旧版本写过：断开时拿不回去（组还在）。返回 (store, 组)。"""
        store = Store(":memory:")
        own = store.load_conversation(HALL, OTHER)
        own.next_short_id = 2
        store.append_round(own, "#1 小红：连通之前", "{}", [(1, 1001, 21, "小红")], now=1)
        sync, _ = store.link_channels((CHANNEL, GUILD, "闲聊"), (HALL, OTHER, "大厅"), now=2)
        store.link_channels((LOBBY, GUILD, "大厅"), (CHANNEL, GUILD, "闲聊"), now=3)
        shared = store.load_conversation(sync, 0)
        shared.next_short_id = 3
        store.append_round(shared, "#2 小红：我养了一只猫", "{}", [(2, 1002, 21, "小红")], now=4, origin=HALL)
        own = store.load_conversation(HALL, OTHER)
        own.next_short_id = 3
        store.append_round(own, "#2 小红：旧版本写的", "{}", [(2, 1999, 21, "小红")], now=5)
        self.assertEqual(store.stale_syncs(), [HALL])
        with self.assertLogs("dsbot.store", "WARNING"):
            store.unlink_channel(HALL, now=6)
        return store, sync

    def test_the_part_left_in_the_group_is_forgotten_once_cleared_or_dissolved(self):
        store, sync = self.left_stale()
        self.assertIn(HALL, store.sync_joined_turns(sync))  # 没拿回去的那部分留在组里整理
        store.drop_origin(sync, HALL)  # 断开以后它清空了记忆：组里它那部分删掉了
        self.assertNotIn(HALL, store.sync_joined_turns(sync))
        store, sync = self.left_stale()
        store.unlink_channel(CHANNEL, now=7)  # 组散了
        self.assertEqual(store.sync_joined_turns(sync), {})


if __name__ == "__main__":
    unittest.main()
