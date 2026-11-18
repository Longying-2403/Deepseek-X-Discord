"""2.8.0 连通的频道，第六轮评审修掉的隐私问题：只有原来那边能看的东西不留在共用的记录、摘要里；它不再看的频道改过的消息不进
组里的记录；清空以前在组里的频道，不连累别的频道的摘要。

- 这里没开记忆共享，它要翻别的频道的记录（名字多半是共用记录里别的频道写的能翻的频道名单上的）：“不翻”的那句写着那个名字，
  组变成跨服务器时和名单一起拿掉，别的服务器的频道看不到。
- 服务器退出了服务器组、组里正好有一轮在问模型：退组时马上拿掉了只有原来那边能看的（能翻的频道名单在更早的一轮里）的话，那一轮
  按原来的组要翻记录、查人设卡的动作，写完以后也拿掉。退组时什么都没拿掉的话，那一轮的动作照旧（不改记录）。
- 连通的频道变成私密的（连通暂停）、在那里关掉了“允许回应”，或者以前在组里、后来变成私密的：那边改过的消息，新内容不写进组里的
  记录，那一行当成删了（还没附上的“改成了什么”、改进去的链接也不带进组里）；它自己那份照常改。删掉的照样删。
- 压缩共用的记录：想翻的频道找不到时列出来的能翻的频道不进摘要（摘要以后不会再查）。没连通的频道照旧。
- 以前在组里的频道清空记忆：它说的压进过那个组的摘要才连同摘要删；没压进去的话，别的频道的摘要照旧。还在组里的频道清空，照旧
  连同共用的摘要一起删。
- 连成一组的服务器里的频道连通（不算跨服务器）：组里的服务器都开了“跨服务器”时带进共用记录的服务器组外的近况，连进来一个没开
  “跨服务器”的服务器的频道时拿掉（组里的服务器带来的照旧）；确认框先说一声，连好以后告诉 bot 主人拿掉了几条。连进来的服务器
  也开了的话照旧。"""

import unittest
from types import SimpleNamespace as NS

import test_chatsync
from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, replies, say, text_channel, text_of, world
from test_chatsync_privacy import ADMINS, ARENA, GAMES, elsewhere, history_of, record
from test_chatsync_r2_privacy import same_group
from test_chatsync_r2_records import asking_to_read, compact
from test_chatsync_r2_review import summarized
from test_chatsync_r3_entries import links_opened
from test_chatsync_r5_scrub import its_replies
from test_chatsync_records import three_linked
from test_dm import OTHER
from test_links_images import PAGE, FakeReader, link_msg
from test_memory import ACTIONS, REPLY, is_review

from dsbot import prompts
from dsbot.models import ChannelPlace
from dsbot.prompts import MESSAGE_DELETED

# 这里没开记忆共享时“不翻”的那句，写着它想翻的频道（第六轮起是 prompts.RECALL_NO_SHARE；以前写在 engine 里，字一样）
NO_SHARE = getattr(prompts, "RECALL_NO_SHARE", "（想翻「{channel}」的记录，但这里没开记忆共享，看不了别的频道。）")
FOURTH = 4  # 第四个服务器：和主服、别的服连成一个服务器组
PLAZA = 400  # 第四服的公开频道 #广场二
SQUARE = 703  # 别的服的公开频道 #广场（和主服在一个服务器组里）
SAID = "@小D 明天开会"  # 小红在别的服 #大厅说的
SECRET = "裁员名单是小王"  # 别的服 #大厅变成私密的以后，她把那条改成了“明天开会：裁员名单是小王、小李”
HIDDEN_PAGE = "https://intra.example.com/layoffs"  # 变成私密的以后改进那条消息的链接


def edited_in_hall():
    """小红改了她在别的服 #大厅发的那条（#1）。"""
    return msg(2, f"@小D 明天开会：{SECRET}、小李", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)


def turn_private(transport):
    """别的服把 #大厅 改成了私密频道（顺便改了名字）：连通暂停，它不看那里的消息。"""
    transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="内部-裁员名单", public=False)
    transport.names[HALL] = "内部-裁员名单"


def own_record(engine, channel, guild):
    """这个频道自己那份记录。"""
    return "\n".join(text_of(t.content) for t in engine.store.load_conversation(channel, guild).turns)


