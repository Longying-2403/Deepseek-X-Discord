"""2.8.0 连通的频道，第三轮评审修掉的隐私问题。

- 组里的频道原来都在同一个服务器，后来跨服务器了，人设卡又是各服务器一张：它在回话里写过的卡（card 动作里是整张卡）也从
  共用的记录里拿掉，它说的话照旧；人设卡所有服务器共用的话，写过的卡照旧留着。
- 翻到的记录、查到的人设卡拿掉了的那一步，前面那条回话里的“翻记录”“查人设卡”动作也拿掉（里面写着频道名、查的是谁）。
- 想翻的频道找不到、同名的有好几个时，那句话后面列着的频道和能翻的频道名单一样拿掉。
- 跨服务器的连通自己会写的（“这里没开人设卡”、不翻、不查的说明）不动：以后再查一遍（每次连通、断开，每次启动）不改记录。
- 服务器退出了服务器组：组里正在跑的那一轮（问模型之前按原来的组带上了近况）写进记录、放开锁以后再查一遍。
- 连通的频道开始跨好几个服务器了（别的服务器的频道连进来；跨服务器的、连成了服务器组的都算）：之前写进共用记录、没写是哪个
  服务器的“语音频道在放什么”写明是那个频道的服务器；写那句的频道照改过的认，没变就不再说一遍。"""

import unittest

from helpers import CHANNEL, GUILD
from test_chatsync import HALL, LOBBY, connect, replies, say, text_of, world
from test_chatsync_privacy import ADMINS, GAMES, PLAYING, elsewhere, history_of, jukebox, last_user, record
from test_chatsync_r2_privacy import same_group
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review, stores

from dsbot.models import ChannelPlace
from dsbot.prompts import (
    MUSIC_IDLE,
    MUSIC_IDLE_AT,
    MUSIC_NOW,
    MUSIC_NOW_AT,
    RECALL_SYNC_OFF,
    WHO_OFF,
    WHO_SYNC_OFF,
)

GUIDES = (110, 111)  # 主服 #游戏 里两个同名的帖子「攻略」
# 阿明要它记住他喜欢吃辣：它写的是整张新卡（#1：在 #闲聊 叫它的那条）
WRITE = ACTIONS({"type": "card", "to": 1, "text": "主服的阿明：吸血鬼领主，喜欢吃辣"}, REPLY(1, "记下了"))
LOOKUPS = ACTIONS({"type": "recall", "channel": "管理组"}, {"type": "who", "name": "小雨"})  # 翻 #管理组 的记录、查小雨的卡


def when_asked(table):
    """叫它的这一轮话里有 table 的键：照那个回（要翻记录、查人设卡……）；别的照 replies() 回（查完那一步不说话）。"""
    others = replies()

    def responder(call):
        if call["json_mode"] and not is_review(call):
            last = text_of(call["messages"][-1]["content"])
            for words, out in table.items():
                if words in last:
                    return out
        return others(call)

    return responder


def turns(engine, sync):
    """共用记录里的每一条（id 和内容，按先后）：看有没有被改写。"""
    return [(t.id, t.content) for t in engine.store.load_conversation(sync, 0).turns]


