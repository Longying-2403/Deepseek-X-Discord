import json
import unittest

from helpers import CHANNEL, GUILD, make_engine, msg

from dsbot.llm import LLMError
from dsbot.render import content_text
from dsbot.models import ImageAttachment

REPLY = lambda to, *texts: json.dumps(  # noqa: E731
    {"actions": [{"type": "reply", "to": to, "messages": list(texts)}]}, ensure_ascii=False
)


class MentionPath(unittest.IsolatedAsyncioTestCase):
    async def test_mention_gets_immediate_reply(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "在呢"))
        await engine.handle_message(msg(501, "@小D 在吗", mentions_bot=True))
        await engine.drain()

        self.assertEqual(len(llm.calls), 1)
        call = llm.calls[0]
        self.assertEqual(call["thinking"], "off")  # 被点名默认不思考，求快
        self.assertTrue(call["json_mode"])
        self.assertEqual(call["user_id"], f"guild-{GUILD}")
        self.assertEqual(transport.sent, [(CHANNEL, "在呢", 501, True)])
        user = call["messages"][-1]["content"]
        self.assertIn("#1 阿明：@小D 在吗 〔@你〕", user)
        self.assertIn("（#1 在叫你，回应一下，用对方那条消息的语言回。）", user)
        st = engine.states[CHANNEL]
        self.assertEqual(st.pending, [])
        self.assertEqual(len(st.conversation.turns), 2)

    async def test_history_is_append_only(self):
        """每一轮请求都必须以上一轮请求 + 上一轮输出开头，这是缓存命中的前提。"""
        outputs = [REPLY(1, "第一句"), REPLY(3, "第二句"), '{"actions": []}']
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])
        await engine.handle_message(msg(501, "hi", mentions_bot=True))
        await engine.drain()
        clock.advance(30)
        await engine.handle_message(msg(502, "随便聊聊", author=12, name="小雨"))
        await engine.handle_message(msg(503, "对吧", mentions_bot=True))
        await engine.drain()
        clock.advance(30)
        await engine.handle_message(msg(504, "再来", mentions_bot=True))
        await engine.drain()

        self.assertEqual(len(llm.calls), 3)
        for i in range(2):
            prev, nxt = llm.calls[i]["messages"], llm.calls[i + 1]["messages"]
            self.assertEqual(nxt[: len(prev)], prev)
            self.assertEqual(nxt[len(prev)], {"role": "assistant", "content": outputs[i]})

    async def test_reply_defaults_to_trigger_when_to_missing(self):
        engine, llm, transport, clock = make_engine(
            lambda call: json.dumps({"actions": [{"type": "reply", "messages": ["好"]}]})
        )
        await engine.handle_message(msg(501, "前情", author=12, name="小雨"))
        await engine.handle_message(msg(502, "@小D 帮我看看", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.sent, [(CHANNEL, "好", 502, True)])

    async def test_plain_text_output_still_answers_mention(self):
        engine, llm, transport, clock = make_engine(lambda call: "在的在的")
        await engine.handle_message(msg(501, "在吗", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.sent, [(CHANNEL, "在的在的", 501, True)])
        stored = engine.states[CHANNEL].conversation.turns[-1].content
        self.assertEqual(json.loads(stored)["actions"][0]["messages"], ["在的在的"])

    async def test_empty_json_output_is_retried_once(self):
        outputs = ["", REPLY(1, "抱歉刚走神了")]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])
        await engine.handle_message(msg(501, "在吗", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(transport.sent[0][1], "抱歉刚走神了")

    async def test_rate_limit_per_user(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "嗯"), defaults={"mention_rate_limit": 2})
        for i in range(3):
            await engine.handle_message(msg(501 + i, f"@小D 第{i}次", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # 三条合并成一轮
        user = llm.calls[0]["messages"][-1]["content"]
        self.assertEqual(user.count("〔@你〕"), 3)  # 都标出来了
        self.assertIn("（#1、#2 在叫你，回应一下，用对方那条消息的语言回。）", user)  # 但只有前两次算数

    async def test_other_bots_never_trigger(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True})
        await engine.handle_message(msg(501, "@小D 你好", mentions_bot=True, is_bot=True, name="别的bot"))
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertEqual(len(engine.states[CHANNEL].pending), 1)  # 但会作为上下文留着

    async def test_typing_time_counts_model_latency(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "短"))
        await engine.handle_message(msg(501, "在吗", mentions_bot=True))
        await engine.drain()
        self.assertEqual(engine.test_sleeps, [engine.ec.typing_min_seconds])


class ProactivePath(unittest.IsolatedAsyncioTestCase):
    async def test_off_by_default(self):
        engine, llm, transport, clock = make_engine()
        await engine.handle_message(msg(501, "有人吗"))
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])

    async def test_waits_for_quiet(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True})
        await engine.handle_message(msg(501, "今晚吃啥"))
        await engine.handle_message(msg(502, "火锅吧", author=12, name="小雨"))
        await engine.tick()
        clock.advance(10)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])  # 还没冷场

        clock.advance(25)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0]["thinking"], "low")
        self.assertIn("（没人叫你，想说再说，拿不准就不说。）", llm.calls[0]["messages"][-1]["content"])
        self.assertEqual(transport.sent, [])  # 模型选择不说话
        st = engine.states[CHANNEL]
        self.assertEqual(st.bucket.tokens, 3.0)  # 沉默不扣额度
        self.assertEqual(len(st.conversation.turns), 2)  # 沉默也记成一轮

    async def test_typing_postpones_judgement(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True})
        await engine.handle_message(msg(501, "我想想怎么说"))
        clock.advance(25)
        engine.handle_typing(CHANNEL)
        clock.advance(10)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])
        clock.advance(25)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)

    async def test_max_wait_when_never_quiet(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True})
        for i in range(30):
            await engine.handle_message(msg(600 + i, f"刷屏{i}", author=11 + i % 2))
            await engine.tick()
            await engine.drain()
            if llm.calls:
                break
            clock.advance(10)
        self.assertEqual(len(llm.calls), 1)
        self.assertGreaterEqual(i * 10, engine.cfg.defaults["max_wait_seconds"])

    async def test_quota_spent_and_refilled_by_turns(self):
        engine, llm, transport, clock = make_engine(
            lambda call: REPLY(None, "插一句"),
            defaults={"proactive": True, "quota_capacity": 1, "refill_turns": 3, "min_speak_gap": 0, "min_eval_interval": 10},
        )
        await engine.handle_message(msg(501, "A", author=11))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(engine.states[CHANNEL].bucket.tokens, 0.0)

        await engine.handle_message(msg(502, "B", author=12))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # 没额度：连判断都不做

        await engine.handle_message(msg(503, "C", author=13))  # 第 3 轮 → 恢复 1 次
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("#2 阿明：B", llm.calls[1]["messages"][-1]["content"])  # 攒着的消息也带上了

    async def test_same_author_burst_is_one_turn(self):
        engine, llm, transport, clock = make_engine(defaults={"refill_turns": 3})
        st = engine.state(CHANNEL, GUILD)
        st.bucket.tokens = 0
        for i in range(5):
            await engine.handle_message(msg(501 + i, f"分行打字{i}"))
            clock.advance(2)
        self.assertEqual(st.bucket.tokens, 0)
        self.assertEqual(st.bucket.turns_since_refill, 1)

    async def test_react_costs_partial_quota(self):
        engine, llm, transport, clock = make_engine(
            lambda call: json.dumps({"actions": [{"type": "react", "to": 1, "emoji": "🎉"}]}),
            defaults={"proactive": True},
        )
        await engine.handle_message(msg(501, "终于下班了"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "🎉")])
        self.assertAlmostEqual(engine.states[CHANNEL].bucket.tokens, 3 - engine.ec.react_cost)

    async def test_min_speak_gap_after_mention_reply(self):
        engine, llm, transport, clock = make_engine(
            lambda call: REPLY(1, "嗯"), defaults={"proactive": True, "min_speak_gap": 120}
        )
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        await engine.handle_message(msg(502, "然后呢", author=12))
        clock.advance(40)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # 刚说过话，主动发言要等够间隔
        clock.advance(90)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)


