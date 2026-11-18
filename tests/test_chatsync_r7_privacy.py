"""2.8.0 连通的频道，第七轮评审修掉的隐私问题：暂停着的频道里还没处理完的消息被改了、连通之前和暂停着的时候发的文件和链接、
什么时候拿掉服务器组外的近况和怎么认出它们，还有共用记录的摘要里怎么叫它。

- 连通的频道变成私密的（连通暂停）、关掉了“允许回应”以后，还没处理完的消息又被本人改了：改过的样子不进组里的记录。还在缓冲区
  里的拿掉（当成删了，又公开了也不补进去）；正在处理的那一批照模型看到的写进记录，写完以后组里那一行当成删了。它带着的文件、
  链接在共用的那份里也拿掉，以后谁要它打开都打不开。还公开的时候改的照旧；只是 Discord 补上了预览的，什么都不改。
- 连通组的记录：同一个人两分钟内发的文件、链接，只有这份记录里有的才直接读——连通之前的闲聊（记进了它自己那份）、暂停着的
  频道里还没进记录的都不读进来。
- 连成一组的服务器里的频道连通（不算跨服务器），连进来没开“跨服务器”的服务器时拿掉服务器组外的近况：标题行里的服务器名从第一个
  “在「”取到最后一个“」服务器的 ”，整个对得上组里的一个服务器才留着；组外也有服务器叫这个名字（bot 在那边，或者记得有人刚在
  那边跟它聊过）就分不出来，也拿掉；名字里写着“」服务器的 ”冒充组里的也一样（只会多拿）。
- 只有连进来的频道带来了组里原来没有、又没开“跨服务器”的服务器才拿：组里的服务器自己后来关掉了“跨服务器”，记录里已经有的照旧
  （组里原来就有的服务器再连进频道来也一样）。确认框里说会拿掉，才拿；连好以后说拿掉了几条的，确认框里都先说过。
- 连通组的记录压缩成摘要：它在几个服务器里的昵称可能不一样，摘要里一律叫它「bot」，每个频道看摘要时都告诉它「bot」就是它。
  没连通的频道照旧用它在那个服务器的昵称。"""

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS

import test_chatsync
from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, chats, connect, replies, say, text_channel, text_of, world
from test_chatsync_lifecycle import answering, hold, spin
from test_chatsync_privacy import ARENA, THIRD, elsewhere, history_of, record
from test_chatsync_r2_privacy import same_group
from test_chatsync_r2_records import asking_to_read, compact, files_read
from test_chatsync_r3_entries import links_opened
from test_chatsync_review import summary_calls
from test_dm import OTHER
from test_engine_files import Reader, doc
from test_links_images import PAGE, FakeReader, link_msg
from test_memory import is_review

from dsbot import prompts
from dsbot.models import ChannelPlace, LinkRef
from dsbot.prompts import MESSAGE_DELETED

# 共用记录的摘要里怎么叫它（第七轮起是 prompts.SYNC_BOT_NAME；以前用的是压缩的那个频道里它的昵称）
BOT = getattr(prompts, "SYNC_BOT_NAME", "bot")
FOURTH = 4  # 第四个服务器：和主服、别的服连成一个服务器组，没开“跨服务器”
PLAZA = 400  # 第四服的公开频道 #广场二
SQUARE = 703  # 别的服的另一个公开频道 #广场
SECRET = "裁员名单是小王"  # #大厅不再公开以后，小红改进那条消息的话
SECRET_PAGE = "https://example.com/layoff-list"  # 不再公开以后改进去的链接
SECRET_FILE = "裁员名单.xlsx"  # 不再公开以后改进去的文件
OUTSIDE = "第三服周末去团建"  # 阿明在服务器组外的第三服 #广场跟它说的
PROMISE = "我答应你周六一起唱歌"  # 它在别的服 #大厅答应小红的


def hers(mid, text, **kw):
    """小红在别的服 #大厅发的一条。"""
    return msg(mid, text, channel_id=HALL, guild_id=OTHER, author=21, name="小红", **kw)


