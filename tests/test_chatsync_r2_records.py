"""2.8.0 连通的频道，第二轮评审修掉的问题：共用记录的编号映射、整理记忆的进度、清空和断开时别的频道的东西。

编号：连进来一个自己那份编号用得多的频道，共用记录的编号往后跳一大段，跳之前的编号映射照样留着（按条数留），删改照样改得到，
断开的频道也拿得回它的编号。整理记忆：组里整理完一段就记在那几条记录上，断开拿回去、重启以后都不再整理一遍；问模型的时候
有频道断开了，它那部分只在它自己那份里整理；安静时整理只占着正在问的那一段，中途压缩了，压掉的照样排队整理（没压掉的下次接着
整理）。清空一个频道的记忆：别的频道排着队的整理、还没附上的“改成了什么”照旧，它那几轮里读过的别的频道的文件被问到时能重新读。
连通之前改过、还没附上的旧消息，连通时改进它自己原来那份。断开以后：改很早的消息、清空记忆（原话已经压进了摘要的也算），
它离开的那个组的记录跟着改；连通期间删掉的文件不会又读得到（没删的照样读得到）。"""

import asyncio
import unittest
from unittest.mock import patch

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, replies, say, text_of, world
from test_chatsync_lifecycle import prompt_of, spin
from test_chatsync_privacy import last_user, record
from test_chatsync_records import msgmap, said, three_linked
from test_chatsync_review import remembering, reviews, summary_calls
from test_dm import OTHER
from test_engine_files import READ, Reader, doc
from test_memory import is_review, stores


def hold_reviews(llm, words, gate):
    """整理记忆问模型时，给它看的聊天里有 words 的那一段停在那里等 gate（“模型还在想”）。"""

    async def hook(call):
        if is_review(call) and words in text_of(call["messages"][-1]["content"]):
            await gate.wait()

    llm.hook = hook


def times_reviewed(llm, words):
    """有 words 的那句话整理过几次。"""
    return sum(words in text for _, text in reviews(llm))


def asking_to_read(words, sid):
    """叫它的这一轮话里有 words：它要看 #sid 的文件；别的照 replies() 回（看完文件那一步不说话）。"""
    others = replies()

    def responder(call):
        if call["json_mode"] and not is_review(call) and words in text_of(call["messages"][-1]["content"]):
            return READ(sid)
        return others(call)

    return responder


def files_read(reader):
    """读过的文件名，按先后。"""
    return [name for name, _ in reader.calls]


def compact(engine, channel):
    """马上压缩这个频道现在用的那份记录（不等上下文太长）。"""
    st = engine.states[channel]
    return engine._compact(st, engine._conversation(st), engine.transport.channel_info(channel), force=True)


