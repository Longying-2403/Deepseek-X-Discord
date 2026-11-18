"""2.8.0 连通的频道，第八轮评审修掉的隐私问题：暂停着的频道里等链接预览时被改了的消息、问模型出错的那一轮留下的编号、
跨服务器的连通里改长期记忆的动作、近况的标题行里写的服务器名、退出了服务器组的服务器在共用记录里留下的东西，还有共用记录的
摘要里怎么叫它。

- 连通的频道变成私密的（连通暂停）、关掉了“允许回应”以后，正在处理、还在等链接预览（还没编号）的那条被本人改了：这一轮不写
  它（改之前的、改过的都不写；叫它的只有这一条的话这一轮就不跑，别的留着以后看），缓冲区里也拿掉，又公开了也不会跟着那里的
  下一轮写进去。
- 连通组的记录：同一个人两分钟内发的文件、链接，只有在这一轮里、或者已经记进这份记录的才直接读。问模型出错的那一轮给那一批
  编了号却没记进记录：暂停着的频道里那条的文件、链接照样不读进来。
- 和别的服务器的频道连通（跨服务器）：它回话里改长期记忆的动作不留在共用的记录里（里面是这个服务器那一条记忆的原文；人设卡
  共用不共用都一样），同一个服务器的连通后来跨服务器了也拿掉。同一个服务器的连通照旧记着。
- 连成一组的服务器里的频道连通，连进来没开“跨服务器”的服务器的频道：写着是哪个服务器的近况都拿掉——写下的服务器名分不出当时
  是组里还是组外的：组外的服务器当时和组里的同名、后来改了名或者 bot 退出了那边的，也一样拿掉。
- 已经断开的频道，它的服务器后来退出了服务器组（bot 也被移出了那边、那边的频道都找不到了也算）：它那几轮里只有那边能看的
  （别的频道的近况、能翻的频道名单、翻到的记录、附着的和查到的人设卡，回话里翻记录、查人设卡、改长期记忆的动作）退组时就拿掉，
  说过的话照旧；退组时没查成的，下次有频道连进来时、下次启动时再查。服务器还在组里的、断开了的频道，它那几轮照旧。
- 共用记录的摘要里它叫 prompts.SYNC_BOT_NAME（〔本 bot〕）：群友、别的 bot 的名字里的〔〕都会被换掉，谁的名字都不会是这个，
  别的 bot 在记录里标的〔bot〕也不是。"""

import unittest
from types import SimpleNamespace as NS
from unittest import mock

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, replies, say, text_of, world
from test_chatsync_lifecycle import answering
from test_chatsync_privacy import ARENA, GAMES, THIRD, history_of, record
from test_chatsync_r2_privacy import same_group
from test_chatsync_r2_records import compact, files_read
from test_chatsync_r3_entries import links_opened
from test_chatsync_r3_privacy import when_asked
from test_chatsync_r4_scrub import kinds_in_replies, writes_fail
from test_chatsync_r6_privacy import FOURTH, PLAZA, four_servers
from test_chatsync_review import summary_calls
from test_dm import OTHER
from test_engine_files import Reader, doc
from test_links_images import FakeReader, link_msg
from test_memory import ACTIONS, REPLY, is_review, stores

from dsbot import prompts
from dsbot.engine import EMBED_WAIT
from dsbot.llm import LLMError
from dsbot.models import ChannelPlace

try:
    from dsbot.commands import start_groups
except ImportError:  # 没装 discord.py
    start_groups = None

BACK = 301  # 第三服的公开频道 #后台
GIF = "https://tenor.com/view/cat-1"  # 要等预览的链接
PHONE = "13800001111"  # 小红在 #大厅说的手机号（#大厅暂停以后她把它改掉了）
SECRET_PAGE = "https://docs.example.com/hall-plan"  # 小红在 #大厅发的链接
OUTSIDE = "第三服周末去团建"  # 阿明在服务器组外的第三服 #广场跟它说的
ENTRY = "管理组：下周三封禁小王"  # 主服的第 1 条长期记忆（在私密的 #管理组 里记下的）
CHANGED = "管理组：下周五封禁小王"  # 阿明让它改成的
EDIT_MEMORY = ACTIONS({"type": "memory", "edit": 1, "text": CHANGED}, REPLY(None, "改好了"))
WHISPER = "后台在商量换版主"  # 阿明在第三服 #后台 跟它说的（只有第三服能看）
LOOKUPS = ACTIONS({"type": "recall", "channel": "后台"}, {"type": "who", "name": "小雨"})  # 翻 #后台 的记录、查小雨的卡
NOTED = ACTIONS({"type": "memory", "add": "第三服下个月换版主"}, REPLY(None, "知道了"))  # 翻完以后记一条长期记忆，回一句
MOD_CARD = "小雨是第三服的版主"  # 小雨的人设卡


