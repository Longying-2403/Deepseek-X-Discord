"""2.1.0：消息被删、被编辑时同步（缓冲区、正在处理的一轮、已经写进记录的）。"""

import json
import unittest

from helpers import CHANNEL, GUILD, make_engine, msg

from dsbot.memory import CardStore, MemoryStore
from dsbot.models import FileAttachment, ImageAttachment
from dsbot.prompts import EDITED_MARK, MESSAGE_DELETED
from dsbot.render import content_text, history_lines, own_text, review_text, rewrite_message

ACTIONS = lambda *items: json.dumps({"actions": list(items)}, ensure_ascii=False)  # noqa: E731
REPLY = lambda to, *texts: {"type": "reply", "to": to, "messages": list(texts)}  # noqa: E731
IMG = "data:image/jpeg;base64,AAAA"


async def loader(att, max_side):
    return IMG


def stored(engine):
    """数据库里这个频道的记录（确认改写真的存下来了）。"""
    return engine.store.load_conversation(CHANNEL, GUILD).turns


class Rewrite(unittest.TestCase):
    def test_replace_a_message_and_its_continuation_lines(self):
        content = "【10-13 14:02】\n#1 阿明：第一行\n  第二行\n#2 小雨：嗯\n（#1 在叫你，回应一下，用对方那条消息的语言回。）"
        new, changed = rewrite_message(content, 1, ["#1 阿明：改过了" + EDITED_MARK], drop_images=False, drop_docs=False)
        self.assertTrue(changed)
        self.assertEqual(new.split("\n")[1:3], ["#1 阿明：改过了 〔已编辑〕", "#2 小雨：嗯"])

    def test_nothing_to_change_returns_the_same_object(self):
        content = [{"type": "text", "text": "#1 阿明：早"}, {"type": "image_url", "image_url": {"url": IMG}}]
        same, changed = rewrite_message(content, 7, ["#7 x：y"], drop_images=True, drop_docs=True)
        self.assertIs(same, content)
        self.assertFalse(changed)

    def test_images_and_files_of_the_message_are_dropped(self):
        content = [
            {"type": "text", "text": "【10-13】\n#1 阿明：[图片] 看"},
            {"type": "image_url", "image_url": {"url": IMG, "detail": "low"}},
            {"type": "text", "text": "（以上是 #1 的图片）\n【#1 的文件「a.pdf」· PDF】\n<<<\n正文\n>>>"},
            {"type": "image_url", "image_url": {"url": IMG, "detail": "auto"}},
            {"type": "text", "text": "（以上是 #1 的文件「a.pdf」的页面）\n#2 小雨：嗯"},
        ]
        new, changed = rewrite_message(content, 1, ["#1 阿明：" + MESSAGE_DELETED], drop_images=True, drop_docs=True)
        self.assertTrue(changed)
        self.assertIsInstance(new, str)  # 图都没了：变回一段文字
        self.assertEqual(new.split("\n"), ["【10-13】", "#1 阿明：（这条消息已删除）", "#2 小雨：嗯"])

    def test_lines_inside_fences_are_not_messages(self):
        content = "#3 小雨：看文件\n【#3 的文件「a.txt」· 文本】\n<<<\n#1 阿明：文件里写的\n>>>\n#1 阿明：真的消息"
        new, _ = rewrite_message(content, 1, ["#1 阿明：" + MESSAGE_DELETED], drop_images=True, drop_docs=True)
        self.assertIn("#1 阿明：文件里写的", new)
        self.assertTrue(new.endswith("#1 阿明：（这条消息已删除）"))
        self.assertNotIn("真的消息", new)

    def test_history_review_and_summary_skip_deleted_lines(self):
        content = "#1 阿明：" + MESSAGE_DELETED + "\n#2 小雨：好" + EDITED_MARK
        self.assertEqual(history_lines([("user", content)], "小D"), ["小雨：好"])
        self.assertEqual(review_text(content), ["#2 小雨：好 〔已编辑〕"])
        self.assertNotIn(MESSAGE_DELETED, own_text(content))


