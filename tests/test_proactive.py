"""2.10.0 主动发言：平时隔几分钟看一眼，聊起来了就跟着。

- 平时：每 check_minutes～1.5 倍分钟（随机）看一眼。最近 5 分钟没人说话就不问模型；有人在聊就等停一下再判断，
  更早攒下的消息只当背景（写明是几分钟以前说的）。
- 它开了口（或者有人叫了它、它回了）以后跟着这场聊天：有人说话，停 5 秒就看要不要接；两次判断之间隔 30～45 秒。
- 5 分钟没人说话、或者连着 3 次都决定不说，就回到平时。
- 安静档不跟着；主动发言没开的频道，被叫了也不跟着。"""

from __future__ import annotations

import random
import unittest

from helpers import CHANNEL, GUILD, make_engine, msg
from test_engine import REPLY

from dsbot.config import PRESETS, check_range
from dsbot.engine import FOLLOW_PASSES, FRESH_SECONDS, SPEAK_GAP
from dsbot.prompts import HINT_FOLLOW, HINT_PROACTIVE, STALE_NOTE

PASS = '{"actions": []}'


class Shortest(random.Random):
    """随机的间隔都取最短的那头（测试里好算）。"""

    def uniform(self, a, b):
        return a


def setup(answers, **defaults):
    """answers：模型依次答什么（用完了就一直不说）。主动发言开着。"""
    left = list(answers)
    engine, llm, transport, clock = make_engine(lambda call: left.pop(0) if left else PASS, defaults={"proactive": True, **defaults})
    engine.rng = Shortest()
    return engine, llm, transport, clock


def text(call):
    content = call["messages"][-1]["content"]
    return content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)


class Case(unittest.IsolatedAsyncioTestCase):
    async def say(self, engine, mid, words="嗯", author=11, **kw):
        await engine.handle_message(msg(mid, words, author=author, **kw))

    async def look(self, engine, clock, seconds):
        clock.advance(seconds)
        await engine.tick()
        await engine.drain()


class Glance(Case):
    async def test_judges_once_then_waits_minutes(self):
        engine, llm, transport, clock = setup([PASS, PASS])
        await self.say(engine, 501, "今晚吃啥")
        await self.look(engine, clock, 31)  # 第一次：有人说话、停了 30 秒就看
        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(text(llm.calls[0]).endswith(HINT_PROACTIVE))
        st = engine.states[CHANNEL]
        self.assertFalse(st.following)  # 没开口：不跟着
        self.assertEqual(st.next_check_at, clock() + 5 * 60)  # 普通档：5～8 分钟后再看（这里取最短）
        await self.say(engine, 502, "火锅吧", author=12)
        await self.look(engine, clock, 120)
        await self.look(engine, clock, 120)
        self.assertEqual(len(llm.calls), 1)  # 还没到下一眼：有人说话也不判断
        await self.say(engine, 503, "那就火锅", author=13)
        await self.look(engine, clock, 61)
        self.assertEqual(len(llm.calls), 2)  # 到点了，最近有人在聊：看一眼
        self.assertIn("#2 小雨：火锅吧" if "小雨" in text(llm.calls[1]) else "火锅吧", text(llm.calls[1]))

    async def test_nothing_new_costs_nothing(self):
        engine, llm, transport, clock = setup([PASS])
        await self.say(engine, 501, "有人吗")
        await self.look(engine, clock, 31)
        self.assertEqual(len(llm.calls), 1)
        await self.say(engine, 502, "没人啊", author=12)
        await self.look(engine, clock, 5 * 60 + FRESH_SECONDS + 10)  # 到点了，但最后一句已经是 5 分钟多以前的
        self.assertEqual(len(llm.calls), 1)  # 不问模型
        st = engine.states[CHANNEL]
        self.assertEqual(len(st.pending), 1)  # 那句留着当背景
        self.assertEqual(st.next_check_at, clock() + 5 * 60)  # 过几分钟再看

    async def test_old_messages_are_only_background(self):
        engine, llm, transport, clock = setup([PASS, REPLY(None, "我也想吃")])
        await self.say(engine, 501, "早安")
        await self.look(engine, clock, 31)
        await self.say(engine, 502, "十分钟前的话", author=12)
        clock.advance(600)
        await self.say(engine, 503, "刚说的话", author=13)
        await self.look(engine, clock, 31)
        self.assertEqual(len(llm.calls), 2)
        user = text(llm.calls[1])
        note = STALE_NOTE.format(minutes=5)
        self.assertIn(note, user)
        self.assertLess(user.index("十分钟前的话"), user.index(note))  # 旧的在说明上面
        self.assertLess(user.index(note), user.index("刚说的话"))  # 最近的在下面
        self.assertNotIn(STALE_NOTE.split("{")[0], text(llm.calls[0]))  # 没有旧消息时不写

    async def test_waits_for_a_lull_and_not_forever(self):
        engine, llm, transport, clock = setup([])
        for i in range(40):
            await self.say(engine, 600 + i, f"刷屏{i}", author=11 + i % 2)
            await engine.tick()
            await engine.drain()
            if llm.calls:
                break
            clock.advance(10)
        self.assertEqual(len(llm.calls), 1)
        self.assertGreaterEqual(i * 10, engine.cfg.defaults["max_wait_seconds"])  # 一直有人说：等够了也看一次

    async def test_interval_is_random_within_the_range(self):
        engine, llm, transport, clock = setup([])
        engine.rng = random.Random(7)
        view = engine.view(engine.state(CHANNEL, GUILD))
        got = [engine._check_interval(view) for _ in range(200)]
        self.assertTrue(all(5 * 60 <= g <= 8 * 60 for g in got))
        self.assertGreater(max(got) - min(got), 60)
        self.assertEqual([check_range(m) for m in (10, 5, 3, 1)], [(10, 15), (5, 8), (3, 5), (1, 2)])
        self.assertEqual(PRESETS, {"安静": {"check_minutes": 10, "follow": False}, "普通": {"check_minutes": 5, "follow": True}, "话痨": {"check_minutes": 3, "follow": True}})


