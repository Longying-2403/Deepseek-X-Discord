"""2.8.0 连通的频道：哪些东西会跨服务器。

和别的服务器的频道连通时（连成一组的服务器算一个服务器），共用的记录里不放只有这里能看的东西：别的频道的近况、能翻的
频道名单、翻到的记录、按名字查到的人设卡；也不按名字到这里的成员里找人。同一个服务器里的频道连通，这些照旧。私信里聊的，
连通的频道都不带。记录里别的服务器的频道写成“#大厅（「别的服」服务器）”，模型照着写（或者写成“「别的服」#大厅”）也不链到
这里同名的频道。连通的频道变成私密的了就暂停。很早的消息改了，附在它所在的那个频道的下一轮；正在放的歌写明是哪个服务器的；
跨服务器以后，原来只有这个服务器能看的（附着的人设卡……）从共用的记录里拿掉；人设卡各服务器分开时，这里不写卡（告诉它请人用 /ds-rp）。"""

import json
import unittest
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, chats, connect, replies, say, text_of, world
from test_dm import DM, OTHER, dm
from test_memory import ACTIONS, REPLY, stores

from dsbot.models import ChannelPlace
from dsbot.prompts import (
    EDIT_NOTE,
    MUSIC_IDLE,
    MUSIC_IDLE_AT,
    MUSIC_NOW,
    MUSIC_NOW_AT,
    RECALL_SYNC_OFF,
    SYNC_FOREIGN,
    SYNC_FOREIGN_CARDS,
    WHO_SYNC_OFF,
)

GAMES = 102  # 主服的公开频道 #游戏
ADMINS = 105  # 主服的私密频道 #管理组（在这个频道上单独开了记忆共享）
THIRD = 3  # 第三个服务器
ARENA = 300  # 第三服的公开频道 #广场
PLAYING = "语音频道「音乐厅」正在放：晴天"
SHARE_ON = "- 别的频道：开了记忆共享，能翻记录的频道写在聊天记录里"
SHARE_OFF = "- 别的频道：这里没开记忆共享，看不到别的频道"


def elsewhere(engine, transport):
    """阿明还会在这些地方跟它聊：主服的 #游戏、#管理组（主服开了记忆共享），第三服的 #广场（主服和第三服都开了跨服务器），私信。"""
    for cid, guild, name, public in ((GAMES, GUILD, "游戏", True), (ADMINS, GUILD, "管理组", False), (ARENA, THIRD, "广场", True)):
        transport.places[cid] = ChannelPlace(guild_id=guild, name=name, public=public)
        transport.names[cid] = name
    transport.guild_names[THIRD] = "第三服"
    transport.places[DM] = ChannelPlace(guild_id=GUILD, name="私信", dm=True)
    transport.dm_with[DM] = "阿明"
    engine.settings.set("guild", GUILD, "share_memory", True)
    engine.settings.set("channel", ADMINS, "share_memory", True)
    engine.settings.set("guild", GUILD, "cross_server", True)
    engine.settings.set("guild", THIRD, "cross_server", True)


async def in_dm(engine, clock, mid, text):
    await engine.handle_message(dm(mid, text))
    await engine.drain()
    clock.advance(60)


def last_user(llm):
    """最近一次聊天请求里这一轮的内容（不看 system prompt：那里有举例的聊天记录）。"""
    return text_of(chats(llm)[-1]["messages"][-1]["content"])


def history_of(llm):
    """最近一次聊天请求里，system prompt 之外的全部内容。"""
    return "\n".join(text_of(m["content"]) for m in chats(llm)[-1]["messages"][1:])


def record(engine, sync, origin=None):
    """共用的记录（origin：只看这个频道的那几条）。"""
    turns = engine.store.load_conversation(sync, 0).turns
    return "\n".join(text_of(t.content) for t in turns if origin is None or t.origin == origin)


def jukebox(engine):
    """假的点歌：status 照 playing 里写的（服务器 -> 在放什么）。"""
    playing = {}

    async def tick():
        return None

    engine.music = NS(status=playing.get, tick=tick)
    return playing


