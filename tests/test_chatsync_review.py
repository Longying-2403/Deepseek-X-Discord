"""2.8.0 连通的频道，第一轮评审修掉的问题：共用记录的整理记忆、压缩摘要的提示、给模型和群友看的说明。

整理记忆：几个频道轮流说话时每个频道的归在一起整理（不切成一小段一小段、压缩时不漏掉）；断开的频道那部分只在它自己那份里整理；
一个服务器预算用完了不耽误别的服务器那部分；重启后连通，连进来的频道自己原来那份照样整理。"""

import json
import unittest
from types import SimpleNamespace as NS

import test_chatsync
from helpers import CHANNEL, GUILD
from test_chatsync import HALL, HERE_LABEL, LOBBY, chats, connect, disconnect, replies, say, text_of, world
from test_dm import OTHER
from test_memory import is_review, stores

from dsbot.llm import LLMError
from dsbot.models import ChannelPlace
from dsbot.prompts import SUMMARY_SYNC_NOTE, SYNC_FOREIGN


def reviews(llm):
    """整理记忆问了几次模型：[(花费记在哪个服务器, 给它看的聊天)]。"""
    return [(c["user_id"], text_of(c["messages"][-1]["content"])) for c in llm.calls if is_review(c)]


def remembering(what):
    """整理记忆时记一条长期记忆（what(给它看的聊天) -> 记什么）；聊天、压缩摘要照 replies() 回。"""
    others = replies()

    def responder(call):
        if is_review(call):
            add = what(text_of(call["messages"][-1]["content"]))
            return json.dumps({"cards": [], "memory": [{"add": add}]}, ensure_ascii=False)
        return others(call)

    return responder


def summary_calls(llm):
    """压缩摘要的请求（只有它不要 JSON）。"""
    return [c for c in llm.calls if not c["json_mode"]]


