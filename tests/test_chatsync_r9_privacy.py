"""2.8.0 连通的频道，第九轮评审修掉的隐私问题：共用记录的摘要里它改长期记忆、按名字查人设卡的动作，服务器退出了服务器组以后
组里的频道那几轮里只有那个服务器能看的东西，找不到了的已经断开的频道算在哪个服务器，还有连通结果里报的那个数。

- 连通组的记录压成摘要时，它的回话里改长期记忆（写着那个服务器那一条记忆的原文）、按名字查人设卡（查的是谁）的动作不写进去：
  摘要以后不会再查，同一个服务器（或者连成一组的服务器）的频道连通、后来别的服务器的频道连进来（跨服务器了），那边被叫到时
  从摘要里也看不到。它说的话照旧写进摘要；没连通的频道压缩时和以前一样。
- 连通组的频道还在一个服务器组里，组里原来有的服务器退出了服务器组：整份记录照变成跨服务器时那样查一遍（近况、能翻的频道名单、
  翻到的记录、查到的人设卡……组里还在的频道那几轮里的也算：写下来的分不出是哪个服务器的，组里还在的服务器的也一起拿掉）。
  那时正好有一轮在跑的，那一轮写进记录以后、放开锁之前再整份查一遍。之后才连进服务器组的服务器的频道连进来，看不到退出的
  那个服务器才能看的。
- 已经断开的频道找不到了（启动时还没连上 Discord、频道删了）：照它自己那份记录记下的服务器算它在哪；服务器还在组里的，它那几轮
  照旧（重启一次不会拿掉同一个服务器的近况、能翻的频道名单、翻到的记录、附着的人设卡）。服务器退出了组的，启动时照样拿掉。
- 有频道连进来时，已经断开的频道那几轮里拿掉的（它的服务器不在组里了）不算进连通结果报的数：预览没说有服务器没开“跨服务器”，
  结果也不说。"""

import unittest
from types import SimpleNamespace as NS
from unittest import mock

import test_chatsync
from helpers import CHANNEL, GUILD, FakeTransport, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, replies, say, text_of, world
from test_chatsync_privacy import ARENA, GAMES, THIRD, record
from test_chatsync_r2_privacy import same_group
from test_chatsync_r2_records import compact
from test_chatsync_r3_privacy import when_asked
from test_chatsync_r4_scrub import kinds_in_replies, writes_fail
from test_chatsync_r6_privacy import FOURTH, PLAZA, echoing
from test_chatsync_review import summary_calls
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review, stores

from dsbot import groups
from dsbot.engine import Engine
from dsbot.models import ChannelPlace

try:
    from dsbot.commands import start_groups
except ImportError:  # 没装 discord.py
    start_groups = None

BACK = 301  # 第三服的公开频道 #后台
ROOM = 103  # 主服的公开频道 #聊天室（连通过，后来断开了）
NOTICE = 104  # 主服的公开频道 #公告（没连通）
NEWBIE = 106  # 主服的公开频道 #新人（后来连进来的）
BLUE = 51  # 小蓝：只在第三服出现过，人设卡记在服务器组里
BLUE_CARD = "小蓝：住在幸福路三号，在准备考研"  # 小蓝的人设卡
RAIN_CARD = "小雨是猫派，周末在幸福路的猫咖打工"  # 小雨在主服的人设卡
WHISPER = "后台在商量换版主"  # 阿明在第三服 #后台 跟它说的（只有第三服和跟它连成一组的服务器能看）
OUTAGE = "公告里说周五停电"  # 阿明在主服 #公告 跟它说的
ENTRY = "管理组：下周三封禁小王"  # 主服的第 1 条长期记忆（在私密的 #管理组 里记下的）
CHANGED = "管理组：下周五封禁小王"  # 阿明让它改成的
EDIT_MEMORY = ACTIONS({"type": "memory", "edit": 1, "text": CHANGED}, REPLY(None, "改好了"))
LOOKUP = ACTIONS({"type": "who", "name": "阿珍"})  # 群友问“新来的版主是谁”，它按名字查阿珍的人设卡
SAYING = {"帮我把记忆改一下": EDIT_MEMORY, "新来的版主是谁": LOOKUP}
ASK_NOTICE = {"帮我翻一下公告": ACTIONS({"type": "recall", "channel": "公告"})}


