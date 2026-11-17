"""2.8.0 连通的频道，第二轮评审修掉的隐私问题。

- 组里的频道原来都在同一个服务器（或同一个服务器组），后来跨服务器了（别的服务器的频道连进来、组里的服务器退出了服务器组）：
  共用记录里只有原来那边能看的（附着的人设卡、别的频道带来的近况、翻到的记录）马上拿掉，别的服务器的频道看不到。
- 连成一组的几个服务器的频道连通（不算跨服务器）：别的服务器的近况，组里这几个服务器都开了“跨服务器”才带进共用的记录；
  正在放的歌写明是哪个服务器的（点歌还是各服务器自己的）。
- 和别的服务器的频道连通、人设卡各服务器一张：这里不附卡、也不写卡，提示词照实说；@ 的名单里没有这里有卡的人
  （记录里写的名字多半是那边的人）。
- 连通的频道变成私密的了：发在别的频道的说明、给别的频道的模型看的，都还是连的时候的名字（现在叫什么，别人不该知道）。
- 连通之前排着、还没附上的“改成了什么”：连通时直接改进它自己那份（只给它看的“连通之前”的概要也照改过的写）。"""

import json
import unittest

import test_chatsync
from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, HALL_LABEL, LOBBY, chats, connect, replies, say, text_of, world
from test_chatsync_privacy import ADMINS, ARENA, GAMES, PLAYING, elsewhere, history_of, jukebox, record
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, stores

from dsbot import prompts
from dsbot.models import ChannelPlace
from dsbot.prompts import MUSIC_NOW, MUSIC_NOW_AT

RECALL_GAMES = json.dumps({"actions": [{"type": "recall", "channel": "游戏"}]}, ensure_ascii=False)


def same_group(engine, *guilds):
    """这几个服务器连成一个服务器组（只登记组：用不着搬人设卡、长期记忆）。"""
    group = engine.store.new_group_id()
    for guild in guilds:
        engine.store.join_group(guild, group, now=1.0)


