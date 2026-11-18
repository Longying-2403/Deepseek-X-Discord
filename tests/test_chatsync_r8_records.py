"""2.8.0 连通的频道，第八轮评审修掉的问题：暂停着的频道里还没处理完的消息又被改了（别的 bot 改的、那一轮没跑成的、那一轮还在
等预览的）、排着队“当成删了”以后公开时又改的，没问模型就记进记录的闲聊被 DeepSeek 拒了，还有一直没有自己一轮的频道里很早的消息
被改了。

- 连通的频道暂停着（变成私密的、关掉了“允许回应”）时，别的 bot 改了它还没处理完的消息：不理（记录本来就不跟着别的 bot 改），
  缓冲区里的照旧是改之前的样子，又公开以后补进共用记录的也是改之前的。还公开时改的照旧换成改过的。
- 正在回的那一轮里的消息，暂停时被本人改了：缓冲区里也拿掉。那一轮没跑成（暂时连不上），它也不会留着再回一次、被别的频道补进
  共用的记录（改之前的、改之后的都不进去）。那一轮还在等预览、还没给它编号的：这一轮就不写进去。
- 暂停时改的在组里那份当成删了，排着队等锁的时候也一样；又公开以后再改：照后来那次写（组里的、断开以后它自己那份都是），排着的
  “当成删了”不是真删。真删了的，之后才到的编辑照旧不算。
- 没问模型就记进记录的那几条（别的连通频道补进来的闲聊、连通时记进它自己那份的闲聊）：带着它们的请求被 DeepSeek 拒了（不是
  太长），换成一句“没有记下来”（开头写着时间、频道的那一行留着），这一批照旧丢掉，之后每个频道照常回。只是暂时出错的照旧留着；
  请求过了的就算数，以后再被拒也不换；太长被拒的照旧压缩，不换。
- 连通组的记录里很早的消息被改了：那个频道一直没有自己的一轮（它的话只是被别的频道补进来）的话，“改成了什么”跟着它下一次补进来
  的那段附上（记成它的），附过了不再附；它暂停着的时候先等着，和它的闲聊一样。"""

import asyncio
import unittest

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, HALL_LABEL, LOBBY, chats, connect, disconnect, say, text_of, world
from test_chatsync_lifecycle import hold, prompt_of, sent, spin
from test_chatsync_privacy import history_of, last_user, record
from test_chatsync_r6_privacy import own_record
from test_chatsync_records import three_linked
from test_chatsync_review import summary_calls
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review

from dsbot import prompts
from dsbot.engine import DELETED_KEEP, EMBED_WAIT
from dsbot.llm import LLMError
from dsbot.models import ChannelPlace
from dsbot.prompts import EDIT_NOTE, MESSAGE_DELETED

# 带着它们的请求被拒了，没问模型就记进记录的那几条换成的这一句（第八轮起是 prompts.PASSIVE_REJECTED）
REJECTED = getattr(prompts, "PASSIVE_REJECTED", "（这里本来有几条消息，DeepSeek 不接受（多半是内容审核），没有记下来。）")
POLL = "投票：周末去哪"  # 投票机（别的 bot）在别的服 #大厅发的，没人叫它
POLL_RESULT = "投票结果：裁掉小王 5 票"  # #大厅不再公开以后，投票机把它改成了这个
ADDRESS = "我家住在幸福路三号"  # 小红在别的服 #大厅叫它时说的
REDACTED = "我家住在……（地址删了）"  # #大厅不再公开以后，她把那条改成了这个
SAID = "@小D 明天开会"  # 小红在别的服 #大厅说的（消息 2）
HIDDEN = "改到周五"  # #大厅不再公开以后，她把那条改成了“明天开会：改到周五”
LATER = "改到周六"  # #大厅又公开以后，她又改成了“明天开会：改到周六”
RISKY = "某个会被内容审核拦下的词"  # 请求里有它，DeepSeek 就拒（400，内容审核）
CHATTER = "周末去不去爬山"  # 没人叫它的一句闲聊（请求里有它照常回）
PHONE = "我家电话是 010-12345678，有事打给我"  # 小红在别的服 #大厅随口说的（那里一直没人叫它）
FIXED = "（电话号码打错了，已删）"  # 那条成了很早的消息以后，她把它改成了这个


