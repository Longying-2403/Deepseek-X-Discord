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
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "here"))
        await engine.handle_message(msg(501, "@Dee you there?", mentions_bot=True))
        await engine.drain()

        self.assertEqual(len(llm.calls), 1)
        call = llm.calls[0]
        self.assertEqual(call["thinking"], "off")  # when called, no thinking by default, so it answers fast
        self.assertTrue(call["json_mode"])
        self.assertEqual(call["user_id"], f"guild-{GUILD}")
        self.assertEqual(transport.sent, [(CHANNEL, "here", 501, True)])
        user = call["messages"][-1]["content"]
        self.assertIn("#1 Alex: @Dee you there? 〔@you〕", user)
        self.assertIn("(#1 called you. Respond, and use the language of their message.)", user)
        st = engine.states[CHANNEL]
        self.assertEqual(st.pending, [])
        self.assertEqual(len(st.conversation.turns), 2)

    async def test_history_is_append_only(self):
        """Every request must start with the previous request + the previous output; that's what makes the cache hit."""
        outputs = [REPLY(1, "first line"), REPLY(3, "second line"), '{"actions": []}']
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])
        await engine.handle_message(msg(501, "hi", mentions_bot=True))
        await engine.drain()
        clock.advance(30)
        await engine.handle_message(msg(502, "just chatting", author=12, name="Mia"))
        await engine.handle_message(msg(503, "right?", mentions_bot=True))
        await engine.drain()
        clock.advance(30)
        await engine.handle_message(msg(504, "one more", mentions_bot=True))
        await engine.drain()

        self.assertEqual(len(llm.calls), 3)
        for i in range(2):
            prev, nxt = llm.calls[i]["messages"], llm.calls[i + 1]["messages"]
            self.assertEqual(nxt[: len(prev)], prev)
            self.assertEqual(nxt[len(prev)], {"role": "assistant", "content": outputs[i]})

    async def test_reply_defaults_to_trigger_when_to_missing(self):
        engine, llm, transport, clock = make_engine(
            lambda call: json.dumps({"actions": [{"type": "reply", "messages": ["sure"]}]})
        )
        await engine.handle_message(msg(501, "some backstory first", author=12, name="Mia"))
        await engine.handle_message(msg(502, "@Dee can you take a look", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.sent, [(CHANNEL, "sure", 502, True)])

    async def test_plain_text_output_still_answers_mention(self):
        engine, llm, transport, clock = make_engine(lambda call: "yep, I'm here")
        await engine.handle_message(msg(501, "you there?", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.sent, [(CHANNEL, "yep, I'm here", 501, True)])
        stored = engine.states[CHANNEL].conversation.turns[-1].content
        self.assertEqual(json.loads(stored)["actions"][0]["messages"], ["yep, I'm here"])

    async def test_empty_json_output_is_retried_once(self):
        outputs = ["", REPLY(1, "sorry, zoned out for a sec")]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])
        await engine.handle_message(msg(501, "you there?", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(transport.sent[0][1], "sorry, zoned out for a sec")

    async def test_rate_limit_per_user(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "mhm"), defaults={"mention_rate_limit": 2})
        for i in range(3):
            await engine.handle_message(msg(501 + i, f"@Dee ping {i}", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # the three are merged into one round
        user = llm.calls[0]["messages"][-1]["content"]
        self.assertEqual(user.count("〔@you〕"), 3)  # all of them are marked
        self.assertIn("(#1, #2 called you. Respond, and use the language of their message.)", user)  # but only the first two count

    async def test_other_bots_never_trigger(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True})
        await engine.handle_message(msg(501, "@Dee hello", mentions_bot=True, is_bot=True, name="OtherBot"))
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertEqual(len(engine.states[CHANNEL].pending), 1)  # but it's kept as context

    async def test_typing_time_counts_model_latency(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "ok"))
        await engine.handle_message(msg(501, "you there?", mentions_bot=True))
        await engine.drain()
        self.assertEqual(engine.test_sleeps, [engine.ec.typing_min_seconds])


class ProactivePath(unittest.IsolatedAsyncioTestCase):
    async def test_off_by_default(self):
        engine, llm, transport, clock = make_engine()
        await engine.handle_message(msg(501, "anyone here?"))
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])

    async def test_waits_for_quiet(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True})
        await engine.handle_message(msg(501, "what's for dinner tonight"))
        await engine.handle_message(msg(502, "hotpot?", author=12, name="Mia"))
        await engine.tick()
        clock.advance(10)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])  # the chat hasn't gone quiet yet

        clock.advance(25)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0]["thinking"], "low")
        self.assertIn("(Nobody called you. Speak only if you want to; when in doubt, stay quiet.)", llm.calls[0]["messages"][-1]["content"])
        self.assertEqual(transport.sent, [])  # the model chose to stay quiet
        st = engine.states[CHANNEL]
        self.assertEqual(st.bucket.tokens, 3.0)  # staying quiet costs no quota
        self.assertEqual(len(st.conversation.turns), 2)  # staying quiet is still recorded as a round

    async def test_typing_postpones_judgement(self):
        engine, llm, transport, clock = make_engine(defaults={"proactive": True})
        await engine.handle_message(msg(501, "let me think how to put this"))
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
            await engine.handle_message(msg(600 + i, f"spam {i}", author=11 + i % 2))
            await engine.tick()
            await engine.drain()
            if llm.calls:
                break
            clock.advance(10)
        self.assertEqual(len(llm.calls), 1)
        self.assertGreaterEqual(i * 10, engine.cfg.defaults["max_wait_seconds"])

    async def test_quota_spent_and_refilled_by_turns(self):
        engine, llm, transport, clock = make_engine(
            lambda call: REPLY(None, "just chiming in"),
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
        self.assertEqual(len(llm.calls), 1)  # no quota: it doesn't even check

        await engine.handle_message(msg(503, "C", author=13))  # 3rd turn → 1 refilled
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("#2 Alex: B", llm.calls[1]["messages"][-1]["content"])  # the messages held back are included too

    async def test_same_author_burst_is_one_turn(self):
        engine, llm, transport, clock = make_engine(defaults={"refill_turns": 3})
        st = engine.state(CHANNEL, GUILD)
        st.bucket.tokens = 0
        for i in range(5):
            await engine.handle_message(msg(501 + i, f"typing it line by line {i}"))
            clock.advance(2)
        self.assertEqual(st.bucket.tokens, 0)
        self.assertEqual(st.bucket.turns_since_refill, 1)

    async def test_react_costs_partial_quota(self):
        engine, llm, transport, clock = make_engine(
            lambda call: json.dumps({"actions": [{"type": "react", "to": 1, "emoji": "🎉"}]}),
            defaults={"proactive": True},
        )
        await engine.handle_message(msg(501, "finally off work"))
        clock.advance(31)
        await engine.tick()
        await engine.drain()
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "🎉")])
        self.assertAlmostEqual(engine.states[CHANNEL].bucket.tokens, 3 - engine.ec.react_cost)

    async def test_min_speak_gap_after_mention_reply(self):
        engine, llm, transport, clock = make_engine(
            lambda call: REPLY(1, "mhm"), defaults={"proactive": True, "min_speak_gap": 120}
        )
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        await engine.handle_message(msg(502, "and then?", author=12))
        clock.advance(40)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # it just spoke, so speaking up on its own waits out the gap
        clock.advance(90)
        await engine.tick()
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)