def hers(mid, text, **kw):
    """小红在别的服 #大厅发的一条。"""
    return msg(mid, text, channel_id=HALL, guild_id=OTHER, author=21, name="小红", **kw)


def set_public(transport, public):
    """别的服把 #大厅改成私密的（连通暂停：它不看那里的消息），或者又改回公开的。"""
    transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=public)


def stop_reading(engine, transport, how):
    """它不再看别的服 #大厅：变成私密的，或者那里的管理员关了“允许回应”（/dsadmin 区域）。"""
    if how == "变成私密的":
        set_public(transport, False)
    else:
        engine.settings.set("channel", HALL, "enabled", False)


def start_reading(engine, transport, how):
    """又公开了（又打开了）。"""
    if how == "变成私密的":
        set_public(transport, True)
    else:
        engine.settings.unset("channel", HALL, "enabled")


def failing_once(words):
    """叫它的这一轮话里有 words：第一次问模型出错（503，DeepSeek 那边暂时不行）；别的照 replies() 回。"""
    others = replies()
    failed = []

    def responder(call):
        if call["json_mode"] and not is_review(call) and not failed and words in text_of(call["messages"][-1]["content"]):
            failed.append(call)
            return LLMError("服务暂时不可用", 503)
        return others(call)

    return responder


def bot_left(transport, guild, channels=()):
    """bot 被移出了那个服务器：服务器名取不到了（DiscordTransport.guild_name 是 None，给模型看的只剩编号），那边的频道
    也找不到了（locate 是 None）。"""
    known_name, known_place = transport.guild_name, transport.locate
    transport.guild_name = lambda g: None if g == guild else known_name(g)
    transport.locate = lambda cid: None if cid in channels else known_place(cid)
    transport.joined.remove(guild)


class EditedWhileItsRoundWaitsForPreviews(unittest.IsolatedAsyncioTestCase):
    async def test_a_message_edited_while_paused_before_its_round_numbered_it_is_never_written(self):
        for how in ("变成私密的", "关掉了允许回应"):
            with self.subTest(how):
                engine, llm, transport, clock = world()
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                waited = []

                async def sleep(seconds):
                    # #大厅这一轮在等那条的链接预览（还没给消息编号）：这时候它不再看 #大厅了，小红把号码改掉
                    if seconds == EMBED_WAIT and not waited:
                        waited.append(seconds)
                        stop_reading(engine, transport, how)
                        engine.handle_edit(hers(2, f"@小D 看这个 {GIF} 我的手机号是（不说了）", mentions_bot=True), edited=True)

                engine.sleep = sleep
                await engine.handle_message(msg(1, "我先吃饭去了", channel_id=HALL, guild_id=OTHER, author=22, name="小蓝"))
                await engine.handle_message(hers(2, f"@小D 看这个 {GIF} 我的手机号是 {PHONE}", mentions_bot=True, awaiting_embeds=True))
                await engine.drain()
                self.assertTrue(waited)  # 对照：这一轮等了预览
                whole = record(engine, sync)
                self.assertNotIn(PHONE, whole)  # 改之前的不写进去……
                self.assertNotIn("不说了", whole)  # ……它不看那里以后改成的也不写
                self.assertEqual(chats(llm), [])  # 叫它的就那一条：这一轮不跑（小蓝那句留着以后看）
                start_reading(engine, transport, how)  # 又公开了（又打开了）
                await say(engine, clock, 3, "@小D 在吗", channel=HALL, author=22, name="小蓝")  # 小蓝在 #大厅叫它
                whole = record(engine, sync)
                self.assertIn("小蓝：我先吃饭去了", whole)
                self.assertIn("小蓝：@小D 在吗", whole)
                self.assertNotIn(PHONE, whole)  # 那条也不在缓冲区里了：不跟着那里的下一轮写进去
                self.assertNotIn(PHONE, history_of(llm))


