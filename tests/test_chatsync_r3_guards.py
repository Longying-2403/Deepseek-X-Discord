"""2.8.0 连通的频道：第二、三轮修复里有好几道保险，原来的测试只看得出其中一道改坏了。这里各补一个只有这一道守着的场景。

- 压缩连通组的记录：压掉的那部分里已经断开的频道说的、退出又连回来的频道之前那次连通时说的，不在组里整理（压缩前挑出来的
  那批原话马上就没了，只靠挑的时候这一关；安静时整理每问一段前还会再看一次谁还在组里）。
- 安静时整理的时候压缩了：问完手上这一段就停（压掉的连同后来说的挑出来排着队，它要是接着问，后来说的那几句之前的就整理两遍）。
- 安静时整理问到一半，有频道断开了：轮到它那段时跳过，排在后面的别的频道照样在这一次整理。
- 断开时拿回来的：它自己那份连通之前还有没整理成的，整理进度不跳过它们。
- 组散了：排着队的整理照样记进人设卡（谁说的先记下了：库里的编号映射跟着组删掉了），也不再给散掉的组记进度、留着它的状态。
- 很早的消息改了、还没附上的那句：那个频道断开时，组里的记录直接改进去（它不会再在组里说话了）。"""

import asyncio
import json
import re
import unittest

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, connect, disconnect, replies, say, text_of, world
from test_chatsync_lifecycle import spin
from test_chatsync_privacy import record
from test_chatsync_r2_records import compact, hold_reviews, times_reviewed
from test_chatsync_records import three_linked
from test_chatsync_review import remembering, reviews, summary_calls
from test_dm import OTHER
from test_memory import is_review, stores

from dsbot.llm import LLMError

HEAVY = "很长的一件事" * 300  # 比后面几轮加起来还长：强制压缩正好压掉它那一轮


class CompactingTheGroup(unittest.IsolatedAsyncioTestCase):
    async def linked_with_old_hall_chat(self):
        """三个频道连通（断开一个，组还在）；最早的一轮是别的服 #大厅说的，很长。平时不整理（攒不够轮数），只看压缩前挑的。"""
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "小红的约定" if "别的服的事" in text else "主服的约定"),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1000},
        )
        three_linked(engine)
        await say(engine, clock, 1, f"@小D 别的服的事{HEAVY}", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 主服的事")
        await say(engine, clock, 3, "@小D 主服的另一件事")
        return engine, llm, clock

    async def test_compacting_the_group_does_not_review_a_channel_that_left(self):
        engine, llm, clock = await self.linked_with_old_hall_chat()
        disconnect(engine, HALL)  # 组还在：#大厅 拿回自己那部分（还没整理过），组里那份也还留着
        await compact(engine, CHANNEL)
        await engine.drain()
        self.assertEqual(len(summary_calls(llm)), 1)  # 真的压缩了：压掉的就是 #大厅 那一轮
        self.assertEqual(times_reviewed(llm, "别的服的事"), 0)  # 不在组里整理
        engine.ec.review_min_rounds = 1
        clock.advance(700)
        await engine.tick()
        await engine.drain()
        self.assertEqual(times_reviewed(llm, "别的服的事"), 1)  # 在它自己那份里整理一次

    async def test_compacting_the_group_does_not_review_the_earlier_link_of_a_channel_that_rejoined(self):
        engine, llm, clock = await self.linked_with_old_hall_chat()
        disconnect(engine, HALL)  # #大厅 拿回这次连通期间的那部分
        connect(engine, HALL, CHANNEL)  # 又连回来：它自己那份（拿回来的那部分）连的时候整理掉
        await engine.drain()
        self.assertEqual(times_reviewed(llm, "别的服的事"), 1)
        await compact(engine, CHANNEL)
        await engine.drain()
        self.assertEqual(len(summary_calls(llm)), 1)
        self.assertEqual(times_reviewed(llm, "别的服的事"), 1)  # 之前那次连通时说的，组里不再整理一遍


async def hear(engine, mid, text, *, channel=CHANNEL, author=11, name="阿明"):
    """有人叫它（不等后台的整理：那边正停着等模型），让这一轮跑完。"""
    guild = engine.transport.locate(channel).guild_id
    await engine.handle_message(msg(mid, text, channel_id=channel, guild_id=guild, author=author, name=name, mentions_bot=True))
    await spin()


class QuietRunAndCompaction(unittest.IsolatedAsyncioTestCase):
    async def test_the_quiet_run_stops_after_a_compaction_so_nothing_is_reviewed_twice(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "记一下"), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1}
        )
        connect(engine)
        await say(engine, clock, 1, "@小D 主服先说")
        await say(engine, clock, 2, "@小D 别的服的旧事", channel=HALL, author=21, name="小红")
        gate = asyncio.Event()
        hold_reviews(llm, "主服先说", gate)
        clock.advance(700)
        await engine.tick()
        await spin()  # 安静时整理：先问主服那段，模型还没回（这一批里别的服那段只有“别的服的旧事”）
        await hear(engine, 3, "@小D 别的服又说" + "很长的新事" * 100, channel=HALL, author=21, name="小红")
        await hear(engine, 4, "@小D 主服再说")
        await compact(engine, CHANNEL)  # 压掉前三轮：别的服的旧事连同后来说的挑出来，是同一段
        gate.set()
        await engine.drain()
        self.assertEqual(len(summary_calls(llm)), 1)
        self.assertEqual(times_reviewed(llm, "主服先说"), 1)
        self.assertEqual(times_reviewed(llm, "别的服的旧事"), 1)  # 安静时整理问完手上那段就停了，没再问它

    async def test_a_channel_that_left_before_its_turn_is_skipped_and_the_rest_are_still_reviewed(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "记一下"), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1}
        )
        three_linked(engine)
        await say(engine, clock, 1, "@小D 别的服的事", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 主服闲聊的事")
        await say(engine, clock, 3, "@小D 主服大厅的事", channel=LOBBY, author=12, name="小雨")
        gate = asyncio.Event()
        hold_reviews(llm, "别的服的事", gate)
        clock.advance(700)
        await engine.tick()
        await spin()  # 先问别的服那段，模型还没回
        disconnect(engine, CHANNEL)  # 这时主服 #闲聊 断开（组里还剩两个）
        gate.set()
        await engine.drain()
        self.assertEqual(times_reviewed(llm, "主服闲聊的事"), 0)  # 它那段跳过：拿回去在它自己那份里整理
        self.assertEqual(times_reviewed(llm, "主服大厅的事"), 1)  # 排在后面的照样在这一次整理


class TakingBack(unittest.IsolatedAsyncioTestCase):
    async def test_old_chat_that_was_never_reviewed_is_not_skipped_when_taking_back(self):
        cards, memory = stores()
        down = {"old": True}
        others = replies()

        def responder(call):
            if is_review(call):
                text = text_of(call["messages"][-1]["content"])
                if "连通之前的事" in text and down["old"]:
                    return LLMError("Service Unavailable", 503)  # 暂时的问题：连的时候、组里整理时都没整理成
                add = "记下了旧的" if "连通之前的事" in text else "记下了新的"
                return json.dumps({"cards": [], "memory": [{"add": add}]}, ensure_ascii=False)
            return others(call)

        engine, llm, transport, clock = world(responder, cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1})
        with self.assertLogs("dsbot.engine", "INFO"):
            await say(engine, clock, 1, "@小D 连通之前的事", channel=HALL, author=21, name="小红")
            connect(engine)
            await engine.drain()
            await say(engine, clock, 2, "@小D 连通以后的事", channel=HALL, author=21, name="小红")
            clock.advance(700)
            await engine.tick()
            await engine.drain()
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["记下了新的"])  # 组里整理过的那段
        disconnect(engine, HALL)  # 组散了：拿回来的那段在组里整理过；连通之前的还没整理成
        down["old"] = False
        clock.advance(5000)
        await engine.tick()
        await engine.drain()
        self.assertEqual(sorted(m.text for m in memory.entries(OTHER)), ["记下了新的", "记下了旧的"])
        self.assertEqual(times_reviewed(llm, "连通以后的事"), 1)  # 拿回来的不再整理一遍