def carrying(mid, kind, what, text="看这个"):
    """小红在 #大厅发的带链接（kind 是“链接”：what 是网址）或者带文件（what 是文件名）的一条。"""
    if kind == "链接":
        return link_msg(mid, f"{text} {what}", [what], channel_id=HALL, guild_id=OTHER, author=21, name="小红")
    return hers(mid, text, files=[doc(what)])


def turn_private(transport):
    """别的服把 #大厅改成了私密频道：连通暂停，它不看那里的消息。"""
    transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=False)


def turn_public(transport):
    """#大厅又改回了公开频道：连通接着用。"""
    transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=True)


def stop_reading(engine, transport, how):
    """它不再看别的服 #大厅：变成私密的，或者那里的管理员关了“允许回应”（/dsadmin 区域）。"""
    if how == "变成私密的":
        turn_private(transport)
    else:
        engine.settings.set("channel", HALL, "enabled", False)


def start_reading(engine, transport, how):
    """又公开了（又打开了）。"""
    if how == "变成私密的":
        turn_public(transport)
    else:
        engine.settings.unset("channel", HALL, "enabled")


def reading_world(responder=None):
    """能打开链接、读文件的 world。返回 (engine, llm, transport, clock, opened)：opened() 是打开过的链接和读过的文件，按先后。"""
    links, files = FakeReader(), Reader()
    engine, llm, transport, clock = world(responder or replies(), link_reader=links)
    engine.file_reader = files
    return engine, llm, transport, clock, lambda: links_opened(links) + files_read(files)


def own_record(engine, channel, guild):
    """这个频道自己那份记录。"""
    return "\n".join(text_of(t.content) for t in engine.store.load_conversation(channel, guild).turns)