class BecomingCrossServer(unittest.IsolatedAsyncioTestCase):
    async def test_same_server_only_content_leaves_the_record_when_another_servers_channel_joins(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            replies(ACTIONS(), ACTIONS(), RECALL_GAMES, ACTIONS(REPLY(None, "翻到了"))), cards=cards, memory=memory
        )
        elsewhere(engine, transport)  # 主服开了记忆共享（#管理组 是单独开了共享的私密频道）
        cards.put(GUILD, 11, "阿明", "主服的阿明：住在幸福路三号", via="command")
        await say(engine, clock, 1, "@小D 管理组决定下周封禁老王", channel=ADMINS)
        await say(engine, clock, 2, "@小D 周六在游戏频道约副本", channel=GAMES)
        sync = connect(engine, CHANNEL, LOBBY)  # 先是主服的两个频道连通
        await say(engine, clock, 3, "@小D 游戏那边约了什么")  # 附了阿明的卡、带了 #管理组 和 #游戏 的近况、翻了 #游戏
        local = ["住在幸福路三号", "下周封禁老王", "周六在游戏频道约副本"]
        self.assertEqual([t for t in local if t in record(engine, sync)], local)  # 对照：同一个服务器的连通照旧，不拿掉
        connect(engine, HALL, CHANNEL)  # 别的服的 #大厅 连进来：跨服务器了
        whole = record(engine, sync)
        self.assertEqual([t for t in local if t in whole], [])  # 只有主服能看的从共用的记录里拿掉了
        self.assertIn("游戏那边约了什么", whole)  # 群友说的照旧
        await say(engine, clock, 4, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
        self.assertEqual([t for t in local if t in history_of(llm)], [])  # 别的服 #大厅 被叫到时看不到

    async def test_group_only_content_leaves_the_record_when_a_server_leaves_the_group(self):
        engine, llm, transport, clock = world(replies(ACTIONS(), RECALL_GAMES, ACTIONS(REPLY(None, "翻到了"))))
        elsewhere(engine, transport)
        same_group(engine, GUILD, OTHER)
        await say(engine, clock, 1, "@小D 周六在游戏频道约副本", channel=GAMES)
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：同一个服务器组，不算跨服务器
        await say(engine, clock, 2, "@小D 游戏那边约了什么")  # 带了 #游戏 的近况、翻了 #游戏
        self.assertIn("周六在游戏频道约副本", record(engine, sync))  # 对照：同一组照旧
        engine.store.unmap(OTHER)  # 别的服退出了服务器组（/dsowner 服务器组 的退组做完了）
        engine.groups_changed([GUILD, OTHER], left=True)
        self.assertNotIn("周六在游戏频道约副本", record(engine, sync))  # 现在跨服务器了：拿掉
        await say(engine, clock, 3, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
        self.assertNotIn("周六在游戏频道约副本", history_of(llm))


class GroupedServers(unittest.IsolatedAsyncioTestCase):
    async def test_other_servers_recents_come_in_only_if_every_linked_server_has_cross_server_on(self):
        engine, llm, transport, clock = world()
        elsewhere(engine, transport)  # 主服、第三服开了“跨服务器”
        same_group(engine, GUILD, OTHER)  # 别的服没开
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：同一组，不算跨服务器
        await say(engine, clock, 1, "@小D 第三服周末去团建", channel=ARENA)
        await say(engine, clock, 2, "@小D 我回主服了")
        self.assertNotIn("第三服周末去团建", record(engine, sync))  # 别的服 #大厅 也读这份记录，别的服没开：不带
        engine.settings.set("guild", OTHER, "cross_server", True)
        await say(engine, clock, 3, "@小D 第三服周日也去", channel=ARENA)
        await say(engine, clock, 4, "@小D 我又回来了")
        self.assertIn("第三服周日也去", record(engine, sync))  # 对照：都开了照常带

    async def test_music_line_names_the_server_when_grouped_servers_are_linked(self):
        engine, llm, transport, clock = world()
        playing = jukebox(engine)
        same_group(engine, GUILD, OTHER)  # 点歌还是各服务器自己的
        sync = connect(engine)
        playing[OTHER] = PLAYING  # 别的服在放歌，主服没在放
        await say(engine, clock, 1, "@小D 在放什么", channel=HALL, author=21, name="小红")
        whole = record(engine, sync)
        self.assertIn(MUSIC_NOW_AT.format(server="别的服", text=PLAYING), whole)  # 写明是别的服的语音频道
        self.assertNotIn(MUSIC_NOW.format(text=PLAYING), whole)  # 主服 #闲聊 也读这份记录：不写服务器会当成自己这边在放


class CrossServerCards(unittest.IsolatedAsyncioTestCase):
    async def test_a_card_owner_here_cannot_change_the_card_and_the_prompt_says_so(self):
        cards, memory = stores()
        write = ACTIONS({"type": "card", "to": 1, "text": "称呼：阿明大人"}, REPLY(1, "好的阿明大人"))
        engine, llm, transport, clock = world(replies(write), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", "主服的阿明：吸血鬼领主", via="command")
        connect(engine)  # 主服 #闲聊 + 别的服 #大厅：跨服务器，人设卡各服务器一张
        await say(engine, clock, 1, "@小D 以后在这里叫我阿明大人")
        asked = chats(llm)[0]
        self.assertNotIn("吸血鬼领主", text_of(asked["messages"][-1]["content"]))  # 对照：他的卡不附（别的服看得到共用的记录）
        self.assertEqual(cards.get(GUILD, 11).text, "主服的阿明：吸血鬼领主")  # 对照：看不到他现在的卡，也不照着改
        note = getattr(prompts, "SYNC_FOREIGN_CARDS", None)  # 第二轮加的那句
        self.assertIsNotNone(note, "提示词里没有“这里不附卡、也改不了”这句")
        self.assertIn(note.strip("\n- "), asked["messages"][0]["content"])  # 照实告诉它（它就不会答应“记下了”）

    async def test_a_card_owner_here_is_not_pinged_for_the_other_servers_namesake(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(
            replies(ACTIONS(), ACTIONS(REPLY(None, "@小红 刚才说她要搬家"))), cards=cards, memory=memory
        )
        cards.put(GUILD, 555, "小红", "主服的小红：喜欢猫", via="command")  # 主服里有卡的小红（最近没在 #闲聊 说话）
        connect(engine)  # 跨服务器，人设卡各服务器一张
        await say(engine, clock, 1, "@小D 我要搬家了", channel=HALL, author=21, name="小红")  # 别的服的小红
        await say(engine, clock, 2, "@小D 那边说什么了")
        self.assertEqual(transport.sent[-1][:2], (CHANNEL, "@小红 刚才说她要搬家"))  # 原样发出去：不链到主服有卡的小红
        self.assertEqual(transport.mentions[-1], ())  # 也不提醒她


class PausedChannelName(unittest.IsolatedAsyncioTestCase):
    """别的服把连通的 #大厅 改成了私密频道、改名“内部-裁员名单”（连通暂停）。借 test_chatsync.OwnerCommand 的假 Discord。"""

    setUp = test_chatsync.OwnerCommand.setUp
    interaction = test_chatsync.OwnerCommand.interaction

    def turn_private(self):
        self.transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="内部-裁员名单", public=False)
        self.transport.names[HALL] = "内部-裁员名单"
        self.bot.get_channel(HALL).name = "内部-裁员名单"

    async def test_unlink_notice_uses_the_name_a_now_private_channel_had_when_linked(self):
        from dsbot.commands import SYNC_NOTICE_LEFT, sync_command

        connect(self.engine)
        self.turn_private()
        ask = self.interaction()
        await sync_command(self.bot, ask, "unlink", str(HALL), None)
        await ask.response.sent[0][1].confirm(self.interaction())
        notices = {c: t for c, t, _, _ in self.transport.sent}
        # 发在主服公开的 #闲聊 里：别的服的私密频道现在叫什么，这边的人不该知道
        self.assertEqual(notices[CHANNEL], SYNC_NOTICE_LEFT.format(others="「别的服」#大厅", rest=""))

    async def test_prompt_uses_the_name_a_now_private_channel_had_when_linked(self):
        connect(self.engine)
        self.turn_private()
        await say(self.engine, self.clock, 1, "@小D 那边怎么没动静了")
        system = chats(self.llm)[-1]["messages"][0]["content"]
        self.assertNotIn("内部-裁员名单", system)
        self.assertIn(HALL_LABEL, system)  # 主服的模型看到的还是连的时候的名字


class EditNoteAtLink(unittest.IsolatedAsyncioTestCase):
    async def test_an_edit_note_pending_at_link_time_is_written_into_the_channels_own_record(self):
        engine, llm, transport, clock = world()
        await say(engine, clock, 1, "@小D 我的手机号是 13800001111")
        for i in range(5):  # #1 成了很早的消息：被改了不改旧记录，等下一轮附一句
            await say(engine, clock, 10 + i, f"@小D 第{i}句")
        edited = msg(1, "@小D 我的手机号不告诉你了", mentions_bot=True)
        engine.handle_edit(edited, edited=True)
        await engine.drain()
        connect(engine)  # 下一轮之前连通了：之后跑的是共用的记录，它自己那份不再往后写
        own = engine.store.load_conversation(CHANNEL, GUILD)
        lines = "\n".join(text_of(t.content) for t in own.turns if t.role == "user")
        self.assertNotIn("13800001111", lines)  # 直接改进它自己那份
        self.assertIn("我的手机号不告诉你了", lines)
        await say(engine, clock, 20, "@小D 连通以后")
        system = chats(llm)[-1]["messages"][0]["content"]
        self.assertIn("## 这个频道连通之前", system)
        self.assertNotIn("13800001111", system)  # 只给 #闲聊 看的“连通之前”的概要也照改过的写


if __name__ == "__main__":
    unittest.main()
