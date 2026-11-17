"""2.8.0 连通的频道，第四轮评审修掉的问题：跨服务器以后，从共用的记录里拿掉只有原来那边能看的东西。

- 服务器退出了服务器组，组里正好有一轮在跑（问模型之前按原来的组带上了近况、能翻的频道名单）：那一轮写进记录以后、放开锁
  之前就再查一遍。排在后面等锁的轮次（别的服务器的频道的）看不到那一轮带来的东西。这次查出错了只记一条（下次启动再查），
  这一轮跑的时候别的频道有人叫它，照样接着回。
- 记录里拿掉了只有原来那边能看的东西：那段时间它要翻记录、查人设卡的动作也从它的每一条回话里拿掉（里面写着频道名、查的是谁；
  查的那一步问模型出错了、没查成的也一样）。什么都没拿掉的话，它的回话原样不动。
- 人设卡各服务器一张：跨服务器的连通里它写卡的动作当时就一个都不留在记录里（要写的就是他现在的卡、什么都没改的也一样），
  以后再查（每次启动、每次连通断开）不改记录，bot 主人也不会看到“有 N 条拿掉了”。
- 写错了没认出来的写卡、翻记录的动作（原话里写着）：一样拿掉，它说的话照旧。
- 一次查完要改的几条一起写进库：中途出错就一条都不改，下次再查照样都拿掉。
- 服务器退出了服务器组：一个连通组查的时候出错，别的组照样查。连通、断开以后先拿掉只该原来那边看的，再补写“在放什么”是哪个
  服务器的，哪一步出错都不耽误另一步，也不让连通的收尾停下（出错的记一条，下次启动再查）。
- 服务器名里有“」”的，写明了服务器的“在放什么”也认得：写那句的频道照改过的认，没变就不再说一遍。"""

import sqlite3
import unittest
from contextlib import contextmanager

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, HALL_LABEL, LOBBY, chats, connect, replies, say, text_of, world
from test_chatsync_privacy import ADMINS, GAMES, PLAYING, elsewhere, history_of, jukebox, last_user, record
from test_chatsync_r2_privacy import same_group
from test_chatsync_r3_privacy import turns
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review, stores

from dsbot.actions import parse_actions
from dsbot.llm import LLMError
from dsbot.models import ChannelPlace
from dsbot.prompts import MUSIC_NOW, MUSIC_NOW_AT, TOOL_FAILED

YARD = 703  # 别的服的另一个公开频道 #后院
CARD = "主服的阿明：住在幸福路三号"  # 阿明在主服的人设卡


def link(engine, channel, to):
    """channel 连进 to 所在的组，只记进库里（还没查过：像下次启动那样直接 scrub_cross）。返回这次连进来的频道。"""
    pa, pb = engine.transport.locate(channel), engine.transport.locate(to)
    return engine.store.link_channels((channel, pa.guild_id, pa.name), (to, pb.guild_id, pb.name), now=3.0)[1]


def join(engine, channel, to):
    """像 /dsowner chatsync 连通 那样把 channel 连进 to 所在的组（不经过命令）：返回拿掉了几条（bot 主人看到的那个数）。"""
    return engine.sync_changed(link(engine, channel, to))


def kinds_in_replies(engine, sync):
    """共用记录里它的每条回话里记着的动作（按先后）。"""
    out = []
    for t in engine.store.load_conversation(sync, 0).turns:
        if t.role == "assistant":
            out += [a.kind for a in parse_actions(text_of(t.content))[0]]
    return out


def turn_with(engine, sync, words, role="user"):
    """共用记录里写着 words 的那一条（的 id）。"""
    return next(t.id for t in engine.store.load_conversation(sync, 0).turns if t.role == role and words in text_of(t.content))


@contextmanager
def writes_fail(engine, where):
    """改写记录（where：哪几条，SQL 条件）时数据库出错，像磁盘满了、文件坏了那样。"""
    engine.store.db.execute(
        f"CREATE TEMP TRIGGER writes_fail BEFORE UPDATE ON turns WHEN {where} BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END"
    )
    try:
        yield
    finally:
        engine.store.db.execute("DROP TRIGGER writes_fail")