class GroupNumbers(unittest.IsolatedAsyncioTestCase):
    async def test_a_busy_channel_joining_does_not_cut_off_the_groups_earlier_numbers(self):
        with patch("dsbot.store.MSGMAP_KEEP", 5):  # 代替 3000
            engine, llm, transport, clock = world()
            sync = connect(engine)
            await say(engine, clock, 1, "@小D 我的手机号是 13800001111")  # 共用记录的 #1
            for i in range(10):  # 「主服」#大厅聊得多：它自己那份已经用到 #10
                await say(engine, clock, 500 + i, f"@小D 大厅第{i}句", channel=LOBBY, author=12, name="小雨")
            connect(engine, LOBBY, CHANNEL)  # 连进组里：共用记录的编号接在它用过的后面，跳到 #11
            await say(engine, clock, 600, "@小D 大厅连进来了", channel=LOBBY, author=12, name="小雨")
            engine.handle_delete(GUILD, CHANNEL, [1])  # 跳之前的消息被删了
            await engine.drain()
            self.assertEqual(said(engine.store.load_conversation(sync, 0))[0], "#1 阿明：（这条消息已删除）")  # 记录照样改得到
            for i in range(5):
                await say(engine, clock, 700 + i, f"@小D 后来第{i}句")
            self.assertEqual(sorted(msgmap(engine.store, sync)), [12, 13, 14, 15, 16])  # 多了照样只留最近 5 个

    async def test_a_channel_leaving_after_the_jump_takes_back_its_numbers(self):
        with patch("dsbot.store.MSGMAP_KEEP", 5):
            engine, llm, transport, clock = world()
            connect(engine)
            await say(engine, clock, 2, "@小D 我家住在幸福路八号", channel=HALL, author=21, name="小红")  # 共用记录的 #1
            for i in range(10):
                await say(engine, clock, 500 + i, f"@小D 大厅第{i}句", channel=LOBBY, author=12, name="小雨")
            connect(engine, LOBBY, CHANNEL)  # 编号跳到 #11
            await say(engine, clock, 600, "@小D 大厅连进来了", channel=LOBBY, author=12, name="小雨")
            self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))  # 别的服 #大厅断开，组还在
            self.assertEqual(msgmap(engine.store, HALL), {1: 2})  # 它那条的编号映射跟着拿回去了
            engine.handle_delete(OTHER, HALL, [2])
            await engine.drain()
            self.assertEqual(said(engine.store.load_conversation(HALL, OTHER)), ["#1 小红：（这条消息已删除）"])