class Budget(unittest.IsolatedAsyncioTestCase):
    async def test_over_budget_only_answers_mentions_then_stops(self):
        engine, llm, transport, clock = make_engine(
            lambda call: REPLY(None, "mhm"), defaults={"proactive": True, "daily_budget_usd": 0.001}
        )
        engine.ledger.add(GUILD, 0.0015, clock())
        await engine.handle_message(msg(501, "what should we talk about"))
        clock.advance(60)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])  # over budget: no speaking up on its own

        await engine.handle_message(msg(502, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # still answers when called

        engine.ledger.add(GUILD, 0.01, clock())
        clock.advance(60)
        await engine.handle_message(msg(503, "@Dee still there?", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # over 2x: stops completely

    async def test_cost_is_recorded(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "mhm"))
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        spent = engine.ledger.guild_spent(GUILD, clock())
        self.assertGreater(spent, 0)
        stats = engine.store.day_stats(engine.ledger.day(clock()), guild_id=GUILD)
        self.assertEqual(stats["outcomes"], {"reply": 1})
        self.assertAlmostEqual(stats["cost"], spent)


class Errors(unittest.IsolatedAsyncioTestCase):
    async def test_insufficient_balance_pauses(self):
        engine, llm, transport, clock = make_engine(lambda call: LLMError("Insufficient Balance", status=402))
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(transport.sent, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "⚠️")])
        self.assertGreater(engine.paused_until, clock())
        await engine.handle_message(msg(502, "@Dee try again", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)  # no more calls while paused

    async def test_error_keeps_messages_for_next_round(self):
        outputs = [LLMError("Server Error", status=500), REPLY(2, "sorry, I froze up there")]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(engine.states[CHANNEL].pending), 1)
        await engine.handle_message(msg(502, "@Dee hey", mentions_bot=True))
        await engine.drain()
        user = llm.calls[1]["messages"][-1]["content"]
        self.assertIn(": @Dee 〔@you〕", user)  # the message that wasn't handled last time is still there
        self.assertIn(": @Dee hey 〔@you〕", user)


class Memory(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_keeps_recent_rounds_and_summarizes_old_ones(self):
        def responder(call):
            if not call["json_mode"]:
                return "Alex likes hotpot; Mia is job hunting"
            return REPLY(None, "uh-huh")

        engine, llm, transport, clock = make_engine(responder, defaults={"context_budget_tokens": 4000})
        llm.prompt_tokens = 5000
        for i in range(14):
            await engine.handle_message(msg(501 + i, f"@Dee round {i}", mentions_bot=True))
            await engine.drain()
            clock.advance(30)

        summary_calls = [c for c in llm.calls if not c["json_mode"]]
        self.assertEqual(len(summary_calls), 1)  # compacted only once, not every round
        self.assertIn("round 0", summary_calls[0]["messages"][1]["content"])
        conv = engine.states[CHANNEL].conversation
        self.assertEqual(conv.summary, "Alex likes hotpot; Mia is job hunting")
        last_rounds = [content_text(t.content) for t in conv.turns if t.role == "user"]
        self.assertTrue(any("round 13" in x for x in last_rounds))
        self.assertGreaterEqual(len(conv.turns), 12)  # the last 6 rounds are kept whole
        self.assertFalse(any("round 0" in x for x in last_rounds))

        await engine.handle_message(msg(520, "@Dee remember?", mentions_bot=True))
        await engine.drain()
        chat_calls = [c for c in llm.calls if c["json_mode"]]
        system = chat_calls[-1]["messages"][0]["content"]
        self.assertIn("Summary of earlier chat", system)
        self.assertIn("Alex likes hotpot", system)

    async def test_history_survives_restart(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "noted"))
        await engine.handle_message(msg(501, "@Dee my name is Alex", mentions_bot=True))
        await engine.drain()
        engine.states.clear()  # simulate a restart: in-memory state is wiped, only the database is left
        await engine.handle_message(msg(502, "@Dee what's my name?", mentions_bot=True))
        await engine.drain()
        messages = llm.calls[-1]["messages"]
        self.assertEqual(messages[: len(llm.calls[0]["messages"])], llm.calls[0]["messages"])
        self.assertIn("#2 Alex: @Dee what's my name?", messages[-1]["content"])  # numbering carries on

    async def test_reset_channel(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "sure"))
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        engine.reset_channel(CHANNEL)
        await engine.handle_message(msg(502, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls[-1]["messages"]), 2)  # system + the new message