def whole_request(llm):
    """最近一次聊天请求的全部内容（连 system prompt：共用记录的摘要在那里）。"""
    return "\n".join(text_of(m["content"]) for m in chats(llm)[-1]["messages"])


def summarized(llm):
    """最近一次压缩时给它看的聊天（【聊天记录】后面那部分）。"""
    return text_of(summary_calls(llm)[-1]["messages"][-1]["content"]).split("【聊天记录】", 1)[-1]


class MemoryAndLookupsInTheSharedSummary(unittest.IsolatedAsyncioTestCase):
    async def test_a_server_that_joins_later_reads_neither_memory_text_nor_lookups_through_the_shared_summary(self):
        for how in ("同一个服务器的两个频道", "连成一组的两个服务器的频道"):
            with self.subTest(how):
                cards, memory = stores()
                engine, llm, transport, clock = world(echoing(SAYING), cards=cards, memory=memory)
                if how == "同一个服务器的两个频道":
                    partner, newcomer, guest = LOBBY, HALL, (21, "小红")  # 后来连进来的是别的服 #大厅
                else:
                    transport.places[ARENA] = ChannelPlace(guild_id=THIRD, name="广场", public=True)
                    transport.names[ARENA] = "广场"
                    transport.guild_names[THIRD] = "第三服"
                    transport.joined.append(THIRD)
                    same_group(engine, GUILD, OTHER)  # 主服、别的服连成一个服务器组：长期记忆记在组里
                    partner, newcomer, guest = HALL, ARENA, (31, "小绿")  # 后来连进来的是服务器组外的第三服 #广场
                scope = engine.store.memory_scope(GUILD)
                memory.add(scope, ENTRY, via="chat")
                connect(engine, CHANNEL, partner)  # 还不算跨服务器：回话里改长期记忆、查人设卡的动作照常记着
                await say(engine, clock, 1, "@小D 那件事改到周五了，帮我把记忆改一下")
                self.assertEqual([m.text for m in memory.entries(scope)], [CHANGED])  # 对照：照样改了
                await say(engine, clock, 2, "@小D 新来的版主是谁")
                await say(engine, clock, 3, "@小D 今天吃什么")
                await compact(engine, CHANNEL)  # 压缩共用的记录（摘要以后不会再查）
                given = summarized(llm)
                self.assertIn("新来的版主是谁", given)  # 对照：改记忆、查人设卡那两轮都压进了摘要
                self.assertIn("改好了", given)  # 它说的话照旧写进摘要
                self.assertNotIn("封禁小王", given)  # 改长期记忆的动作（那一条的原文）不写进去
                self.assertNotIn("阿珍", given)  # 按名字查的是谁也不写
                connect(engine, newcomer, CHANNEL)  # 别的服务器的频道连进来：跨服务器了
                await say(engine, clock, 4, "@小D 你们那边最近怎么样", channel=newcomer, author=guest[0], name=guest[1])
                seen = whole_request(llm)
                self.assertIn("那件事改到周五了", seen)  # 对照：那边的请求里带着共用记录的摘要（压掉的那两轮在里面）
                self.assertNotIn("封禁小王", seen)  # 那边被叫到时，摘要里也没有这边的那条记忆
                self.assertNotIn("阿珍", seen)

    async def test_control_a_channels_own_record_is_summarized_as_before(self):
        # 对照（control）：没连通的频道压缩它自己那份记录时和以前一样，改长期记忆、查人设卡的动作照旧写进摘要
        cards, memory = stores()
        engine, llm, transport, clock = world(echoing(SAYING), cards=cards, memory=memory)
        memory.add(GUILD, ENTRY, via="chat")
        await say(engine, clock, 1, "@小D 那件事改到周五了，帮我把记忆改一下")
        await say(engine, clock, 2, "@小D 新来的版主是谁")
        await say(engine, clock, 3, "@小D 今天吃什么")
        await compact(engine, CHANNEL)
        given = summarized(llm)
        self.assertIn(f"小D 改了第 1 条长期记忆：{CHANGED}", given)
        self.assertIn("小D 查了「阿珍」的人设卡", given)