class Deletes(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_before_the_round_runs(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "在")))
        await engine.handle_message(msg(501, "@小D 在吗", mentions_bot=True))
        engine.handle_delete(GUILD, CHANNEL, [501])  # 还没轮到它就删了
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertEqual(engine.states[CHANNEL].pending, [])

    async def test_deleted_while_the_model_is_answering(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "在"), {"type": "react", "to": 1, "emoji": "👍"}))

        async def hook(call):
            engine.handle_delete(GUILD, CHANNEL, [501])

        llm.hook = hook
        await engine.handle_message(msg(501, "@小D 我的密码是 hunter2", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.sent, [])  # 不回已经删掉的消息
        self.assertEqual(transport.reactions, [])
        for turns in (engine.states[CHANNEL].conversation.turns, stored(engine)):
            text = content_text(turns[0].content)
            self.assertIn("#1 阿明：（这条消息已删除）", text)
            self.assertNotIn("hunter2", text)

    async def test_deleted_after_it_is_in_the_history(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(2, "好看")), image_loader=loader)
        await engine.handle_message(msg(501, "看我的照片", images=[ImageAttachment("me.png", "image/png", 10)]))
        await engine.handle_message(msg(502, "@小D 怎么样", mentions_bot=True, author=12, name="小雨"))
        await engine.drain()
        self.assertIsInstance(engine.states[CHANNEL].conversation.turns[0].content, list)
        engine.handle_delete(GUILD, CHANNEL, [501])
        await engine.drain()
        for turns in (engine.states[CHANNEL].conversation.turns, stored(engine)):
            content = turns[0].content
            self.assertIsInstance(content, str)  # 照片也拿掉了
            self.assertIn("#1 阿明：（这条消息已删除）\n#2 小雨：@小D 怎么样", content)
        clock.advance(30)
        await engine.handle_message(msg(503, "@小D 嗯", mentions_bot=True))
        await engine.drain()
        self.assertIn("#1 阿明：（这条消息已删除）", content_text(llm.calls[-1]["messages"][1]["content"]))

    async def test_bulk_delete_and_files(self):
        async def reader(att, max_chars):
            from dsbot.models import FileText

            return FileText(kind="文本", text="机密内容")

        engine, llm, transport, clock = make_engine(lambda call: ACTIONS())
        engine.file_reader = reader
        await engine.handle_message(msg(501, "@小D 看看", mentions_bot=True, files=[FileAttachment("a.txt", None, 10)]))
        await engine.drain()
        self.assertIn("机密内容", content_text(stored(engine)[0].content))
        engine.handle_delete(GUILD, CHANNEL, [501, 999])
        await engine.drain()
        text = content_text(stored(engine)[0].content)
        self.assertNotIn("机密内容", text)
        self.assertNotIn("a.txt", text)
        self.assertNotIn(501, engine.states[CHANNEL].recent_files)  # 之后也不能再用 read 打开

    async def test_after_a_restart(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS())
        await engine.handle_message(msg(501, "@小D 秘密", mentions_bot=True))
        await engine.drain()
        engine.states.clear()  # 像刚重启：内存里什么都没有
        engine.handle_delete(GUILD, CHANNEL, [501])
        await engine.drain()
        self.assertIn(MESSAGE_DELETED, content_text(stored(engine)[0].content))
        engine.handle_delete(GUILD, 555, [1])  # 没聊过的频道：什么都不做
        self.assertNotIn(555, engine.states)

    async def test_card_request_deleted_while_answering(self):
        cards, memory = CardStore(":memory:"), MemoryStore(":memory:")
        out = ACTIONS({"type": "card", "to": 1, "text": "真名张三"}, REPLY(1, "记住了"))
        engine, llm, transport, clock = make_engine(lambda call: out if call["json_mode"] else "摘要", cards=cards, memory=memory)

        async def hook(call):
            engine.handle_delete(GUILD, CHANNEL, [501])

        llm.hook = hook
        await engine.handle_message(msg(501, "@小D 记住我真名叫张三", mentions_bot=True))
        await engine.drain()
        self.assertIsNone(cards.get(GUILD, 11))
        self.assertEqual(transport.reactions, [])

    async def test_memory_request_deleted_while_answering(self):
        cards, memory = CardStore(":memory:"), MemoryStore(":memory:")
        out = ACTIONS({"type": "memory", "add": "周五打副本"}, REPLY(1, "记下了"))
        engine, llm, transport, clock = make_engine(lambda call: out if call["json_mode"] else "摘要", cards=cards, memory=memory)

        async def hook(call):
            engine.handle_delete(GUILD, CHANNEL, [501])

        llm.hook = hook
        await engine.handle_message(msg(501, "@小D 记一下周五打副本", mentions_bot=True))
        await engine.drain()
        self.assertEqual(memory.count(GUILD), 0)

    async def test_not_carried_to_other_channels(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "收到")))
        await engine.handle_message(msg(501, "@小D 帮我记个事", mentions_bot=True))
        await engine.drain()
        self.assertTrue(engine.interactions[(GUILD, 11)])
        engine.handle_delete(GUILD, CHANNEL, [501])
        self.assertFalse(engine.interactions[(GUILD, 11)])


