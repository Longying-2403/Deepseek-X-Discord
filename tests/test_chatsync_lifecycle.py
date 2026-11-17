"""2.8.0 连通的频道：连通、断开、清空记忆和正在跑的轮次碰在一起的时候。

连通时排着队的那一轮换到共用的记录上跑，拿的是共用记录的锁；等的时候才开口的频道也排在连通后面，连通以后马上回；
清空记忆先等用这几份记录的频道正在回的那一轮写完；清空时正在整理的旧记录作废；把别的频道的聊天补进来不动那边的冷却；
连通之前攒着、没叫它的聊天记进各自原来那份（不进共用的记录），叫它的那句连通以后照常回。"""

import asyncio
import unittest
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, OWNER, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, say, text_of, world
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review, stores

from dsbot.engine import PASSIVE_REPLY


async def spin(n=100):
    """让排着的任务跑一会儿（不用真的等时间）。"""
    for _ in range(n):
        await asyncio.sleep(0)


def answering(table):
    """按这一轮的话回：话里有 table 的键就回那句（挂在叫它的那条上），别的不说话；整理记忆、压缩摘要另外回。
    几个频道同时在跑，问模型的先后不一定：按内容回，不按顺序。"""

    def responder(call):
        if is_review(call):
            return '{"cards": [], "memory": []}'
        if not call["json_mode"]:
            return "摘要"
        last = text_of(call["messages"][-1]["content"])
        return next((ACTIONS(REPLY(None, out)) for words, out in table.items() if words in last), ACTIONS())

    return responder


def hold(llm, gates, spy=None):
    """问模型时这一轮的话里有 gates 的键：停在那里等那个 Event（“正在等模型回话”）。spy(这一轮的话) 在停下之前调用。"""

    async def hook(call):
        if not call["json_mode"] or is_review(call):
            return
        last = text_of(call["messages"][-1]["content"])
        if spy is not None:
            spy(last)
        for words, gate in gates.items():
            if words in last:
                await gate.wait()
                return

    llm.hook = hook


def users(engine, key, guild=0):
    """这份记录里每一轮群友说的（连通组的记录 guild 是 0）。"""
    return [text_of(t.content) for t in engine.store.load_conversation(key, guild).turns if t.role == "user"]


def sent(transport):
    """发出去的话：[(频道, 文字)]。"""
    return [(c, t) for c, t, _, _ in transport.sent]


def prompt_of(call):
    """这次请求模型看到的全部：system prompt、记录、这一轮。"""
    return "\n".join(text_of(m["content"]) for m in call["messages"])


