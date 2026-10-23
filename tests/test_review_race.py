"""整理记忆的时候又压缩了旧记录：压掉的原话排着队接着整理，不丢、不重复整理。"""

import asyncio
import json
import unittest

from helpers import CHANNEL, GUILD, make_engine, msg

from dsbot.llm import LLMError
from dsbot.render import own_text
from test_memory import ACTIONS, REPLY, chat, is_review, stores, three_rounds, user_text

TOO_LONG = "This model's maximum context length is 65536 tokens. However, you requested 70000 tokens"
LONG = "很长" * 200
EMPTY = '{"cards": [], "memory": []}'


async def settle(engine):
    """在整理记忆的模型调用里用：等这期间叫它的那几轮跑完（不能等整理记忆自己）。"""
    me = asyncio.current_task()
    while pending := [t for t in engine._tasks if not t.done() and t is not me]:
        await asyncio.gather(*pending, return_exceptions=True)


def overflowing(base):
    """state["boom"] 为真时，下一次聊天请求报“上下文太长”（会强制压缩旧记录）。"""
    state = {"boom": False}

    def responder(call):
        if state["boom"] and call["json_mode"] and not is_review(call):
            state["boom"] = False
            return LLMError(TOO_LONG, status=400)
        return base(call)

    return responder, state


async def talk(engine, clock, texts, start, state=None, authors=((11, "阿明"), (12, "小雨"))):
    """叫它几轮（在整理记忆的模型调用里也能用）；给了 state 就再来一轮“上下文太长”，强制压缩。"""
    for i, text in enumerate(texts):
        uid, name = authors[i % len(authors)]
        await engine.handle_message(msg(start + i, f"@小D {text}", author=uid, name=name, mentions_bot=True))
        await settle(engine)
        clock.advance(30)
    if state is not None:
        state["boom"] = True
        await engine.handle_message(msg(start + len(texts), "@小D 太长了", author=13, name="阿杰", mentions_bot=True))
        await settle(engine)
        clock.advance(30)


def setup(review=EMPTY, **kw):
    """开着人设卡和长期记忆、能被“上下文太长”强制压缩的引擎。"""
    cards, memory = stores()
    responder, state = overflowing(chat([ACTIONS(REPLY(None, "嗯"))], review=review))
    engine, llm, transport, clock = make_engine(responder, cards=cards, memory=memory, **kw)
    llm.prompt_tokens = 10  # 平时不压缩：只在“上下文太长”时压
    return engine, llm, clock, state, memory


def during_review(llm, *actions):
    """第 k 次整理记忆的模型调用进行中（还没回）：做 actions[k-1]（None = 什么都不做）。"""

    async def hook(call):
        if not is_review(call):
            return
        k = sum(is_review(c) for c in llm.calls)
        if k <= len(actions) and actions[k - 1] is not None:
            await actions[k - 1]()

    llm.hook = hook


def asked(llm, labels):
    """每次整理记忆的请求里有哪几句（按先后）。"""
    return [[w for w in labels if w in user_text(c)] for c in llm.calls if is_review(c)]


def reviewed_counts(engine, llm, labels):
    """每句话：(还在记录里, 整理过几次)。"""
    reviews = [user_text(c) for c in llm.calls if is_review(c)]
    kept = "".join(own_text(t.content) for t in engine.states[CHANNEL].conversation.turns if t.role == "user")
    return {label: (label in kept, sum(label in r for r in reviews)) for label in labels}


def chunky(label, lines=4):
    """一轮占整理记忆的一段（review_max_chars = 2000 时）。"""
    return label + "\n".join([LONG] * lines)