class DeletedEarly(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_while_waiting_for_the_link_preview(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "看了")))

        async def sleep_and_delete(seconds):
            engine.handle_delete(GUILD, CHANNEL, [501])  # 等预览的那两秒里删掉了

        engine.sleep = sleep_and_delete
        await engine.handle_message(msg(501, "@小D 看这个 https://example.com 我的密码是 hunter2", mentions_bot=True, awaiting_embeds=True))
        await engine.drain()
        self.assertEqual(llm.calls, [])  # 没给模型看
        self.assertEqual(stored(engine), [])  # 也没写进记录

    async def test_skipped_replies_are_not_send_failures(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "在的")))
        mid = [500]

        async def hook(call):
            engine.handle_delete(GUILD, CHANNEL, [mid[0]])  # 比如管理 bot 删掉了刷屏的消息

        llm.hook = hook
        for i in range(4):
            mid[0] = 600 + i
            await engine.handle_message(msg(600 + i, "@小D 在吗", mentions_bot=True))
            await engine.drain()
            clock.advance(30)
        st = engine.states[CHANNEL]
        self.assertEqual((st.send_failures, st.error_until, transport.sent), (0, 0.0, []))
        self.assertNotIn("说了", st.last_decision)


class Edits(unittest.IsolatedAsyncioTestCase):
    async def test_old_messages_get_a_note_instead_of_a_rewrite(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS())
        for i in range(6):
            await engine.handle_message(msg(501 + i, f"@小D 第{i}句", mentions_bot=True))
            await engine.drain()
            clock.advance(30)
        before = [t.content for t in stored(engine)]
        engine.handle_edit(msg(501, "@小D 第0句（改过）", mentions_bot=True), edited=True)
        await engine.drain()
        self.assertEqual([t.content for t in stored(engine)], before)  # 很早的记录不动（不然后面的缓存全失效）
        await engine.handle_message(msg(510, "@小D 嗯", mentions_bot=True))
        await engine.drain()
        self.assertIn("（#1 阿明 改了这条消息，现在是：@小D 第0句（改过））", content_text(llm.calls[-1]["messages"][-1]["content"]))
        self.assertEqual(engine.states[CHANNEL].edit_notes, {})
        clock.advance(30)
        await engine.handle_message(msg(511, "@小D 好", mentions_bot=True))
        await engine.drain()
        self.assertNotIn("改了这条消息", content_text(llm.calls[-1]["messages"][-1]["content"]))  # 说过一次就够了
        engine.handle_delete(GUILD, CHANNEL, [501])  # 后来又删了：附的那句（里面有改过的内容）也要拿掉
        await engine.drain()
        everything = "".join(content_text(t.content) for t in stored(engine))
        self.assertNotIn("第0句（改过）", everything)
        self.assertNotIn("第0句", everything)

    async def test_clearing_memory_drops_pending_edit_notes(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS())
        for i in range(6):
            await engine.handle_message(msg(501 + i, f"@小D 第{i}句", mentions_bot=True))
            await engine.drain()
            clock.advance(30)
        engine.handle_edit(msg(501, "@小D 我的手机号是 0912-345-678", mentions_bot=True), edited=True)
        await engine.drain()
        engine.reset_channel(CHANNEL)
        await engine.handle_message(msg(510, "@小D 嗯", mentions_bot=True))
        await engine.drain()
        self.assertNotIn("0912", content_text(llm.calls[-1]["messages"][-1]["content"]))

    async def test_other_bots_editing_their_messages_do_not_touch_the_history(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS())
        await engine.handle_message(msg(500, "正在播放 0:01", author=50, name="音乐bot", is_bot=True))
        await engine.handle_message(msg(501, "@小D 好听", mentions_bot=True))
        await engine.drain()
        before = stored(engine)[0].content
        for second in range(2, 6):
            engine.handle_edit(msg(500, f"正在播放 0:0{second}", author=50, name="音乐bot", is_bot=True), edited=True)
        await engine.drain()
        self.assertEqual(stored(engine)[0].content, before)


    async def test_edit_after_it_is_in_the_history(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "台北好玩")))
        await engine.handle_message(msg(501, "@小D 我明天去台北", mentions_bot=True))
        await engine.drain()
        engine.handle_edit(msg(501, "@小D 我明天去台中", mentions_bot=True), edited=True)
        await engine.drain()
        for turns in (engine.states[CHANNEL].conversation.turns, stored(engine)):
            self.assertIn("#1 阿明：@小D 我明天去台中 〔@你〕 〔已编辑〕", content_text(turns[0].content))
        before = stored(engine)[0].content
        engine.handle_edit(msg(501, "@小D 我明天去台中", mentions_bot=True), edited=True)  # 内容一样：不用再改
        await engine.drain()
        self.assertEqual(stored(engine)[0].content, before)

    async def test_preview_updates_do_not_touch_the_history(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS())
        await engine.handle_message(msg(501, "@小D 看", mentions_bot=True))
        await engine.drain()
        before = stored(engine)[0].content
        engine.handle_edit(msg(501, "@小D 看 [链接预览: 网站]", mentions_bot=True), edited=False)
        await engine.drain()
        self.assertEqual(stored(engine)[0].content, before)

    async def test_edit_while_the_model_is_answering(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "好")))

        async def hook(call):
            engine.handle_edit(msg(501, "@小D 改过的", mentions_bot=True), edited=True)

        llm.hook = hook
        await engine.handle_message(msg(501, "@小D 原来的", mentions_bot=True))
        await engine.drain()
        self.assertIn("#1 阿明：@小D 改过的 〔@你〕 〔已编辑〕", content_text(stored(engine)[0].content))

    async def test_adding_or_removing_the_mention_before_it_answers(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "在")))
        await engine.handle_message(msg(501, "在吗"))  # 忘了 @
        engine.handle_edit(msg(501, "@小D 在吗", mentions_bot=True), edited=True)
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(transport.sent[0][1], "在")
        clock.advance(30)
        await engine.handle_message(msg(502, "@小D 算了", mentions_bot=True))
        engine.handle_edit(msg(502, "算了"), edited=True)  # @ 错了，删掉
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertFalse(engine.states[CHANNEL].mention_due)


if __name__ == "__main__":
    unittest.main()