class DmTalk(unittest.IsolatedAsyncioTestCase):
    async def test_dm_talk_never_goes_into_a_linked_channels_round(self):
        for other in (HALL, LOBBY):  # 和别的服务器的频道连通、和同一个服务器的频道连通
            with self.subTest(linked_with=other):
                engine, llm, transport, clock = world()
                elsewhere(engine, transport)
                await in_dm(engine, clock, 1, "我下周要去医院复查")
                await say(engine, clock, 2, "@小D 在吗", channel=GAMES)
                self.assertIn("在私信里跟你聊过", last_user(llm))  # 没连通的频道照常带上
                sync = connect(engine, CHANNEL, other)
                await say(engine, clock, 3, "@小D 我来这边了")
                self.assertNotIn("在私信里跟你聊过", last_user(llm))  # 连通的频道：私信里聊的不进共用的记录
                self.assertNotIn("医院复查", record(engine, sync))


class CrossServerLink(unittest.IsolatedAsyncioTestCase):
    async def test_nothing_from_elsewhere_goes_into_the_shared_record(self):
        engine, llm, transport, clock = world()
        elsewhere(engine, transport)
        sync = connect(engine)
        # 阿明每在别处跟它聊一回，就回到连通的 #闲聊 叫它一次（近况只带最近的：一处一处地看）
        talks = [(GAMES, "@小D 游戏频道的事"), (ADMINS, "@小D 管理组的事"), (ARENA, "@小D 第三服的事"), (DM, "私信里的事")]
        for i, (where, text) in enumerate(talks):
            if where == DM:
                await in_dm(engine, clock, 10 + i, text)
            else:
                await say(engine, clock, 10 + i, text, channel=where)
            await say(engine, clock, 20 + i, "@小D 我回来了")
            here = last_user(llm)
            self.assertNotIn("跟你聊过", here, where)  # 同服务器的频道、单独开了共享的私密频道、跨服务器、私信：都不带
            self.assertNotIn("（能翻记录的别的频道：", here)  # 主服开了记忆共享，名单也不写
        whole = record(engine, sync)
        for _, text in talks:
            self.assertNotIn(text.removeprefix("@小D "), whole)
        system = chats(llm)[-1]["messages"][0]["content"]
        self.assertIn(SYNC_FOREIGN, system)
        self.assertIn(SHARE_OFF, system)  # 主服开了记忆共享，这里也说翻不了
        self.assertNotIn(SHARE_ON, system)

    async def test_recall_and_who_are_refused(self):
        cards, memory = stores()
        tools = json.dumps({"actions": [{"type": "recall", "channel": "游戏"}, {"type": "who", "name": "小雨"}]}, ensure_ascii=False)
        engine, llm, transport, clock = world(replies(ACTIONS(), tools, ACTIONS(REPLY(None, "这里翻不了"))), cards=cards, memory=memory)
        elsewhere(engine, transport)
        cards.put(GUILD, 12, "小雨", "小雨住在幸福路", via="command")
        await say(engine, clock, 1, "@小D 周六一起打副本", channel=GAMES)
        sync = connect(engine)
        await say(engine, clock, 2, "@小D 游戏那边约了什么？小雨是谁")
        result = last_user(llm)
        self.assertIn(RECALL_SYNC_OFF.format(channel="游戏"), result)
        self.assertIn(WHO_SYNC_OFF, result)
        whole = record(engine, sync)
        self.assertIn(RECALL_SYNC_OFF.format(channel="游戏"), whole)  # 记进共用记录的只是“这里不翻”
        self.assertNotIn("周六一起打副本", whole)
        self.assertNotIn("小雨住在幸福路", whole)

    async def test_members_here_are_not_looked_up_by_name(self):
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(REPLY(None, "@小红 刚才说她要搬家"))))
        transport.members = {"小红": {555}}  # 主服里正好也有个叫“小红”的人（不是别的服那个）
        connect(engine)
        await say(engine, clock, 1, "@小D 我要搬家了", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 那边说什么了")
        self.assertIn("@小红 刚才说她要搬家", transport.sent[-1][1])
        self.assertEqual(transport.member_searches, [])
        self.assertEqual(transport.mentions[-1], ())  # 不会 @ 到主服同名的人


class OneServerLink(unittest.IsolatedAsyncioTestCase):
    async def test_same_server_link_keeps_recall_recents_and_names(self):
        recall = json.dumps({"actions": [{"type": "recall", "channel": "游戏"}]}, ensure_ascii=False)
        engine, llm, transport, clock = world(replies(ACTIONS(), recall, ACTIONS(REPLY(None, "@小雨 游戏那边约了周六"))))
        elsewhere(engine, transport)
        transport.members = {"小雨": {556}}
        await say(engine, clock, 1, "@小D 周六一起打副本", channel=GAMES)
        sync = connect(engine, CHANNEL, LOBBY)  # 同一个服务器的两个频道
        await say(engine, clock, 2, "@小D 游戏那边约了什么")
        asked, answered = chats(llm)[-2:]
        self.assertNotIn(SYNC_FOREIGN, asked["messages"][0]["content"])
        self.assertIn(SHARE_ON, asked["messages"][0]["content"])
        first = text_of(asked["messages"][-1]["content"])
        self.assertIn("（能翻记录的别的频道：", first)
        self.assertIn("在 #游戏 跟你聊过：", first)
        found = text_of(answered["messages"][-1]["content"])
        self.assertIn("（你翻了 #游戏 的记录：）", found)
        self.assertIn("阿明：@小D 周六一起打副本", found)
        self.assertIn("（你翻了 #游戏 的记录：）", record(engine, sync))
        self.assertEqual(transport.mentions[-1], (556,))  # 照常按名字到成员里找人

    async def test_servers_in_one_group_count_as_one_server(self):
        recall = json.dumps({"actions": [{"type": "recall", "channel": "游戏"}]}, ensure_ascii=False)
        engine, llm, transport, clock = world(replies(ACTIONS(), recall, ACTIONS(REPLY(None, "翻到了"))))
        elsewhere(engine, transport)
        group = engine.store.new_group_id()
        engine.store.join_group(GUILD, group, now=1.0)
        engine.store.join_group(OTHER, group, now=1.0)  # 主服和别的服连成了一组
        await say(engine, clock, 1, "@小D 周六一起打副本", channel=GAMES)
        connect(engine)  # 主服 #闲聊 和别的服 #大厅
        await say(engine, clock, 2, "@小D 游戏那边约了什么")
        asked, answered = chats(llm)[-2:]
        self.assertNotIn(SYNC_FOREIGN, asked["messages"][0]["content"])
        self.assertIn("在 #游戏 跟你聊过：", text_of(asked["messages"][-1]["content"]))
        found = text_of(answered["messages"][-1]["content"])
        self.assertIn("（你翻了 #游戏 的记录：）", found)
        self.assertIn("阿明：@小D 周六一起打副本", found)


class ChannelNames(unittest.IsolatedAsyncioTestCase):
    async def test_other_servers_channels_are_not_linked_to_this_servers_channel(self):
        said = "#大厅（「别的服」服务器） 在聊游戏，「别的服」#大厅 也是，「必看」#大厅 记得看"
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(REPLY(None, said))))
        transport.channel_map = {"大厅": {LOBBY}}  # 主服自己也有个 #大厅（没连通）
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 我在聊游戏", channel=HALL, author=21, name="小红")
        self.assertIn(" · #大厅（「别的服」服务器）】", record(engine, sync, HALL))  # 记录里这样写那边的频道
        await say(engine, clock, 2, "@小D 那边在聊什么")
        sent = transport.sent[-1][1]
        self.assertIn("#大厅（「别的服」服务器） 在聊游戏", sent)  # 照记录的写法：不链到主服的 #大厅
        self.assertIn("「别的服」#大厅 也是", sent)  # 短的写法也一样
        self.assertIn(f"「必看」<#{LOBBY}> 记得看", sent)  # 「」里不是服务器的名字：照常链到这里的
        self.assertEqual(sent.count("<#"), 1)

    async def test_a_known_servers_short_form_is_not_linked_without_links_either(self):
        engine, llm, transport, clock = world(replies(ACTIONS(REPLY(None, "「别的服」#大厅 在聊游戏，「必看」#大厅 记得看"))))
        transport.channel_map = {"大厅": {LOBBY}}
        await say(engine, clock, 1, "@小D 别的服在聊什么")  # 没连通
        sent = transport.sent[-1][1]
        self.assertIn("「别的服」#大厅 在聊游戏", sent)  # bot 也在别的服：写的是那边的 #大厅
        self.assertIn(f"「必看」<#{LOBBY}> 记得看", sent)