def leave_group_while_asking(engine, llm):
    """叫它的这一轮第一次问模型、还没回的时候，别的服退出了服务器组（/dsowner 服务器组 的退组做完了：马上查一遍记录）。
    返回一个列表，退了以后里面有东西。"""
    left = []

    async def hook(call):
        if not left and call["json_mode"] and not is_review(call):
            left.append(True)
            engine.store.unmap(OTHER)
            engine.groups_changed([GUILD, OTHER], left=True)

    llm.hook = hook
    return left


def echoing(table):
    """聊天：叫它的这一轮话里有 table 的键就照那个回（要翻记录……），别的不说话。压缩摘要：把给它看的聊天原样写进摘要
    （摘要里会有什么一看就知道）。"""

    def responder(call):
        if is_review(call):
            return '{"cards": [], "memory": []}'
        last = text_of(call["messages"][-1]["content"])
        if not call["json_mode"]:
            return "摘要：" + last.split("【聊天记录】", 1)[-1].strip()
        return next((out for words, out in table.items() if words in last), ACTIONS())

    return responder


def four_servers(engine, transport, *, fourth_cross=False):
    """主服、别的服、第四服连成一个服务器组。主服、别的服（还有组外的第三服）开了“跨服务器”，第四服开没开看 fourth_cross。
    阿明还会在第三服 #广场（组外）、别的服 #广场（组里）跟它聊。"""
    elsewhere(engine, transport)  # 主服、第三服开了“跨服务器”
    engine.settings.set("guild", OTHER, "cross_server", True)
    if fourth_cross:
        engine.settings.set("guild", FOURTH, "cross_server", True)
    for cid, guild, name in ((PLAZA, FOURTH, "广场二"), (SQUARE, OTHER, "广场")):
        transport.places[cid] = ChannelPlace(guild_id=guild, name=name, public=True)
        transport.names[cid] = name
    transport.guild_names[FOURTH] = "第四服"
    transport.joined = [OTHER, FOURTH]
    same_group(engine, GUILD, OTHER, FOURTH)


class RecallWhereSharingIsOff(unittest.IsolatedAsyncioTestCase):
    async def test_its_refusal_naming_the_channel_leaves_the_record_when_another_servers_channel_joins(self):
        recall = ACTIONS({"type": "recall", "channel": "管理组", "about": "封禁名单"})
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(REPLY(None, "在")), recall, ACTIONS(REPLY(None, "这里翻不了"))))
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        engine.settings.set("channel", LOBBY, "share_memory", False)  # 主服 #大厅单独关了记忆共享
        await say(engine, clock, 1, "@小D 管理组开会", channel=ADMINS)
        sync = connect(engine, CHANNEL, LOBBY)  # 主服 #闲聊 + 主服 #大厅：同一个服务器，照常写能翻的频道名单
        await say(engine, clock, 2, "@小D 在吗")  # #闲聊这一轮带上了名单（上面有 #管理组）
        # 主服 #大厅：它照记录里的名单要翻 #管理组
        await say(engine, clock, 3, "@小D 帮我翻一下那个私密频道的封禁名单", channel=LOBBY, author=12, name="小雨")
        self.assertIn(NO_SHARE.format(channel="管理组"), record(engine, sync))  # 对照：这里没开记忆共享，不翻，那句写着名字
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅连进来：跨服务器了
        self.assertNotIn("管理组", record(engine, sync))  # 名单、翻记录的动作、“不翻”的那句都拿掉了：以后连进来的也看不到
        await say(engine, clock, 4, "@小D 在吗", channel=HALL, author=21, name="小红")
        self.assertNotIn("管理组", history_of(llm))  # 别的服 #大厅被叫到时看不到主服私密频道的名字