class UnprocessedMessagesEditedWhilePaused(unittest.IsolatedAsyncioTestCase):
    async def test_a_buffered_message_edited_after_it_stopped_reading_the_channel_never_reaches_the_shared_record(self):
        for how in ("变成私密的", "关掉了允许回应"):
            with self.subTest(how):
                engine, llm, transport, clock = world()
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                await engine.handle_message(hers(2, "随便说说"))  # 小红随口一句（没叫它）：还在 #大厅的缓冲区里
                stop_reading(engine, transport, how)
                engine.handle_edit(hers(2, f"随便说说：{SECRET}"), edited=True)  # 那以后她改了那条
                start_reading(engine, transport, how)  # 又公开了（又打开了），这中间那里没人说话
                await say(engine, clock, 3, "@小D 大家在聊什么")  # 主服 #闲聊：先把 #大厅缓冲区里的补进共用的记录
                whole = record(engine, sync)
                self.assertNotIn(SECRET, whole)  # 改过的样子没进共用的记录
                self.assertNotIn("随便说说", whole)  # 那条当成删了：从缓冲区里拿掉，又公开了也不补进去
                self.assertNotIn(SECRET, history_of(llm))

    async def test_a_message_edited_while_its_round_asks_becomes_a_delete_and_its_file_or_link_is_not_opened(self):
        for kind, before, after in (("链接", PAGE, SECRET_PAGE), ("文件", "菜单.docx", SECRET_FILE)):
            with self.subTest(kind):
                engine, llm, transport, clock, opened = reading_world(asking_to_read("打开 #1", 1))
                sync = connect(engine)
                await engine.handle_message(carrying(2, kind, before))  # 小红在 #大厅发了带链接（文件）的一条，没叫它
                gate = asyncio.Event()
                hold(llm, {"大家好": gate})
                await engine.handle_message(msg(3, "@小D 大家好", channel_id=HALL, guild_id=OTHER, author=22, name="小蓝", mentions_bot=True))
                await spin()  # 小蓝叫它：两条一起在 #大厅这一轮里，正在等模型回话（小红那条不是叫它的人发的，这一轮不直接打开）
                turn_private(transport)  # 这时候别的服把 #大厅改成了私密的
                engine.handle_edit(carrying(2, kind, after), edited=True)  # 小红把那条改成了别的链接（文件）
                gate.set()
                await engine.drain()
                llm.hook = None
                self.assertIn(f"#1 小红：{MESSAGE_DELETED}", record(engine, sync))  # 照模型看到的写进去，写完以后当成删了
                await say(engine, clock, 4, "@小D 打开 #1 的看看")  # 主服 #闲聊的阿明要它打开那条的链接（文件）
                self.assertEqual(opened(), [])  # 改之前的、改之后的都打不开：共用的那份里已经没有了
                self.assertNotIn(after, record(engine, sync))

    async def test_control_an_edit_while_the_channel_is_public_is_used_as_before(self):
        # 对照（control）：还公开的时候改的照旧：缓冲区里那条换成改过的样子补进共用的记录；同一个人两分钟内在别的频道叫它，
        # 改进去的链接（文件）照样直接打开（补进来以后就在这份记录里了）
        for kind, what, content in (("链接", PAGE, f"{PAGE} 的正文"), ("文件", "周报.docx", "周报.docx 的内容")):
            with self.subTest(kind):
                engine, llm, transport, clock, opened = reading_world()
                sync = connect(engine)
                await engine.handle_message(hers(2, "看这个"))
                engine.handle_edit(carrying(2, kind, what), edited=True)  # #大厅还公开：她给那条加上了链接（文件）
                clock.advance(30)
                await say(engine, clock, 3, "@小D 在吗", author=21, name="小红")  # 小红在主服 #闲聊叫它（她也在主服）
                self.assertEqual(opened(), [what])
                whole = record(engine, sync)
                self.assertIn("#1 小红：", whole)
                self.assertIn(content, whole)

    async def test_control_a_preview_added_while_it_is_paused_changes_nothing(self):
        # 对照（control）：暂停着的时候 Discord 给缓冲区里那条补上了链接预览（不是本人改的）：那条照旧留着，又公开了照样补进去
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await engine.handle_message(carrying(2, "链接", PAGE))
        turn_private(transport)
        engine.handle_edit(hers(2, f"看这个 {PAGE}", links=[LinkRef(PAGE, title="新闻标题", site="新闻网")]), edited=False)
        turn_public(transport)
        await say(engine, clock, 3, "@小D 大家在聊什么")
        self.assertIn(f"小红：看这个 {PAGE}", record(engine, sync))


class AutoReadsInTheSharedRecord(unittest.IsolatedAsyncioTestCase):
    async def test_the_same_persons_recent_file_or_link_is_read_in_only_if_it_is_in_the_shared_record(self):
        for kind, what, content in (("链接", PAGE, f"{PAGE} 的正文"), ("文件", "薪资表.xlsx", "薪资表.xlsx 的内容")):
            for when in ("连通之前发的", "暂停着的频道里还没进记录的"):
                with self.subTest(kind=kind, when=when):
                    engine, llm, transport, clock, opened = reading_world()
                    if when == "连通之前发的":
                        await engine.handle_message(carrying(2, kind, what))  # 小红在 #大厅随手发的（没叫它）
                        sync = connect(engine)  # 这时候连通：那条记进 #大厅自己那份（连通之前的聊天不给别的频道看）
                        self.assertIn("看这个", own_record(engine, HALL, OTHER))
                    else:
                        sync = connect(engine)
                        await engine.handle_message(carrying(2, kind, what))
                        turn_private(transport)  # 还没人叫它，#大厅就变成私密的了：那条不补进共用的记录
                    clock.advance(30)
                    await say(engine, clock, 3, "@小D 在吗", author=21, name="小红")  # 两分钟内小红在主服 #闲聊叫它
                    self.assertEqual(opened(), [])  # 不在共用的记录里：不直接读进来
                    self.assertNotIn(content, record(engine, sync))

    async def test_control_an_unlinked_channel_still_reads_the_same_persons_file_or_link_pushed_out_of_the_round(self):
        # 对照（control）：没连通的频道（默认模式，和 2.7.1 一样）：同一个人两分钟内发的，被后面刷屏挤出了这一轮（没有编号）
        # 也照样直接读
        for kind, what in (("链接", PAGE), ("文件", "周报.docx")):
            with self.subTest(kind):
                engine, llm, transport, clock, opened = reading_world()
                post = link_msg(1, f"看这个 {PAGE}", [PAGE]) if kind == "链接" else msg(1, "看这个", files=[doc(what)])
                await engine.handle_message(post)  # 阿明在主服 #闲聊发的（没叫它）
                for i in range(engine.ec.max_batch_messages):
                    await engine.handle_message(msg(10 + i, f"刷屏{i}", author=12, name="小雨"))
                await say(engine, clock, 99, "@小D 你看看")  # 阿明叫它：他那条不在这一轮给模型看的消息里了
                self.assertEqual(opened(), [what])