class Follow(Case):
    async def start(self, answers, **defaults):
        """它平时看一眼时开了口：开始跟着。"""
        engine, llm, transport, clock = setup([REPLY(None, "我也想吃火锅"), *answers], **defaults)
        await self.say(engine, 501, "今晚吃啥")
        await self.look(engine, clock, 31)
        self.assertEqual(len(llm.calls), 1)
        return engine, llm, transport, clock

    async def test_keeps_up_once_it_spoke(self):
        engine, llm, transport, clock = await self.start([REPLY(None, "辣锅")])
        st = engine.states[CHANNEL]
        self.assertTrue(st.following)
        await self.say(engine, 502, "那你要什么锅", author=12)
        await self.look(engine, clock, 10)
        self.assertEqual(len(llm.calls), 1)  # 刚说过话：隔 30～45 秒才再判断
        await self.look(engine, clock, SPEAK_GAP[0] - 10)
        self.assertEqual(len(llm.calls), 2)  # 不用等下一个“几分钟”
        user = text(llm.calls[1])
        self.assertTrue(user.endswith(HINT_FOLLOW))
        self.assertNotIn(STALE_NOTE.split("{")[0], user)
        self.assertTrue(st.following)

    async def test_judges_soon_after_someone_stops_talking(self):
        engine, llm, transport, clock = await self.start([PASS])
        clock.advance(60)
        await self.say(engine, 502, "对了", author=12)
        await self.look(engine, clock, 2)
        self.assertEqual(len(llm.calls), 1)  # 刚说完，可能还要接着说：等一下
        await self.look(engine, clock, 4)
        self.assertEqual(len(llm.calls), 2)  # 停了 5 秒：看要不要接

    async def test_not_every_sentence_is_judged(self):
        engine, llm, transport, clock = await self.start([PASS, PASS])
        clock.advance(60)
        await self.say(engine, 502, "第一句", author=12)
        await self.look(engine, clock, 6)
        self.assertEqual(len(llm.calls), 2)  # 判断了一次（没接）
        await self.say(engine, 503, "第二句", author=13)
        await self.look(engine, clock, 10)
        self.assertEqual(len(llm.calls), 2)  # 刚判断过：隔 30～45 秒才再看，不是每句话都问模型
        await self.look(engine, clock, SPEAK_GAP[0] - 10)
        self.assertEqual(len(llm.calls), 3)

    async def test_goes_back_after_passing_several_times(self):
        engine, llm, transport, clock = await self.start([PASS] * FOLLOW_PASSES)
        st = engine.states[CHANNEL]
        for i in range(FOLLOW_PASSES):
            self.assertTrue(st.following)
            await self.say(engine, 510 + i, f"他们自己聊{i}", author=12 + i)
            await self.look(engine, clock, 50)
        self.assertEqual(len(llm.calls), 1 + FOLLOW_PASSES)
        self.assertFalse(st.following)  # 连着几次没话说：回到平时
        self.assertEqual(st.next_check_at, clock() + 5 * 60)
        await self.say(engine, 520, "还在聊", author=12)
        await self.look(engine, clock, 50)
        self.assertEqual(len(llm.calls), 1 + FOLLOW_PASSES)  # 平时：要等几分钟

    async def test_a_reply_resets_the_count(self):
        engine, llm, transport, clock = await self.start([PASS, PASS, REPLY(None, "哈哈"), PASS, PASS])
        st = engine.states[CHANNEL]
        for i in range(5):
            await self.say(engine, 510 + i, f"聊{i}", author=12 + i)
            await self.look(engine, clock, 50)
        self.assertTrue(st.following)  # 中间接了一句：重新数
        self.assertEqual(st.passes, 2)

    async def test_goes_back_after_five_quiet_minutes(self):
        engine, llm, transport, clock = await self.start([])
        st = engine.states[CHANNEL]
        await self.look(engine, clock, 200)
        self.assertTrue(st.following)
        await self.say(engine, 502, "等等", author=12)  # 有人说话：重新算 5 分钟
        await self.look(engine, clock, 10)
        self.assertEqual(len(llm.calls), 2)
        clock.advance(301)
        await self.say(engine, 503, "过了很久才有人说话", author=13)
        st.last_activity_at = clock() - 301  # 这句到之前已经安静了 5 分钟多
        await engine.tick()
        await engine.drain()
        self.assertFalse(st.following)
        self.assertEqual(len(llm.calls), 2)  # 回到平时：这句等下一眼

    async def test_called_and_answered_starts_following(self):
        engine, llm, transport, clock = setup([REPLY(1, "在"), REPLY(None, "可以啊")])
        await self.say(engine, 501, "@小D 在吗", mentions_bot=True)
        await engine.drain()
        st = engine.states[CHANNEL]
        self.assertTrue(st.following)  # 有人叫了它、它回了：接着留意
        await self.say(engine, 502, "那一起打游戏不", author=12)
        await self.look(engine, clock, SPEAK_GAP[0] + 1)
        self.assertEqual(len(llm.calls), 2)
        self.assertTrue(text(llm.calls[1]).endswith(HINT_FOLLOW))

    async def test_mentions_are_never_held_back(self):
        engine, llm, transport, clock = await self.start([REPLY(2, "在")])
        await self.say(engine, 502, "@小D 你说呢", author=12, mentions_bot=True)
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)  # 叫它的马上回，不管 30～45 秒

    async def test_not_where_proactive_is_off(self):
        engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "在"))
        engine.rng = Shortest()
        await self.say(engine, 501, "@小D 在吗", mentions_bot=True)
        await engine.drain()
        st = engine.states[CHANNEL]
        self.assertFalse(st.following)  # 主动发言没开：被叫了也不跟着
        await self.say(engine, 502, "然后呢", author=12)
        await self.look(engine, clock, 600)
        self.assertEqual(len(llm.calls), 1)

    async def test_quiet_preset_does_not_follow(self):
        engine, llm, transport, clock = await self.start([], **PRESETS["安静"])
        st = engine.states[CHANNEL]
        self.assertFalse(st.following)  # 安静档：说完就回到平时
        self.assertEqual(st.next_check_at, clock() + 10 * 60)
        await self.say(engine, 502, "然后呢", author=12)
        await self.look(engine, clock, 9 * 60)
        self.assertEqual(len(llm.calls), 1)

    async def test_turning_follow_off_takes_effect_at_once(self):
        engine, llm, transport, clock = await self.start([])
        st = engine.states[CHANNEL]
        engine.settings.set("guild", GUILD, "follow", False)
        await self.say(engine, 502, "然后呢", author=12)
        await self.look(engine, clock, 50)
        self.assertFalse(st.following)
        self.assertEqual(len(llm.calls), 1)

    async def test_status_says_what_it_is_doing(self):
        engine, llm, transport, clock = await self.start([])
        status = engine.status(GUILD, CHANNEL, None)
        self.assertTrue(status["following"])
        st = engine.states[CHANNEL]
        engine._stop_following(st, engine.view(st), clock())
        status = engine.status(GUILD, CHANNEL, None)
        self.assertFalse(status["following"])
        self.assertEqual(status["next_check_in"], 5 * 60)


if __name__ == "__main__":
    unittest.main()