class Images(unittest.IsolatedAsyncioTestCase):
    async def test_images_are_attached_with_detail(self):
        sides = {}

        async def loader(att, max_side):
            sides[att.filename] = max_side
            return f"data:image/jpeg;base64,{att.filename}"

        engine, llm, transport, clock = make_engine(lambda call: REPLY(2, "haha"), image_loader=loader)
        await engine.handle_message(msg(501, "check out this meme", images=[ImageAttachment("meme", "image/png", 10)]))
        await engine.handle_message(
            msg(502, "@Dee what does this pic mean", mentions_bot=True, author=12, name="Mia",
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
        self.assertIn("#1 Alex: [image] check out this meme", text)
        self.assertIn("(Above: images from #1)", text)
        self.assertEqual(sides, {"q": 1024, "meme": 512})  # images not from the person calling it are scaled down more

    async def test_image_limit_prefers_recent(self):
        async def loader(att, max_side):
            return "data:image/jpeg;base64," + att.filename

        engine, llm, transport, clock = make_engine(lambda call: "{\"actions\": []}", image_loader=loader)
        engine.ec.max_images_per_round = 2
        imgs = [ImageAttachment(f"p{i}", "image/png", 1) for i in range(3)]
        await engine.handle_message(msg(501, "old pic", images=[imgs[0]]))
        await engine.handle_message(msg(502, "new pics", images=imgs[1:], mentions_bot=True))
        await engine.drain()
        content = llm.calls[0]["messages"][-1]["content"]
        urls = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
        self.assertEqual(urls, ["data:image/jpeg;base64,p1", "data:image/jpeg;base64,p2"])



class Robustness(unittest.IsolatedAsyncioTestCase):
    """Regression tests for problems found in code review."""

    async def test_bad_request_drops_batch_instead_of_blocking_channel(self):
        outputs = [LLMError("Content Exists Risk", status=400), REPLY(2, "here")]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])
        await engine.handle_message(msg(501, "@Dee something that will get rejected", mentions_bot=True))
        await engine.drain()
        self.assertEqual(engine.states[CHANNEL].pending, [])  # dropped, so it isn't resent every time
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "⚠️")])
        await engine.handle_message(msg(502, "@Dee you there?", mentions_bot=True))
        await engine.drain()
        user = llm.calls[1]["messages"][-1]["content"]
        self.assertNotIn("will get rejected", user)
        self.assertEqual(len(transport.sent), 1)

    async def test_bad_request_with_images_retries_without_them(self):
        async def loader(att, max_side):
            return "data:image/jpeg;base64,AAAA"

        def responder(call):
            content = call["messages"][-1]["content"]
            if isinstance(content, list):
                return LLMError("bad image", status=400)
            return REPLY(1, "the pic won't open, but I saw what you said")

        engine, llm, transport, clock = make_engine(responder, image_loader=loader)
        await engine.handle_message(
            msg(501, "@Dee look at this pic", mentions_bot=True, images=[ImageAttachment("x.png", "image/png", 10)])
        )
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertIsInstance(llm.calls[1]["messages"][-1]["content"], str)
        self.assertEqual(len(transport.sent), 1)

    async def test_crash_after_paid_call_is_recorded_and_cools_down(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "mhm"), defaults={"proactive": True})
        real = engine.store.append_round
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("disk full")
            return real(*args, **kwargs)

        engine.store.append_round = flaky
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        stats = engine.store.day_stats(engine.ledger.day(clock()), guild_id=GUILD)
        self.assertEqual(stats["calls"], 1)  # the money spent was recorded in the database
        self.assertGreater(stats["cost"], 0)
        st = engine.states[CHANNEL]
        self.assertFalse(any(m.trigger for m in st.pending))
        self.assertGreater(st.error_until, clock())
        for _ in range(3):  # during the cooldown it doesn't spend money again on every tick
            clock.advance(5)
            await engine.tick()
            await engine.drain()
        self.assertEqual(len(llm.calls), 1)

    async def test_failed_call_does_not_swallow_new_mentions(self):
        outputs = [LLMError("Server Error", status=500), REPLY(2, "coming")]
        engine, llm, transport, clock = make_engine(lambda call: outputs[len(llm.calls) - 1])

        async def hook(call):
            if len(llm.calls) == 1:  # someone else calls it while the first call is still in progress
                await engine.handle_message(msg(502, "@Dee I have a question too", mentions_bot=True, author=12, name="Mia"))

        llm.hook = hook
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("called you", llm.calls[1]["messages"][-1]["content"])
        self.assertEqual(len(transport.sent), 1)

    async def test_reset_during_round_is_not_undone(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "mhm"))

        async def hook(call):
            if len(llm.calls) == 1:
                engine.reset_channel(CHANNEL)

        llm.hook = hook
        await engine.handle_message(msg(501, "@Dee here's my secret", mentions_bot=True))
        await engine.drain()
        self.assertEqual(engine.store.load_conversation(CHANNEL, GUILD).turns, [])
        llm.hook = None
        await engine.handle_message(msg(502, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls[-1]["messages"]), 2)

    async def test_unsendable_channel_spends_quota_and_backs_off(self):
        engine, llm, transport, clock = make_engine(
            lambda call: REPLY(None, "just saying something"),
            defaults={"proactive": True, "quota_capacity": 5, "refill_turns": 1, "min_speak_gap": 0, "min_eval_interval": 10},
        )
        transport.can_send = False
        for i in range(5):
            await engine.handle_message(msg(600 + i, f"message {i}", author=20 + i))
            clock.advance(31)
            await engine.tick()
            await engine.drain()
        st = engine.states[CHANNEL]
        self.assertEqual(len(llm.calls), 3)  # pauses after 3 rounds in a row that couldn't send
        self.assertGreater(st.error_until, clock())
        self.assertTrue(engine.status(GUILD, CHANNEL, None)["send_blocked"])

    async def test_mention_survives_batch_overflow(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "here"))
        engine.ec.max_batch_messages = 5
        await engine.handle_message(msg(501, "@Dee do me a favor", mentions_bot=True))
        for i in range(10):
            await engine.handle_message(msg(600 + i, f"spam {i}", author=12, name="Mia"))
        await engine.drain()
        user = llm.calls[0]["messages"][-1]["content"]
        self.assertIn("#1 Alex: @Dee do me a favor 〔@you〕", user)
        self.assertIn("(#1 called you. Respond, and use the language of their message.)", user)
        self.assertIn("(5 earlier messages not shown)", user)
        self.assertEqual(transport.sent[0][2], 501)

    async def test_stale_messages_are_pruned(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "mhm"))
        await engine.handle_message(msg(501, "something said long ago"))
        clock.advance(7 * 3600)
        await engine.handle_message(msg(502, "@Dee", mentions_bot=True))
        await engine.drain()
        user = llm.calls[0]["messages"][-1]["content"]
        self.assertNotIn("long ago", user)

    async def test_batch_header_shows_time_range(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "mhm"))
        await engine.handle_message(msg(501, "morning"))
        clock.advance(600)
        await engine.handle_message(msg(502, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertTrue(llm.calls[0]["messages"][-1]["content"].startswith("【10-07 20:00 ~ 20:10】"))

    async def test_paused_mentions_get_warning(self):
        engine, llm, transport, clock = make_engine()
        engine.paused_until = clock() + 600
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertEqual(transport.reactions, [(CHANNEL, 501, "⚠️")])

    async def test_truncated_thinking_retries_without_thinking(self):
        from dsbot.models import Completion, Usage

        def responder(call):
            if call["thinking"] != "off":
                return Completion(content="", reasoning="overthinking " * 100, usage=Usage(5000, 4000, 1000, 6000, 6000), finish_reason="length")
            return REPLY(1, "short answer: yes")

        engine, llm, transport, clock = make_engine(responder, defaults={"mention_thinking": "high"})
        await engine.handle_message(msg(501, "@Dee will this work?", mentions_bot=True))
        await engine.drain()
        self.assertEqual([c["thinking"] for c in llm.calls], ["high", "off"])
        self.assertEqual(transport.sent[0][1], "short answer: yes")
        outcomes = engine.store.day_stats(engine.ledger.day(clock()))["outcomes"]
        self.assertEqual(outcomes, {"empty": 1, "reply": 1})

    async def test_global_budget_ignores_guild_override(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "mhm"), defaults={"proactive": True})
        engine.settings.set("global", 0, "global_daily_budget_usd", "0.001")
        engine.settings.set("guild", GUILD, "global_daily_budget_usd", "100")  # must not get around the global cap
        engine.ledger.add(999, 0.002, clock())
        await engine.handle_message(msg(501, "what should we talk about"))
        clock.advance(60)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])