class ReviewSharedRecord(unittest.IsolatedAsyncioTestCase):
    async def test_compacting_reviews_every_dropped_round_once_per_channel(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(replies(), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1000})
        sync = connect(engine)
        here, there = [], []  # 主服、别的服说的
        for i in range(5):
            # 两边轮流被叫；被叫之前另一边随口聊的先补进记录（记成那边的）：记录里每一两条就换一个频道
            there.append(f"别的服闲聊{i}")
            await say(engine, clock, 100 + 4 * i, there[-1], channel=HALL, author=21, name="小红", call=False)
            here.append(f"主服第{i}句")
            await say(engine, clock, 101 + 4 * i, f"@小D {here[-1]}")
            here.append(f"主服闲聊{i}")
            await say(engine, clock, 102 + 4 * i, here[-1], call=False)
            there.append(f"别的服第{i}句")
            await say(engine, clock, 103 + 4 * i, f"@小D {there[-1]}", channel=HALL, author=21, name="小红")
        st = engine.states[CHANNEL]
        await engine._compact(st, engine._conversation(st), transport.channel_info(CHANNEL), force=True)
        await engine.drain()
        summary_in = text_of(summary_calls(llm)[-1]["messages"][-1]["content"])
        dropped = [w for w in here + there if w in summary_in]
        # 压掉的那部分换了四次以上频道（以前每换一次就切一段，压缩前只整理前 4 段，后面的就没整理过）
        self.assertGreaterEqual(len(dropped), 8)
        # 现在压掉的每一句都整理过、只整理一次；每个频道的归在一起：一边问一次
        done = reviews(llm)
        for w in dropped:
            self.assertEqual(sum(w in text for _, text in done), 1, w)
        self.assertEqual(sorted(uid for uid, _ in done), ["guild-1", "guild-2"])
        for uid, text in done:
            others = there if uid == "guild-1" else here
            self.assertFalse([w for w in others if w in text], uid)  # 一次只整理一个服务器那边的
        # 安静了再整理：没压掉的一次整理完（还是一边一次），压掉的不再整理
        engine.ec.review_min_rounds = 1
        clock.advance(700)
        await engine.tick()
        await engine.drain()
        done = reviews(llm)
        self.assertEqual(len(done), 4)
        for w in here + there:
            self.assertEqual(sum(w in text for _, text in done), 1, w)
        whole = engine.store.load_conversation(sync, 0)
        self.assertEqual(whole.reviewed_upto, whole.turns[-1].id)

    async def test_a_part_that_failed_is_retried_without_losing_or_repeating_the_others(self):
        cards, memory = stores()
        down = {"on": True}  # 整理主服那部分时暂时连不上
        ok = remembering(lambda text: "主服的约定" if "主服的事" in text else "别的服的约定")

        def responder(call):
            if is_review(call) and down["on"] and "主服的事" in text_of(call["messages"][-1]["content"]):
                return LLMError("连不上", 503)
            return ok(call)

        engine, llm, transport, clock = world(responder, cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1})
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 别的服的事", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 主服的事")
        await say(engine, clock, 3, "@小D 别的服又一件", channel=HALL, author=21, name="小红")
        clock.advance(700)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await engine.tick()
            await engine.drain()
        # 别的服先说话，它那部分（前后两轮）先整理、整理好了；接着整理主服那部分时连不上
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["别的服的约定"])
        self.assertEqual(memory.entries(GUILD), [])
        down["on"] = False
        clock.advance(1300)  # 失败过一次：隔久一点再整理
        await engine.tick()
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["主服的约定"])  # 主服那轮在别的服两轮中间，也没丢
        self.assertEqual(sum("别的服的事" in text for _, text in reviews(llm)), 1)  # 整理过的不再整理一遍
        whole = engine.store.load_conversation(sync, 0)
        self.assertEqual(whole.reviewed_upto, whole.turns[-1].id)

    async def test_a_channel_that_left_has_its_part_reviewed_once_in_its_own_record(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "小红养了一只猫"), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1}
        )
        sync = connect(engine)
        connect(engine, LOBBY, CHANNEL)  # 三个频道
        await say(engine, clock, 5, "@小D 我养了一只猫", channel=HALL, author=21, name="小红")
        await say(engine, clock, 6, "@小D 主服这边")
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))  # 还没整理就断开了，组里还剩两个
        await say(engine, clock, 7, "@小D 断开以后", channel=HALL, author=21, name="小红")
        clock.advance(700)
        await engine.tick()
        await engine.drain()
        cat = [text for _, text in reviews(llm) if "我养了一只猫" in text]
        self.assertEqual(len(cat), 1)  # 只整理一次
        self.assertIn("断开以后", cat[0])  # 在它自己那份里（拿回去的连同断开以后说的）
        self.assertEqual(sum("主服这边" in text for _, text in reviews(llm)), 1)  # 组里还连着的照常整理
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红养了一只猫"])
        for key, guild in ((HALL, OTHER), (sync, 0)):
            conv = engine.store.load_conversation(key, guild)
            self.assertEqual(conv.reviewed_upto, conv.turns[-1].id, key)

    async def test_one_server_over_budget_does_not_skip_the_others_part_when_compacting(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "记下了"), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1000}
        )
        connect(engine)
        await say(engine, clock, 1, "@小D 主服的话")
        for i in range(4):
            await say(engine, clock, 10 + i, f"@小D 别的服第{i}句", channel=HALL, author=21, name="小红")
        engine.settings.set("guild", GUILD, "daily_budget_usd", 0.0000001)  # 主服今天的预算用完了，别的服还有
        st = engine.states[HALL]
        await engine._compact(st, engine._conversation(st), transport.channel_info(HALL), force=True)
        await engine.drain()
        summary_in = text_of(summary_calls(llm)[-1]["messages"][-1]["content"])
        self.assertIn("主服的话", summary_in)
        dropped = [f"别的服第{i}句" for i in range(4) if f"别的服第{i}句" in summary_in]
        self.assertTrue(dropped)
        # 别的服压掉的那几句照常整理（记进别的服）；主服那句不整理（马上压掉了，只好不整理）
        done = reviews(llm)
        self.assertEqual([uid for uid, _ in done], ["guild-2"])
        for w in dropped:
            self.assertIn(w, done[0][1])
        self.assertNotIn("主服的话", done[0][1])
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["记下了"])
        self.assertEqual(memory.entries(GUILD), [])

    async def test_at_quiet_time_an_over_budget_servers_part_waits_for_its_budget(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "主服的约定" if "主服的事" in text else "别的服的约定"),
            cards=cards,
            memory=memory,
            engine_cfg={"review_min_rounds": 1},
        )
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 主服的事")
        await say(engine, clock, 2, "@小D 别的服的事", channel=HALL, author=21, name="小红")
        engine.settings.set("guild", GUILD, "daily_budget_usd", 0.0000001)  # 主服今天的预算用完了
        for _ in range(2):  # 安静了两次，预算一直不够
            clock.advance(700)
            await engine.tick()
            await engine.drain()
        # 别的服那部分照常整理（只整理一次）；主服那部分等着，不算整理过
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["别的服的约定"])
        self.assertEqual(memory.entries(GUILD), [])
        self.assertEqual([uid for uid, _ in reviews(llm)], ["guild-2"])
        whole = engine.store.load_conversation(sync, 0)
        self.assertLess(whole.reviewed_upto, whole.turns[0].id)  # 主服那轮在最前面
        # 预算够了（调高了，或者第二天）：主服那部分接着整理，别的服那部分不再整理一遍
        engine.settings.set("guild", GUILD, "daily_budget_usd", 5.0)
        clock.advance(700)
        await engine.tick()
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["主服的约定"])
        self.assertEqual(sum("别的服的事" in text for _, text in reviews(llm)), 1)
        whole = engine.store.load_conversation(sync, 0)
        self.assertEqual(whole.reviewed_upto, whole.turns[-1].id)

    async def test_linking_after_a_restart_reviews_the_channels_own_old_chat(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            remembering(lambda text: "周日一起爬山"), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1000}
        )
        await say(engine, clock, 1, "@小D 我们约好了：周日爬山", channel=HALL, author=21, name="小红")  # 还没到整理的时候
        engine.states.clear()
        engine.convs.clear()  # 像刚重启：那个频道还没有状态
        connect(engine)
        await engine.drain()
        done = reviews(llm)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][0], "guild-2")  # 花费记在别的服
        self.assertIn("周日爬山", done[0][1])
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["周日一起爬山"])  # 记进它自己的服务器
        self.assertEqual(memory.entries(GUILD), [])
        own = engine.store.load_conversation(HALL, OTHER)
        self.assertEqual(own.reviewed_upto, own.turns[-1].id)