class ReviewRace(unittest.IsolatedAsyncioTestCase):
    def assertIdle(self, st):
        self.assertEqual((st.reviewing, st.review_held, st.review_jobs), (False, 0, []))

    async def test_compaction_while_reviewing_queues_the_rest(self):
        for evict in (False, True):
            with self.subTest(evict=evict):
                engine, llm, clock, state, _ = setup()
                await three_rounds(engine, clock)

                async def compact():
                    if evict:
                        engine.states[CHANNEL].conversation = None  # 问模型的时候记录被清出内存（之后重新载入）
                    await talk(engine, clock, [f"新话题{i}" + LONG for i in range(2)], 600, state)

                during_review(llm, compact)
                clock.advance(600)
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await engine.tick()
                    await engine.drain()
                labels = ["第0句", "第1句", "第2句", "新话题0", "新话题1"]
                self.assertEqual(
                    reviewed_counts(engine, llm, labels),
                    {"第0句": (False, 1), "第1句": (False, 1), "第2句": (False, 1), "新话题0": (False, 1), "新话题1": (True, 0)},
                )
                self.assertEqual(asked(llm, labels), [["第0句", "第1句", "第2句"], ["新话题0"]])  # 先问完手上的，再整理排队的
                st = engine.states[CHANNEL]
                self.assertEqual(st.conversation.reviewed_upto, st.conversation.turns[0].id - 1)
                self.assertEqual(engine.store.load_conversation(CHANNEL, GUILD).reviewed_upto, st.conversation.turns[0].id - 1)
                self.assertIdle(st)

    async def test_shutdown_stops_the_queue(self):
        """关机（closing）：问完手上这一段就停，排着队的不再接着问（关机时只等聊天的轮次跑完）。"""
        engine, llm, clock, state, _ = setup()
        await three_rounds(engine, clock)

        async def compact_then_close():
            await talk(engine, clock, [f"新话题{i}" + LONG for i in range(2)], 600, state)
            engine.closing = True

        during_review(llm, compact_then_close)
        clock.advance(600)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.tick()
            await engine.drain()
        self.assertEqual(len([c for c in llm.calls if is_review(c)]), 1)  # 排队的那批没问
        self.assertIdle(engine.states[CHANNEL])

    async def test_no_compaction_while_closing(self):
        """关机的时候不压缩：压掉的那部分来不及整理记忆，留到下次启动再压（记录都还在数据库里）。"""
        engine, llm, clock, state, _ = setup()
        await three_rounds(engine, clock)
        engine.closing = True
        conv = engine.states[CHANNEL].conversation
        before = [t.id for t in conv.turns]
        await engine._compact(engine.states[CHANNEL], conv, engine.transport.channel_info(CHANNEL), force=True)
        self.assertEqual([t.id for t in conv.turns], before)
        self.assertFalse([c for c in llm.calls if not c["json_mode"]])  # 摘要也没问

    async def test_shutdown_during_the_summary_call(self):
        """问摘要的时候关机了：摘要回来也不压（压掉的那部分来不及整理记忆，留到下次启动）。"""
        engine, llm, clock, state, _ = setup()
        await three_rounds(engine, clock)
        st = engine.states[CHANNEL]
        conv = st.conversation
        before = [t.id for t in conv.turns]

        async def hook(call):
            if not call["json_mode"]:  # 摘要请求进行中
                engine.closing = True

        llm.hook = hook
        await engine._compact(st, conv, engine.transport.channel_info(CHANNEL), force=True)
        self.assertEqual(len([c for c in llm.calls if not c["json_mode"]]), 1)
        self.assertEqual([t.id for t in conv.turns], before)
        self.assertEqual(st.review_jobs, [])

    async def test_second_compaction_while_compact_review_runs(self):
        engine, llm, clock, state, _ = setup()
        labels = [f"第{i}句话" for i in range(4)] + ["新话题0", "新话题1"]
        with self.assertLogs("dsbot.engine", "WARNING"):
            await talk(engine, clock, [w + LONG for w in labels[:4]], 501)
            during_review(llm, lambda: talk(engine, clock, [w + LONG for w in labels[4:]], 600, state))
            await talk(engine, clock, [], 590, state)  # 第一次压缩：后台整理压掉的；整理的时候又压缩了一次
            await engine.drain()
        counts = reviewed_counts(engine, llm, labels)
        for label, (kept, times) in counts.items():
            self.assertEqual(times, 0 if kept else 1, label)  # 压掉的整理一次，还在记录里的不整理
        self.assertEqual(counts["第3句话"], (False, 1))  # 第二次压掉的
        st = engine.states[CHANNEL]
        self.assertEqual(st.conversation.reviewed_upto, st.conversation.turns[0].id - 1)
        self.assertIdle(st)

    async def test_failed_idle_review_still_runs_the_queue(self):
        for error in (LLMError("Server Error", status=500), RuntimeError("整理时出了个 bug")):
            with self.subTest(error=type(error).__name__):
                results = iter([error])
                engine, llm, clock, state, _ = setup(lambda call: next(results, EMPTY))
                await three_rounds(engine, clock)
                during_review(llm, lambda: talk(engine, clock, [f"新话题{i}" + LONG for i in range(2)], 600, state))
                clock.advance(600)
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await engine.tick()
                    await engine.drain()
                labels = ["第0句", "新话题0", "新话题1"]
                self.assertEqual(reviewed_counts(engine, llm, labels), {"第0句": (False, 1), "新话题0": (False, 1), "新话题1": (True, 0)})
                st = engine.states[CHANNEL]
                self.assertIdle(st)
                clock.advance(3600)
                await engine.tick()
                await engine.drain()
                self.assertEqual(reviewed_counts(engine, llm, labels), {"第0句": (False, 1), "新话题0": (False, 1), "新话题1": (True, 1)})
                self.assertEqual(st.conversation.reviewed_upto, st.conversation.turns[-1].id)

    async def test_clearing_memory_drops_queued_batches(self):
        n = {"i": 0}

        def review(call):
            n["i"] += 1
            return json.dumps({"memory": [{"add": f"第{n['i']}批"}]}, ensure_ascii=False)

        engine, llm, clock, state, memory = setup(review)
        await three_rounds(engine, clock)
        after_reset = []

        async def compact_reset_compact():
            await talk(engine, clock, [f"新话题{i}" + LONG for i in range(2)], 600, state)
            st = engine.states[CHANNEL]
            queued = len(st.review_jobs)
            engine.reset_channel(CHANNEL)
            after_reset.append((queued, list(st.review_jobs), st.review_held))
            await talk(engine, clock, ["清空后0", "清空后1"], 700, state)

        during_review(llm, compact_reset_compact)
        clock.advance(600)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.tick()
            await engine.drain()
        self.assertEqual(after_reset, [(1, [], 0)])  # 清空前排进来的那批扔掉了
        reviews = [user_text(c) for c in llm.calls if is_review(c)]
        self.assertEqual(len(reviews), 2)
        self.assertIn("清空后0", reviews[1])
        self.assertNotIn("新话题", reviews[1])
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["第2批"])  # 清空前问的那段作废
        st = engine.states[CHANNEL]
        self.assertEqual(st.conversation.reviewed_upto, st.conversation.turns[0].id - 1)
        self.assertIdle(st)

    async def test_compaction_takes_over_the_chunks_not_yet_asked(self):
        labels = ["第0段", "第1段", "第2段", "后来0", "后来1"]
        for fail in (True, False):
            with self.subTest(fail=fail):
                results = iter([LLMError("Server Error", status=500)] if fail else [])
                engine, llm, clock, state, _ = setup(lambda call: next(results, EMPTY), engine_cfg={"review_max_chars": 2000})
                await talk(engine, clock, [chunky(f"第{i}段") for i in range(3)], 501)  # 每轮各是一段
                during_review(llm, lambda: talk(engine, clock, ["后来0", "后来1"], 600, state))  # 压掉 第0段、第1段
                clock.advance(600)
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await engine.tick()
                    await engine.drain()
                self.assertEqual(asked(llm, labels), [["第0段"], ["第1段"]])  # 问完手上这段就停；第1段归压缩那批
                st = engine.states[CHANNEL]
                self.assertIdle(st)
                self.assertGreater(st.unreviewed_rounds, 0)  # 第2段还在记录里，没整理
                clock.advance(3600)
                await engine.tick()
                await engine.drain()
                self.assertEqual(asked(llm, labels), [["第0段"], ["第1段"], ["第2段", "后来0", "后来1"]])
                self.assertEqual(st.conversation.reviewed_upto, st.conversation.turns[-1].id)
                self.assertEqual(st.unreviewed_rounds, 0)

    async def test_compaction_inside_the_chunk_being_asked_does_not_stop_the_review(self):
        labels = ["开头0", "开头1", "开头2", "第1段", "第2段", "后来0", "后来1"]
        engine, llm, clock, state, _ = setup(engine_cfg={"review_max_chars": 2000})
        # 开头三轮各是一整行很长的（整理时截短，三轮合成一段；压缩时占了一大半），后面两轮各是一段
        await talk(engine, clock, [f"开头{i}" + "很长" * 900 for i in range(3)] + [chunky("第1段"), chunky("第2段")], 501)
        during_review(llm, lambda: talk(engine, clock, ["后来0", "后来1"], 600, state))  # 只压掉正在问的那一段
        clock.advance(600)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.tick()
            await engine.drain()
        self.assertEqual(asked(llm, labels), [labels[:3], ["第1段"], ["第2段"]])  # 没有排队的：接着问完这一批
        self.assertEqual(
            reviewed_counts(engine, llm, labels),
            {**{w: (False, 1) for w in labels[:3]}, "第1段": (True, 1), "第2段": (True, 1), "后来0": (True, 0), "后来1": (True, 0)},
        )
        st = engine.states[CHANNEL]
        self.assertIdle(st)
        self.assertEqual(st.unreviewed_rounds, 0)
        turns = st.conversation.turns
        at = next(i for i, t in enumerate(turns) if t.role == "user" and "第2段" in own_text(t.content))
        self.assertEqual(st.conversation.reviewed_upto, turns[at + 1].id)

    async def test_two_compactions_during_one_idle_chunk(self):
        labels = ["第0句", "第1句", "第2句"] + [f"新话题{i}" for i in range(4)]
        for crash in (False, True):
            with self.subTest(crash=crash):
                n = {"i": 0}

                def review(call, crash=crash):
                    n["i"] += 1
                    return RuntimeError("整理时出了个 bug") if crash and n["i"] == 2 else EMPTY

                engine, llm, clock, state, _ = setup(review)
                await three_rounds(engine, clock)

                async def compact_twice():
                    await talk(engine, clock, [f"新话题{i}" + LONG for i in range(2)], 600, state)
                    await talk(engine, clock, [f"新话题{i}" + LONG for i in range(2, 4)], 610, state)

                during_review(llm, compact_twice)
                clock.advance(600)
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await engine.tick()
                    await engine.drain()
                # 按先后：手上这段、第一次压掉的、第二次压掉的（前一批出错不耽误后一批）
                self.assertEqual(asked(llm, labels), [labels[:3], ["新话题0"], ["新话题1", "新话题2"]])
                self.assertEqual(reviewed_counts(engine, llm, ["新话题3"]), {"新话题3": (True, 0)})
                st = engine.states[CHANNEL]
                self.assertEqual(st.conversation.reviewed_upto, st.conversation.turns[0].id - 1)
                self.assertIdle(st)

    async def test_compaction_while_the_queue_drains_uses_the_same_worker(self):
        labels = ["第0句", "第1句", "第2句"] + [f"新话题{i}" for i in range(4)]
        live = {"now": 0, "most": 0}

        def review(call):
            live["now"] -= 1
            return EMPTY

        engine, llm, clock, state, _ = setup(review)
        await three_rounds(engine, clock)
        actions = [
            lambda: talk(engine, clock, [f"新话题{i}" + LONG for i in range(2)], 600, state),
            lambda: talk(engine, clock, [f"新话题{i}" + LONG for i in range(2, 4)], 610, state),  # 整理排队的那批时又压缩
        ]

        async def hook(call):
            if not is_review(call):
                return
            live["now"] += 1
            live["most"] = max(live["most"], live["now"])
            k = sum(is_review(c) for c in llm.calls)
            if k <= len(actions):
                await actions[k - 1]()

        llm.hook = hook
        clock.advance(600)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.tick()
            await engine.drain()
        self.assertEqual(asked(llm, labels), [labels[:3], ["新话题0"], ["新话题1", "新话题2"]])
        self.assertEqual(live["most"], 1)  # 同一时间只有一个在整理
        st = engine.states[CHANNEL]
        self.assertEqual(st.conversation.reviewed_upto, st.conversation.turns[0].id - 1)
        self.assertIdle(st)

    async def test_failed_summary_queues_nothing(self):
        labels = [f"第{i}句话" for i in range(4)]
        for how in ("error", "empty", "reset"):
            with self.subTest(how=how):
                cards, memory = stores()
                base, state = overflowing(chat([ACTIONS(REPLY(None, "嗯"))]))

                def responder(call, how=how):
                    if not call["json_mode"] and how != "reset":  # 压缩摘要
                        return LLMError("Server Error", status=500) if how == "error" else "  "
                    return base(call)

                engine, llm, transport, clock = make_engine(responder, cards=cards, memory=memory)
                llm.prompt_tokens = 10
                if how == "reset":

                    async def hook(call):
                        if not call["json_mode"]:
                            engine.reset_channel(CHANNEL)  # 写摘要的时候清空了记忆

                    llm.hook = hook
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await talk(engine, clock, [w + LONG for w in labels], 501, state)
                    await engine.drain()
                self.assertEqual(len([c for c in llm.calls if not c["json_mode"]]), 1)  # 真的去压缩了
                self.assertFalse(any(is_review(c) for c in llm.calls))  # 没压成：不挑出来整理
                st = engine.states[CHANNEL]
                self.assertIdle(st)
                clock.advance(600)
                await engine.tick()
                await engine.drain()
                if how == "reset":
                    self.assertFalse(any(is_review(c) for c in llm.calls))  # 清空的记录不再整理
                    continue
                self.assertEqual(reviewed_counts(engine, llm, labels), {w: (True, 1) for w in labels})  # 还在记录里：安静时整理一次
                self.assertEqual(st.conversation.reviewed_upto, st.conversation.turns[-1].id)

    async def test_cancelled_review_drops_the_queue(self):
        engine, llm, clock, state, _ = setup()
        await three_rounds(engine, clock)
        queued = []

        async def compact_then_shutdown():
            await talk(engine, clock, [f"新话题{i}" + LONG for i in range(2)], 600, state)
            queued.append(len(engine.states[CHANNEL].review_jobs))
            asyncio.current_task().cancel()  # 关机：整理记忆的任务被取消
            await asyncio.sleep(0)

        during_review(llm, compact_then_shutdown)
        clock.advance(600)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.tick()
            await engine.drain()
        self.assertEqual(queued, [1])
        labels = ["第0句", "新话题0", "新话题1"]
        self.assertEqual(asked(llm, labels), [["第0句"]])  # 取消之后不再问模型
        st = engine.states[CHANNEL]
        self.assertIdle(st)
        clock.advance(3600)
        await engine.tick()
        await engine.drain()
        self.assertEqual(asked(llm, labels), [["第0句"], ["新话题1"]])  # 没卡住：以后照常整理

    async def test_plan_skips_turns_up_to_after(self):
        engine, llm, clock, state, _ = setup()
        await three_rounds(engine, clock)
        st = engine.states[CHANNEL]
        conv = st.conversation
        info = engine.transport.channel_info(CHANNEL)
        ids = [t.id for t in conv.turns]
        plan = engine._review_plan(st, conv, info, upto=ids[3], after=ids[1])
        text = "\n".join(line for chunk in plan.chunks for line in chunk.lines)
        self.assertEqual([w for w in ("第0句", "第1句", "第2句") if w in text], ["第1句"])
        self.assertEqual(plan.last_id, ids[3])
        self.assertEqual((conv.reviewed_upto, st.unreviewed_rounds), (0, 3))  # 只是挑出来，不记进度
        self.assertIsNone(engine._review_plan(st, conv, info, upto=ids[3], after=ids[3]))
        self.assertEqual((conv.reviewed_upto, st.unreviewed_rounds), (0, 3))

    async def test_queued_batch_from_before_a_reset_is_skipped(self):
        from dsbot.engine import _ReviewJob

        engine, llm, clock, state, _ = setup()
        await three_rounds(engine, clock)
        st = engine.states[CHANNEL]
        conv = st.conversation
        info = engine.transport.channel_info(CHANNEL)
        plan = engine._review_plan(st, conv, info, upto=conv.turns[-1].id)
        st.review_jobs += [_ReviewJob(conv, info, plan, st.generation - 1), _ReviewJob(conv, info, plan, st.generation)]
        st.reviewing = True
        await engine._compact_review(st)
        self.assertEqual(len([c for c in llm.calls if is_review(c)]), 1)  # 清空前排进来的那批不整理
        self.assertEqual(conv.reviewed_upto, conv.turns[-1].id)
        self.assertIdle(st)


if __name__ == "__main__":
    unittest.main()