class Budget(unittest.IsolatedAsyncioTestCase):
    async def test_over_budget_only_answers_mentions_then_stops(self):
        engine, llm, transport, clock = make_engine(
            lambda call: REPLY(None, "嗯"), defaults={"proactive": True, "daily_budget_usd": 0.001}
        )
        engine.ledger.add(GUILD, 0.0015, clock())
        await engine.handle_message(msg(501, "聊点啥"))
        clock.advance(60)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])  # 超预算：不主动

        await engine.handle_message(msg(502, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # 点名还回

        engine.ledger.add(GUILD, 0.01, clock())
        clock.advance(60)
        await engine.handle_message(msg(503, "@小D 还在吗", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # 超过 2 倍：彻底停

    async def test_cost_is_recorded(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "嗯"))
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        spent = engine.ledger.guild_spent(GUILD, clock())
        self.assertGreater(spent, 0)
        stats = engine.store.day_stats(engine.ledger.day(clock()), guild_id=GUILD)
        self.assertEqual(stats["outcomes"], {"reply": 1})
        self.assertAlmostEqual(stats["cost"], spent)


class Errors(unittest.IsolatedAsyncioTestCase):
    async def test_insufficient_balance_pauses(self):
        engine, llm, transport, clock = make_engine(lambda call: LLMError("Insufficient Balance", status=402))
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.sent, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "⚠️")])
        self.assertGreater(engine.paused_until, clock())
        await engine.handle_message(msg(502, "@小D 再试", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # 暂停期间不再调用

    async def test_error_keeps_messages_for_next_round(self):
        outputs = [LLMError("Server Error", status=500), REPLY(2, "刚才卡了")]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(engine.states[CHANNEL].pending), 1)
        await engine.handle_message(msg(502, "@小D 喂", mentions_bot=True))
        await engine.drain()
        user = llm.calls[1]["messages"][-1]["content"]
        self.assertIn("：@小D 〔@你〕", user)  # 上次没处理的消息还在
        self.assertIn("：@小D 喂 〔@你〕", user)


class Memory(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_keeps_recent_rounds_and_summarizes_old_ones(self):
        def responder(call):
            if not call["json_mode"]:
                return "阿明喜欢火锅；小雨在找工作"
            return REPLY(None, "嗯嗯")

        engine, llm, transport, clock = make_engine(responder, defaults={"context_budget_tokens": 4000})
        llm.prompt_tokens = 5000
        for i in range(14):
            await engine.handle_message(msg(501 + i, f"@小D 第{i}轮", mentions_bot=True))
            await engine.drain()
            clock.advance(30)

        summary_calls = [c for c in llm.calls if not c["json_mode"]]
        self.assertEqual(len(summary_calls), 1)  # 只压了一次，不是每轮都压
        self.assertIn("第0轮", summary_calls[0]["messages"][1]["content"])
        conv = engine.states[CHANNEL].conversation
        self.assertEqual(conv.summary, "阿明喜欢火锅；小雨在找工作")
        last_rounds = [content_text(t.content) for t in conv.turns if t.role == "user"]
        self.assertTrue(any("第13轮" in x for x in last_rounds))
        self.assertGreaterEqual(len(conv.turns), 12)  # 最近 6 轮完整保留
        self.assertFalse(any("第0轮" in x for x in last_rounds))

        await engine.handle_message(msg(520, "@小D 还记得吗", mentions_bot=True))
        await engine.drain()
        chat_calls = [c for c in llm.calls if c["json_mode"]]
        system = chat_calls[-1]["messages"][0]["content"]
        self.assertIn("更早的聊天摘要", system)
        self.assertIn("阿明喜欢火锅", system)

    async def test_history_survives_restart(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "记住了"))
        await engine.handle_message(msg(501, "@小D 我叫阿明", mentions_bot=True))
        await engine.drain()
        engine.states.clear()  # 模拟重启：内存状态清空，只剩数据库
        await engine.handle_message(msg(502, "@小D 我叫啥", mentions_bot=True))
        await engine.drain()
        messages = llm.calls[-1]["messages"]
        self.assertEqual(messages[: len(llm.calls[0]["messages"])], llm.calls[0]["messages"])
        self.assertIn("#2 阿明：@小D 我叫啥", messages[-1]["content"])  # 编号接着往下

    async def test_reset_channel(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "好"))
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        engine.reset_channel(CHANNEL)
        await engine.handle_message(msg(502, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls[-1]["messages"]), 2)  # system + 新消息