class SharedPrompts(unittest.IsolatedAsyncioTestCase):
    async def test_compacting_a_shared_record_asks_which_channel_each_thing_was_in(self):
        engine, llm, transport, clock = world(replies())
        connect(engine)
        for i in range(3):
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")
            await say(engine, clock, 20 + i, f"@小D 别的服第{i}句", channel=HALL, author=21, name="小红")
            await say(engine, clock, 30 + i, f"@小D 大厅第{i}句", channel=LOBBY)  # 没连通的频道
        for channel in (CHANNEL, LOBBY):
            st = engine.states[channel]
            await engine._compact(st, engine._conversation(st), transport.channel_info(channel), force=True)
        linked, alone = (c["messages"][0]["content"] for c in summary_calls(llm))
        self.assertIn(SUMMARY_SYNC_NOTE, linked)
        self.assertNotIn(SUMMARY_SYNC_NOTE, alone)

    async def test_linked_prompt_says_its_words_here_are_in_the_shared_record(self):
        engine, llm, transport, clock = world()
        await say(engine, clock, 1, "@小D 连通之前的事")
        connect(engine)
        await say(engine, clock, 2, "@小D 接着说")
        system = chats(llm)[-1]["messages"][0]["content"]
        self.assertIn("## 这个频道连通之前", system)
        # 不再说“别的频道的人看不到你在这里说的话”（和共用的记录自相矛盾），也不说大家都知道连通了
        self.assertNotIn("别的频道的人看不到你在这里说的话", system)
        self.assertNotIn("都知道连通了", system)
        self.assertIn("你在这里说的话也在共用的记录里：你在别的频道被叫到、或者自己开口时都看得到", system)
        self.assertIn("你在这里说的话会进共用的记录、别的频道也会看到", system)
        self.assertIn(f"你现在在 {HERE_LABEL}。", system)
        self.assertIn(SYNC_FOREIGN, system)  # 连到了别的服务器

    async def test_same_server_link_has_no_cross_server_line(self):
        engine, llm, transport, clock = world()
        connect(engine, CHANNEL, LOBBY)  # 同一个服务器的两个频道
        await say(engine, clock, 1, "@小D 你好")
        system = chats(llm)[-1]["messages"][0]["content"]
        self.assertIn(f"你现在在 {HERE_LABEL}。", system)
        self.assertIn("## 连通的频道", system)
        self.assertNotIn(SYNC_FOREIGN, system)
        self.assertNotIn("不全在同一个服务器", system)