class TurnedPrivate(unittest.IsolatedAsyncioTestCase):
    async def test_a_linked_channel_turned_private_is_paused(self):
        engine, llm, transport, clock = world(replies(ACTIONS(REPLY(None, "主服好")), ACTIONS(REPLY(None, "又能聊了"))))
        sync = connect(engine)
        await say(engine, clock, 1, "公开的时候随口聊", channel=HALL, author=21, name="小红", call=False)
        transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=False)  # 别的服的管理员把 #大厅 改成了私密的
        self.assertTrue(engine.status(OTHER, HALL, None)["sync_paused"])
        self.assertFalse(engine.status(GUILD, CHANNEL, None)["sync_paused"])
        await say(engine, clock, 2, "@小D 那边在聊什么")
        self.assertNotIn("公开的时候随口聊", history_of(llm))  # 那边缓冲区里的不补进共用的记录
        with self.assertLogs("dsbot.engine", "WARNING") as logs:
            await say(engine, clock, 3, "@小D 这里现在是私密频道了：内部消息", channel=HALL, author=21, name="小红")
            await say(engine, clock, 4, "内部的事", channel=HALL, author=21, name="小红", call=False)
        self.assertEqual(sum("连通暂停" in r.getMessage() for r in logs.records), 1)  # 只提醒一次
        self.assertEqual(len(chats(llm)), 1)  # 那边不跑一轮
        self.assertEqual(engine.status(OTHER, HALL, None)["pending"], 0)  # 也不攒着
        whole = record(engine, sync)
        for text in ("公开的时候随口聊", "内部消息", "内部的事"):
            self.assertNotIn(text, whole)
        # 改回公开：接着连通
        transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=True)
        self.assertFalse(engine.status(OTHER, HALL, None)["sync_paused"])
        await say(engine, clock, 5, "@小D 改回公开了", channel=HALL, author=21, name="小红")
        self.assertEqual(transport.sent[-1][:2], (HALL, "又能聊了"))
        self.assertIn("小红：@小D 改回公开了", record(engine, sync, HALL))

    async def test_a_round_not_started_yet_is_dropped_when_it_turns_private(self):
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await engine.handle_message(msg(1, "@小D 叫你", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=False)  # 那一轮还没开始就改成私密的了
        await engine.drain()
        self.assertEqual(chats(llm), [])
        self.assertEqual(engine.store.load_conversation(sync, 0).turns, [])
        self.assertEqual(engine.status(OTHER, HALL, None)["pending"], 0)


class EditNotes(unittest.IsolatedAsyncioTestCase):
    async def test_an_old_messages_edit_note_goes_into_its_own_channels_round(self):
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 我的手机号是 138", channel=HALL, author=21, name="小红")
        for i in range(5):  # 记录往后走：那条成了很早的消息（改了不改旧记录，下一轮附一句）
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")
        edited = msg(1, "@小D 我的手机号是 13800001111", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)
        engine.handle_edit(edited, edited=True)
        await engine.drain()
        await say(engine, clock, 20, "@小D 主服接着聊")
        note = EDIT_NOTE.format(sid=1, name="小红", text="@小D 我的手机号是 13800001111")
        self.assertNotIn("13800001111", last_user(llm))  # 不附在主服这一轮（不然记成了主服的）
        self.assertIn(note, history_of(llm))  # 跟着别的服那边补进来的那段附上（记成别的服的）：那边一直没有自己的一轮也看得到
        self.assertIn(note, record(engine, sync, HALL))
        self.assertNotIn("13800001111", record(engine, sync, CHANNEL))
        await say(engine, clock, 21, "@小D 那边接着聊", channel=HALL, author=21, name="小红")
        self.assertNotIn(note, last_user(llm))  # 已经附过了：不再附一次

    async def test_a_paused_channels_edit_note_waits_and_is_not_attached_to_another_channels_round(self):
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 我的手机号是 138", channel=HALL, author=21, name="小红")
        for i in range(5):  # 那条成了很早的消息
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")
        edited = msg(1, "@小D 我的手机号是 13800001111", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)
        engine.handle_edit(edited, edited=True)  # 公开的时候改的：等着附一句
        await engine.drain()
        transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=False)  # 别的服 #大厅暂停了：它的不补进来
        await say(engine, clock, 20, "@小D 主服接着聊")
        self.assertNotIn("13800001111", history_of(llm))  # 不附在主服这一轮（不然记成了主服的）
        self.assertNotIn("13800001111", record(engine, sync))
        self.assertIn(1, engine.convs[sync].edit_notes)  # 等着：那边改回公开、或者断开时再说