class Images(unittest.IsolatedAsyncioTestCase):
    async def test_images_are_attached_with_detail(self):
        sides = {}

        async def loader(att, max_side):
            sides[att.filename] = max_side
            return f"data:image/jpeg;base64,{att.filename}"

        engine, llm, transport, clock = make_engine(lambda call: REPLY(2, "哈哈"), image_loader=loader)
        await engine.handle_message(msg(501, "看这个梗", images=[ImageAttachment("meme", "image/png", 10)]))
        await engine.handle_message(
            msg(502, "@小D 这图啥意思", mentions_bot=True, author=12, name="小雨",
                images=[ImageAttachment("q", "image/png", 10)])
        )
        await engine.drain()
        content = llm.calls[0]["messages"][-1]["content"]
        self.assertIsInstance(content, list)
        images = [p["image_url"] for p in content if p["type"] == "image_url"]
        self.assertEqual(
            images,
            [{"url": "data:image/jpeg;base64,meme", "detail": "low"}, {"url": "data:image/jpeg;base64,q", "detail": "auto"}],
        )
        text = "".join(p.get("text", "") for p in content)
        self.assertIn("#1 阿明：[图片] 看这个梗", text)
        self.assertIn("（以上是 #1 的图片）", text)
        self.assertEqual(sides, {"q": 1024, "meme": 512})  # 不是叫它的图缩得更小

    async def test_image_limit_prefers_recent(self):
        async def loader(att, max_side):
            return "data:image/jpeg;base64," + att.filename

        engine, llm, transport, clock = make_engine(lambda call: "{\"actions\": []}", image_loader=loader)
        engine.ec.max_images_per_round = 2
        imgs = [ImageAttachment(f"p{i}", "image/png", 1) for i in range(3)]
        await engine.handle_message(msg(501, "旧图", images=[imgs[0]]))
        await engine.handle_message(msg(502, "新图", images=imgs[1:], mentions_bot=True))
        await engine.drain()
        content = llm.calls[0]["messages"][-1]["content"]
        urls = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
        self.assertEqual(urls, ["data:image/jpeg;base64,p1", "data:image/jpeg;base64,p2"])