class GroupLeftWhileARoundAsks(unittest.IsolatedAsyncioTestCase):
    async def test_the_rounds_lookups_leave_its_reply_even_if_the_channel_list_was_in_an_earlier_round(self):
        lookups = ACTIONS({"type": "recall", "channel": "管理组", "about": "封禁名单"}, {"type": "who", "name": "小雨"})
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(REPLY(None, "在")), lookups, ACTIONS(REPLY(None, "这次没翻成"))))
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        same_group(engine, GUILD, OTHER)
        await say(engine, clock, 1, "@小D 管理组开会", channel=ADMINS)
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：同一个服务器组，不算跨服务器
        await say(engine, clock, 2, "@小D 在吗")  # 能翻的频道名单写在这一轮（名单变了才再写）
        self.assertIn("#管理组", record(engine, sync))
        left = leave_group_while_asking(engine, llm)  # 退组时马上拿掉了上一轮的名单
        await say(engine, clock, 3, "@小D 帮我翻一下那个私密频道的封禁名单")  # 它照问的时候看到的名单要翻 #管理组、查小雨的卡
        llm.hook = None
        self.assertTrue(left)
        replied = its_replies(engine, sync)
        self.assertNotIn("管理组", replied)  # 这一轮要翻 #管理组 的动作，写完以后也拿掉了
        self.assertNotIn("小雨", replied)  # 要查小雨的人设卡的动作也一样（里面是人名）
        await say(engine, clock, 4, "@小D 在吗", channel=HALL, author=21, name="小红")
        self.assertNotIn("管理组", history_of(llm))  # 别的服 #大厅被叫到时看不到主服私密频道的名字

    async def test_control_a_leave_that_removes_nothing_keeps_the_rounds_lookups(self):
        # 对照（control）：退组时记录里没有只该原来那边看的（没开记忆共享：没有名单），那一轮要翻的是群友自己说出来的频道：
        # 写完以后再查也不碰它的动作（和跨服务器的连通自己写的一样，不改记录）
        recall = ACTIONS({"type": "recall", "channel": "游戏"})
        engine, llm, transport, clock = world(replies(recall, ACTIONS(REPLY(None, "这次没翻成"))))
        same_group(engine, GUILD, OTHER)
        sync = connect(engine)
        left = leave_group_while_asking(engine, llm)
        await say(engine, clock, 1, "@小D 帮我翻一下 #游戏 那边的记录")
        llm.hook = None
        self.assertTrue(left)
        self.assertIn("游戏", its_replies(engine, sync))