def carrying(mid, kind, what, text, **kw):
    """小红在 #大厅发的带链接（kind 是“链接”：what 是网址）或者带文件（what 是文件名）的一条。"""
    if kind == "链接":
        return link_msg(mid, f"{text} {what}", [what], channel_id=HALL, guild_id=OTHER, author=21, name="小红", **kw)
    return hers(mid, text, files=[doc(what)], **kw)


def reading_world(responder=None):
    """能打开链接、读文件的 world，主服 #闲聊 + 别的服 #大厅连通。返回 (engine, transport, clock, 连通组, opened)：
    opened(clear=False) 是打开过的链接和读过的文件（clear：看完清掉）。"""
    links, files = FakeReader(), Reader()
    engine, llm, transport, clock = world(responder or replies(), link_reader=links)
    engine.file_reader = files

    def opened(clear=False):
        done = links_opened(links) + files_read(files)
        if clear:
            links.calls.clear()
            files.calls.clear()
        return done

    return engine, transport, clock, connect(engine), opened


class AutoReadsAfterAFailedRound(unittest.IsolatedAsyncioTestCase):
    async def test_a_paused_channels_file_or_link_is_not_read_in_although_its_failed_round_numbered_it(self):
        for kind, what, content in (("链接", SECRET_PAGE, f"{SECRET_PAGE} 的正文"), ("文件", "裁员名单.docx", "裁员名单.docx 的内容")):
            with self.subTest(kind):
                engine, transport, clock, sync, opened = reading_world(failing_once("看看这个"))
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await engine.handle_message(carrying(2, kind, what, "@小D 看看这个", mentions_bot=True))
                    await engine.drain()  # 小红在 #大厅叫它：这一轮问模型出错了，那一批编了号（#1），没记进记录
                self.assertNotIn("看看这个", record(engine, sync))  # 对照：没记进去，还在 #大厅的缓冲区里
                opened(clear=True)
                set_public(transport, False)  # 这时候别的服把 #大厅改成了私密的
                clock.advance(30)
                await say(engine, clock, 3, "@小D 在吗", author=21, name="小红")  # 两分钟内小红在主服 #闲聊叫它
                self.assertEqual(opened(), [])  # 不在这一轮里，也没记进这份记录：不直接读进来
                self.assertNotIn(content, record(engine, sync))

    async def test_control_the_same_persons_file_or_link_in_this_rounds_batch_is_still_read(self):
        # 对照（control）：连通组的记录里，同一个人刚发的、和叫它的那条在同一批里的（这一轮才记进记录）照样直接读
        for kind, what, content in (("链接", SECRET_PAGE, f"{SECRET_PAGE} 的正文"), ("文件", "周报.docx", "周报.docx 的内容")):
            with self.subTest(kind):
                engine, transport, clock, sync, opened = reading_world()
                await engine.handle_message(carrying(2, kind, what, "看看这个"))  # 小红在 #大厅随手发的（没叫它）
                await say(engine, clock, 3, "@小D 你看看", channel=HALL, author=21, name="小红")  # 接着叫它：两条在同一批里
                self.assertEqual(opened(), [what])
                self.assertIn(content, record(engine, sync))


class MemoryEditsAcrossServers(unittest.IsolatedAsyncioTestCase):
    async def memory_edited(self, other, *, shared_cards=False):
        """主服 #闲聊 和 other 连通；阿明在 #闲聊 让它改主服的长期记忆，它照做了。返回 (engine, llm, clock, 连通组)。"""
        cards, memory = stores()
        memory.add(GUILD, ENTRY, via="chat")
        engine, llm, transport, clock = world(replies(EDIT_MEMORY), cards=cards, memory=memory)
        if shared_cards:
            cards.set_shared(True, scope_of=engine.store.memory_scope, alive=set())
        sync = connect(engine, CHANNEL, other)
        await say(engine, clock, 1, "@小D 那件事改到周五了，帮我把记忆改一下")
        self.assertEqual([m.text for m in memory.entries(GUILD)], [CHANGED])  # 对照：主服的长期记忆照样改了
        return engine, llm, clock, sync

    async def test_another_servers_channel_never_sees_the_text_of_this_servers_memory(self):
        for cards in ("人设卡各服务器一张", "人设卡所有服务器共用"):
            with self.subTest(cards):
                engine, llm, clock, sync = await self.memory_edited(HALL, shared_cards=cards == "人设卡所有服务器共用")
                whole = record(engine, sync)
                self.assertNotIn("封禁小王", whole)  # 记下的回话里没有改长期记忆的动作（压缩时也就进不了摘要）
                self.assertIn("改好了", whole)  # 它说的话照旧
                await say(engine, clock, 2, "@小D 你们那边最近怎么样", channel=HALL, author=21, name="小红")
                self.assertNotIn("封禁小王", history_of(llm))  # 别的服 #大厅被叫到时看不到主服那条记忆

    async def test_its_memory_edits_leave_the_record_when_the_link_becomes_cross_server(self):
        engine, llm, clock, sync = await self.memory_edited(LOBBY)  # 先是主服的两个频道连通
        self.assertIn(CHANGED, record(engine, sync))  # 对照：同一个服务器的连通照旧记着（本来就是这个服务器的记忆）
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅连进来：跨服务器了
        whole = record(engine, sync)
        self.assertNotIn("封禁小王", whole)
        self.assertIn("改好了", whole)
        await say(engine, clock, 2, "@小D 你们那边最近怎么样", channel=HALL, author=21, name="小红")
        self.assertNotIn("封禁小王", history_of(llm))