class ItsOwnReplies(unittest.IsolatedAsyncioTestCase):
    async def test_cards_it_wrote_leave_its_replies_when_another_servers_channel_joins(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(replies(WRITE), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", "主服的阿明：吸血鬼领主", via="command")
        sync = connect(engine, CHANNEL, LOBBY)  # 先是主服的两个频道连通：照常附卡、写卡
        await say(engine, clock, 1, "@小D 记住我喜欢吃辣")
        self.assertEqual(cards.get(GUILD, 11).text, "主服的阿明：吸血鬼领主，喜欢吃辣")  # 对照：写成了
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅 连进来：跨服务器了，人设卡各服务器一张
        whole = record(engine, sync)
        self.assertNotIn("吸血鬼领主", whole)  # 附着的卡拿掉了，它回话里写的那张整卡也拿掉
        self.assertIn("记下了", whole)  # 它说的话照旧
        await say(engine, clock, 2, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
        self.assertNotIn("吸血鬼领主", history_of(llm))  # 别的服 #大厅 被叫到时看不到主服的卡

    async def test_lookups_in_its_reply_leave_with_their_results(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(replies(ACTIONS(), LOOKUPS), cards=cards, memory=memory)
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        cards.put(GUILD, 12, "小雨", "小雨住在幸福路", via="command")
        await say(engine, clock, 1, "@小D 下周封禁老王", channel=ADMINS)
        sync = connect(engine, CHANNEL, LOBBY)
        await say(engine, clock, 2, "@小D 那边开会说了什么？那个人是谁")  # 它翻了 #管理组、查了小雨的卡
        found = last_user(llm)
        self.assertIn("下周封禁老王", found)  # 对照：同一个服务器的连通照常翻、照常查
        self.assertIn("小雨住在幸福路", found)
        connect(engine, HALL, CHANNEL)  # 跨服务器了：翻到的、查到的拿掉
        whole = record(engine, sync)
        self.assertNotIn("管理组", whole)  # 前面那条回话里要翻 #管理组 的动作也拿掉（私密频道的名字）
        self.assertNotIn("小雨", whole)  # 查的是谁也一样
        await say(engine, clock, 3, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
        seen = history_of(llm)
        self.assertNotIn("管理组", seen)
        self.assertNotIn("小雨", seen)

    async def test_control_with_shared_cards_the_cards_it_wrote_stay(self):
        # 对照（control）：人设卡所有服务器共用的话，别的服务器本来就用这张卡：它写过的卡照旧留在共用的记录里
        cards, memory = stores()
        engine, llm, transport, clock = world(replies(WRITE), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", "主服的阿明：吸血鬼领主", via="command")
        cards.set_shared(True, scope_of=engine.store.memory_scope, alive=set())
        sync = connect(engine, CHANNEL, LOBBY)
        await say(engine, clock, 1, "@小D 记住我喜欢吃辣")
        connect(engine, HALL, CHANNEL)
        self.assertIn("主服的阿明：吸血鬼领主，喜欢吃辣", record(engine, sync))  # 新写的那张只在它的回话里


class ScrubbingAgain(unittest.IsolatedAsyncioTestCase):
    async def test_scrubbing_again_later_leaves_the_record_as_it_is(self):
        cards, memory = stores()
        lookups = ACTIONS({"type": "recall", "channel": "游戏"}, {"type": "who", "name": "小雨"})
        engine, llm, transport, clock = world(when_asked({"记住": WRITE, "游戏那边": lookups}), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", "主服的阿明：吸血鬼领主", via="command")
        engine.settings.set("guild", OTHER, "cards", False)  # 别的服没开人设卡
        sync = connect(engine, CHANNEL, LOBBY)
        await say(engine, clock, 1, "@小D 记住我喜欢吃辣")
        connect(engine, HALL, CHANNEL)  # 跨服务器了：第一次查，拿掉了主服的卡
        await say(engine, clock, 2, "@小D 游戏那边约了什么？小雨是谁", channel=HALL, author=21, name="小红")
        await say(engine, clock, 3, "@小D 游戏那边呢？小雨是谁")
        whole = record(engine, sync)
        for line in (RECALL_SYNC_OFF.format(channel="游戏"), WHO_OFF, WHO_SYNC_OFF):
            self.assertIn(line, whole)  # 对照：跨服务器以后它自己写的（不翻、这里没开人设卡、不查）
        before = turns(engine, sync)
        self.assertEqual(engine.scrub_cross(sync), 0)  # 以后再查一遍（每次启动、下次连通断开）：什么都不改
        self.assertEqual(turns(engine, sync), before)  # 记录原样（不会每次重启都改一遍，缓存也不失效）


class RecallLists(unittest.IsolatedAsyncioTestCase):
    async def test_lines_listing_channels_to_recall_leave_the_record_when_another_server_joins(self):
        recalls = {
            "帮我翻一下那个频道": ACTIONS({"type": "recall", "channel": "不存在的频道"}),
            "再帮我翻一个": ACTIONS({"type": "recall", "channel": "攻略"}),
        }
        engine, llm, transport, clock = world(when_asked(recalls))
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        for guide in GUIDES:
            transport.places[guide] = ChannelPlace(guild_id=GUILD, name="攻略", parent_id=GAMES, public=True)
        await say(engine, clock, 1, "@小D 管理组开会", channel=ADMINS)
        await say(engine, clock, 2, "@小D 游戏频道开黑", channel=GAMES)
        sync = connect(engine, CHANNEL, LOBBY)
        await say(engine, clock, 3, "@小D 帮我翻一下那个频道")  # 找不到：后面列着能翻的频道
        self.assertIn("管理组", last_user(llm))  # 对照：列出来的有单独开了共享的私密频道
        for i, guide in enumerate(GUIDES):
            await say(engine, clock, 4 + i, "@小D 这个帖子写了什么", channel=guide)
        await say(engine, clock, 6, "@小D 再帮我翻一个")  # 同名的有好几个：后面列着是哪几个
        self.assertIn("帖子「攻略」", last_user(llm))  # 对照
        connect(engine, HALL, CHANNEL)  # 跨服务器了
        await say(engine, clock, 7, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
        seen = history_of(llm)
        self.assertNotIn("管理组", seen)  # 找不到的那句列着的频道拿掉了
        self.assertNotIn("帖子「攻略」", seen)  # 好几个同名的那句列着的也拿掉了


class GroupLeftDuringARound(unittest.IsolatedAsyncioTestCase):
    async def test_a_round_in_flight_when_a_server_leaves_the_group_is_scrubbed_once_written(self):
        engine, llm, transport, clock = world()
        elsewhere(engine, transport)
        same_group(engine, GUILD, OTHER)
        await say(engine, clock, 1, "@小D 周六在游戏频道约副本", channel=GAMES)
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：同一个服务器组，不算跨服务器（带近况）
        left = []

        async def leave_while_asking(call):
            if not left and call["json_mode"] and not is_review(call):
                left.append(True)  # #闲聊 这一轮正在等模型回话：别的服退出了服务器组（/dsowner 服务器组 的退组做完了）
                engine.store.unmap(OTHER)
                engine.groups_changed([GUILD, OTHER], left=True)

        llm.hook = leave_while_asking
        await say(engine, clock, 2, "@小D 游戏那边约了什么")
        llm.hook = None
        self.assertTrue(left)
        self.assertIn("周六在游戏频道约副本", last_user(llm))  # 对照：这一轮问模型时按原来的组带上了 #游戏 的近况
        self.assertNotIn("周六在游戏频道约副本", record(engine, sync))  # 写进记录以后又查了一遍：拿掉了
        await say(engine, clock, 3, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
        self.assertNotIn("周六在游戏频道约副本", history_of(llm))


class MusicLines(unittest.IsolatedAsyncioTestCase):
    async def test_music_lines_from_the_one_server_period_name_the_server_when_another_server_joins(self):
        for grouped in (False, True):  # 跨服务器的连通；两个服务器连成了组（不算跨服务器，点歌还是各服务器自己的）
            with self.subTest(grouped=grouped):
                engine, llm, transport, clock = world()
                playing = jukebox(engine)
                if grouped:
                    same_group(engine, GUILD, OTHER)
                sync = connect(engine, CHANNEL, LOBBY)  # 先是主服的两个频道连通：“在放什么”不写是哪个服务器的
                playing[GUILD] = PLAYING
                await say(engine, clock, 1, "@小D 在放什么")
                del playing[GUILD]
                await say(engine, clock, 2, "@小D 停了吗")
                plain = [MUSIC_NOW.format(text=PLAYING), MUSIC_IDLE]
                self.assertEqual([line for line in plain if line in record(engine, sync)], plain)  # 对照
                connect(engine, HALL, CHANNEL)  # 别的服 #大厅 连进来：开始跨两个服务器
                labelled = [MUSIC_NOW_AT.format(server="主服", text=PLAYING), MUSIC_IDLE_AT.format(server="主服")]
                self.assertEqual([line for line in labelled if line in record(engine, sync)], labelled)  # 写明是主服的
                await say(engine, clock, 3, "@小D 你们那边在放歌吗", channel=HALL, author=21, name="小红")  # 别的服没在放
                seen = history_of(llm)
                self.assertEqual([line for line in plain if line in seen], [])  # 不会当成别的服自己的语音频道

    async def test_the_channel_that_wrote_the_line_does_not_say_it_again_once_it_is_labelled(self):
        engine, llm, transport, clock = world()
        playing = jukebox(engine)
        connect(engine, CHANNEL, LOBBY)
        playing[GUILD] = PLAYING
        await say(engine, clock, 1, "@小D 在放什么")
        connect(engine, HALL, CHANNEL)  # 那句改成写明是主服的
        await say(engine, clock, 2, "@小D 还在放吗")  # 还是那首
        self.assertNotIn(MUSIC_NOW_AT.format(server="主服", text=PLAYING), last_user(llm))  # 照改过的认：没变，不再说一遍


if __name__ == "__main__":
    unittest.main()