class EditsWhereItNoLongerReads(unittest.IsolatedAsyncioTestCase):
    async def test_an_edit_made_after_it_stopped_reading_the_channel_is_a_delete_in_the_shared_record(self):
        for how in ("变成私密的", "关掉了允许回应"):
            with self.subTest(how):
                engine, llm, transport, clock = world()
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                await say(engine, clock, 2, SAID, channel=HALL, author=21, name="小红")
                if how == "变成私密的":
                    turn_private(transport)  # 连通暂停
                else:
                    engine.settings.set("channel", HALL, "enabled", False)  # 别的服的管理员在 #大厅关了“允许回应”（/dsadmin 区域）
                engine.handle_edit(edited_in_hall(), edited=True)
                await engine.drain()
                whole = record(engine, sync)
                self.assertNotIn(SECRET, whole)  # 改的新内容没写进共用的记录
                self.assertIn(f"#1 小红：{MESSAGE_DELETED}", whole)  # 那一行当成删了
                await say(engine, clock, 3, "@小D 那边最近说什么了")  # 主服 #闲聊
                self.assertNotIn(SECRET, history_of(llm))

    async def test_an_old_messages_edit_made_after_it_turned_private_is_not_written_into_the_group_when_it_leaves(self):
        engine, llm, transport, clock = world()
        sync = three_linked(engine)  # 主服 #闲聊、别的服 #大厅、主服 #大厅：别的服 #大厅断开以后组还在
        await say(engine, clock, 2, SAID, channel=HALL, author=21, name="小红")
        for i in range(6):  # 那条成了很早的消息：被改了不改旧记录，等它那个频道下一轮附一句“改成了什么”
            await say(engine, clock, 10 + i, f"@小D 第{i}句")
        public = msg(2, "@小D 明天下午开会", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)
        engine.handle_edit(public, edited=True)  # 还公开的时候改过一次：那句等着附上
        await engine.drain()
        turn_private(transport)
        engine.handle_edit(edited_in_hall(), edited=True)  # 变成私密的以后又改了
        await engine.drain()
        disconnect(engine, HALL)  # bot 主人照 /dsadmin 状态 说的把暂停的频道断开（以前：还没附上的那句这时直接写进组里的记录）
        whole = record(engine, sync)
        self.assertNotIn(SECRET, whole)  # 私密频道里改的没写进组里的记录
        self.assertIn(f"#1 小红：{MESSAGE_DELETED}", whole)  # 那一行当成删了：还公开时等着的那句也不再写进去
        await say(engine, clock, 20, "@小D 刚才大家说了什么", channel=LOBBY, author=12, name="小雨")
        self.assertNotIn(SECRET, history_of(llm))  # 组里还在的频道看不到

    async def test_a_former_member_that_turned_private_has_its_edit_only_in_its_own_record(self):
        engine, llm, transport, clock = world()
        sync = three_linked(engine)
        await say(engine, clock, 2, SAID, channel=HALL, author=21, name="小红")
        turn_private(transport)
        disconnect(engine, HALL)  # 断开了，组还在：它连通期间说的还留在组里（拿回去的那份另外一份）
        engine.handle_edit(edited_in_hall(), edited=True)
        await engine.drain()
        whole = record(engine, sync)
        self.assertNotIn(SECRET, whole)  # 私密频道里改的，不进别的频道还在用的记录
        self.assertIn(f"#1 小红：{MESSAGE_DELETED}", whole)
        self.assertIn(SECRET, own_record(engine, HALL, OTHER))  # 它自己那份照常改

    async def test_a_link_edited_in_after_it_turned_private_is_not_opened_in_the_group(self):
        ask = "看看小红发的那个"  # 主服 #闲聊叫它的这一轮话里有这句：它要看 #1 的链接

        def posted(url):  # 小红在别的服 #大厅发的那条带链接的消息（改过以后也是这个样子）
            return link_msg(2, f"看看这个 {url}", [url], channel_id=HALL, guild_id=OTHER, author=21, name="小红")

        for leaves in (False, True):
            with self.subTest("断开以后" if leaves else "暂停着"):
                links = FakeReader()
                engine, llm, transport, clock = world(asking_to_read(ask, 1), link_reader=links)
                three_linked(engine)
                await engine.handle_message(posted(PAGE))
                clock.advance(300)  # 过了一阵才有人叫它（不自动打开）：那条补进共用的记录，是 #1
                await say(engine, clock, 3, "@小D 随便聊聊")
                turn_private(transport)
                if leaves:
                    disconnect(engine, HALL)  # 组还在：组里那份还带着它的链接
                edited = []

                async def edit_while_asking(call):
                    if not edited and call["json_mode"] and not is_review(call):
                        edited.append(True)  # 它正在想要不要看的时候，小红把链接改了
                        engine.handle_edit(posted(HIDDEN_PAGE), edited=True)

                llm.hook = edit_while_asking
                await say(engine, clock, 4, f"@小D {ask}")
                llm.hook = None
                self.assertTrue(edited)
                self.assertEqual(links_opened(links), [])  # 私密频道里改进去的链接，组里不打开

    async def test_control_a_delete_in_a_paused_channel_still_reaches_the_shared_record(self):
        # 对照（control）：暂停着的频道里删掉的消息，照样从共用的记录里删（改过的才当成删了，删的不能反而不管）
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await say(engine, clock, 2, "@小D 我的手机号是 13800001111", channel=HALL, author=21, name="小红")
        turn_private(transport)
        engine.handle_delete(OTHER, HALL, [2])
        await engine.drain()
        self.assertNotIn("13800001111", record(engine, sync))


class SharedSummary(unittest.IsolatedAsyncioTestCase):
    async def compacted_after_a_missed_recall(self, *, linked):
        """主服 #闲聊（linked：和主服 #大厅连通）有人要它翻一个找不到的频道：那句后面列着能翻的频道（有 #管理组）。
        聊了几轮以后压缩，翻记录的那一轮压进摘要。返回 (engine, llm, clock, sync)。"""
        engine, llm, transport, clock = world(echoing({"帮我翻一下": ACTIONS({"type": "recall", "channel": "不存在的频道"})}))
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        await say(engine, clock, 1, "@小D 下周开会定一下封禁名单", channel=ADMINS)  # #管理组 有记录：能翻
        sync = connect(engine, CHANNEL, LOBBY) if linked else None  # 同一个服务器：照常能翻记录
        await say(engine, clock, 2, "@小D 帮我翻一下那个频道的记录")
        for i in range(3):
            await say(engine, clock, 3 + i, f"@小D 随便聊{i}")
        await compact(engine, CHANNEL)
        self.assertIn("帮我翻一下那个频道的记录", summarized(llm))  # 对照：翻记录的那一轮压进了摘要
        return engine, llm, clock, sync

    async def test_the_channels_listed_after_a_missed_recall_do_not_go_into_the_shared_summary(self):
        engine, llm, clock, sync = await self.compacted_after_a_missed_recall(linked=True)
        self.assertNotIn("管理组", summarized(llm))  # 能翻的频道名单没给写摘要的看（摘要以后不会再查）
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅连进来：跨服务器了
        await say(engine, clock, 10, "@小D 你们之前聊了什么", channel=HALL, author=21, name="小红")
        seen = "\n".join(text_of(m["content"]) for m in chats(llm)[-1]["messages"])
        self.assertNotIn("管理组", seen)  # 别的服 #大厅被叫到时（摘要在 system prompt 里）看不到主服私密频道的名字

    async def test_control_an_unlinked_channels_own_summary_is_fed_as_before(self):
        # 对照（control）：没连通的频道（默认模式，和 2.7.1 一样）：它自己的摘要照旧看得到那句
        engine, llm, clock, sync = await self.compacted_after_a_missed_recall(linked=False)
        self.assertIn("能翻的：#管理组", summarized(llm))