class RecentsUnderAServerName(unittest.IsolatedAsyncioTestCase):
    async def test_an_outside_server_named_like_a_group_server_back_then_still_loses_its_recents(self):
        for how in ("后来改了名", "bot 后来被移出了第三服"):
            with self.subTest(how):
                engine, llm, transport, clock = world()
                four_servers(engine, transport)  # 主服、别的服、第四服连成一个服务器组，第四服没开“跨服务器”；第三服在组外
                transport.joined.append(THIRD)  # bot 也在第三服
                transport.guild_names[THIRD] = "别的服"  # 第三服那时候也叫“别的服”
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                await say(engine, clock, 1, f"@小D {OUTSIDE}", channel=ARENA)
                await say(engine, clock, 2, "@小D 我回主服了")
                self.assertIn("在「别的服」服务器的 #广场", record(engine, sync))  # 对照：组外的近况带进来了，写的是那时候的名字
                if how == "后来改了名":
                    transport.guild_names[THIRD] = "第三服"  # 现在的名字和组里的对不上了
                else:
                    bot_left(transport, THIRD, (ARENA,))  # 现在只剩编号
                connect(engine, PLAZA, CHANNEL)  # 第四服 #广场二连进来：带来了没开“跨服务器”的服务器
                self.assertNotIn(OUTSIDE, record(engine, sync))  # 写着服务器名的近况都拿掉：分不出当时是组里还是组外的
                await say(engine, clock, 3, "@小D 你们刚才聊了什么", channel=PLAZA, author=31, name="小雪")
                self.assertNotIn(OUTSIDE, history_of(llm))  # 第四服 #广场二被叫到时看不到组外的


def third_in_the_group(engine, transport, cards):
    """主服、别的服、第三服连成一个服务器组（第三服开了记忆共享：#广场 那一轮带得到 #后台 的近况、翻得到 #后台 的记录；
    小雨的卡记在组里）。主服 #闲聊、别的服 #大厅、第三服 #广场连通（在一个服务器组里，不算跨服务器）。返回连通组。"""
    for cid, name in ((ARENA, "广场"), (BACK, "后台")):
        transport.places[cid] = ChannelPlace(guild_id=THIRD, name=name, public=True)
        transport.names[cid] = name
    transport.guild_names[THIRD] = "第三服"
    transport.joined.append(THIRD)
    same_group(engine, GUILD, OTHER, THIRD)
    engine.settings.set("guild", THIRD, "share_memory", True)
    cards.put(engine.store.group_of(THIRD), 12, "小雨", MOD_CARD, via="command")
    sync = connect(engine)
    connect(engine, ARENA, CHANNEL)
    return sync


async def talked_in_third(engine, clock):
    """阿明在第三服 #后台 跟它说了悄悄话；小雨在 #广场说了句话，阿明接着在 #广场叫它：那一轮带上了 #后台 的近况、能翻的频道
    名单和小雨的卡，它翻了 #后台 的记录、查了小雨的卡，又记了一条长期记忆（都在共用的记录里）。然后 #广场 断开（记录里留着它说
    过的）。"""
    await say(engine, clock, 11, f"@小D {WHISPER}", channel=BACK)
    await engine.handle_message(msg(12, "大家好", channel_id=ARENA, guild_id=THIRD, author=12, name="小雨"))
    await say(engine, clock, 13, "@小D 我来广场了，那边在聊什么", channel=ARENA)
    disconnect(engine, ARENA)