class SyncTexts(unittest.IsolatedAsyncioTestCase):
    """连通、断开时给 bot 主人看的说明、在频道里发的说明和 /ds 状态（借 test_chatsync.OwnerCommand 的假 Discord）。"""

    setUp = test_chatsync.OwnerCommand.setUp
    interaction = test_chatsync.OwnerCommand.interaction

    async def link(self, a, b):
        from dsbot.commands import sync_command

        ask = self.interaction()
        await sync_command(self.bot, ask, "link", str(a), str(b))
        before = len(self.transport.sent)
        await ask.response.sent[0][1].confirm(self.interaction())
        return ask.response.sent[0][0], {c: t for c, t, _, _ in self.transport.sent[before:]}

    async def unlink(self, channel):
        from dsbot.commands import sync_command

        ask = self.interaction()
        await sync_command(self.bot, ask, "unlink", str(channel), None)
        before = len(self.transport.sent)
        await ask.response.sent[0][1].confirm(self.interaction())
        return ask.response.sent[0][0], {c: t for c, t, _, _ in self.transport.sent[before:]}

    async def test_joining_and_leaving_say_who_sees_the_shared_record(self):
        from dsbot.commands import SYNC_NOTICE_LEFT_REST

        _, notices = await self.link(CHANNEL, HALL)  # 新建的组：两边都是“连通了”
        for cid in (CHANNEL, HALL):
            self.assertIn("连通了", notices[cid])
            self.assertNotIn("加进了连通组", notices[cid])
        # 第三个频道加进已经在聊的组：它看得到组里已经共用的记录（连同摘要），说明里都要写清楚
        preview, notices = await self.link(LOBBY, CHANNEL)
        self.assertIn("「主服」#大厅也会看到组里已经共用的记录（更早的摘要、已经断开的频道连通期间说过的也算）", preview)
        self.assertIn("bot 主人把这个频道加进了连通组", notices[LOBBY])
        self.assertIn("那几个频道之前共用的记录它也知道", notices[LOBBY])
        for cid in (CHANNEL, HALL):
            self.assertIn("「主服」#大厅也连进来了", notices[cid])
            self.assertIn("这里之前共用的记录（连同更早的摘要），那边以后也看得到", notices[cid])
        # 一个断开、组里还剩两个：共用的摘要不带走；还连着的频道也还记得它说过的
        preview, notices = await self.unlink(LOBBY)
        self.assertIn("共用记录更早的摘要也不带走", preview)
        self.assertIn("要让它们也忘掉，在「主服」#大厅用 `/ds 清空记忆`", preview)
        self.assertIn(SYNC_NOTICE_LEFT_REST, notices[LOBBY])
        self.assertIn("还连着的频道也还记得", notices[LOBBY])
        # 最后两个断开、组散了：共用的记录连同摘要删掉；没有还连着的频道了
        preview, notices = await self.unlink(CHANNEL)
        self.assertIn("共用记录更早的摘要也不带走", preview)
        self.assertIn("共用的记录（连同更早的摘要）删掉", preview)
        self.assertEqual(set(notices), {CHANNEL, HALL})
        for text in notices.values():
            self.assertIn("不再连通了", text)
            self.assertNotIn("还连着的频道", text)

    async def test_status_shows_the_link_and_a_pause_only_for_a_private_channel(self):
        from dsbot.commands import status_text

        bot = NS(**vars(self.bot), settings=self.engine.settings, transport=self.transport, music_problem="没装")
        here = NS(guild_id=GUILD, channel_id=CHANNEL, channel=NS(category=None, category_id=None))
        self.assertNotIn("📡", status_text(bot, here))
        connect(self.engine)
        text = status_text(bot, here)
        self.assertIn("📡 连通（Beta）：这个频道和「别的服」#大厅共用一份聊天记录（bot 主人连的；它在哪个频道就只在那里说话；私信里聊的、私信记忆都不带进来）", text)
        self.assertNotIn("连通暂停", text)
        self.assertIs(self.engine.status(GUILD, CHANNEL, None)["sync_paused"], False)
        self.assertIs(self.engine.status(GUILD, LOBBY, None)["sync_paused"], False)  # 没连通
        self.transport.places[CHANNEL] = ChannelPlace(guild_id=GUILD, name="闲聊", public=False)  # 后来改成了私密频道
        self.assertIs(self.engine.status(GUILD, CHANNEL, None)["sync_paused"], True)
        self.assertIn("连通暂停", status_text(bot, here))


if __name__ == "__main__":
    unittest.main()