class ReviewProgress(unittest.IsolatedAsyncioTestCase):
    async def partly_reviewed(self):
        """主服今天的预算用完了：安静时整理只整理了别的服那部分（共用记录的整理进度没往后挪）。"""
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "主服的约定" if "主服的事" in text else "别的服的约定"),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        connect(engine)
        await say(engine, clock, 1, "@小D 主服的事")
        await say(engine, clock, 2, "@小D 别的服的事", channel=HALL, author=21, name="小红")
        engine.settings.set("guild", GUILD, "daily_budget_usd", 0.0000001)
        clock.advance(700)
        await engine.tick()
        await engine.drain()
        self.assertEqual([uid for uid, _ in reviews(llm)], ["guild-2"])
        return engine, llm, clock, memory

    async def test_a_part_reviewed_in_the_group_is_not_reviewed_again_after_taking_it_back(self):
        engine, llm, clock, memory = await self.partly_reviewed()
        disconnect(engine, HALL)  # 只有两个频道：组散了，各自拿回自己那部分
        await say(engine, clock, 3, "@小D 断开以后", channel=HALL, author=21, name="小红")
        clock.advance(700)
        await engine.tick()
        await engine.drain()
        self.assertEqual(times_reviewed(llm, "别的服的事"), 1)  # 在组里整理过：拿回来也算整理过
        self.assertEqual(times_reviewed(llm, "断开以后"), 1)  # 断开以后说的照常整理

    async def test_a_part_reviewed_in_the_group_is_not_reviewed_again_after_a_restart(self):
        engine, llm, clock, memory = await self.partly_reviewed()
        engine.states.clear()
        engine.convs.clear()  # 重启：内存里什么都没有
        engine.settings.set("guild", GUILD, "daily_budget_usd", 5.0)  # 第二天，主服又有预算了
        await say(engine, clock, 3, "@小D 第二天")
        clock.advance(700)
        await engine.tick()
        await engine.drain()
        self.assertEqual(times_reviewed(llm, "别的服的事"), 1)  # 整理过的记在记录上：重启以后也不再整理
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["主服的约定"])  # 主服那部分接着整理

    async def test_a_channel_that_leaves_during_the_review_has_its_part_reviewed_only_in_its_own_record(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "主服的约定" if "主服的事" in text else "别的服的约定"),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        three_linked(engine)
        await say(engine, clock, 1, "@小D 别的服的事", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 主服的事")
        gate = asyncio.Event()
        hold_reviews(llm, "别的服的事", gate)
        clock.advance(700)
        await engine.tick()
        await spin()
        self.assertEqual(times_reviewed(llm, "别的服的事"), 1)  # 先整理别的服那部分，正在问模型
        disconnect(engine, CHANNEL)  # 这时候主服 #闲聊断开了（组里还剩两个）
        gate.set()
        await engine.drain()
        await say(engine, clock, 3, "@小D 断开以后")
        clock.advance(700)
        await engine.tick()
        await engine.drain()
        mine = [text for _, text in reviews(llm) if "主服的事" in text]
        self.assertEqual(len(mine), 1)  # 组里不再整理它那部分：只整理一次
        self.assertIn("断开以后", mine[0])  # 在它自己那份里（拿回去的连同断开以后说的）


class ReviewWhileCompacting(unittest.IsolatedAsyncioTestCase):
    async def three_rounds(self):
        """别的服、主服、别的服各说一句：整理时别的服的归成一段，主服的一段。"""
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "记一条"), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1}
        )
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 别的服早先的一句话", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 主服第一句话很重要")
        await say(engine, clock, 3, "@小D 别的服第二句话", channel=HALL, author=21, name="小红")
        return engine, llm, clock, sync

    async def test_compacting_during_a_quiet_review_queues_the_part_it_has_not_reached(self):
        with patch("dsbot.engine.MAX_SYNC_REVIEW_CHUNKS", 1):  # 安静时一次只整理一段
            engine, llm, clock, sync = await self.three_rounds()
            gate = asyncio.Event()
            hold_reviews(llm, "别的服早先的一句话", gate)
            clock.advance(700)
            await engine.tick()
            await spin()
            self.assertEqual(times_reviewed(llm, "别的服早先的一句话"), 1)  # 别的服那段正在问模型
            await compact(engine, CHANNEL)  # 这时候压缩了旧记录
            self.assertIn("主服第一句话很重要", text_of(summary_calls(llm)[-1]["messages"][-1]["content"]))
            gate.set()
            await engine.drain()
            self.assertEqual(times_reviewed(llm, "主服第一句话很重要"), 1)  # 这次没轮到的那段压掉了：排队整理过了
            self.assertEqual(times_reviewed(llm, "别的服早先的一句话"), 1)  # 正在问的那段不再挑出来整理一遍

    async def test_control_parts_over_the_per_pass_limit_are_reviewed_next_time(self):
        # 对照（control）：没有压缩时，这次没轮到的那段下次照记录接着整理，整理过的不再整理
        with patch("dsbot.engine.MAX_SYNC_REVIEW_CHUNKS", 1):
            engine, llm, clock, sync = await self.three_rounds()
            for _ in range(2):  # 安静了两次
                clock.advance(700)
                await engine.tick()
                await engine.drain()
            for words in ("别的服早先的一句话", "主服第一句话很重要", "别的服第二句话"):
                self.assertEqual(times_reviewed(llm, words), 1, words)
            whole = engine.store.load_conversation(sync, 0)
            self.assertEqual(whole.reviewed_upto, whole.turns[-1].id)