def deepseek(fail=None):
    """假的 DeepSeek。聊天：请求里（system prompt、记录、这一轮）有 RISKY 的像内容审核那样拒掉（400）；fail 是 (words, 错误)：
    第一个带着 words 的请求出这个错；别的回一句“在的”。整理记忆什么都不记；压缩摘要把给它看的聊天原样写成摘要。"""
    failed = []

    def responder(call):
        if is_review(call):
            return '{"cards": [], "memory": []}'
        if not call["json_mode"]:
            return text_of(call["messages"][-1]["content"]).split("【聊天记录】\n", 1)[-1]
        if fail is not None and not failed and fail[0] in prompt_of(call):
            failed.append(True)
            return fail[1]
        if RISKY in prompt_of(call):
            return LLMError("Content Exists Risk", status=400)
        return ACTIONS(REPLY(None, "在的"))

    return responder


def hers(mid, text, **kw):
    """小红在别的服 #大厅发的一条。"""
    return msg(mid, text, channel_id=HALL, guild_id=OTHER, author=21, name="小红", **kw)


def poll(text):
    """投票机（别的 bot）在别的服 #大厅发的那条（消息 5）。"""
    return msg(5, text, channel_id=HALL, guild_id=OTHER, author=99, name="投票机", is_bot=True)


def edited(words):
    """小红把她在别的服 #大厅说的那条（消息 2）改成了“明天开会：words”。"""
    return hers(2, f"{SAID}：{words}", mentions_bot=True)


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


def user_turns(engine, key, guild=0, origin=None):
    """这份记录里每一轮群友说的（origin：只看这个频道的那几轮；连通组的记录 guild 是 0）。"""
    turns = engine.store.load_conversation(key, guild).turns
    return [text_of(t.content) for t in turns if t.role == "user" and (origin is None or t.origin == origin)]


def purge(engine, guild, channel, start=50_000):
    """管理员在这个频道一次删了很多条（Discord 一批最多删 100 条）：一共 DELETED_KEEP 条，都不在记录里。"""
    end = start + DELETED_KEEP
    for first in range(start, end, 100):
        engine.handle_delete(guild, channel, range(first, min(first + 100, end)))


class AnotherBotsEditsWhilePaused(unittest.IsolatedAsyncioTestCase):
    async def test_another_bots_edit_made_while_it_stopped_reading_the_channel_is_ignored(self):
        for how in ("变成私密的", "关掉了允许回应"):
            with self.subTest(how):
                engine, llm, transport, clock = world()
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                await engine.handle_message(poll(POLL))  # 别的 bot 的消息只当上下文：没人叫它，在 #大厅的缓冲区里
                stop_reading(engine, transport, how)
                engine.handle_edit(poll(POLL_RESULT), edited=True)  # 那以后投票机把它改了
                start_reading(engine, transport, how)  # 又公开了（又打开了），这中间那里没人说话
                await say(engine, clock, 6, "@小D 大家在聊什么")  # 主服 #闲聊：先把 #大厅缓冲区里的补进共用的记录
                whole = record(engine, sync)
                self.assertNotIn("裁掉小王", whole)  # 暂停时改成的样子没进共用的记录
                self.assertNotIn("裁掉小王", history_of(llm))
                self.assertIn(f"投票机 〔bot〕：{POLL}", whole)  # 缓冲区里的照旧是改之前的

    async def test_control_another_bots_edit_while_the_channel_is_public_is_used_as_before(self):
        # 对照（control）：#大厅还公开的时候投票机改的：缓冲区里那条换成改过的样子，补进共用的记录（不是连通的 bot 编辑全都不理）
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await engine.handle_message(poll(POLL))
        engine.handle_edit(poll(POLL_RESULT), edited=True)
        await say(engine, clock, 6, "@小D 大家在聊什么")
        self.assertIn(f"投票机 〔bot〕：{POLL_RESULT}", record(engine, sync))