SUMMARY = "两边的旧事：主服周日爬山，别的服周六唱歌"


class ClearingAfterLeaving(unittest.IsolatedAsyncioTestCase):
    async def joined_after_a_compaction(self):
        """主服 #闲聊、别的服 #大厅聊过的压进了组的摘要；主服 #大厅后来才连进来，说了一句（没压缩）。返回 (engine, llm, clock, sync)。"""
        base = replies()

        def responder(call):
            return SUMMARY if not call["json_mode"] else base(call)

        engine, llm, transport, clock = world(responder)
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        await say(engine, clock, 1, "@小D 周日爬山")
        await say(engine, clock, 2, "@小D 周六唱歌", channel=HALL, author=21, name="小红")
        await say(engine, clock, 3, "@小D 主服最近")
        await compact(engine, CHANNEL)  # 头两轮压进组的摘要
        connect(engine, LOBBY, CHANNEL)  # 主服 #大厅连进来
        await say(engine, clock, 4, "@小D 我只是路过", channel=LOBBY, author=12, name="小雨")
        self.assertEqual(engine.store.load_conversation(sync, 0).summary, SUMMARY)
        return engine, llm, clock, sync

    async def test_clearing_a_channel_that_left_keeps_a_group_summary_none_of_its_words_went_into(self):
        engine, llm, clock, sync = await self.joined_after_a_compaction()
        disconnect(engine, LOBBY)  # 组还在（主服 #闲聊 + 别的服 #大厅），它说的那句还留在组里
        self.assertEqual(engine.store.origin_syncs(LOBBY), [sync])
        await engine.reset_linked(LOBBY)  # 主服 #大厅 /dsadmin 清空记忆（断开时收到的说明就是这么教的）
        self.assertNotIn("我只是路过", record(engine, sync))  # 对照：它那部分删掉了
        self.assertEqual(engine.store.load_conversation(sync, 0).summary, SUMMARY)  # 它说的没压进摘要：别的频道的摘要照旧
        await say(engine, clock, 5, "@小D 我们以前聊过什么", channel=HALL, author=21, name="小红")
        self.assertIn("别的服周六唱歌", text_of(chats(llm)[-1]["messages"][0]["content"]))  # 别的服 #大厅照样记得压进摘要的事

    async def test_control_clearing_a_channel_still_in_the_group_drops_the_shared_summary(self):
        # 对照（control）：还在组里的频道清空，照旧连同共用的摘要一起删（摘要里分不开；清空的回复里也是这么说的）
        engine, llm, clock, sync = await self.joined_after_a_compaction()
        await engine.reset_linked(LOBBY)
        self.assertIsNone(engine.store.load_conversation(sync, 0).summary)