def four_servers(engine, transport, *, third="第三服"):
    """主服、别的服、第四服连成一个服务器组。主服、别的服和组外的第三服（名字是 third，bot 也在那里）开了“跨服务器”，第四服没开。
    阿明会在第三服 #广场跟它聊；第四服有 #广场二，别的服还有 #广场。"""
    elsewhere(engine, transport)  # 主服、第三服开了“跨服务器”；ARENA 是第三服 #广场
    engine.settings.set("guild", OTHER, "cross_server", True)
    for cid, guild, name in ((PLAZA, FOURTH, "广场二"), (SQUARE, OTHER, "广场")):
        transport.places[cid] = ChannelPlace(guild_id=guild, name=name, public=True)
        transport.names[cid] = name
    transport.guild_names.update({THIRD: third, FOURTH: "第四服"})
    transport.joined = [OTHER, THIRD, FOURTH]
    same_group(engine, GUILD, OTHER, FOURTH)


async def brought_in_from_outside(engine, clock):
    """主服 #闲聊 + 别的服 #大厅连通（同一个服务器组，两边都开了“跨服务器”）。阿明在组外的第三服 #广场跟它聊过，回到 #闲聊叫它：
    那段近况带进了共用的记录。返回连通组。"""
    sync = connect(engine)
    await say(engine, clock, 1, f"@小D {OUTSIDE}", channel=ARENA)
    await say(engine, clock, 2, "@小D 我回主服了")
    return sync


class OutsideServersNamedLikeTheGroup(unittest.IsolatedAsyncioTestCase):
    async def joined_by_the_fourth_server(self, third, how="bot 还在第三服"):
        """组外的第三服叫 third。阿明从那边带来的近况进了共用的记录以后，第四服 #广场二连进来，小雪在那边叫它。
        返回 (共用的记录, 小雪那一轮模型看到的)。"""
        engine, llm, transport, clock = world(replies())
        four_servers(engine, transport, third=third)
        sync = await brought_in_from_outside(engine, clock)
        self.assertIn(OUTSIDE, record(engine, sync))  # 对照：组里的服务器都开了“跨服务器”，组外的近况带进来了
        if how == "bot 已经退出了第三服":
            transport.joined.remove(THIRD)  # 只记得阿明刚才在那边跟它聊过
        elif how == "在第三服聊的是好几个小时以前的事":
            clock.advance(7 * 3600)  # bot 还在第三服；跟它聊过的过了几个小时就不再带了（也不记得了），记录里的还在
            await engine.tick()
            await engine.drain()
            self.assertNotIn(THIRD, {guild for guild, _ in engine.interactions})
        connect(engine, PLAZA, CHANNEL)  # 第四服 #广场二连进来：没开“跨服务器”
        await say(engine, clock, 3, "@小D 你们刚才聊了什么", channel=PLAZA, author=31, name="小雪")
        return record(engine, sync), history_of(llm)

    async def test_an_outside_server_with_a_group_servers_name_still_loses_its_recents(self):
        for how in ("bot 还在第三服", "bot 已经退出了第三服", "在第三服聊的是好几个小时以前的事"):
            with self.subTest(how):
                whole, seen = await self.joined_by_the_fourth_server("别的服", how)  # 组外的第三服也叫“别的服”
                self.assertNotIn(OUTSIDE, whole)  # 和组里的别的服分不出来：也拿掉
                self.assertNotIn(OUTSIDE, seen)  # 第四服 #广场二被叫到时看不到（那边平时看不到组外的）

    async def test_an_outside_server_whose_name_imitates_the_header_still_loses_its_recents(self):
        # 标题行成了“在「主服」服务器的 #大厅 在「主服」服务器的 #广场”“在「主服」服务器的 #后院」服务器的 #广场”
        for third in ("主服」服务器的 #大厅 在「主服", "主服」服务器的 #后院"):
            with self.subTest(third):
                whole, seen = await self.joined_by_the_fourth_server(third)
                self.assertNotIn(OUTSIDE, whole)  # 取出来的整个名字不是组里的哪个服务器：拿掉
                self.assertNotIn(OUTSIDE, seen)