class LinkWhileBusy(unittest.IsolatedAsyncioTestCase):
    async def test_round_queued_during_linking_holds_the_shared_lock(self):
        """主服、别的服都在回话时 bot 主人连通了。主服排着队的下一轮换到共用的记录上跑，拿着共用记录的锁：
        别的服这时候叫它，要等这一轮写完才轮到。主服连通之前随口说的那句记进它自己那份，不进共用的。"""
        gates = {words: asyncio.Event() for words in ("第一轮A", "第一轮B", "第二轮A")}
        seen = {}
        engine, llm, transport, clock = world(answering({"第二轮A": "回主服第二轮", "那边叫你": "回别的服"}))

        def spy(last):
            if "第二轮A" in last:
                sync = engine.store.sync_of(CHANNEL)
                seen["shared"] = engine.states[CHANNEL].conv.key == sync
                seen["locked"] = engine.convs[sync].lock.locked()
            elif "那边叫你" in last:
                seen["b_meanwhile"] = not gates["第二轮A"].is_set()

        hold(llm, gates, spy)
        await engine.handle_message(msg(1, "@小D 第一轮A", mentions_bot=True))
        await engine.handle_message(msg(11, "@小D 第一轮B", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        clock.advance(1)
        await engine.handle_message(msg(2, "随口一句"))
        await engine.handle_message(msg(3, "@小D 第二轮A", mentions_bot=True))  # 主服正忙：排着队

        async def link_now():
            async with engine.sync_guard([CHANNEL, HALL]):
                connect(engine)

        linking = asyncio.create_task(link_now())
        await spin()
        gates["第一轮A"].set()  # 主服第一轮写完：连通再等别的服那一轮
        await spin()
        gates["第一轮B"].set()  # 别的服也写完：连通
        await linking
        await spin()
        self.assertTrue(seen.get("shared"))  # 主服第二轮已经在共用的记录上问模型了
        await engine.handle_message(msg(20, "@小D 那边叫你", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        gates["第二轮A"].set()
        await engine.drain()
        self.assertTrue(seen["locked"])  # 问模型的时候拿着共用记录的锁
        self.assertFalse(seen["b_meanwhile"])  # 别的服等主服这一轮写完
        self.assertEqual(sent(transport), [(CHANNEL, "回主服第二轮"), (HALL, "回别的服")])
        sync = engine.store.sync_of(CHANNEL)
        self.assertTrue(any("随口一句" in u for u in users(engine, CHANNEL, GUILD)))  # 连通之前说的：记进主服自己那份
        self.assertFalse(any("随口一句" in u for u in users(engine, sync)))
        self.assertTrue(any("第二轮A" in u for u in users(engine, sync)))

    async def test_a_channel_that_first_talks_while_linking_is_answered_after_it(self):
        """连通要等别的服正在回的那一轮。这时候主服（重启以后还没人说过话，内存里还没有它）有人叫它：这一轮排在连通后面，
        连通以后在共用的记录里回，不用等下一次定时检查。"""
        gates = {"别的服在忙": asyncio.Event(), "主服叫你": asyncio.Event()}
        engine, llm, transport, clock = world(answering({"别的服在忙": "回别的服", "主服叫你": "回主服"}))
        hold(llm, gates)
        await engine.handle_message(msg(11, "@小D 别的服在忙", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        self.assertNotIn(CHANNEL, engine.states)
        self.assertNotIn(CHANNEL, engine.convs)

        async def link_now():
            async with engine.sync_guard([CHANNEL, HALL]):
                connect(engine)

        linking = asyncio.create_task(link_now())
        await spin()
        await engine.handle_message(msg(1, "@小D 主服叫你", mentions_bot=True))
        await spin()
        self.assertFalse(any("主服叫你" in text_of(c["messages"][-1]["content"]) for c in chats(llm)))  # 排在连通后面
        gates["别的服在忙"].set()
        await linking
        gates["主服叫你"].set()
        await engine.drain()  # 不调用 tick
        self.assertIn((CHANNEL, "回主服"), sent(transport))
        sync = engine.store.sync_of(CHANNEL)
        self.assertTrue(any("主服叫你" in u for u in users(engine, sync)))
        self.assertEqual(users(engine, CHANNEL, GUILD), [])


class BeforeLinking(unittest.IsolatedAsyncioTestCase):
    async def test_chatter_from_before_linking_goes_into_each_channels_own_record(self):
        """连通之前攒着、没叫它的聊天（还在缓冲区里）：连通时记进各自原来那份，当成它看过、没说话；不进共用的记录，
        另一个频道也看不到。新建的组：两个频道都是这次连进来的。"""
        engine, llm, transport, clock = world()
        await say(engine, clock, 1, "主服连通前的悄悄话", call=False)
        await say(engine, clock, 2, "别的服连通前的悄悄话", channel=HALL, author=21, name="小红", call=False)
        sync = connect(engine)
        for channel, guild, line in ((CHANNEL, GUILD, "阿明：主服连通前的悄悄话"), (HALL, OTHER, "小红：别的服连通前的悄悄话")):
            self.assertEqual(engine.states[channel].pending, [])
            own = engine.store.load_conversation(channel, guild).turns
            self.assertEqual(len(own), 2)
            self.assertIn(line, text_of(own[0].content))
            self.assertEqual(own[1].content, PASSIVE_REPLY)  # 它没说话
        self.assertEqual(engine.store.load_conversation(sync, 0).turns, [])
        await say(engine, clock, 3, "@小D 那边在聊什么", channel=HALL, author=21, name="小红")
        await say(engine, clock, 4, "@小D 这边呢")
        there, here = chats(llm)
        self.assertNotIn("主服连通前的悄悄话", prompt_of(there))
        self.assertNotIn("别的服连通前的悄悄话", prompt_of(here))
        self.assertIn("小红：别的服连通前的悄悄话", there["messages"][0]["content"])  # 自己的只在“连通之前”那段里
        self.assertIn("阿明：主服连通前的悄悄话", here["messages"][0]["content"])
        self.assertFalse(any("悄悄话" in u for u in users(engine, sync)))

    async def test_a_call_waiting_at_linking_is_answered_in_the_shared_record(self):
        """连通的时候主服正在回别的话，后面攒着：随口聊的一句、叫它的一句、接着聊的一句。随口聊的两句（叫它那句的前后）
        都是连通之前说的，记进主服自己那份；只有叫它的那句留着，连通以后在共用的记录里回。"""
        engine, llm, transport, clock = world(answering({"等你回我": "连通以后回你"}))
        here = engine.state(CHANNEL, GUILD)
        async with here.lock:  # 主服正在回别的话（占着它那份记录）：这几条先攒着
            await engine.handle_message(msg(1, "先随口聊一句", author=12, name="小雨"))
            await engine.handle_message(msg(2, "@小D 等你回我", mentions_bot=True))
            await engine.handle_message(msg(3, "接着聊一句", author=12, name="小雨"))
        async with engine.sync_guard([CHANNEL, HALL]):
            sync = connect(engine)
            self.assertEqual([m.message_id for m in here.pending], [2])  # 只留着叫它的那句
        await engine.drain()
        own = engine.store.load_conversation(CHANNEL, GUILD).turns
        self.assertEqual(len(own), 2)
        self.assertIn("小雨：先随口聊一句", text_of(own[0].content))
        self.assertIn("小雨：接着聊一句", text_of(own[0].content))
        self.assertEqual(own[1].content, PASSIVE_REPLY)
        self.assertEqual([s[:3] for s in transport.sent], [(CHANNEL, "连通以后回你", 2)])  # 挂在叫它的那条上
        shared = engine.store.load_conversation(sync, 0).turns
        self.assertEqual([t.origin for t in shared], [CHANNEL, CHANNEL])
        self.assertIn("等你回我", text_of(shared[0].content))
        self.assertNotIn("接着聊一句", text_of(shared[0].content))  # 连通之前说的：不进共用的记录
        self.assertNotIn("先随口聊一句", text_of(shared[0].content))

    async def test_a_channel_joining_an_active_group_keeps_its_earlier_chatter(self):
        """已经在聊的连通组又连进来一个频道（大厅）：它连通之前攒着的聊天记进它自己那份，组里的频道看不到；
        攒着的叫它的那句连通以后在共用的记录里回。"""
        engine, llm, transport, clock = world(answering({"大厅叫你": "回大厅"}))
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 主服先聊")
        lobby = engine.state(LOBBY, GUILD)
        async with lobby.lock:  # 大厅正在回别的话：这两条先攒着
            await engine.handle_message(msg(5, "大厅连通前的悄悄话", channel_id=LOBBY, author=12, name="小雨"))
            await engine.handle_message(msg(6, "@小D 大厅叫你", channel_id=LOBBY, author=12, name="小雨", mentions_bot=True))
        async with engine.sync_guard([LOBBY, CHANNEL, HALL]):
            connect(engine, LOBBY, CHANNEL)
            self.assertEqual([m.message_id for m in lobby.pending], [6])
        await engine.drain()
        own = engine.store.load_conversation(LOBBY, GUILD).turns
        self.assertEqual(len(own), 2)
        self.assertIn("小雨：大厅连通前的悄悄话", text_of(own[0].content))
        self.assertEqual(own[1].content, PASSIVE_REPLY)
        self.assertEqual([s[:3] for s in transport.sent], [(LOBBY, "回大厅", 6)])
        shared = engine.store.load_conversation(sync, 0).turns
        self.assertEqual(shared[-1].origin, LOBBY)
        self.assertIn("大厅叫你", text_of(shared[-2].content))
        await say(engine, clock, 7, "@小D 大厅那边说了啥")
        self.assertNotIn("大厅连通前的悄悄话", prompt_of(chats(llm)[-1]))  # 组里的频道看不到
        self.assertIn("大厅叫你", prompt_of(chats(llm)[-1]))  # 连通以后说的照常共用
        self.assertFalse(any("悄悄话" in u for u in users(engine, sync)))


class ClearWhileBusy(unittest.IsolatedAsyncioTestCase):
    async def asking_elsewhere(self):
        """主服、别的服连通；主服说过一句，别的服叫它、正在等模型回话。"""
        self.gate = asyncio.Event()
        engine, llm, transport, clock = world(answering({"主服说的": "主服好", "别的服的问题": "回答别的服"}))
        hold(llm, {"别的服的问题": self.gate})
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 主服说的")
        await engine.handle_message(msg(2, "@小D 别的服的问题", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        return engine, transport, sync

    async def test_clearing_waits_for_a_round_running_in_another_linked_channel(self):
        """主服清空记忆的时候，别的服（连通的，另一个服务器）正在等模型回话：等那一轮写完再清。别的服的问题照样回、
        照样记在共用的记录里；主服说过的从共用的记录里删掉。"""
        engine, transport, sync = await self.asking_elsewhere()
        clearing = asyncio.create_task(engine.reset_linked(CHANNEL))
        await spin()
        self.assertFalse(clearing.done())  # 等别的服那一轮
        self.assertTrue(any("主服说的" in u for u in users(engine, sync)))
        self.gate.set()
        await clearing
        await engine.drain()
        self.assertIn((HALL, "回答别的服"), sent(transport))
        shared = users(engine, sync)
        self.assertTrue(any("别的服的问题" in u for u in shared))
        self.assertFalse(any("主服说的" in u for u in shared))

    async def test_clearing_that_waits_too_long_gives_up_and_changes_nothing(self):
        """别的服那一轮一直没写完：等太久就不清了（抛 TimeoutError），共用的记录不动，那一轮照常回、照常写进去。"""
        engine, transport, sync = await self.asking_elsewhere()
        before = users(engine, sync)
        with self.assertRaises(asyncio.TimeoutError):
            await engine.reset_linked(CHANNEL, timeout=0.01)
        self.assertEqual(users(engine, sync), before)
        self.gate.set()
        await engine.drain()
        self.assertIn((HALL, "回答别的服"), sent(transport))
        shared = users(engine, sync)
        self.assertTrue(any("主服说的" in u for u in shared))
        self.assertTrue(any("别的服的问题" in u for u in shared))

    async def test_clearing_after_leaving_waits_for_the_group_it_left(self):
        """大厅断开以后，它连通期间说的还留在组里的记录里。清空大厅的记忆时组里别的服正在回话：也要等那一轮写完，
        再把大厅那部分从组的记录里删掉。"""
        gate = asyncio.Event()
        engine, llm, transport, clock = world(answering({"别的服的问题": "回答别的服"}))
        hold(llm, {"别的服的问题": gate})
        sync = connect(engine)
        connect(engine, LOBBY, CHANNEL)
        await say(engine, clock, 1, "@小D 大厅连通时说的", channel=LOBBY, author=12, name="小雨")
        disconnect(engine, LOBBY)  # 组里还剩主服、别的服
        await engine.handle_message(msg(2, "@小D 别的服的问题", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        clearing = asyncio.create_task(engine.reset_linked(LOBBY))
        await spin()
        self.assertFalse(clearing.done())  # 等组里那一轮
        gate.set()
        await clearing
        await engine.drain()
        self.assertIn((HALL, "回答别的服"), sent(transport))
        shared = users(engine, sync)
        self.assertTrue(any("别的服的问题" in u for u in shared))
        self.assertFalse(any("大厅连通时说的" in u for u in shared))  # 大厅那部分从组的记录里删掉了
        self.assertEqual(users(engine, LOBBY, GUILD), [])  # 它自己那份也清空了

    async def test_clearing_discards_the_review_of_the_old_own_record(self):
        """连通时，主服自己原来那份里还没整理过的聊天排队整理。整理还在等模型的时候主服清空了记忆：整理的结果作废，
        不拿清空掉的聊天写长期记忆。"""
        cards, memory = stores()
        gate = asyncio.Event()

        def responder(call):
            if is_review(call):
                return '{"cards": [], "memory": [{"add": "清空之前的事"}]}'
            return ACTIONS()

        engine, llm, transport, clock = world(responder, cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1000})
        await say(engine, clock, 1, "@小D 以前的事")

        async def stall(call):
            if is_review(call):
                await gate.wait()

        llm.hook = stall
        connect(engine)  # 主服自己原来那份排队整理
        await spin()
        self.assertTrue(any(is_review(c) for c in llm.calls))  # 正在问模型
        await engine.reset_linked(CHANNEL)
        gate.set()
        await engine.drain()
        self.assertEqual(memory.entries(GUILD), [])
        self.assertEqual(users(engine, CHANNEL, GUILD), [])


class Deferred:
    """斜杠命令的回应：可以先 defer（显示“正在思考”），之后的话用 followup 发。"""

    def __init__(self):
        self.deferred = False
        self.said = []

    def is_done(self):
        return self.deferred

    async def defer(self, ephemeral=False, thinking=False):
        self.deferred = True

    async def send_message(self, text, view=None, ephemeral=False):
        self.said.append(text)


class ClearCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")

    async def test_clear_command_in_a_linked_channel_waits_then_confirms(self):
        """/ds 清空记忆 在连通的频道里：先回“正在处理”，等别的频道正在回的那一轮写完再清，清完再说一声。"""
        from dsbot.commands import register_commands

        gate = asyncio.Event()
        engine, llm, transport, clock = world(answering({"别的服的问题": "回答别的服"}))
        hold(llm, {"别的服的问题": gate})
        added = []
        tree = NS(add_command=lambda c, **kw: added.append(c), command=lambda **kw: (lambda f: f), error=lambda f: f)
        bot = NS(store=engine.store, engine=engine, settings=engine.settings, cfg=engine.cfg, cards=None, memory=None, tree=tree)
        bot.secrets = NS(owner_id=OWNER, control_guild_ids=())
        register_commands(bot)
        clear = next(c for c in added if c.name == "ds").get_command("清空记忆").callback
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 主服说的")
        await engine.handle_message(msg(2, "@小D 别的服的问题", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        response = Deferred()

        async def followup(text, ephemeral=False):
            response.said.append(text)

        task = asyncio.create_task(clear(NS(channel_id=CHANNEL, guild_id=GUILD, user=NS(id=OWNER), response=response, followup=NS(send=followup))))
        await spin()
        self.assertTrue(response.deferred)
        self.assertEqual(response.said, [])  # 还在等别的服那一轮
        gate.set()
        await task
        await engine.drain()
        self.assertIn("已清空：这个频道在连通的记录里说过的话", response.said[-1])
        self.assertIn((HALL, "回答别的服"), sent(transport))
        shared = users(engine, sync)
        self.assertTrue(any("别的服的问题" in u for u in shared))
        self.assertFalse(any("主服说的" in u for u in shared))


class Absorbing(unittest.IsolatedAsyncioTestCase):
    async def test_absorbing_keeps_the_other_channels_pause_and_cooldown(self):
        """主服被叫到时把别的服刚才的聊天补进记录：不算别的服跑了一轮。别的服发不出消息的 30 分钟暂停、
        上次判断的时间都不动。"""
        engine, llm, transport, clock = world()
        engine.settings.set("channel", HALL, "proactive", True)
        sync = connect(engine)
        hall = engine.state(HALL, OTHER)
        hall.send_failures = 3
        paused = hall.error_until = clock() + 1800  # 别的服连着发不出消息：暂停主动发言
        judged = hall.last_eval_at = clock() - 30
        await say(engine, clock, 1, "那边随口聊", channel=HALL, author=21, name="小红", call=False)
        await say(engine, clock, 2, "@小D 这边叫你")
        self.assertTrue(any("那边随口聊" in u for u in users(engine, sync)))  # 补进了共用的记录
        self.assertEqual(hall.pending, [])
        self.assertEqual((hall.error_until, hall.last_eval_at, hall.send_failures), (paused, judged, 3))
        self.assertTrue(engine.status(OTHER, HALL, None)["send_blocked"])  # 暂停照旧


if __name__ == "__main__":
    unittest.main()