def third_leaves_the_group(engine):
    """第三服退出了服务器组（/dsowner 服务器组 的退组做完了：照退组之前的组查一遍连通组的记录）。"""
    store = engine.store
    before = store.group_members(store.group_of(THIRD))
    store.start_leaving(THIRD, now=3.0)
    store.unmap(THIRD)
    store.done_leaving(THIRD)
    engine.groups_changed(before, left=True)


def leave_check_fails(engine):
    """第三服退组时查记录出错了（数据库出错，像磁盘满了）：这次没拿掉（出错的那条日志不打出来）。"""
    with writes_fail(engine, "1"), mock.patch("dsbot.engine.log"):
        third_leaves_the_group(engine)


class FormerMembersWhoseServerLeftTheGroup(unittest.IsolatedAsyncioTestCase):
    def world(self):
        cards, memory = stores()
        responder = when_asked({"我来广场了": LOOKUPS, "你翻了 #后台 的记录": NOTED})  # #广场那一轮：先翻、查，再记一条
        engine, llm, transport, clock = world(responder, cards=cards, memory=memory)
        sync = third_in_the_group(engine, transport, cards)
        return engine, llm, transport, clock, sync

    async def test_what_only_its_server_could_see_leaves_the_record_when_the_server_leaves_the_group(self):
        for how in ("bot 还在第三服", "bot 也被移出了第三服"):
            with self.subTest(how):
                engine, llm, transport, clock, sync = self.world()
                await talked_in_third(engine, clock)
                whole = record(engine, sync)
                for words in (WHISPER, "#后台", MOD_CARD):
                    self.assertIn(words, whole)  # 对照：第三服还在组里时照常带进来（近况、名单、翻到的、查到的）
                self.assertEqual(kinds_in_replies(engine, sync), ["recall", "who", "memory", "reply"])  # 对照：它翻了、查了、记了
                if how == "bot 也被移出了第三服":
                    bot_left(transport, THIRD, (ARENA, BACK))  # 那边的频道都找不到了：也当成组外的
                third_leaves_the_group(engine)  # 主服 #闲聊 + 别的服 #大厅还在一个服务器组里
                whole = record(engine, sync)
                self.assertNotIn(WHISPER, whole)  # #后台 的近况、翻到的 #后台 的记录拿掉了
                self.assertNotIn("后台", whole)  # 能翻的频道名单、回话里要翻 #后台 的动作也拿掉
                self.assertNotIn(MOD_CARD, whole)  # 附着的、查到的小雨的卡也一样（人设卡各服务器一张）
                self.assertEqual(kinds_in_replies(engine, sync), ["reply"])  # 回话里翻记录、查人设卡、改长期记忆的动作都没了
                self.assertIn("小雨：大家好", whole)  # 说过的话照旧
                self.assertIn("阿明：@小D 我来广场了，那边在聊什么", whole)
                await say(engine, clock, 14, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
                self.assertNotIn(WHISPER, history_of(llm))  # 别的服 #大厅被叫到时看不到
                self.assertNotIn(MOD_CARD, history_of(llm))

    async def test_a_join_removes_it_when_the_check_at_the_leave_failed(self):
        engine, llm, transport, clock, sync = self.world()
        await talked_in_third(engine, clock)
        leave_check_fails(engine)
        self.assertIn(WHISPER, record(engine, sync))  # 对照：退组时没拿掉
        transport.places[PLAZA] = ChannelPlace(guild_id=FOURTH, name="广场二", public=True)
        transport.names[PLAZA] = "广场二"
        transport.guild_names[FOURTH] = "第四服"
        engine.store.join_group(FOURTH, engine.store.group_of(GUILD), now=4.0)  # 第四服连进服务器组（没开“跨服务器”）
        connect(engine, PLAZA, CHANNEL)  # 它的 #广场二连进来：连好以后再查一遍
        self.assertNotIn(WHISPER, record(engine, sync))
        self.assertNotIn(MOD_CARD, record(engine, sync))
        await say(engine, clock, 14, "@小D 大家好", channel=PLAZA, author=41, name="小紫")
        self.assertNotIn(WHISPER, history_of(llm))  # 第四服 #广场二被叫到时看不到第三服的

    async def test_the_startup_check_removes_it_when_the_check_at_the_leave_failed(self):
        if start_groups is None:
            self.skipTest("没装 discord.py")
        engine, llm, transport, clock, sync = self.world()
        await talked_in_third(engine, clock)
        leave_check_fails(engine)
        self.assertIn(WHISPER, record(engine, sync))  # 对照：退组时没拿掉
        engine.states.clear()
        engine.convs.clear()  # 重启了：内存里什么都没有
        cfg = NS(engine=NS(memory_max_entries=50), bot=NS(shared_cards=False))
        start_groups(NS(store=engine.store, engine=engine, cards=engine.cards, memory=engine.memory, cfg=cfg))  # 启动时查一遍
        self.assertNotIn(WHISPER, record(engine, sync))
        self.assertNotIn(MOD_CARD, record(engine, sync))
        await say(engine, clock, 14, "@小D 你们刚才聊了什么", channel=HALL, author=21, name="小红")
        self.assertNotIn(WHISPER, history_of(llm))

    async def test_control_a_former_member_whose_server_is_still_in_the_group_keeps_its_turns(self):
        # 对照（control）：主服 #大厅也连通过、断开了，主服还在组里：之后再查（启动时、有频道连进来时）它那几轮带来的（主服 #游戏
        # 的近况）照旧；还没连上 Discord、哪个频道都找不到的时候，照它自己那份记录记下的服务器算（第三服退组那一次是整份都查）
        engine, llm, transport, clock, sync = self.world()
        transport.places[GAMES] = ChannelPlace(guild_id=GUILD, name="游戏", public=True)
        transport.names[GAMES] = "游戏"
        engine.settings.set("guild", GUILD, "share_memory", True)  # 主服开了记忆共享：#大厅那一轮带得到 #游戏 的近况
        connect(engine, LOBBY, CHANNEL)
        await say(engine, clock, 21, "@小D 主服游戏开黑", channel=GAMES)
        await say(engine, clock, 22, "@小D 我来大厅了", channel=LOBBY)
        disconnect(engine, LOBBY)
        self.assertEqual(engine.scrub_outsiders(sync), 0)  # 有频道连进来时的那次检查
        locate = transport.locate
        transport.locate = lambda channel_id: None  # 启动时还没连上 Discord：哪个频道都找不到
        try:
            self.assertEqual(engine.scrub_outsiders(sync), 0)
        finally:
            transport.locate = locate
        whole = record(engine, sync, origin=LOBBY)  # 主服 #大厅那几轮
        self.assertIn("（阿明 刚才在 #游戏 跟你聊过：", whole)  # 只拿掉服务器不在组里的：主服的照旧
        self.assertIn("主服游戏开黑", whole)


class TheBotsLabelInTheSharedSummary(unittest.IsolatedAsyncioTestCase):
    async def test_no_member_or_other_bot_in_the_shared_summary_goes_by_the_bots_label(self):
        label = prompts.SYNC_BOT_NAME  # 共用记录的摘要里它的叫法
        engine, llm, transport, clock = world(answering({"周六我请大家吃饭": "好呀，我记下了"}))
        connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        await engine.handle_message(msg(5, "欢迎新人", channel_id=HALL, guild_id=OTHER, author=99, name="MEE6", is_bot=True))
        await say(engine, clock, 6, "@小D 周六我请大家吃饭", channel=HALL, author=21, name=label)  # 别的服的群友的昵称正好是这个
        await say(engine, clock, 7, "@小D 主服这边")
        await compact(engine, CHANNEL)  # 压缩共用的记录
        given = text_of(summary_calls(llm)[-1]["messages"][-1]["content"]).split("【聊天记录】", 1)[-1]
        self.assertIn(f"{label}：好呀，我记下了", given)  # 它说的照旧写成这个叫法
        said = {line.split("：", 1)[1]: line.split("：", 1)[0] for line in given.split("\n") if line.startswith("#")}  # 话 -> 谁说的
        self.assertNotIn(label, said["@小D 周六我请大家吃饭 〔@你〕"])  # 那个群友不是它（名字里的〔〕换掉了）
        self.assertNotIn(label, said["欢迎新人"])  # 别的 bot 也不是（标的是〔bot〕）


if __name__ == "__main__":
    unittest.main()
