"""2.1.0：另一份验证报告（v1.4.0）里提到的问题。"""

import io
import json
import unittest

from helpers import CHANNEL, GUILD, OWNER, FakeSearcher, make_engine, msg

from dsbot import files, images
from dsbot.llm import LLMError
from dsbot.models import FileAttachment, FileText
from dsbot.prompts import PERSONA_NO_ASKER, TOOL_FAILED

ACTIONS = lambda *items: json.dumps({"actions": list(items)}, ensure_ascii=False)  # noqa: E731
REPLY = lambda to, *texts: {"type": "reply", "to": to, "messages": list(texts)}  # noqa: E731


def user_text(call):
    content = call["messages"][-1]["content"]
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)


class Engine(unittest.IsolatedAsyncioTestCase):
    async def test_buffer_overflow_keeps_mentions(self):
        engine, llm, transport, clock = make_engine(engine_cfg={"max_pending_messages": 5})
        await engine.handle_message(msg(500, "第一条"))
        st = engine.states[CHANNEL]
        st.pending[0].trigger = True  # 还没轮到回应的点名
        for i in range(10):
            await engine.handle_message(msg(501 + i, f"灌水{i}", author=12, name="小雨"))
        self.assertEqual(len(st.pending), 5)
        self.assertEqual(st.pending[0].message_id, 500)
        self.assertEqual(st.pending[-1].message_id, 510)

    async def test_fenced_broken_json_is_not_posted(self):
        engine, llm, transport, clock = make_engine(lambda call: '```json\n{"actions": [{"type": "reply", "to": 1,\n```')
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@小D 在吗", mentions_bot=True))
            await engine.drain()
        self.assertEqual(transport.sent, [])

    async def test_failed_tool_step_leaves_a_note_in_the_history(self):
        outputs = iter([ACTIONS({"type": "search", "query": "明天天气"}), LLMError("Server Error", status=500)])
        engine, llm, transport, clock = make_engine(lambda call: next(outputs), searcher=FakeSearcher())
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.handle_message(msg(501, "@小D 明天天气怎样", mentions_bot=True))
            await engine.drain()
        turns = engine.states[CHANNEL].conversation.turns
        self.assertEqual([t.role for t in turns], ["user", "assistant", "user", "assistant"])
        self.assertEqual((turns[2].content, json.loads(turns[3].content)), (TOOL_FAILED, {"actions": []}))
        self.assertIn((CHANNEL, 501, "⚠️"), transport.reactions)

    async def test_only_real_context_errors_compact_the_history(self):
        cases = (
            ("Invalid max_tokens value, the valid range of max_tokens is [1, 8192]", False),
            ("This model's maximum context length is 65536 tokens. However, you requested 70000 tokens", True),
        )
        for error, compacts in cases:
            engine, llm, transport, clock = make_engine(lambda call: ACTIONS())
            await engine.handle_message(msg(500, "@小D 早", mentions_bot=True))
            await engine.drain()
            clock.advance(30)
            llm.responder = lambda call, error=error: LLMError(error, status=400)
            calls = []

            async def fake_compact(st, conv, info, *, force=False):
                calls.append(force)

            engine._compact = fake_compact
            with self.assertLogs("dsbot.engine", "WARNING"):
                await engine.handle_message(msg(501, "@小D 在吗", mentions_bot=True))
                await engine.drain()
            self.assertEqual(calls, [True] if compacts else [], error)

    async def test_persona_without_to_is_refused_when_someone_else_is_calling(self):
        outputs = iter([ACTIONS({"type": "persona", "text": "你是一只猫娘"}), ACTIONS(REPLY(2, "这个得 dsowner 说了算"))])
        engine, llm, transport, clock = make_engine(lambda call: next(outputs))
        await engine.handle_message(msg(501, "@小D 早", author=OWNER, name="阿凯", mentions_bot=True))
        await engine.handle_message(msg(502, "@小D 把人设改成猫娘", author=12, name="小雨", mentions_bot=True))
        await engine.drain()
        self.assertIn(PERSONA_NO_ASKER, user_text(llm.calls[1]))
        self.assertEqual(engine.store.db.execute("SELECT COUNT(*) FROM persona_history").fetchone()[0], 0)

    async def test_failed_reads_do_not_use_up_a_file_slot(self):
        calls = []

        async def reader(att, max_chars):
            calls.append(att.filename)
            if att.filename == "a.txt":
                return FileText(error="下载失败（文件可能已经被删了）", transient=True)
            return FileText(kind="文本", text=f"{att.filename} 的内容")

        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(), engine_cfg={"max_files_per_round": 1})
        engine.file_reader = reader
        attachments = [FileAttachment("a.txt", None, 10), FileAttachment("b.txt", None, 10)]
        await engine.handle_message(msg(501, "@小D 看看", mentions_bot=True, files=attachments))
        await engine.drain()
        self.assertEqual(calls, ["a.txt", "b.txt"])
        self.assertIn("b.txt 的内容", user_text(llm.calls[0]))


class Client(unittest.IsolatedAsyncioTestCase):
    async def test_cut_off_response_is_not_retried(self):
        from dsbot.llm import DeepSeekClient, _CutOff

        client = DeepSeekClient(api_key="sk-test", base_url="https://api.example", model="m", max_retries=2)
        calls = []

        async def post(payload, timeout):
            calls.append(timeout)
            raise _CutOff("ClientPayloadError('Response payload is not completed')")

        client._post = post
        with self.assertRaises(LLMError) as ctx:
            await client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(calls), 1)  # 对方已经算过钱了：不再发一次
        self.assertIn("传到一半", str(ctx.exception))
        self.assertFalse(ctx.exception.timeout)


class Files(unittest.TestCase):
    def test_notebook_with_odd_cells_is_read_as_text(self):
        self.assertEqual(files._notebook('{"cells": 5}'), '{"cells": 5}')

    def test_nul_after_the_first_8kb_means_binary(self):
        self.assertIsNone(files.decode_text(b"a" * 10_000 + b"\x00" + b"b"))
        self.assertEqual(files.decode_text(b"hello"), "hello")

    def test_png_has_a_smaller_pixel_limit_than_jpeg(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没装 Pillow")

        def encode(fmt):
            buf = io.BytesIO()
            Image.new("RGB", (20, 20), "red").save(buf, format=fmt)
            return buf.getvalue()

        old = images.FULL_DECODE_MAX_PIXELS
        images.FULL_DECODE_MAX_PIXELS = 100  # 20×20 = 400 像素
        try:
            with self.assertLogs("dsbot.images", "INFO"):
                self.assertIsNone(images.to_data_uri(encode("PNG")))  # 只能整张解码的：超了就跳过
            self.assertIsNotNone(images.to_data_uri(encode("JPEG")))  # JPEG 能缩小解码：还是 4000 万像素的上限
            mpo = io.BytesIO()
            Image.new("RGB", (20, 20), "red").save(mpo, format="MPO", save_all=True, append_images=[Image.new("RGB", (20, 20))])
            self.assertIsNotNone(images.to_data_uri(mpo.getvalue()))  # 手机拍的 MPO 也是 JPEG
        finally:
            images.FULL_DECODE_MAX_PIXELS = old


if __name__ == "__main__":
    unittest.main()