class GroupLeftWhileARoundRuns(unittest.IsolatedAsyncioTestCase):
    async def test_a_round_waiting_on_the_lock_does_not_see_what_the_round_in_flight_brought_from_the_old_group(self):
        engine, llm, transport, clock = world()
        elsewhere(engine, transport)  # 主服开了记忆共享
        same_group(engine, GUILD, OTHER)
        await say(engine, clock, 1, "@小D 周六在游戏频道约副本", channel=GAMES)
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：同一个服务器组，不算跨服务器（带近况、能翻的频道名单）
        left = []

        async def leave_while_asking(call):
            if not left and call["json_mode"] and not is_review(call):
                left.append(True)  # #闲聊 这一轮正在等模型回话：别的服退出了服务器组（/dsowner 服务器组 的退组做完了）
                engine.store.unmap(OTHER)
                engine.groups_changed([GUILD, OTHER], left=True)

        llm.hook = leave_while_asking
        await engine.handle_message(msg(2, "@小D 游戏那边约了什么", mentions_bot=True))
        # 小红紧跟着在 #大厅 叫它：那一轮排在 #闲聊 这一轮后面等同一把锁
        await engine.handle_message(msg(3, "@小D 在吗", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await engine.drain()
        llm.hook = None
        self.assertTrue(left)
        asked = [text_of(c["messages"][-1]["content"]) for c in chats(llm)]
        self.assertIn("周六在游戏频道约副本", asked[-2])  # 对照：#闲聊 这一轮问模型时按原来的组带上了 #游戏 的近况
        self.assertIn(HALL_LABEL, asked[-1])  # 接着跑的是 #大厅 那一轮
        seen = history_of(llm)
        self.assertNotIn("周六在游戏频道约副本", seen)  # #闲聊 那一轮带来的近况，别的服 #大厅 被叫到时看不到
        self.assertNotIn("#游戏", seen)  # 能翻的频道名单也看不到
        self.assertNotIn("周六在游戏频道约副本", record(engine, sync))

    async def test_control_a_scrub_that_fails_at_the_end_of_the_round_does_not_hold_up_a_mention_that_came_in(self):
        # 对照（control）：#闲聊 这一轮跑的时候别的服退出了服务器组，这一轮写完再查时数据库出错了（下次启动再查）：
        # 这一轮跑的时候 #大厅 有人叫它，照样接着回（不用等下一次定时检查）
        engine, llm, transport, clock = world()
        elsewhere(engine, transport)
        same_group(engine, GUILD, OTHER)
        await say(engine, clock, 1, "@小D 周六在游戏频道约副本", channel=GAMES)
        sync = connect(engine)
        left = []

        async def leave_while_asking(call):
            if not left and call["json_mode"] and not is_review(call):
                left.append(True)
                engine.store.unmap(OTHER)
                engine.groups_changed([GUILD, OTHER], left=True)
                # 小红这时候在 #大厅 叫它：锁正被 #闲聊 这一轮拿着，等这一轮跑完叫醒
                await engine.handle_message(msg(3, "@小D 在吗", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))

        llm.hook = leave_while_asking
        # 改写这份记录时数据库出错（这一轮的日志一起收下，出错的那条也在里面）
        with writes_fail(engine, f"OLD.channel_id = {sync}"), self.assertLogs("dsbot.engine", "INFO"):
            await engine.handle_message(msg(2, "@小D 游戏那边约了什么", mentions_bot=True))
            await engine.drain()
        llm.hook = None
        self.assertTrue(left)
        self.assertIn(HALL_LABEL, last_user(llm))  # #大厅 那一轮照样跑了


class LookupsInItsReplies(unittest.IsolatedAsyncioTestCase):
    async def test_a_recall_whose_tool_step_failed_also_loses_its_channel_name(self):
        recall = ACTIONS({"type": "recall", "channel": "管理组", "about": "封禁名单"})
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(), recall, LLMError("server error", 503)))
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        await say(engine, clock, 1, "@小D 管理组开会", channel=ADMINS)
        await say(engine, clock, 2, "@小D 游戏频道开黑", channel=GAMES)
        sync = connect(engine, CHANNEL, LOBBY)  # 先是主服的两个频道连通：能翻的频道名单照常写进共用的记录
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 3, "@小D 帮我翻一下那个私密频道的封禁名单")  # 它要翻 #管理组，查的那一步问模型出错了
        whole = record(engine, sync)
        self.assertIn(TOOL_FAILED, whole)  # 对照：记的是“没查成”，没有翻到的记录可拿
        self.assertIn('"channel": "管理组"', whole)  # 对照：要翻 #管理组 的动作记在它的回话里
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅 连进来：跨服务器了
        self.assertNotIn("管理组", record(engine, sync))  # 名单拿掉了，要翻 #管理组 的那个动作也拿掉了
        await say(engine, clock, 4, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
        self.assertNotIn("管理组", history_of(llm))  # 别的服 #大厅 被叫到时看不到主服私密频道的名字

    async def test_control_a_scrub_that_finds_nothing_leaves_its_lookups_alone(self):
        # 对照（control）：主服的两个频道连通，没开记忆共享、没开人设卡：记录里没有只该主服看的。它要翻记录、查人设卡（查的
        # 那一步出错了），又写错了一次翻记录（没认出来）。别的服的频道连进来：什么都没拿掉，它的回话原样不动
        lookups = ACTIONS({"type": "recall", "channel": "游戏"}, {"type": "who", "name": "小雨"})
        wrong = ACTIONS({"type": "recall", "频道": "游戏"}, REPLY(None, "我再翻翻"))  # 频道写在认不得的字段里
        engine, llm, transport, clock = world(replies(lookups, LLMError("server error", 503), wrong))
        sync = connect(engine, CHANNEL, LOBBY)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 1, "@小D 游戏那边约了什么？小雨是谁")
        await say(engine, clock, 2, "@小D 再翻一次游戏那边")
        whole = record(engine, sync)
        self.assertIn(TOOL_FAILED, whole)
        self.assertIn('"频道": "游戏"', whole)
        before = turns(engine, sync)
        self.assertEqual(join(engine, HALL, CHANNEL), 0)  # 跨服务器了，可是没什么要拿的：bot 主人不会看到“有 N 条拿掉了”
        self.assertEqual(turns(engine, sync), before)  # 要翻、要查的动作原样留着（记录不改，缓存不失效）