def grouped_with_third(engine, transport, cards, memory):
    """主服、别的服、第三服连成一个服务器组（真的连组：人设卡、长期记忆都记在组里）；小蓝的卡记在组里，他只在第三服出现过。
    主服、第三服开了记忆共享（第三服没开“跨服务器”）；第四服还不在组里，开了“跨服务器”（它连进来时不会因为“没开”拿掉近况）。
    主服 #闲聊 + 别的服 #大厅连通（在一个服务器组里，不算跨服务器）。返回连通组。"""
    transport.places[BACK] = ChannelPlace(guild_id=THIRD, name="后台", public=True)
    transport.places[PLAZA] = ChannelPlace(guild_id=FOURTH, name="广场二", public=True)
    transport.names.update({BACK: "后台", PLAZA: "广场二"})
    transport.guild_names.update({THIRD: "第三服", FOURTH: "第四服"})
    transport.joined = [OTHER, THIRD, FOURTH]
    store = engine.store
    groups.link(store, cards, memory, OTHER, GUILD, now=1.0)
    groups.link(store, cards, memory, THIRD, GUILD, now=1.1)
    scope = store.memory_scope(GUILD)
    cards.put(scope, BLUE, "小蓝", BLUE_CARD, via="chat", now=1.2)
    cards.note_seen(scope, BLUE, THIRD, "小蓝")
    for guild in (GUILD, THIRD):
        engine.settings.set("guild", guild, "share_memory", True)
    engine.settings.set("guild", FOURTH, "cross_server", True)
    return connect(engine)


def third_leaves(engine, cards, memory):
    """第三服退出了服务器组（/dsowner 服务器组 退组做完了：小蓝的卡跟着第三服走、从组里拿掉），照退组之前的组查一遍连通组的记录。"""
    store = engine.store
    before = store.group_members(store.group_of(THIRD))
    groups.unlink(store, cards, memory, THIRD, now=3.0)
    engine.groups_changed(before, left=True)


async def fourth_joins_and_asks(engine, llm, clock, cards, memory):
    """第四服后来连进服务器组（从来没和第三服在一个组里），它的 #广场二 连进来，小紫在那里叫它。返回那一次请求的全部内容。"""
    joined = groups.link(engine.store, cards, memory, FOURTH, GUILD, now=4.0)
    engine.groups_changed(joined.members, left=False)
    connect(engine, PLAZA, CHANNEL)
    await say(engine, clock, 30, "@小D 大家好", channel=PLAZA, author=41, name="小紫")
    return whole_request(llm)