class Robustness(unittest.IsolatedAsyncioTestCase):
    """评审发现的问题的回归测试。"""

    async def test_bad_request_drops_batch_instead_of_blocking_channel(self):
        outputs = [LLMError("Content Exists Risk", status=400), REPLY(2, "在")]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])
        await engine.handle_message(msg(501, "@小D 某条会被拒的内容", mentions_bot=True))
        await engine.drain()
        self.assertEqual(engine.states[CHANNEL].pending, [])  # 丢掉了，不会每次重发
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "⚠️")])
        await engine.handle_message(msg(502, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        user = llm.calls[1]["messages"][-1]["content"]
        self.assertNotIn("会被拒的内容", user)
        self.assertEqual(len(transport.sent), 1)

    async def test_bad_request_with_images_retries_without_them(self):
        async def loader(att, max_side):
            return "data:image/jpeg;base64,AAAA"

        def responder(call):
            content = call["messages"][-1]["content"]
            if isinstance(content, list):
                return LLMError("bad image", status=400)
            return REPLY(1, "图打不开，不过我看到你说的了")

        engine, llm, transport, clock = make_engine(responder, image_loader=loader)
        await engine.handle_message(
            msg(501, "@小D 看图", mentions_bot=True, images=[ImageAttachment("x.png", "image/png", 10)])
        )
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertIsInstance(llm.calls[1]["messages"][-1]["content"], str)
        self.assertEqual(len(transport.sent), 1)

    async def test_crash_after_paid_call_is_recorded_and_cools_down(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "嗯"), defaults={"proactive": True})
        real = engine.store.append_round
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("disk full")
            return real(*args, **kwargs)

        engine.store.append_round = flaky
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        stats = engine.store.day_stats(engine.ledger.day(clock()), guild_id=GUILD)
        self.assertEqual(stats["calls"], 1)  # 花了的钱记进了数据库
        self.assertGreater(stats["cost"], 0)
        st = engine.states[CHANNEL]
        self.assertFalse(any(m.trigger for m in st.pending))
        self.assertGreater(st.error_until, clock())
        for _ in range(3):  # 冷却期内不会每次 tick 都再花钱
            clock.advance(5)
            await engine.tick()
            await engine.drain()
        self.assertEqual(len(llm.calls), 1)

    async def test_failed_call_does_not_swallow_new_mentions(self):
        outputs = [LLMError("Server Error", status=500), REPLY(2, "来了")]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])

        async def hook(call):
            if len(llm.calls) == 1:  # 第一次调用进行中，又有人叫它
                await engine.handle_message(msg(502, "@小D 我也问一下", mentions_bot=True, author=12, name="小雨"))

        llm.hook = hook
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("在叫你", llm.calls[1]["messages"][-1]["content"])
        self.assertEqual(len(transport.sent), 1)

    async def test_reset_during_round_is_not_undone(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "嗯"))

        async def hook(call):
            if len(llm.calls) == 1:
                engine.reset_channel(CHANNEL)

        llm.hook = hook
        await engine.handle_message(msg(501, "@小D 我的秘密", mentions_bot=True))
        await engine.drain()
        self.assertEqual(engine.store.load_conversation(CHANNEL, GUILD).turns, [])
        llm.hook = None
        await engine.handle_message(msg(502, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls[-1]["messages"]), 2)

    async def test_unsendable_channel_spends_quota_and_backs_off(self):
        engine, llm, transport, clock = make_engine(
            lambda call: REPLY(None, "说点啥"),
            defaults={"proactive": True, "quota_capacity": 5, "refill_turns": 1, "min_speak_gap": 0, "min_eval_interval": 10},
        )
        transport.can_send = False
        for i in range(5):
            await engine.handle_message(msg(600 + i, f"消息{i}", author=20 + i))
            clock.advance(31)
            await engine.tick()
            await engine.drain()
        st = engine.states[CHANNEL]
        self.assertEqual(len(llm.calls), 3)  # 连续 3 轮发不出去后暂停
        self.assertGreater(st.error_until, clock())
        self.assertTrue(engine.status(GUILD, CHANNEL, None)["send_blocked"])

    async def test_mention_survives_batch_overflow(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "在"))
        engine.ec.max_batch_messages = 5
        await engine.handle_message(msg(501, "@小D 帮个忙", mentions_bot=True))
        for i in range(10):
            await engine.handle_message(msg(600 + i, f"刷屏{i}", author=12, name="小雨"))
        await engine.drain()
        user = llm.calls[0]["messages"][-1]["content"]
        self.assertIn("#1 阿明：@小D 帮个忙 〔@你〕", user)
        self.assertIn("（#1 在叫你，回应一下，用对方那条消息的语言回。）", user)
        self.assertIn("（前面还有 5 条消息没显示）", user)
        self.assertEqual(transport.sent[0][2], 501)

    async def test_stale_messages_are_pruned(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "嗯"))
        await engine.handle_message(msg(501, "很久以前的话"))
        clock.advance(7 * 3600)
        await engine.handle_message(msg(502, "@小D", mentions_bot=True))
        await engine.drain()
        user = llm.calls[0]["messages"][-1]["content"]
        self.assertNotIn("很久以前", user)

    async def test_batch_header_shows_time_range(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "嗯"))
        await engine.handle_message(msg(501, "早"))
        clock.advance(600)
        await engine.handle_message(msg(502, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertTrue(llm.calls[0]["messages"][-1]["content"].startswith("【10-07 20:00 ~ 20:10】"))

    async def test_paused_mentions_get_warning(self):
        engine, llm, transport, clock = make_engine()
        engine.paused_until = clock() + 600
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "⚠️")])

    async def test_truncated_thinking_retries_without_thinking(self):
        from dsbot.models import Completion, Usage

        def responder(call):
            if call["thinking"] != "off":
                return Completion(content="", reasoning="想太多" * 100, usage=Usage(5000, 4000, 1000, 6000, 6000), finish_reason="length")
            return REPLY(1, "简单说：可以")

        engine, llm, transport, clock = make_engine(responder, defaults={"mention_thinking": "high"})
        await engine.handle_message(msg(501, "@小D 这个能行吗", mentions_bot=True))
        await engine.drain()
        self.assertEqual([c["thinking"] for c in llm.calls], ["high", "off"])
        self.assertEqual(transport.sent[0][1], "简单说：可以")
        outcomes = engine.store.day_stats(engine.ledger.day(clock()))["outcomes"]
        self.assertEqual(outcomes, {"empty": 1, "reply": 1})

    async def test_global_budget_ignores_guild_override(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "嗯"), defaults={"proactive": True})
        engine.settings.set("global", 0, "global_daily_budget_usd", "0.001")
        engine.settings.set("guild", GUILD, "global_daily_budget_usd", "100")  # 不应该绕过全局上限
        engine.ledger.add(999, 0.002, clock())
        await engine.handle_message(msg(501, "聊点啥"))
        clock.advance(60)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])