class ClearingOneChannel(unittest.IsolatedAsyncioTestCase):
    async def test_clearing_one_channel_keeps_the_others_queued_compaction_review(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "小红的约定" if "别的服要记住的事" in text else "主服的约定"),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1000},
        )
        connect(engine)
        await say(engine, clock, 1, "@小D 别的服要记住的事", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 主服的话")
        for i in range(3):
            await say(engine, clock, 3 + i, f"@小D 别的服后来{i}", channel=HALL, author=21, name="小红")
        gate = asyncio.Event()
        hold_reviews(llm, "别的服要记住的事", gate)
        await compact(engine, HALL)
        dropped = text_of(summary_calls(llm)[-1]["messages"][-1]["content"])
        self.assertIn("别的服要记住的事", dropped)  # 压掉了：只剩排着队的那批里还有原话
        self.assertIn("主服的话", dropped)
        await spin()
        self.assertEqual(times_reviewed(llm, "别的服要记住的事"), 1)  # 正在问模型
        await engine.reset_linked(CHANNEL)  # 这时候主服清空了记忆
        gate.set()
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红的约定"])  # 别的服那部分照样整理完
        self.assertEqual(memory.entries(GUILD), [])  # 清空了的主服那部分不再整理

    async def test_clearing_one_channel_keeps_the_others_edit_notes(self):
        engine, llm, transport, clock = world()
        connect(engine)
        await say(engine, clock, 50, "@小D 我住在北京", channel=HALL, author=21, name="小红")
        for i in range(5):  # 主服接着聊：那条成了很早的消息（改了不动旧记录，别的服下一轮附一句）
            await say(engine, clock, 60 + i, f"@小D 主服第{i}句")
        edited = msg(50, "@小D 我住在上海", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)
        engine.handle_edit(edited, edited=True)
        await engine.drain()
        self.assertIn(50, engine.states[HALL].edit_notes)
        await engine.reset_linked(CHANNEL)  # 主服清空了记忆
        await say(engine, clock, 51, "@小D 我住在哪", channel=HALL, author=21, name="小红")
        self.assertIn("我住在上海", last_user(llm))  # 别的服那句“改成了什么”照样附在它这一轮

    async def test_after_clearing_a_file_read_in_its_rounds_can_be_read_again(self):
        engine, llm, transport, clock = world(asking_to_read("看看那边的文件", 1))
        reader = engine.file_reader = Reader()
        sync = connect(engine)
        await engine.handle_message(msg(601, "", channel_id=HALL, guild_id=OTHER, author=21, name="小红", files=[doc("报告.docx")]))
        clock.advance(300)  # 传了一阵才有人叫它：不自动读
        await say(engine, clock, 602, "@小D 随便聊聊")  # 别的服传的文件补进了共用的记录
        self.assertEqual(engine.store.load_msgmap(sync)[1], 601)  # 带文件的那条是 #1
        await say(engine, clock, 603, "@小D 看看那边的文件")  # 在主服的这一轮读了
        self.assertEqual(files_read(reader), ["报告.docx"])
        await engine.reset_linked(CHANNEL)  # 主服清空了记忆：读到的内容随那一轮一起删掉了
        await say(engine, clock, 604, "@小D 看看那边的文件", channel=HALL, author=21, name="小红")
        self.assertEqual(files_read(reader), ["报告.docx", "报告.docx"])  # 不当成“前面已经看过了”：重新读
        self.assertIn("报告.docx 的内容", record(engine, sync))


class EditBeforeLinking(unittest.IsolatedAsyncioTestCase):
    async def test_an_edit_note_waiting_at_linking_is_written_into_its_own_old_record(self):
        engine, llm, transport, clock = world()
        await say(engine, clock, 1, "@小D 我最喜欢苹果")
        for i in range(5):  # 接着聊：那条成了很早的消息（改了先记着，下一轮附一句）
            await say(engine, clock, 10 + i, f"@小D 闲聊{i}")
        engine.handle_edit(msg(1, "@小D 我最喜欢香蕉", mentions_bot=True), edited=True)
        await engine.drain()
        self.assertIn(1, engine.states[CHANNEL].edit_notes)  # 还没附上就连通了
        connect(engine)
        own = said(engine.store.load_conversation(CHANNEL, GUILD))
        self.assertEqual(own[0], "#1 阿明：@小D 我最喜欢香蕉 〔@你〕 〔已编辑〕")  # 直接改进它自己原来那份（以后不往里写了）
        await say(engine, clock, 2, "@小D 我最喜欢什么")
        system = chats(llm)[-1]["messages"][0]["content"]
        self.assertIn("我最喜欢香蕉", system)  # “连通之前”那段照改过的写
        self.assertNotIn("我最喜欢苹果", system)


class AfterLeaving(unittest.IsolatedAsyncioTestCase):
    async def test_editing_an_old_message_after_leaving_rewrites_the_group_record(self):
        engine, llm, transport, clock = world()
        sync = three_linked(engine)
        await say(engine, clock, 5, "@小D 我的手机号是 13800001111", channel=HALL, author=21, name="小红")
        for i in range(6):  # 组里接着聊：那条成了很早的消息
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")
        disconnect(engine, HALL)
        edited = msg(5, "@小D 号码撤回了", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)
        engine.handle_edit(edited, edited=True)
        await engine.drain()
        # 别的服 #大厅不会再在组里跑一轮附上“改成了什么”：直接改组的记录
        self.assertEqual(said(engine.store.load_conversation(sync, 0))[0], "#1 小红：@小D 号码撤回了 〔@你〕 〔已编辑〕")
        await say(engine, clock, 20, "@小D 刚才谁留了号码？")
        self.assertNotIn("13800001111", prompt_of(chats(llm)[-1]))  # 组里还连着的频道看不到撤回的号码

    async def test_clearing_after_leaving_also_drops_a_group_summary_holding_its_part(self):
        base = replies()

        def responder(call):
            if not call["json_mode"]:
                return "小红说她的手机号是 13800001111"  # 压缩时摘要里记下了小红说的
            return base(call)

        engine, llm, transport, clock = world(responder)
        sync = three_linked(engine)
        await say(engine, clock, 5, "@小D 我的手机号是 13800001111", channel=HALL, author=21, name="小红")
        disconnect(engine, HALL)  # 组里还剩主服的 #闲聊、#大厅
        await say(engine, clock, 6, "@小D 主服这边")
        await say(engine, clock, 7, "@小D 大厅这边", channel=LOBBY, author=12, name="小雨")
        await compact(engine, CHANNEL)
        group = engine.store.load_conversation(sync, 0)
        self.assertNotIn(HALL, {t.origin for t in group.turns})  # 它的原话都压掉了……
        self.assertIn("13800001111", group.summary)  # ……压进了组的摘要
        await engine.reset_linked(HALL)  # 断开以后在别的服 #大厅 /ds 清空记忆
        self.assertNotIn("13800001111", engine.store.load_conversation(sync, 0).summary or "")  # 组的摘要也删掉了
        self.assertEqual(engine.store.origin_syncs(HALL), [])  # 组里不再有它的东西：以后清空不再动那个组

    async def unlinked_after_a_file(self, delete):
        """主服 #闲聊有人传了个文件，聊了一阵连通；delete：连通期间删掉那条。断开以后有人要它看那个文件。返回读过的文件。"""
        engine, llm, transport, clock = world(asking_to_read("那个文件里写了啥", 1))
        reader = engine.file_reader = Reader()
        await engine.handle_message(msg(501, "", files=[doc("机密.docx")]))
        clock.advance(300)  # 传了一阵才有人叫它：不自动读
        await say(engine, clock, 502, "@小D 晚上吃啥")
        self.assertEqual(engine.store.load_msgmap(CHANNEL)[1], 501)  # 带文件的那条是 #1
        connect(engine)
        if delete:
            engine.handle_delete(GUILD, CHANNEL, [501])
            await engine.drain()
        disconnect(engine, CHANNEL)
        await say(engine, clock, 503, "@小D 那个文件里写了啥")
        return files_read(reader)

    async def test_a_file_deleted_while_linked_is_not_readable_after_unlinking(self):
        self.assertEqual(await self.unlinked_after_a_file(delete=True), [])  # 删掉的文件不会又读得到

    async def test_control_a_file_not_deleted_is_still_readable_after_unlinking(self):
        # 对照（control）：没删的文件连通时搬进共用的那份，断开时跟着搬回来，照样读得到
        self.assertEqual(await self.unlinked_after_a_file(delete=False), ["机密.docx"])


if __name__ == "__main__":
    unittest.main()