class ServerLeavingTheGroupOfAGroupedLink(unittest.IsolatedAsyncioTestCase):
    async def test_what_only_the_departed_server_could_see_leaves_every_turn_when_it_leaves_the_group(self):
        cards, memory = stores()
        lookups = ACTIONS({"type": "recall", "channel": "后台"}, {"type": "who", "name": "小蓝"})
        engine, llm, transport, clock = world(when_asked({"帮我翻翻那边的记录": lookups}), cards=cards, memory=memory)
        sync = grouped_with_third(engine, transport, cards, memory)
        await say(engine, clock, 1, f"@小D {WHISPER}", channel=BACK)  # 阿明在第三服 #后台（它自己那份记录）
        await say(engine, clock, 2, "@小D 帮我翻翻那边的记录，再看看小蓝是谁")  # 阿明回到主服 #闲聊（还在组里的频道）
        whole = record(engine, sync)
        for words in ("在「第三服」服务器的 #后台 跟你聊过", "能翻记录的别的频道", "你翻了 #后台（「第三服」服务器） 的记录", "幸福路三号"):
            self.assertIn(words, whole)  # 对照：第三服还在组里时照常带进来（近况、能翻的频道名单、翻到的记录、查到的人设卡）
        self.assertEqual(kinds_in_replies(engine, sync), ["recall", "who"])  # 对照：它翻了 #后台、查了小蓝
        third_leaves(engine, cards, memory)  # 主服 #闲聊 + 别的服 #大厅还在一个服务器组里
        self.assertIsNone(cards.get(engine.store.memory_scope(GUILD), BLUE))  # 对照：小蓝的卡跟着第三服走了
        whole = record(engine, sync)
        self.assertNotIn(WHISPER, whole)  # 近况、翻到的记录拿掉了（组里还在的频道那一轮里的也算）
        self.assertNotIn("能翻记录的别的频道", whole)  # 能翻的频道名单整个拿掉（分不出是哪个服务器的）
        self.assertNotIn("后台", whole)
        self.assertNotIn("幸福路三号", whole)  # 查到的小蓝的卡也拿掉
        self.assertEqual(kinds_in_replies(engine, sync), [])  # 回话里翻记录、查人设卡的动作也拿掉
        self.assertIn("阿明：@小D 帮我翻翻那边的记录，再看看小蓝是谁", whole)  # 说过的话照旧
        seen = await fourth_joins_and_asks(engine, llm, clock, cards, memory)
        for words in (WHISPER, "后台", "幸福路三号"):
            self.assertNotIn(words, seen)  # 后来连进组的第四服的频道被叫到时都看不到

    async def test_a_round_that_runs_during_the_leave_is_checked_whole_once_it_is_written(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(replies(), cards=cards, memory=memory)
        sync = grouped_with_third(engine, transport, cards, memory)
        await say(engine, clock, 1, f"@小D {WHISPER}", channel=BACK)
        left = []

        async def leave_while_asking(call):
            if not left and call["json_mode"] and not is_review(call):
                left.append(True)  # 主服 #闲聊这一轮按原来的组带上了 #后台 的近况、名单，正在等模型回话：第三服退出了服务器组
                third_leaves(engine, cards, memory)

        llm.hook = leave_while_asking
        await say(engine, clock, 2, "@小D 我回主服了")
        llm.hook = None
        self.assertTrue(left)
        self.assertIn(WHISPER, text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 对照：这一轮问模型时带上了 #后台 的近况
        whole = record(engine, sync)
        self.assertNotIn(WHISPER, whole)  # 这一轮写进去以后、放开锁之前，整份记录又查了一遍
        self.assertNotIn("后台", whole)
        self.assertIn("阿明：@小D 我回主服了", whole)
        self.assertNotIn(WHISPER, await fourth_joins_and_asks(engine, llm, clock, cards, memory))


class ColdTransport(FakeTransport):
    """刚启动、还没登录 Discord 时的样子：discord.py 的缓存是空的，哪个频道、服务器都找不到（DSBot.__init__ 里的 start_groups
    就是这时候跑的）。"""

    def locate(self, channel_id):
        return None

    def guild_name(self, guild_id):
        return None

    def guild_ids(self):
        return []


def restart(engine):
    """重启了（同一个数据库，新的 Engine，内存里什么都没有），还没登录 Discord：start_groups 查一遍连通组的记录。"""
    cold = Engine(
        engine.cfg,
        engine.settings,
        engine.store,
        engine.llm,
        engine.ledger,
        ColdTransport(),
        clock=engine.clock,
        sleep=engine.sleep,
        cards=engine.cards,
        memory=engine.memory,
    )
    cfg = NS(engine=NS(memory_max_entries=50), bot=NS(shared_cards=False))
    start_groups(NS(store=engine.store, engine=cold, cards=engine.cards, memory=engine.memory, cfg=cfg))


class FormerMembersItCannotFind(unittest.IsolatedAsyncioTestCase):
    async def room_talked(self, engine, transport, clock):
        """主服 #闲聊、#大厅、#聊天室连通（同一个服务器：近况、能翻的频道名单、翻记录、附着的人设卡照常；主服开了记忆共享）。阿明
        在 #游戏 跟它约了副本，在 #公告 说了停电；小雨在 #聊天室 说了句话，阿明接着在 #聊天室 叫它翻 #公告：那一轮带上了近况、能翻的
        频道名单、小雨的卡，翻到了 #公告 的记录。然后 #聊天室 断开（它说过的留在组里）。返回连通组。"""
        for cid, name in ((GAMES, "游戏"), (ROOM, "聊天室"), (NOTICE, "公告"), (NEWBIE, "新人")):
            transport.places[cid] = ChannelPlace(guild_id=GUILD, name=name, public=True)
            transport.names[cid] = name
        engine.settings.set("guild", GUILD, "share_memory", True)
        engine.cards.put(GUILD, 12, "小雨", RAIN_CARD, via="command")
        sync = connect(engine, CHANNEL, LOBBY)
        connect(engine, ROOM, CHANNEL)
        await say(engine, clock, 1, "@小D 周六一起打副本", channel=GAMES)
        await say(engine, clock, 2, f"@小D {OUTAGE}", channel=NOTICE)
        await engine.handle_message(msg(3, "今天猫咖好忙", channel_id=ROOM, author=12, name="小雨"))
        await say(engine, clock, 4, "@小D 帮我翻一下公告", channel=ROOM)
        whole = record(engine, sync, origin=ROOM)
        for words in ("在 #游戏 跟你聊过：", "周六一起打副本", "#公告", f"阿明：@小D {OUTAGE}", RAIN_CARD):
            self.assertIn(words, whole)  # 对照：同一个服务器的连通，这些照常写进去
        self.assertEqual(kinds_in_replies(engine, sync), ["recall"])  # 对照：它翻了 #公告
        disconnect(engine, ROOM)  # 组里还剩 #闲聊、#大厅
        return sync

    async def test_a_former_member_it_cannot_find_is_placed_by_its_own_record_and_keeps_its_in_group_content(self):
        for how in ("启动时还没连上 Discord", "那个频道后来删了"):
            with self.subTest(how):
                if how == "启动时还没连上 Discord" and start_groups is None:
                    self.skipTest("没装 discord.py")
                cards, memory = stores()
                engine, llm, transport, clock = world(when_asked(ASK_NOTICE), cards=cards, memory=memory)
                sync = await self.room_talked(engine, transport, clock)
                kept = record(engine, sync, origin=ROOM)
                if how == "启动时还没连上 Discord":
                    restart(engine)  # #聊天室找不到、也没有连通的那一行了：照它自己那份记录记下的服务器（主服）算
                else:
                    known = transport.locate
                    transport.locate = lambda cid: None if cid == ROOM else known(cid)  # #聊天室后来被删掉了
                    connect(engine, NEWBIE, CHANNEL)  # 主服 #新人连进来：连好以后查一遍断开了的频道那几轮
                self.assertEqual(record(engine, sync, origin=ROOM), kept)  # 主服还在组里：它那几轮一点都不拿掉
                self.assertEqual(kinds_in_replies(engine, sync), ["recall"])  # 回话里翻 #公告 的动作也照旧

    async def test_control_before_login_a_former_member_whose_server_left_the_group_still_loses_it(self):
        # 对照（control）：启动时还没连上 Discord，找不到的已经断开的频道照它自己那份记录算在第三服：第三服已经退出了服务器组
        # （退组时改写记录出错了，那次没拿掉），它那几轮里只有第三服能看的照样拿掉
        if start_groups is None:
            self.skipTest("没装 discord.py")
        cards, memory = stores()
        engine, llm, transport, clock = world(replies(), cards=cards, memory=memory)
        sync = grouped_with_third(engine, transport, cards, memory)
        transport.places[ARENA] = ChannelPlace(guild_id=THIRD, name="广场", public=True)
        transport.names[ARENA] = "广场"
        connect(engine, ARENA, CHANNEL)  # 第三服 #广场也连进来
        await say(engine, clock, 1, f"@小D {WHISPER}", channel=BACK)
        await say(engine, clock, 2, "@小D 我来广场了", channel=ARENA)  # 那一轮带上了 #后台 的近况（同一个服务器的）
        disconnect(engine, ARENA)
        with writes_fail(engine, "1"), mock.patch("dsbot.engine.log"):
            third_leaves(engine, cards, memory)  # 出错的那条日志不打出来
        self.assertIn(WHISPER, record(engine, sync, origin=ARENA))  # 对照：退组时没拿掉
        restart(engine)
        self.assertNotIn(WHISPER, record(engine, sync, origin=ARENA))
        self.assertIn("阿明：@小D 我来广场了", record(engine, sync, origin=ARENA))  # 说过的话照旧


class LinkResultAfterCheckingFormerMembers(unittest.IsolatedAsyncioTestCase):
    """/dsowner chatsync 连通（借 test_chatsync.OwnerCommand 的假 Discord，加上主服 #游戏）。"""

    interaction = test_chatsync.OwnerCommand.interaction

    def setUp(self):
        test_chatsync.OwnerCommand.setUp(self)
        self.transport.places[GAMES] = ChannelPlace(guild_id=GUILD, name="游戏", public=True)
        self.transport.names[GAMES] = "游戏"
        games = test_chatsync.text_channel(self.bot.get_channel(CHANNEL).guild, GAMES, "游戏", 2)
        known = self.bot.get_channel
        self.bot.get_channel = lambda cid: games if cid == GAMES else known(cid)
        self.llm.responder = when_asked(ASK_NOTICE)

    async def test_the_link_result_does_not_blame_cross_server_settings_for_what_former_members_lost(self):
        from dsbot.commands import sync_command

        engine, clock = self.engine, self.clock
        sync = connect(engine, HALL, CHANNEL)  # 别的服 #大厅 + 主服 #闲聊：跨服务器
        connect(engine, GAMES, CHANNEL)  # + 主服 #游戏
        await say(engine, clock, 1, "@小D 帮我翻一下公告", channel=HALL, author=21, name="小红")  # 跨服务器不让翻
        self.assertEqual(kinds_in_replies(engine, sync), ["recall"])  # 对照：要翻的那个动作留在回话里（跨服务器的连通自己写的不动）
        disconnect(engine, HALL)  # 别的服 #大厅断开：组里剩主服的两个频道（同一个服务器）
        ask = self.interaction()
        await sync_command(self.bot, ask, "link", str(LOBBY), str(CHANNEL))  # 主服 #大厅连进来
        preview, view, _ = ask.response.sent[0]
        click = self.interaction()
        await view.confirm(click)
        result = click.done[-1]
        self.assertIn("已经连通：", result)
        # 对照：连好以后查了断开的 #大厅那几轮（别的服不在主服的服务器组里），拿掉了那个动作
        self.assertEqual(kinds_in_replies(engine, sync), [])
        self.assertNotIn("没开“跨服务器”", preview)  # 连进来的主服本来就在组里：预览什么都没说
        self.assertNotIn("没开“跨服务器”", result)  # 结果也不说有服务器没开“跨服务器”
        self.assertNotIn("拿掉了", result)


if __name__ == "__main__":
    unittest.main()