class InFlightMessagesEditedWhilePaused(unittest.IsolatedAsyncioTestCase):
    async def test_a_message_edited_while_paused_during_a_round_that_failed_is_neither_answered_later_nor_absorbed(self):
        for later in ("主服 #闲聊叫它：#大厅的补进来", "#大厅又有人叫它：它自己下一轮"):
            with self.subTest(later):
                engine, llm, transport, clock = world(deepseek(fail=(ADDRESS, LLMError("连不上", 503))))
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                gate = asyncio.Event()
                hold(llm, {ADDRESS: gate})
                await engine.handle_message(hers(2, f"@小D {ADDRESS}", mentions_bot=True))
                await spin()  # 小红叫它：#大厅这一轮在等模型回话
                turn_private(transport)  # 这时候别的服把 #大厅改成了私密的
                engine.handle_edit(hers(2, f"@小D {REDACTED}", mentions_bot=True), edited=True)  # 她把地址删了
                gate.set()
                await engine.drain()
                llm.hook = None
                self.assertEqual(len(chats(llm)), 1)  # 对照：那一轮问了模型，暂时连不上（503），什么都没写
                self.assertEqual(user_turns(engine, sync), [])
                turn_public(transport)  # 又公开了，这中间那里没人说话
                clock.advance(120)
                if later.startswith("主服"):
                    await say(engine, clock, 3, "@小D 在吗")
                else:
                    await say(engine, clock, 3, "@小D 在吗", channel=HALL, author=22, name="小蓝")
                self.assertEqual(len(sent(transport)), 1)  # 对照：这一轮照常回了
                whole = record(engine, sync)
                for text in ("幸福路三号", "地址删了"):
                    self.assertNotIn(text, whole)  # 改之前的、改之后的都不进共用的记录
                    self.assertNotIn(text, history_of(llm))

    async def test_a_message_edited_while_paused_as_its_round_waits_for_previews_is_left_out_of_the_shared_record(self):
        engine, llm, transport, clock = world(deepseek())
        sync = connect(engine)
        waited = []

        async def preview_wait(seconds):
            # 叫它的那条带着的图（GIF）预览还没到：这一轮先等一会儿，这时候 #大厅改成了私密的，她把地址删了
            if seconds == EMBED_WAIT and not waited:
                waited.append(seconds)
                turn_private(transport)
                engine.handle_edit(hers(2, f"@小D {REDACTED}", mentions_bot=True), edited=True)

        engine.sleep = preview_wait
        await engine.handle_message(hers(2, f"@小D {ADDRESS}", mentions_bot=True, awaiting_embeds=True))
        await engine.drain()
        self.assertEqual(waited, [EMBED_WAIT])  # 对照：这一轮等了预览
        whole = record(engine, sync)
        for text in ("幸福路三号", "地址删了"):
            self.assertNotIn(text, whole)  # 还没编号：这一轮不写进去（写进去的就是改之前的）
        self.assertEqual(chats(llm), [])  # 叫它的就这一条：这一轮不跑
        turn_public(transport)
        await say(engine, clock, 3, "@小D 在吗")  # 又公开以后主服 #闲聊叫它：也不补进来
        whole = record(engine, sync)
        for text in ("幸福路三号", "地址删了"):
            self.assertNotIn(text, whole)
            self.assertNotIn(text, history_of(llm))