class MemberServersThatTurnedCrossServerOff(unittest.IsolatedAsyncioTestCase):
    async def test_a_join_from_a_server_already_in_the_group_keeps_what_the_record_holds(self):
        for newcomer, how in ((LOBBY, "主服 #大厅：主服开着“跨服务器”"), (SQUARE, "别的服 #广场：别的服自己关掉了")):
            with self.subTest(how):
                engine, llm, transport, clock = world()
                four_servers(engine, transport)
                sync = await brought_in_from_outside(engine, clock)
                engine.settings.set("guild", OTHER, "cross_server", False)  # 别的服自己关掉了“跨服务器”：已经看过的照旧留着
                connect(engine, newcomer, CHANNEL)  # 组里原来就有的服务器又连进来一个频道：没带来新的服务器
                self.assertIn(OUTSIDE, record(engine, sync))


class LinkCommandAfterAMemberTurnedCrossServerOff(unittest.IsolatedAsyncioTestCase):
    """/dsowner chatsync 连通（借 test_chatsync.OwnerCommand 的假 Discord，加上第四服）。主服 #闲聊 + 别的服 #大厅连通时带进了
    组外的近况，后来别的服自己关掉了“跨服务器”。"""

    interaction = test_chatsync.OwnerCommand.interaction

    def setUp(self):
        test_chatsync.OwnerCommand.setUp(self)
        fourth = NS(id=FOURTH, name="第四服", me=NS(), text_channels=[])
        plaza = text_channel(fourth, PLAZA, "广场二")
        known_channel, known_guild = self.bot.get_channel, self.bot.get_guild
        self.bot.get_channel = lambda cid: plaza if cid == PLAZA else known_channel(cid)
        self.bot.get_guild = lambda gid: fourth if gid == FOURTH else known_guild(gid)
        self.bot.guilds.append(fourth)
        four_servers(self.engine, self.transport)

    async def link(self, newcomer):
        """bot 主人把 newcomer 连进组里（确认框照常点确认）。返回 (确认框说要拿掉, 连好以后说拿掉了, 连通组)。"""
        from dsbot.commands import sync_command

        sync = await brought_in_from_outside(self.engine, self.clock)
        self.engine.settings.set("guild", OTHER, "cross_server", False)  # 别的服自己关掉了“跨服务器”
        ask = self.interaction()
        await sync_command(self.bot, ask, "link", str(newcomer), str(CHANNEL))
        preview, view, _ = ask.response.sent[0]
        click = self.interaction()
        await view.confirm(click)
        result = click.done[-1]
        self.assertIn("已经连通：", result)
        warned = "所在的服务器没开“跨服务器”：共用记录里从服务器组外带来的近况" in preview
        removed = "拿掉了从服务器组外带来的近况" in result
        return warned, removed, sync

    async def test_a_join_from_a_server_already_in_the_group_is_neither_announced_nor_scrubbed(self):
        warned, removed, sync = await self.link(LOBBY)  # 主服 #大厅
        self.assertEqual((warned, removed), (False, False))  # 什么都不用拿：确认框不提，连好以后也不说拿掉了
        self.assertIn(OUTSIDE, record(self.engine, sync))

    async def test_a_newcomer_server_with_cross_server_off_is_announced_and_scrubbed(self):
        warned, removed, sync = await self.link(PLAZA)  # 第四服 #广场二：组里原来没有、没开“跨服务器”
        self.assertEqual((warned, removed), (True, True))  # 确认框先说一声，连好以后说拿掉了几条
        self.assertNotIn(OUTSIDE, record(self.engine, sync))


