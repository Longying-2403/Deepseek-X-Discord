"""2.8.0 连通的频道，第二轮评审修掉的问题：共用记录的整理记忆。

安静时整理只占着正在问的那一段：问的时候压缩了旧记录，压掉的（正在问的那段除外）挑出来排队整理，这次问不到的也不漏掉。
一个服务器那部分整理不成只耽误它自己：别的服务器的照常整理；连着失败放弃时只放弃它那部分，等预算的那部分不算整理过；
压缩前挑出来的那批有一段没整理成，别的服务器那段照样整理。清空一个连通的频道、组散了：压缩前挑出来还没整理完的照样
整理完（清空的那个频道的不再整理）。分开整理完的那几段每条记录上都记着：断开拿回去、重启以后都不再整理一遍。连通时
它自己原来那份没整理成（暂时出错、预算用完了）不算整理过，组里下次整理完接着整理。断开、组散了拿回去还没整理的，不用等
再聊几轮就整理。"""

import asyncio
import unittest
from unittest.mock import patch

from helpers import CHANNEL, GUILD
from test_chatsync import HALL, connect, disconnect, say, text_of, world
from test_chatsync_lifecycle import spin
from test_chatsync_review import remembering, reviews, summary_calls
from test_dm import OTHER
from test_memory import is_review, stores

from dsbot.engine import MAX_REVIEW_FAILURES
from dsbot.llm import LLMError


def failing(ok, fail):
    """整理记忆时 fail(给它看的聊天) 返回一个错误，就当成问模型出了这个错（没整理成）；不然照 ok 回。"""

    def responder(call):
        if is_review(call):
            err = fail(text_of(call["messages"][-1]["content"]))
            if err is not None:
                return err
        return ok(call)

    return responder


def cat_noted(text):
    """整理到别的服第一轮（我的猫叫咪咪）时记一条长期记忆。"""
    return "小红的猫叫咪咪" if "我的猫叫咪咪" in text else None


async def take_turns(engine, clock, rounds=4):
    """两边轮流被叫，主服先说：整理时主服那部分是第一段，别的服的排在后面。别的服第一轮说的是“我的猫叫咪咪”。"""
    for i in range(rounds):
        await say(engine, clock, 10 + 2 * i, f"@小D 主服第{i}句")
        first = "我的猫叫咪咪" if i == 0 else f"别的服第{i}句"
        await say(engine, clock, 11 + 2 * i, f"@小D {first}", channel=HALL, author=21, name="小红")


async def compact(engine):
    """主服那边聊长了：压缩共用记录的旧记录（压掉的有两边的头几轮）。"""
    st = engine.states[CHANNEL]
    await engine._compact(st, engine._conversation(st), engine.transport.channel_info(CHANNEL), force=True)


def summarized(llm):
    """最近一次压缩摘要给模型看的聊天（压掉的那部分）。"""
    return text_of(summary_calls(llm)[-1]["messages"][-1]["content"])


async def quiet(engine, clock, seconds=5000, times=1):
    """安静了一阵（times 次），到时间就整理记忆。"""
    for _ in range(times):
        clock.advance(seconds)
        await engine.tick()
        await engine.drain()


class CompactionDuringQuietReview(unittest.IsolatedAsyncioTestCase):
    async def test_a_part_compacted_during_a_quiet_review_is_reviewed_even_if_that_run_stops_before_it(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(cat_noted), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1}
        )
        connect(engine)
        await take_turns(engine, clock)
        asked = []  # 压缩的时候正在问的那一段

        async def hook(call):
            if is_review(call) and not asked:
                asked.append(text_of(call["messages"][-1]["content"]))
                await compact(engine)  # 问模型的时候压缩了旧记录

        llm.hook = hook
        with patch("dsbot.engine.MAX_SYNC_REVIEW_CHUNKS", 1):  # 代替 8：这次安静时整理只问得到第一段（主服那段）
            await quiet(engine, clock, 700)
            self.assertIn("主服第0句", asked[0])
            self.assertIn("我的猫叫咪咪", summarized(llm))  # 别的服第一轮压掉了（它那段这次问不到）
            await quiet(engine, clock, times=2)
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红的猫叫咪咪"])  # 压掉的那轮挑出来排队整理了
        self.assertEqual(sum("主服第0句" in text for _, text in reviews(llm)), 1)  # 守着：正在问的那段不另外挑出来再整理一遍


class FailureIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_a_part_that_keeps_failing_does_not_hold_up_the_other_servers_part(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            failing(
                remembering(lambda text: "小红养了一只猫" if "我养了一只猫" in text else None),
                lambda text: LLMError("Content Exists Risk", 400) if "主服的话" in text else None,  # 每次都被拒：算失败
            ),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        connect(engine)
        await say(engine, clock, 1, "@小D 主服的话")  # 主服先说：先问它那段，别的服那段排在它后面
        await say(engine, clock, 2, "@小D 我养了一只猫", channel=HALL, author=21, name="小红")
        with self.assertLogs("dsbot.engine", "WARNING"):
            await quiet(engine, clock, 100_000)
            # 主服那段没整理成：只是这次不问它后面的，别的服那部分照样整理
            self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫"])
            await quiet(engine, clock, 100_000, times=MAX_REVIEW_FAILURES)
        # 守着：主服那部分连着被拒几次就放弃了（别的服那部分整理成了，也不把主服的失败次数清零）
        self.assertEqual(sum("主服的话" in text for _, text in reviews(llm)), MAX_REVIEW_FAILURES)

    async def test_giving_up_on_one_servers_part_does_not_skip_a_part_waiting_for_budget(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            failing(
                remembering(lambda text: "小红养了一只猫" if "我养了一只猫" in text else None),
                lambda text: LLMError("Content Exists Risk", 400) if "不能处理的话" in text else None,
            ),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        connect(engine)
        await say(engine, clock, 1, "@小D 主服不能处理的话")
        await say(engine, clock, 2, "@小D 我养了一只猫", channel=HALL, author=21, name="小红")
        engine.settings.set("guild", OTHER, "daily_budget_usd", 0.0000001)  # 别的服今天的预算用完了：它那部分等着
        with self.assertLogs("dsbot.engine", "WARNING"):
            for wait in (700, 1300, 2500):  # 同一天里安静了三次（失败过等得越来越久）
                await quiet(engine, clock, wait)
        self.assertEqual(sum("不能处理的话" in text for _, text in reviews(llm)), MAX_REVIEW_FAILURES)  # 主服那部分放弃了
        engine.settings.set("guild", OTHER, "daily_budget_usd", 5.0)  # 预算调高了（或者第二天）
        await say(engine, clock, 3, "@小D 主服第二句")
        await quiet(engine, clock, times=2)
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫"])  # 放弃主服那部分不连带它

    async def test_a_failed_chunk_in_a_compaction_review_does_not_drop_the_other_servers_chunk(self):
        cards, memory = stores()
        down = {"left": 1}  # 问主服那段的时候连不上一次

        def fail(text):
            if "主服第0句" in text and down["left"]:
                down["left"] -= 1
                return LLMError("连不上", 503)
            return None

        engine, llm, transport, clock = world(
            failing(remembering(cat_noted), fail), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1000}
        )
        connect(engine)
        await take_turns(engine, clock)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await compact(engine)  # 压掉的挑出来排队整理：主服那段在前、别的服那段在后
            await engine.drain()
        self.assertIn("我的猫叫咪咪", summarized(llm))
        self.assertEqual(down["left"], 0)  # 主服那段没整理成
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红的猫叫咪咪"])  # 别的服那段照样整理


class ClearAndDissolve(unittest.IsolatedAsyncioTestCase):
    async def review_in_flight(self):
        """两边各说了四轮，压缩旧记录：压掉的（两个服务器的各一段）排队整理，正在问主服那一段、模型还没回。"""

        def noted(text):
            return cat_noted(text) or ("阿明说过主服的事" if "主服第0句" in text else None)

        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(noted), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1000}
        )
        connect(engine)
        await take_turns(engine, clock)
        gate = asyncio.Event()

        async def hook(call):
            if is_review(call):
                await gate.wait()  # 模型回得慢

        llm.hook = hook
        await compact(engine)
        self.assertIn("我的猫叫咪咪", summarized(llm))
        await spin()
        self.assertIn("主服第0句", reviews(llm)[-1][1])
        return engine, memory, gate

    async def test_clearing_one_channel_still_reviews_the_others_compacted_chat(self):
        engine, memory, gate = await self.review_in_flight()
        await engine.reset_linked(CHANNEL)  # 主服 /ds 清空记忆：只清它那部分
        gate.set()
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红的猫叫咪咪"])  # 别的服压掉的接着整理
        # 守着：清空的那个频道压掉的不跟着接着整理（正在问的那段问完作废）
        self.assertEqual([m.text for m in memory.entries(GUILD)], [])

    async def test_dissolving_still_finishes_the_review_of_compacted_chat(self):
        engine, memory, gate = await self.review_in_flight()
        disconnect(engine, CHANNEL)  # 只有两个频道：组散了（没人要清空什么）
        gate.set()
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红的猫叫咪咪"])
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明说过主服的事"])  # 正在问的主服那段也整理完