class PublicEditsAfterAQueuedHide(unittest.IsolatedAsyncioTestCase):
    async def test_an_edit_made_once_it_is_public_again_replaces_the_hide_that_waits_for_the_lock(self):
        for where, how in (
            ("组里别的频道这一轮拿着锁", "变成私密的"),
            ("它自己这一轮正在问模型", "变成私密的"),
            ("它自己这一轮正在问模型", "关掉了允许回应"),
        ):
            with self.subTest(where=where, how=how):
                engine, llm, transport, clock = world(deepseek())
                sync = three_linked(engine)  # 主服 #闲聊、别的服 #大厅、主服 #大厅：别的服 #大厅断开以后组还在
                gate = asyncio.Event()
                if where == "组里别的频道这一轮拿着锁":
                    await say(engine, clock, 2, SAID, channel=HALL, author=21, name="小红")  # 记进了组里那份：#1
                    hold(llm, {"等一下": gate})
                    await engine.handle_message(msg(3, "@小D 等一下", mentions_bot=True))  # 阿明这一轮拿着组的锁，在等模型回话
                else:
                    hold(llm, {"明天开会": gate})
                    await engine.handle_message(hers(2, SAID, mentions_bot=True))  # 小红这一轮在等模型回话
                await spin()
                stop_reading(engine, transport, how)
                engine.handle_edit(edited(HIDDEN), edited=True)  # 暂停时改的：组里那份当成删了，排着队等锁
                await spin()
                start_reading(engine, transport, how)
                engine.handle_edit(edited(LATER), edited=True)  # 又公开以后她又改了一次
                await spin()
                gate.set()
                await engine.drain()
                llm.hook = None
                whole = record(engine, sync)
                self.assertIn(f"{SAID}：{LATER}", whole)  # 照后来那次写：排着的“当成删了”不是真删
                self.assertNotIn(MESSAGE_DELETED, whole)
                self.assertNotIn(HIDDEN, whole)
                self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
                await engine.drain()
                own = own_record(engine, HALL, OTHER)
                self.assertIn(f"{SAID}：{LATER}", own)  # 它自己那份拿回去的也是后来那次
                self.assertNotIn(MESSAGE_DELETED, own)
                self.assertNotIn(HIDDEN, own)

    async def test_control_an_edit_that_arrives_after_a_real_delete_is_still_ignored(self):
        # 对照（control）：排着队的是真删（暂停时改过、后来又删了），之后才到的编辑照旧不算（记着的删除被别的删除挤掉了也一样）
        engine, llm, transport, clock = world(deepseek())
        sync = three_linked(engine)
        await say(engine, clock, 2, SAID, channel=HALL, author=21, name="小红")
        gate = asyncio.Event()
        hold(llm, {"等一下": gate})
        await engine.handle_message(msg(3, "@小D 等一下", mentions_bot=True))  # 阿明这一轮拿着组的锁
        await spin()
        turn_private(transport)
        engine.handle_edit(edited(HIDDEN), edited=True)  # 暂停时改的：排着队“当成删了”
        engine.handle_delete(OTHER, HALL, [2])  # 她又真的删了
        purge(engine, OTHER, HALL)  # 别的服的管理员又在 #大厅一次删了很多条：记着的删除里没有那条了
        turn_public(transport)
        engine.handle_edit(edited(LATER), edited=True)  # 删掉以后才到的一次编辑
        await spin()
        gate.set()
        await engine.drain()
        llm.hook = None
        whole = record(engine, sync)
        self.assertIn(f"#1 小红：{MESSAGE_DELETED}", whole)
        self.assertNotIn(LATER, whole)
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
        await engine.drain()
        own = own_record(engine, HALL, OTHER)
        self.assertIn(f"#1 小红：{MESSAGE_DELETED}", own)
        self.assertNotIn(LATER, own)
        self.assertNotIn(HIDDEN, own)