class Areas(unittest.IsolatedAsyncioTestCase):
    """Area switches: channel > the thread's parent channel > category > server."""

    async def test_disabled_channel_is_ignored_completely(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "here"))
        engine.settings.set("channel", CHANNEL, "enabled", "off")
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(llm.calls, [])
        self.assertNotIn(CHANNEL, engine.states)  # not even buffered

    async def test_whitelist_by_category(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "here"))
        engine.settings.set("guild", GUILD, "enabled", "off")
        engine.settings.set("category", 50, "enabled", "on")
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True, category_id=50))
        await engine.handle_message(msg(502, "@Dee", mentions_bot=True, channel_id=200, category_id=60))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual([s[0] for s in transport.sent], [CHANNEL])

    async def test_channel_setting_beats_category(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "here"))
        engine.settings.set("category", 50, "enabled", "off")
        engine.settings.set("channel", CHANNEL, "enabled", "on")
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True, category_id=50))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)

    async def test_thread_follows_parent_then_category(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "here"))
        engine.settings.set("category", 50, "enabled", "off")
        thread = dict(channel_id=300, parent_id=CHANNEL, category_id=50, mentions_bot=True)
        await engine.handle_message(msg(501, "@Dee", **thread))
        await engine.drain()
        self.assertEqual(llm.calls, [])
        engine.settings.set("channel", CHANNEL, "enabled", "on")  # turn on the thread's parent channel
        await engine.handle_message(msg(502, "@Dee", **thread))
        await engine.drain()
        self.assertEqual(len(llm.calls), 1)

    async def test_disabling_drops_buffered_messages(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(None, "mhm"), defaults={"proactive": True})
        await engine.handle_message(msg(501, "a message waiting in the buffer"))
        engine.settings.set("channel", CHANNEL, "enabled", "off")
        clock.advance(60)
        await engine.tick()
        await engine.drain()
        self.assertEqual(llm.calls, [])  # speaking up on its own stops too
        engine.settings.set("channel", CHANNEL, "enabled", "on")
        await engine.handle_message(msg(502, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertIn("a message waiting in the buffer", llm.calls[0]["messages"][-1]["content"])  # nothing was handled while it was off, so it's still there when turned back on
        engine.settings.set("channel", CHANNEL, "enabled", "off")
        await engine.handle_message(msg(503, "something said after it was turned off"))
        self.assertEqual(engine.states[CHANNEL].pending, [])


class LinkPreviews(unittest.IsolatedAsyncioTestCase):
    """GIF picker / image links: the preview image only arrives a second or two later, through a "message update"."""

    async def test_late_preview_is_merged_into_pending_message(self):
        async def loader(att, max_side):
            return "data:image/jpeg;base64," + att.filename

        engine, llm, transport, clock = make_engine(lambda call: REPLY(2, "lmao"), image_loader=loader)
        await engine.handle_message(msg(501, "https://tenor.com/view/cat-123", awaiting_embeds=True))
        updated = msg(501, "", images=[ImageAttachment("cat.png", None, 0, kind="gif")])
        self.assertTrue(engine.refresh_message(updated))
        await engine.handle_message(msg(502, "@Dee look at this", mentions_bot=True, author=12, name="Mia"))
        await engine.drain()
        content = llm.calls[0]["messages"][-1]["content"]
        text = "".join(p.get("text", "") for p in content)
        self.assertIn("#1 Alex: [GIF]", text)
        self.assertNotIn("tenor.com", text)
        self.assertEqual([p["image_url"]["url"] for p in content if p["type"] == "image_url"], ["data:image/jpeg;base64,cat.png"])

    async def test_mention_with_link_waits_for_preview(self):
        async def loader(att, max_side):
            return "data:image/jpeg;base64," + att.filename

        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "that cat is so ugly"), image_loader=loader)
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            if seconds == 2.0:  # while it waits, Discord pushes a message update with the preview
                engine.refresh_message(
                    msg(501, "@Dee look", mentions_bot=True, images=[ImageAttachment("cat.png", None, 0, kind="gif")])
                )

        engine.sleep = sleep
        await engine.handle_message(msg(501, "@Dee look https://tenor.com/view/cat-1", mentions_bot=True, awaiting_embeds=True))
        await engine.drain()
        self.assertEqual(waits[0], 2.0)
        content = llm.calls[0]["messages"][-1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[1]["image_url"]["detail"], "auto")  # images from the person calling it get a closer look

    async def test_no_wait_without_links(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "here"))
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(engine.test_sleeps), 1)  # only the one for "typing"; no waiting for a preview

    async def test_refresh_after_round_is_ignored(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "here"))
        await engine.handle_message(msg(501, "@Dee", mentions_bot=True))
        await engine.drain()
        self.assertFalse(engine.refresh_message(msg(501, "edited")))


if __name__ == "__main__":
    unittest.main()