class ReviewMarks(unittest.IsolatedAsyncioTestCase):
    async def partly_reviewed(self):
        """别的服先说、主服后说；别的服今天的预算用完了：安静时只整理了主服那部分（组的进度停在别的服那轮前面）。"""
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "周日一起爬山" if "周日爬山" in text else None),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        connect(engine)
        await say(engine, clock, 1, "@小D 别的服先说", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 主服的约定：周日爬山")
        engine.settings.set("guild", OTHER, "daily_budget_usd", 0.0000001)  # 别的服今天的预算用完了
        await quiet(engine, clock, 700)
        self.assertEqual([uid for uid, _ in reviews(llm)], ["guild-1"])  # 只整理了主服那部分
        return engine, llm, clock

    async def test_a_part_reviewed_in_a_partial_run_is_not_reviewed_again_after_unlinking(self):
        engine, llm, clock = await self.partly_reviewed()
        disconnect(engine, CHANNEL)  # 组散了：两边各拿回自己那部分
        await say(engine, clock, 3, "@小D 断开以后")
        await quiet(engine, clock, 700)
        self.assertTrue(any("断开以后" in text for _, text in reviews(llm)))  # 主服自己那份整理过了
        self.assertEqual(sum("周日爬山" in text for _, text in reviews(llm)), 1)  # 拿回来的那轮在组里整理过：不再整理

    async def test_a_part_reviewed_in_a_partial_run_is_not_reviewed_again_after_a_restart(self):
        engine, llm, clock = await self.partly_reviewed()
        engine.states.clear()
        engine.convs.clear()  # 像刚重启：内存里什么都没有
        engine.settings.set("guild", OTHER, "daily_budget_usd", 5.0)  # 第二天
        await say(engine, clock, 3, "@小D 第二天", channel=HALL, author=21, name="小红")
        await quiet(engine, clock, 700)
        self.assertTrue(any("别的服先说" in text for _, text in reviews(llm)))  # 别的服那部分这次整理了
        self.assertEqual(sum("周日爬山" in text for _, text in reviews(llm)), 1)  # 主服那部分不再整理一遍


class FrozenRecord(unittest.IsolatedAsyncioTestCase):
    async def link_when_review_cannot_run(self, *, down=False, over_budget=False):
        """别的服 #大厅 自己那份里有还没整理的约定；连通时它那份冻住了（以后不往里写），要整理掉，可那时整理不成。
        后来好了：组里下次安静时整理完，接着整理它。"""
        cards, memory = stores()
        state = {"down": down}
        engine, llm, transport, clock = world(
            failing(
                remembering(lambda text: "周日一起爬山" if "周日爬山" in text else None),
                lambda text: LLMError("连不上", 503) if state["down"] else None,
            ),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1000},
        )
        await say(engine, clock, 1, "@小D 我们约好了：周日爬山", channel=HALL, author=21, name="小红")  # 还没到整理的时候
        if over_budget:
            engine.settings.set("guild", OTHER, "daily_budget_usd", 0.0000001)
        connect(engine)
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(OTHER)], [])  # 连的时候没整理成
        state["down"] = False  # 连得上了
        engine.settings.set("guild", OTHER, "daily_budget_usd", 5.0)  # 预算调高了（或者第二天）
        engine.ec.review_min_rounds = 1
        await say(engine, clock, 2, "@小D 连通以后")
        await quiet(engine, clock, 700)
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["周日一起爬山"])  # 没算成整理过：组里整理完接着整理了它

    async def test_pre_link_chat_is_reviewed_later_if_the_review_at_link_hits_a_transient_error(self):
        with self.assertLogs("dsbot.engine", "WARNING"):
            await self.link_when_review_cannot_run(down=True)

    async def test_pre_link_chat_is_reviewed_later_if_its_server_is_over_budget_at_link_time(self):
        await self.link_when_review_cannot_run(over_budget=True)


class TakenBack(unittest.IsolatedAsyncioTestCase):
    async def test_chat_taken_back_when_the_group_dissolves_is_reviewed_without_new_rounds(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "小红养了一只猫" if "我养了一只猫" in text else ("阿明周日爬山" if "周日爬山" in text else None)),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        connect(engine)
        await say(engine, clock, 5, "@小D 我养了一只猫", channel=HALL, author=21, name="小红")
        await say(engine, clock, 6, "@小D 我周日爬山")
        disconnect(engine, CHANNEL)  # 组里还没整理就散了：两边各拿回自己那部分
        await quiet(engine, clock, times=2)  # 安静了好几个小时，两边都没人再说话
        # 拿回去还没整理的照样整理（不用等那边再聊几轮），各记进自己的服务器
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫"])
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["阿明周日爬山"])


if __name__ == "__main__":
    unittest.main()