class ChatterRecordedWithoutAskingTheModel(unittest.IsolatedAsyncioTestCase):
    async def test_absorbed_chatter_that_deepseek_rejects_is_replaced_and_every_channel_is_answered_again(self):
        for there in (False, True):
            with self.subTest("别的服 #大厅接着叫它" if there else "主服 #闲聊接着叫它"):
                engine, llm, transport, clock = world(deepseek())
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                await say(engine, clock, 1, f"我随口说了{RISKY}", channel=HALL, author=21, name="小红", call=False)  # 没人叫它
                await say(engine, clock, 2, "@小D 有人吗")  # 主服 #闲聊：先把那条补进共用的记录，这次请求被拒了
                self.assertEqual(sent(transport), [])  # 对照：被拒了，没回
                if there:
                    await say(engine, clock, 3, "@小D 大厅这边呢", channel=HALL, author=22, name="小蓝")
                else:
                    await say(engine, clock, 3, "@小D 还在吗")
                self.assertEqual(sent(transport), [(HALL if there else CHANNEL, "在的")])  # 照常回：不会每一轮都被拒
                first = user_turns(engine, sync, origin=HALL)[0].split("\n")  # 补进来的那段
                self.assertIn(HALL_LABEL, first[0])  # 开头写着时间、频道的那一行留着……
                self.assertEqual(first[1:], [REJECTED])  # ……那几条换成一句“没有记下来”
                whole = record(engine, sync)
                self.assertNotIn(RISKY, whole)
                self.assertNotIn("有人吗", whole)  # 对照：被拒的那一批照旧丢掉（不再问一次）
                self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))

    async def test_chatter_settled_at_link_time_that_deepseek_rejects_is_replaced_in_its_own_record(self):
        engine, llm, transport, clock = world(deepseek())
        await say(engine, clock, 1, f"我随口说了{RISKY}", author=21, name="小红", call=False)  # 主服 #闲聊（还没连通），没人叫它
        sync = connect(engine)  # 连通：那条记进 #闲聊自己那份（不进共用的），它那份的概要 #闲聊每一轮都带着
        await say(engine, clock, 2, "@小D 有人吗")  # 这次请求被拒了
        self.assertEqual(sent(transport), [])  # 对照：被拒了，没回
        await say(engine, clock, 3, "@小D 还在吗")
        self.assertEqual(sent(transport), [(CHANNEL, "在的")])  # 照常回：不会每一轮都被拒
        settled = user_turns(engine, CHANNEL, GUILD)[-1].split("\n")  # 连通时记进它自己那份的那段
        self.assertRegex(settled[0], r"^【.+】$")  # 开头写着时间的那一行留着……
        self.assertEqual(settled[1:], [REJECTED])  # ……那几条换成一句“没有记下来”
        self.assertNotIn(RISKY, own_record(engine, CHANNEL, GUILD))
        self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))
        self.assertNotIn(RISKY, record(engine, sync))  # 对照：共用的记录里本来就没有

    async def test_control_chatter_that_met_only_a_transient_error_stays_and_a_request_that_went_through_vets_it(self):
        # 对照（control）：只是暂时连不上（503）的，那几条照旧留着；带着它们的请求过了就算数，以后这个频道自己的一批被拒，也不换
        for settled in (False, True):
            with self.subTest("连通时记进 #闲聊自己那份的" if settled else "从别的服 #大厅补进来的"):
                engine, llm, transport, clock = world(deepseek(fail=("有人吗", LLMError("连不上", 503))))
                if settled:
                    await say(engine, clock, 1, CHATTER, author=21, name="小红", call=False)
                    connect(engine)
                    where = (CHANNEL, GUILD)  # 记在 #闲聊自己那份
                else:
                    where = (connect(engine),)  # 记在共用的那份
                    await say(engine, clock, 1, CHATTER, channel=HALL, author=21, name="小红", call=False)
                await say(engine, clock, 2, "@小D 有人吗")  # 暂时连不上：不是那几条的问题
                self.assertEqual(sent(transport), [])
                self.assertIn(CHATTER, "\n".join(user_turns(engine, *where)))  # 留着
                await say(engine, clock, 3, "@小D 还在吗")  # 这次过了：带着那几条
                self.assertIn(CHATTER, prompt_of(chats(llm)[-1]))
                self.assertEqual(sent(transport), [(CHANNEL, "在的")])
                await say(engine, clock, 4, f"@小D {RISKY}")  # 阿明这一批被拒了（400）：丢掉
                kept = "\n".join(user_turns(engine, *where))
                self.assertIn(CHATTER, kept)  # 已经算数了的不换
                self.assertNotIn(REJECTED, kept)

    async def test_control_a_request_rejected_as_too_long_is_compacted_and_keeps_the_absorbed_chatter(self):
        # 对照（control）：请求太长被拒（413、说上下文太长的 400）：照旧压缩，补进来的那几条不换（不是它们的问题）
        for status, words in ((413, "HTTP 413"), (400, "This model's maximum context length is 65536 tokens")):
            with self.subTest(status=status):
                engine, llm, transport, clock = world(deepseek(fail=(CHATTER, LLMError(words, status))))
                sync = connect(engine)
                for i in range(2):
                    await say(engine, clock, 10 + i, f"@小D 主服第{i}句")
                await say(engine, clock, 1, CHATTER, channel=HALL, author=21, name="小红", call=False)
                await say(engine, clock, 2, "@小D 有人吗")  # 主服 #闲聊：先把那条补进来，这次请求太长被拒了
                self.assertEqual(len(summary_calls(llm)), 1)  # 压缩了
                self.assertIn(CHATTER, record(engine, sync))  # 补进来的那条没换
                self.assertNotIn(REJECTED, record(engine, sync))
                await say(engine, clock, 3, "@小D 还在吗")
                self.assertIn(CHATTER, prompt_of(chats(llm)[-1]))
                self.assertEqual(sent(transport)[-1], (CHANNEL, "在的"))