def echoing_summary(table):
    """聊天照 answering(table) 回；压缩摘要把给它看的聊天原样写成摘要（摘要里怎么叫它一看就知道）。"""
    chat = answering(table)

    def responder(call):
        if not call["json_mode"] and not is_review(call):
            return text_of(call["messages"][-1]["content"]).split("【聊天记录】\n", 1)[-1]
        return chat(call)

    return responder


SAYING = {"周六唱歌吗": PROMISE, "这边": "主服好"}


class TheBotInTheSharedSummary(unittest.IsolatedAsyncioTestCase):
    async def test_the_shared_summary_calls_it_bot_and_every_channel_is_told_that_is_itself(self):
        engine, llm, transport, clock = world(echoing_summary(SAYING))
        real = transport.channel_info
        transport.channel_info = lambda cid: replace(real(cid), bot_name="阿狸") if cid == HALL else real(cid)  # 它在别的服叫阿狸
        connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        await say(engine, clock, 1, "@阿狸 周六唱歌吗", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 主服这边")
        await compact(engine, CHANNEL)  # 主服这一轮压缩共用的记录（它在主服叫小D）
        system, given = (text_of(m["content"]) for m in summary_calls(llm)[-1]["messages"])
        self.assertIn(f"{BOT}：{PROMISE}", given)  # 给写摘要的看的聊天里，它说的一律写成「bot」说的
        self.assertNotIn(f"小D：{PROMISE}", given)
        self.assertIn(f"「{BOT}」就是它自己", system)
        self.assertIn(f"一律叫它「{BOT}」", system)  # 连通的那句说明也让写摘要的这么叫它
        await say(engine, clock, 3, "@阿狸 你答应过我什么", channel=HALL, author=21, name="小红")
        prompt = chats(llm)[-1]["messages"][0]["content"]
        self.assertIn("「阿狸」", prompt)  # 别的服 #大厅里告诉它，它叫阿狸……
        self.assertIn(f"{BOT}：{PROMISE}", prompt)  # ……摘要里它答应过的事写成「bot」说的
        self.assertIn(f"摘要里的「{BOT}」就是你", prompt)  # 也告诉它「bot」就是它自己
        self.assertNotIn(f"小D：{PROMISE}", prompt)  # 不会当成是别人（小D）答应的

    async def test_control_an_unlinked_channels_summary_still_calls_it_by_its_nickname(self):
        # 对照（control）：没连通的频道（默认模式，和 2.7.1 一样）：摘要里照旧用它在这个服务器的昵称，也不提「bot」
        engine, llm, transport, clock = world(echoing_summary(SAYING))
        await say(engine, clock, 1, "@小D 周六唱歌吗")
        await say(engine, clock, 2, "@小D 主服这边")
        await compact(engine, CHANNEL)
        system, given = (text_of(m["content"]) for m in summary_calls(llm)[-1]["messages"])
        self.assertIn(f"小D：{PROMISE}", given)
        self.assertIn("「小D」就是它自己", system)
        await say(engine, clock, 3, "@小D 你答应过我什么")
        prompt = chats(llm)[-1]["messages"][0]["content"]
        self.assertIn(f"小D：{PROMISE}", prompt)
        self.assertNotIn(f"「{BOT}」就是你", prompt)


if __name__ == "__main__":
    unittest.main()