class NewcomerWithoutCrossServer(unittest.IsolatedAsyncioTestCase):
    async def chatted_elsewhere(self, engine, clock):
        """主服 #闲聊 + 别的服 #大厅连通（同一个服务器组，两边都开了“跨服务器”）。阿明在主服 #游戏、组里的别的服 #广场、组外的
        第三服 #广场跟它聊过，每次回到 #闲聊 叫它：三段近况都带进了共用的记录。返回连通组。"""
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 主服游戏开黑", channel=GAMES)  # 这个服务器自己的
        await say(engine, clock, 2, "@小D 我回主服了")
        await say(engine, clock, 3, "@小D 第三服周末去团建", channel=ARENA)  # 服务器组外的
        await say(engine, clock, 4, "@小D 别的服周六唱歌", channel=SQUARE)  # 组里别的服务器的
        await say(engine, clock, 5, "@小D 又回来了")
        whole = record(engine, sync)
        for words in ("主服游戏开黑", "第三服周末去团建", "别的服周六唱歌"):
            self.assertIn(words, whole)  # 对照：组里的服务器都开了“跨服务器”，组外的近况也照常带进来
        return sync

    async def test_recents_from_outside_the_server_group_leave_the_record_when_a_newcomers_server_has_cross_server_off(self):
        engine, llm, transport, clock = world()
        four_servers(engine, transport)  # 第四服没开“跨服务器”
        sync = await self.chatted_elsewhere(engine, clock)
        connect(engine, PLAZA, CHANNEL)  # 第四服 #广场二连进来：还在一个服务器组里，不算跨服务器
        whole = record(engine, sync)
        self.assertNotIn("第三服周末去团建", whole)  # 服务器组外的近况拿掉了
        self.assertIn("主服游戏开黑", whole)  # 这个服务器自己的（没写服务器）照旧
        # 写着是哪个服务器的都拿掉（组里的也算）：写下的名字分不出当时是组里还是组外的（同名、改过名、bot 退出了那个服务器）
        self.assertNotIn("别的服周六唱歌", whole)
        await say(engine, clock, 6, "@小D 你们刚才聊了什么", channel=PLAZA, author=31, name="小雪")
        self.assertNotIn("第三服周末去团建", history_of(llm))  # 第四服 #广场二被叫到时看不到（那边平时看不到组外的）

    async def test_control_a_newcomer_whose_server_has_cross_server_on_keeps_them(self):
        # 对照（control）：连进来的服务器也开了“跨服务器”：组外的近况本来就带得进来，照旧留着
        engine, llm, transport, clock = world()
        four_servers(engine, transport, fourth_cross=True)
        sync = await self.chatted_elsewhere(engine, clock)
        connect(engine, PLAZA, CHANNEL)
        self.assertIn("第三服周末去团建", record(engine, sync))


class LinkCommandWithoutCrossServer(unittest.IsolatedAsyncioTestCase):
    """/dsowner chatsync 连通（借 test_chatsync.OwnerCommand 的假 Discord，加上第四服）。"""

    interaction = test_chatsync.OwnerCommand.interaction

    def setUp(self):
        test_chatsync.OwnerCommand.setUp(self)
        fourth = NS(id=FOURTH, name="第四服", me=NS(), text_channels=[])
        plaza = text_channel(fourth, PLAZA, "广场二")
        known_channel, known_guild = self.bot.get_channel, self.bot.get_guild
        self.bot.get_channel = lambda cid: plaza if cid == PLAZA else known_channel(cid)
        self.bot.get_guild = lambda gid: fourth if gid == FOURTH else known_guild(gid)
        self.bot.guilds.append(fourth)
        four_servers(self.engine, self.transport)  # 第四服没开“跨服务器”

    async def test_the_owner_is_told_before_and_after_that_recents_from_outside_the_group_are_removed(self):
        from dsbot.commands import sync_command

        engine = self.engine
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        await say(engine, self.clock, 1, "@小D 第三服周末去团建", channel=ARENA)
        await say(engine, self.clock, 2, "@小D 我回主服了")
        self.assertIn("第三服周末去团建", record(engine, sync))  # 对照：组外的近况带进了共用的记录
        ask = self.interaction()
        await sync_command(self.bot, ask, "link", str(PLAZA), str(CHANNEL))
        preview = ask.response.sent[0][0]
        # 确认框先说一声
        self.assertIn("「第四服」#广场二所在的服务器没开“跨服务器”：共用记录里从服务器组外带来的近况（有的话）会先拿掉", preview)
        click = self.interaction()
        await ask.response.sent[0][1].confirm(click)
        result = click.done[-1]
        self.assertIn("已经连通：", result)
        # 连好以后告诉 bot 主人拿掉了几条（不是“跨服务器了”：组还在一个服务器组里）
        self.assertIn("有服务器没开“跨服务器”：共用记录里有 1 条拿掉了从服务器组外带来的近况。", result)
        self.assertNotIn("组里现在跨服务器了", result)


if __name__ == "__main__":
    unittest.main()