class CardsInItsReplies(unittest.IsolatedAsyncioTestCase):
    async def test_a_card_action_that_changes_nothing_is_not_kept_in_a_cross_server_link(self):
        # 要写的就是他现在的卡；他没有卡，要写的是空的：都不算没写成（以前就留在了记录里，下次一查又拿掉）
        for card, text, words in (("吸血鬼领主", "吸血鬼领主", "我的人设卡就写：吸血鬼领主"), (None, "", "我的人设卡空着就好")):
            with self.subTest(card=card):
                cards, memory = stores()
                write = ACTIONS({"type": "card", "to": 1, "text": text}, REPLY(1, "好"))
                engine, llm, transport, clock = world(replies(write), cards=cards, memory=memory)
                if card is not None:
                    cards.put(GUILD, 11, "阿明", card, via="command")
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：一开始就跨服务器，人设卡各服务器一张
                await say(engine, clock, 1, f"@小D {words}")
                self.assertEqual(kinds_in_replies(engine, sync), ["reply"])  # 写卡的动作当时就不留在记录里，它说的话照旧
                before = turns(engine, sync)
                self.assertEqual(join(engine, LOBBY, CHANNEL), 0)  # 主服 #大厅 也连进来：bot 主人不会看到“有 1 条拿掉了”
                self.assertEqual(turns(engine, sync), before)  # 记录原样（缓存不失效）