class MusicLine(unittest.IsolatedAsyncioTestCase):
    async def test_music_line_names_the_server_in_a_cross_server_link(self):
        engine, llm, transport, clock = world()
        playing = jukebox(engine)
        connect(engine)
        playing[GUILD] = PLAYING
        await say(engine, clock, 1, "@小D 在放什么")
        line = MUSIC_NOW_AT.format(server="主服", text=PLAYING)
        self.assertIn(line, last_user(llm))  # 写明是哪个服务器的语音频道
        engine.states.clear()
        engine.convs.clear()  # 重启：从记录里认出写过的那句（写明了服务器的也认得）
        await say(engine, clock, 2, "@小D 还在放吗")
        self.assertNotIn(line, last_user(llm))  # 没变：不再说一遍
        del playing[GUILD]
        await say(engine, clock, 3, "@小D 停了吗")
        idle = MUSIC_IDLE_AT.format(server="主服")
        self.assertIn(idle, last_user(llm))
        engine.states.clear()
        engine.convs.clear()  # 再重启：“没在放歌”那句也认得
        await say(engine, clock, 4, "@小D 还停着吗")
        self.assertNotIn(idle, last_user(llm))

    async def test_music_line_stays_plain_in_a_same_server_link(self):
        engine, llm, transport, clock = world()
        playing = jukebox(engine)
        connect(engine, CHANNEL, LOBBY)
        playing[GUILD] = PLAYING
        await say(engine, clock, 1, "@小D 在放什么")
        self.assertIn(MUSIC_NOW.format(text=PLAYING), last_user(llm))
        self.assertNotIn("「主服」的语音频道", last_user(llm))
        del playing[GUILD]
        await say(engine, clock, 2, "@小D 停了吗")
        self.assertIn(MUSIC_IDLE, last_user(llm))