class Areas(unittest.IsolatedAsyncioTestCase):
    """区域开关：频道 > 帖子所在频道 > 类别 > 服务器。"""

    async def test_disabled_channel_is_ignored_completely(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "在"))
        engine.settings.set("channel", CHANNEL, "enabled", "关")
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertNotIn(CHANNEL, engine.states)  # 连缓冲都没有

    async def test_whitelist_by_category(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "在"))
        engine.settings.set("guild", GUILD, "enabled", "关")
        engine.settings.set("category", 50, "enabled", "开")
        await engine.handle_message(msg(501, "@小D", mentions_bot=True, category_id=50))
        await engine.handle_message(msg(502, "@小D", mentions_bot=True, channel_id=200, category_id=60))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual([s[0] for s in transport.sent], [CHANNEL])

    async def test_channel_setting_beats_category(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "在"))
        engine.settings.set("category", 50, "enabled", "关")
        engine.settings.set("channel", CHANNEL, "enabled", "开")
        await engine.handle_message(msg(501, "@小D", mentions_bot=True, category_id=50))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)

    async def test_thread_follows_parent_then_category(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "在"))
        engine.settings.set("category", 50, "enabled", "关")
        thread = dict(channel_id=300, parent_id=CHANNEL, category_id=50, mentions_bot=True)
        await engine.handle_message(msg(501, "@小D", **thread))
        await engine.drain()
        self.assertEqual(llm.calls, [])
        engine.settings.set("channel", CHANNEL, "enabled", "开")  # 放开帖子所在的频道
        await engine.handle_message(msg(502, "@小D", **thread))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)

    async def test_disabling_drops_buffered_messages(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "嗯"), defaults={"proactive": True})
        await engine.handle_message(msg(501, "攒着的消息"))
        engine.settings.set("channel", CHANNEL, "enabled", "关")
        clock.advance(60)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])  # 主动发言也停了
        engine.settings.set("channel", CHANNEL, "enabled", "开")
        await engine.handle_message(msg(502, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertIn("攒着的消息", llm.calls[0]["messages"][-1]["content"])  # 关掉期间没再处理，重新打开时还在
        engine.settings.set("channel", CHANNEL, "enabled", "关")
        await engine.handle_message(msg(503, "关掉后说的话"))
        self.assertEqual(engine.states[CHANNEL].pending, [])


class LinkPreviews(unittest.IsolatedAsyncioTestCase):
    """GIF 选单 / 图片链接：预览图晚一两秒才通过“消息更新”补上。"""

    async def test_late_preview_is_merged_into_pending_message(self):
        async def loader(att, max_side):
            return "data:image/jpeg;base64," + att.filename

        engine, llm, transport, clock = make_engine(lambda call: REPLY(2, "笑死"), image_loader=loader)
        await engine.handle_message(msg(501, "https://tenor.com/view/cat-123", awaiting_embeds=True))
        updated = msg(501, "", images=[ImageAttachment("cat.png", None, 0, kind="gif")])
        self.assertTrue(engine.refresh_message(updated))
        await engine.handle_message(msg(502, "@小D 看这个", mentions_bot=True, author=12, name="小雨"))
        await engine.drain()
        content = llm.calls[0]["messages"][-1]["content"]
        text = "".join(p.get("text", "") for p in content)
        self.assertIn("#1 阿明：[GIF]", text)
        self.assertNotIn("tenor.com", text)
        self.assertEqual([p["image_url"]["url"] for p in content if p["type"] == "image_url"], ["data:image/jpeg;base64,cat.png"])

    async def test_mention_with_link_waits_for_preview(self):
        async def loader(att, max_side):
            return "data:image/jpeg;base64," + att.filename

        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "这猫好丑"), image_loader=loader)
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            if seconds == 2.0:  # 等待期间 Discord 推来了带预览的消息更新
                engine.refresh_message(
                    msg(501, "@小D 你看", mentions_bot=True, images=[ImageAttachment("cat.png", None, 0, kind="gif")])
                )

        engine.sleep = sleep
        await engine.handle_message(msg(501, "@小D 你看 https://tenor.com/view/cat-1", mentions_bot=True, awaiting_embeds=True))
        await engine.drain()
        self.assertEqual(waits[0], 2.0)
        content = llm.calls[0]["messages"][-1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[1]["image_url"]["detail"], "auto")  # 叫它的人发的图看清楚点

    async def test_no_wait_without_links(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "在"))
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(engine.test_sleeps), 1)  # 只有“打字”那一次，没有等预览

    async def test_refresh_after_round_is_ignored(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "在"))
        await engine.handle_message(msg(501, "@小D", mentions_bot=True))
        await engine.drain()
        self.assertFalse(engine.refresh_message(msg(501, "改过的")))


if __name__ == "__main__":
    unittest.main()