class EditsInAChannelWithoutRoundsOfItsOwn(unittest.IsolatedAsyncioTestCase):
    async def old_line_edited(self):
        """主服 #闲聊 + 别的服 #大厅连通。小红在 #大厅随口说了一句（#1，那里一直没人叫它），主服 #闲聊叫它的时候补进来；主服又聊了
        几轮，那条成了很早的消息，她才把它改了（改了不改旧记录，附一句改成了什么）。"""
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await say(engine, clock, 901, PHONE, channel=HALL, author=21, name="小红", call=False)
        for i in range(6):
            await say(engine, clock, 910 + i, f"@小D 主服第{i}句")
        self.assertIn(f"#1 小红：{PHONE}", record(engine, sync, HALL))  # 对照：补进来了
        engine.handle_edit(hers(901, FIXED), edited=True)
        await engine.drain()
        return engine, llm, transport, clock, sync

    async def test_an_old_lines_edit_comes_in_with_that_channels_next_chatter_as_its_own(self):
        engine, llm, transport, clock, sync = await self.old_line_edited()
        note = EDIT_NOTE.format(sid=1, name="小红", text=FIXED)
        await say(engine, clock, 902, "周末一起吃饭吗", channel=HALL, author=21, name="小红", call=False)
        await say(engine, clock, 920, "@小D 大家在聊什么")  # 主服 #闲聊：#大厅刚才说的补进来，“改成了什么”跟着附上
        self.assertIn(note, history_of(llm))
        self.assertNotIn(FIXED, last_user(llm))  # 不附在主服这一轮里（不然记成了主服的）
        hall = user_turns(engine, sync, origin=HALL)
        self.assertTrue(any(note in t and "周末一起吃饭吗" in t for t in hall))  # 和它刚才说的记在一段里：记成 #大厅的
        self.assertNotIn(FIXED, record(engine, sync, CHANNEL))
        await say(engine, clock, 921, "@小D 主服又聊了一句")
        self.assertEqual(record(engine, sync).count(FIXED), 1)  # 附过了：不再附

    async def test_its_edit_note_waits_while_it_is_paused_like_its_chatter(self):
        for how in ("变成私密的", "关掉了允许回应"):
            with self.subTest(how):
                engine, llm, transport, clock, sync = await self.old_line_edited()  # 还公开的时候改的
                stop_reading(engine, transport, how)
                await say(engine, clock, 920, "@小D 大家在聊什么")
                self.assertNotIn(FIXED, history_of(llm))  # 暂停着：先等着（它的闲聊也不补进来）
                start_reading(engine, transport, how)
                await say(engine, clock, 921, "@小D 那边呢")
                note = EDIT_NOTE.format(sid=1, name="小红", text=FIXED)
                self.assertIn(note, history_of(llm))  # 又公开（又打开）了：附上
                self.assertIn(note, record(engine, sync, HALL))
                self.assertNotIn(FIXED, record(engine, sync, CHANNEL))


if __name__ == "__main__":
    unittest.main()