class Dissolving(unittest.IsolatedAsyncioTestCase):
    async def test_a_queued_review_still_writes_cards_and_leaves_nothing_of_the_group_behind(self):
        cards, memory = stores()
        others = replies()

        def responder(call):
            if is_review(call):
                text = text_of(call["messages"][-1]["content"])
                found = re.search(r"#(\d+) 小红：[^\n]*我的猫叫咪咪", text)
                if found:
                    return json.dumps({"cards": [{"to": int(found.group(1)), "text": "养了一只叫咪咪的猫"}]}, ensure_ascii=False)
                return json.dumps({"cards": []})
            return others(call)

        engine, llm, transport, clock = world(responder, cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1000})
        sync = connect(engine)
        await say(engine, clock, 1, f"@小D 主服的事{HEAVY}")
        await say(engine, clock, 2, f"@小D 我的猫叫咪咪{HEAVY}", channel=HALL, author=21, name="小红")
        await say(engine, clock, 3, "@小D 主服后来的事")
        gate = asyncio.Event()

        async def hook(call):
            if is_review(call):
                await gate.wait()  # 模型回得慢

        llm.hook = hook
        await compact(engine, CHANNEL)  # 压掉前两轮：两个服务器各一段，排队整理（正在问主服那段）
        await spin()
        self.assertEqual(len(reviews(llm)), 1)
        disconnect(engine, CHANNEL)  # 只有两个频道：组散了，库里组的编号映射也删了
        gate.set()
        await engine.drain()
        card = cards.get(OTHER, 21)
        self.assertIsNotNone(card)  # 照样认得出 #2 是小红说的
        self.assertEqual(card.text, "养了一只叫咪咪的猫")
        self.assertIsNone(engine.store.db.execute("SELECT 1 FROM conversations WHERE channel_id = ?", (sync,)).fetchone())
        self.assertNotIn(sync, engine.convs)


class EditNoteWhenLeaving(unittest.IsolatedAsyncioTestCase):
    async def test_an_edit_note_waiting_in_the_group_is_written_into_the_group_record_when_its_channel_leaves(self):
        engine, llm, transport, clock = world()
        sync = three_linked(engine)
        await say(engine, clock, 5, "@小D 我的号码是 138", channel=HALL, author=21, name="小红")
        for i in range(5):
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")  # 那条变成了很早的消息
        engine.handle_edit(msg(5, "@小D 我的号码改成 139", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True), edited=True)
        await engine.drain()
        self.assertIn(5, engine.convs[sync].edit_notes)  # 等着别的服下一轮告诉它
        self.assertIn("我的号码是 138", record(engine, sync))
        disconnect(engine, HALL)  # 别的服没再说话就断开了（组还在）
        text = record(engine, sync)
        self.assertIn("我的号码改成 139", text)  # 组里的记录直接改了
        self.assertNotIn("我的号码是 138", text)


if __name__ == "__main__":
    unittest.main()