class CardWrites(unittest.IsolatedAsyncioTestCase):
    async def test_cross_server_link_with_per_server_cards_neither_shows_nor_writes_cards(self):
        cards, memory = stores()
        write = ACTIONS({"type": "card", "to": 2, "text": "叫我阿明大人"}, REPLY(2, "记下了"))  # #2：在别的服叫它的那条
        engine, llm, transport, clock = world(replies(ACTIONS(), write), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", "主服的阿明：吸血鬼领主", via="command")
        connect(engine, CHANNEL, LOBBY)  # 先是主服的两个频道连通
        await say(engine, clock, 1, "@小D 我在主服")
        self.assertIn("主服的阿明：吸血鬼领主", last_user(llm))  # 同一个服务器的连通：主服的卡附进了共用的记录
        connect(engine, HALL, CHANNEL)  # 后来别的服的 #大厅 也连进来：跨服务器了
        await say(engine, clock, 2, "@小D 以后在这里叫我阿明大人", channel=HALL)
        self.assertIn("#2 阿明：@小D 以后在这里叫我阿明大人", last_user(llm))
        self.assertNotIn("吸血鬼领主", history_of(llm))  # 主服的卡从共用的记录里拿掉了（别的服看得到这份记录）
        self.assertIn(SYNC_FOREIGN_CARDS.strip(chr(10) + "- "), text_of(chats(llm)[-1]["messages"][0]["content"]))  # 告诉它这里改不了
        self.assertIsNone(cards.get(OTHER, 11))  # 看不到他现在的卡：这里不写
        self.assertEqual(cards.get(GUILD, 11).text, "主服的阿明：吸血鬼领主")


if __name__ == "__main__":
    unittest.main()