class ItemsItWroteWrong(unittest.IsolatedAsyncioTestCase):
    async def test_a_card_item_the_parser_did_not_accept_leaves_its_reply_too(self):
        cards, memory = stores()
        wrong = ACTIONS({"type": "card", "to": "阿明", "text": f"{CARD}，喜欢吃辣"}, REPLY(1, "记下了"))  # to 写成了名字：没认出来
        engine, llm, transport, clock = world(replies(wrong), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", CARD, via="command")
        sync = connect(engine, CHANNEL, LOBBY)  # 先是主服的两个频道连通：照常附卡
        await say(engine, clock, 1, "@小D 记住我喜欢吃辣")
        self.assertEqual(cards.get(GUILD, 11).text, CARD)  # 对照：没认出来，卡没写成
        self.assertIn('"to": "阿明"', record(engine, sync))  # 对照：它的原话记在回话里
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅 连进来：跨服务器了，人设卡各服务器一张
        whole = record(engine, sync)
        self.assertNotIn("住在幸福路三号", whole)  # 附着的卡拿掉了，原话里写着的那张整卡也拿掉了
        self.assertIn("记下了", whole)  # 它说的话照旧

    async def test_a_recall_item_the_parser_did_not_accept_leaves_its_reply_too(self):
        wrong = ACTIONS({"type": "recall", "频道": "管理组", "about": "封禁名单"}, REPLY(None, "我翻翻看"))  # 频道写在认不得的字段里
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(), wrong))
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        await say(engine, clock, 1, "@小D 管理组开会", channel=ADMINS)
        await say(engine, clock, 2, "@小D 游戏频道开黑", channel=GAMES)
        sync = connect(engine, CHANNEL, LOBBY)  # 主服的两个频道连通：能翻的频道名单照常写进共用的记录
        await say(engine, clock, 3, "@小D 帮我翻一下那个私密频道的封禁名单")
        self.assertIn('"频道": "管理组"', record(engine, sync))  # 对照：没认出来（没去翻），原话记在它的回话里
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅 连进来：跨服务器了
        whole = record(engine, sync)
        self.assertNotIn("管理组", whole)  # 名单拿掉了，原话里写着要翻 #管理组 的那条也拿掉了
        self.assertIn("我翻翻看", whole)  # 它说的话照旧


class OneWrite(unittest.IsolatedAsyncioTestCase):
    async def test_a_scrub_that_fails_partway_changes_nothing_and_the_next_one_removes_everything(self):
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS({"type": "recall", "channel": "管理组"})))
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        await say(engine, clock, 1, "@小D 下周封禁老王", channel=ADMINS)
        sync = connect(engine, CHANNEL, LOBBY)
        await say(engine, clock, 2, "@小D 那边开会说了什么")  # 它翻了 #管理组（翻到了）
        self.assertIn("下周封禁老王", record(engine, sync))  # 对照：同一个服务器的连通照常翻
        request = turn_with(engine, sync, '"channel": "管理组"', role="assistant")  # 它要翻 #管理组 的那条回话
        link(engine, HALL, CHANNEL)  # 别的服 #大厅 连进来了，还没查
        before = turns(engine, sync)
        with writes_fail(engine, f"OLD.id = {request}"), self.assertRaises(sqlite3.Error):
            engine.scrub_cross(sync)  # 查的时候改写那条回话出错了
        self.assertEqual(turns(engine, sync), before)  # 一条都没改：不会只拿掉了翻到的，留下要翻 #管理组 的动作
        engine.scrub_cross(sync)  # 下次再查（下次启动）
        self.assertNotIn("管理组", record(engine, sync))  # 翻到的、能翻的频道名单、要翻 #管理组 的动作都拿掉了


class WhenAStepFails(unittest.IsolatedAsyncioTestCase):
    def failing(self, engine, where, step):
        """改写 where 那几条记录时数据库出错，做 step()。返回 (step 的结果, 传出来的错)：出错的那一步该记一条 ERROR，
        错不该传出来（退组、连通的收尾照常做完）。"""
        with writes_fail(engine, where):
            try:
                with self.assertLogs("dsbot.engine", "ERROR"):
                    return step(), None
            except sqlite3.Error as exc:
                return None, exc

    async def test_a_group_whose_scrub_fails_does_not_stop_the_other_groups(self):
        engine, llm, transport, clock = world()
        elsewhere(engine, transport)  # 主服开了记忆共享
        transport.places[YARD] = ChannelPlace(guild_id=OTHER, name="后院", public=True)
        transport.names[YARD] = "后院"
        same_group(engine, GUILD, OTHER)
        await say(engine, clock, 1, "@小D 周六在游戏频道约副本", channel=GAMES)
        first = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        second = connect(engine, LOBBY, YARD)  # 主服 #大厅 + 别的服 #后院：同一个服务器组，都带近况
        await say(engine, clock, 2, "@小D 游戏那边约了什么")
        await say(engine, clock, 3, "@小D 游戏那边约了什么", channel=LOBBY)
        self.assertIn("周六在游戏频道约副本", record(engine, second))  # 对照
        engine.store.unmap(OTHER)  # 别的服退出了服务器组：两个连通组都跨服务器了
        # 先查的那个组改写记录时数据库出错
        _, escaped = self.failing(engine, f"OLD.channel_id = {first}", lambda: engine.groups_changed([GUILD, OTHER], left=True))
        self.assertNotIn("周六在游戏频道约副本", record(engine, second))  # 别的组照样拿掉了
        self.assertIsNone(escaped)  # 退组的收尾照常做完（出错的那个组下次启动再查）

    async def linked_with_music(self):
        """主服的两个频道连通（阿明有主服的卡，人设卡各服务器一张）：第一轮附了他的卡，第二轮写了“在放什么”（没写是哪个
        服务器的）。返回 (engine, 组, 附卡的那条, 写着在放什么的那条)。"""
        cards, memory = stores()
        engine, llm, transport, clock = world(cards=cards, memory=memory)
        playing = jukebox(engine)
        cards.put(GUILD, 11, "阿明", CARD, via="command")
        sync = connect(engine, CHANNEL, LOBBY)
        await say(engine, clock, 1, "@小D 你好")
        playing[GUILD] = PLAYING
        await say(engine, clock, 2, "@小D 在放什么")
        attached, music = turn_with(engine, sync, CARD), turn_with(engine, sync, MUSIC_NOW.format(text=PLAYING))
        self.assertNotEqual(attached, music)  # 对照：卡和“在放什么”在两条里
        return engine, sync, attached, music

    async def test_a_music_relabel_that_fails_does_not_skip_the_scrub(self):
        engine, sync, attached, music = await self.linked_with_music()
        # 别的服 #大厅 连进来；补写“在放什么”是哪个服务器的时候数据库出错
        scrubbed, escaped = self.failing(engine, f"OLD.id = {music}", lambda: join(engine, HALL, CHANNEL))
        self.assertNotIn("住在幸福路三号", record(engine, sync))  # 只该主服看的照样先拿掉了
        self.assertIsNone(escaped)  # 连通的收尾照常做完
        self.assertEqual(scrubbed, 1)  # bot 主人照样看到拿掉了几条

    async def test_a_scrub_that_fails_does_not_stop_the_rest_of_the_link_change(self):
        engine, sync, attached, music = await self.linked_with_music()
        # 别的服 #大厅 连进来；拿掉附着的卡的时候数据库出错（下次启动再查）
        _, escaped = self.failing(engine, f"OLD.id = {attached}", lambda: join(engine, HALL, CHANNEL))
        self.assertIn(MUSIC_NOW_AT.format(server="主服", text=PLAYING), record(engine, sync))  # “在放什么”照样写明是主服的
        self.assertIsNone(escaped)  # 连通的收尾照常做完（通知、给 bot 主人的结果）


class MusicLines(unittest.IsolatedAsyncioTestCase):
    async def test_a_labelled_music_line_is_recognised_when_the_server_name_has_a_closing_bracket(self):
        engine, llm, transport, clock = world()
        transport.guild_names[GUILD] = "「夜猫子」俱乐部"  # 服务器名里有“」”
        playing = jukebox(engine)
        sync = connect(engine, CHANNEL, LOBBY)
        playing[GUILD] = PLAYING
        await say(engine, clock, 1, "@小D 在放什么")
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅 连进来：那句改成写明是「夜猫子」俱乐部的
        line = MUSIC_NOW_AT.format(server="「夜猫子」俱乐部", text=PLAYING)
        self.assertIn(line, record(engine, sync))  # 对照
        await say(engine, clock, 2, "@小D 还在放吗")  # 还是那首
        self.assertNotIn(line, last_user(llm))  # 照改过的认：没变，不再说一遍


if __name__ == "__main__":
    unittest.main()
