"""2.8.0 连通的频道，第九轮评审修掉的问题：没问模型就记进记录的那几条（别的连通频道补进来的闲聊、连通时记进它自己那份的
闲聊、直接改进共用记录的编辑）在清空记忆、断开以后还记着；换掉的那段里附着的“改成了什么”；每个频道各记各的文件、链接；
连通组整理记忆时每个频道各数各的失败次数。

- 清空一个连通频道的记忆（正在连通的、已经断开了的都算）：共用的记录里还没问过模型的那几条（别的频道补进来的闲聊、直接改成了
  新内容的消息）照旧记着，之后带着它们的请求被 DeepSeek 拒了（不是太长）照样换掉，不会卡住几个频道。
- 连通时记进它自己那份、还没问过模型的闲聊：断开以后（组散了、离开还在的组都一样）照旧记着，带着整份记录的请求被拒了换成一句
  “没有记下来”，之后照常回。
- 连通期间这个频道的一轮请求过了：那一轮的“连通之前的概要”带全了那几条才算数（以后被拒也不换）；太长、概要只带了后面一段的
  不算，等断开以后带着整份记录的请求来定。
- 补进来的那段被换掉时，里面附着的“（#1 小红 改了这条消息，现在是：……）”换成一句那条改过了（改成的内容不留：可能就是它被拒），
  之后的请求照样知道那条改过。
- 共用的记录里直接改成了新内容的消息（最近的消息被编辑）：带着它的请求被拒了（不是太长），那一行当成删了，之后每个频道照常回；
  请求过了的就算数，以后别的原因被拒也不动。
- 带文件、带链接的消息每个频道各记最近 30 条：别的连通频道发得多，挤不掉这个频道刚发的；没连通的频道照旧最多 30 条。
- 连通组安静时整理记忆：每个频道那部分各数各的，只有它自己连着失败三次才放弃（整理成了、放弃了都从头数）；一次整理不管几个
  频道没整理成，下次只多等一次的时间。"""

import re
import unittest

from helpers import CHANNEL, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, replies, say, text_of, world
from test_chatsync_lifecycle import prompt_of, users
from test_chatsync_privacy import history_of, record
from test_chatsync_r2_records import files_read, times_reviewed
from test_chatsync_r2_review import quiet
from test_chatsync_r3_entries import links_opened
from test_chatsync_r6_privacy import own_record
from test_chatsync_records import three_linked
from test_chatsync_review import reviews
from test_dm import OTHER
from test_engine_files import READ, Reader, doc
from test_links_images import FakeReader, link_msg
from test_memory import ACTIONS, REPLY, is_review, stores

from dsbot import prompts
from dsbot.engine import MAX_REVIEW_FAILURES
from dsbot.llm import LLMError
from dsbot.prompts import MESSAGE_DELETED, PASSIVE_REJECTED

# 换掉的那段里附着的“改成了什么”留下的这一句（第九轮起是 prompts.EDIT_NOTE_LOST）
LOST = getattr(prompts, "EDIT_NOTE_LOST", "（#{sid} {name} 改过这条消息，改成了什么没记下来。）")
RISKY = "某个会被内容审核拦下的词"  # 请求里有它，DeepSeek 就拒（400，内容审核）
CHATTER = "周末去不去爬山"  # 没人叫它的一句闲聊（请求里有它照常回）
LONG = "今天的天气真不错，我们下午去公园散步吧，顺便买点水果回来。" * 3  # 小蓝一句长的闲聊（八十来字）
PHONE = "我家电话是 010-12345678，有事打给我"  # 小红在别的服 #大厅随口说的（那里一直没人叫它）
FIXED = "（电话号码打错了，已删）"  # 那条成了很早的消息以后，她把它改成了这个


class DeepSeek:
    """假的 DeepSeek。聊天：busy 还有次数时先回 503（DeepSeek 忙，重试也没成：不看内容）；请求里（system prompt、记录、这一轮）
    有 RISKY 的像内容审核那样拒掉（400）；别的回一句“在的”。results 按先后记着每次聊天请求怎么样了。整理记忆什么都不记，
    压缩摘要回“摘要”。"""

    def __init__(self):
        self.busy = 0
        self.results = []

    def __call__(self, call):
        if is_review(call):
            return '{"cards": [], "memory": []}'
        if not call["json_mode"]:
            return "摘要"
        if self.busy:
            self.busy -= 1
            self.results.append("503")
            return LLMError("HTTP 503：Service Unavailable", status=503)
        if RISKY in prompt_of(call):
            self.results.append("拒了")
            return LLMError("HTTP 400：Content Exists Risk", status=400)
        self.results.append("回了")
        return ACTIONS(REPLY(None, "在的"))


def hers(mid, text, **kw):
    """小红在别的服 #大厅发的一条。"""
    return msg(mid, text, channel_id=HALL, guild_id=OTHER, author=21, name="小红", **kw)


def posted(kind, mid, label, text, **kw):
    """一条带链接（kind 是“链接”：https://example.com/<label>）或者带文件（<label>.docx）的消息。"""
    if kind == "链接":
        url = f"https://example.com/{label}"
        return link_msg(mid, f"{text} {url}", [url], **kw)
    return msg(mid, text, files=[doc(f"{label}.docx")], **kw)


def opened(kind, reader):
    """打开过的链接（读过的文件）的 label，按先后。"""
    if kind == "链接":
        return [url.rsplit("/", 1)[-1] for url in links_opened(reader)]
    return [name.removesuffix(".docx") for name in files_read(reader)]


def reading_world(kind, responder):
    """能打开链接（kind 是“链接”）或者能读文件的 world()，加上那个假的读链接、读文件的（记着读过什么）。"""
    if kind == "链接":
        reader = FakeReader()
        engine, llm, transport, clock = world(responder, link_reader=reader)
    else:
        reader = Reader()
        engine, llm, transport, clock = world(responder)
        engine.file_reader = reader
    return engine, llm, transport, clock, reader


def reading(words, wanted):
    """叫它的这一轮话里有 words：它要看 wanted 里那几条消息（“名字：说的话”，照记录里的编号；附件写在话的前面）的文件、
    链接；别的照 replies() 回。"""
    others = replies()

    def sid(text, line):
        who, said = line.split("：", 1)
        return int(re.search(rf"#(\d+) {who}：[^\n]*{said}", text).group(1))

    def responder(call):
        if call["json_mode"] and not is_review(call) and words in text_of(call["messages"][-1]["content"]):
            text = prompt_of(call)
            return READ(*(sid(text, line) for line in wanted))
        return others(call)

    return responder


def reviewing(failing):
    """整理记忆：给它看的聊天里有 failing 里的话，那一段就等太久没写完（超时，算模型那边的问题）；别的什么都不记。
    聊天照 replies() 回。failing 可以中途改。"""
    others = replies()

    def responder(call):
        if is_review(call):
            if any(words in text_of(call["messages"][-1]["content"]) for words in failing):
                return LLMError("请求超时（180 秒）", timeout=True)
            return '{"cards": [], "memory": []}'
        return others(call)

    return responder


class ClearingALinkedChannel(unittest.IsolatedAsyncioTestCase):
    async def test_what_the_shared_record_holds_unvetted_is_still_withdrawn_after_a_clear(self):
        for kind in ("补进来的闲聊", "直接改进记录的编辑"):
            for cleared in ("主服 #闲聊（正在连通）", "主服 #大厅（已经断开了）"):
                with self.subTest(kind=kind, cleared=cleared):
                    ds = DeepSeek()
                    engine, llm, transport, clock = world(ds)
                    sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                    channel = CHANNEL
                    if cleared.startswith("主服 #大厅"):
                        connect(engine, LOBBY, CHANNEL)
                        await say(engine, clock, 50, "@小D 主服大厅这边", channel=LOBBY, author=12, name="小雨")
                        disconnect(engine, LOBBY)  # 它说过的留在组里：以后清空它，组里那份也要删掉它那部分
                        channel = LOBBY
                    with self.assertLogs("dsbot.engine", "WARNING"):
                        if kind == "补进来的闲聊":
                            await say(engine, clock, 1, f"我随口说了{RISKY}", channel=HALL, author=21, name="小红", call=False)
                        else:
                            await say(engine, clock, 1, "@小D 大厅这边好", channel=HALL, author=21, name="小红")
                            engine.handle_edit(hers(1, f"@小D {RISKY}", mentions_bot=True), edited=True)  # 刚说的那条：直接改进记录
                            await engine.drain()
                        ds.busy = 1
                        await say(engine, clock, 2, "@小D 在吗")  # 主服 #闲聊：DeepSeek 忙（503），那条没问成也没被拒
                        self.assertIn(RISKY, record(engine, sync))  # 对照：那条还在共用的记录里
                        await engine.reset_linked(channel)  # 有人在这个频道 /ds 清空记忆（别的频道说的不动）
                        await say(engine, clock, 3, "@小D 大厅这边呢", channel=HALL, author=22, name="小蓝")  # 带着那条：被拒了
                        await say(engine, clock, 4, "@小D 还在吗", channel=HALL, author=22, name="小蓝")
                    self.assertEqual(ds.results[-3:], ["503", "拒了", "回了"])  # 只拒了一次：之后照常回
                    hall = record(engine, sync, HALL)
                    self.assertNotIn(RISKY, hall)
                    # 补进来的那几条换成一句“没有记下来”；改过的那一行当成删了
                    self.assertIn(PASSIVE_REJECTED if kind == "补进来的闲聊" else f"小红：{MESSAGE_DELETED}", hall)


class SettledChatterAfterUnlinking(unittest.IsolatedAsyncioTestCase):
    async def test_settled_chatter_deepseek_rejects_is_withdrawn_once_the_channel_has_unlinked(self):
        for how in ("别的服 #大厅断开（组散了）", "主服 #闲聊断开（组散了）", "别的服 #大厅离开还在的组"):
            with self.subTest(how):
                ds = DeepSeek()
                engine, llm, transport, clock = world(ds)
                if how.endswith("还在的组"):
                    connect(engine, CHANNEL, LOBBY)  # 主服 #闲聊 + 主服 #大厅：#大厅走了组还在
                await say(engine, clock, 1, f"我随口说了{RISKY}", channel=HALL, author=21, name="小红", call=False)  # 没人叫它
                connect(engine, HALL, CHANNEL)  # 连进来：那条记进 #大厅自己那份（不进共用的），还没问过模型
                self.assertIn(RISKY, own_record(engine, HALL, OTHER))
                disconnect(engine, CHANNEL if how.startswith("主服") else HALL)  # #大厅还没人叫过它就断开了
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await say(engine, clock, 2, "@小D 在吗", channel=HALL, author=21, name="小红")  # 带着整份记录：被拒了
                await say(engine, clock, 3, "@小D 还在吗", channel=HALL, author=21, name="小红")
                self.assertEqual(ds.results, ["拒了", "回了"])  # 只拒了一次：之后照常回
                settled = users(engine, HALL, OTHER)[0].split("\n")  # 连通时记进它自己那份的那段
                self.assertRegex(settled[0], r"^【.+】$")  # 开头写着时间的那一行留着……
                self.assertEqual(settled[1:], [PASSIVE_REJECTED])  # ……那几条换成一句“没有记下来”
                self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))


class SettledChatterAndTheLinkedRound(unittest.IsolatedAsyncioTestCase):
    async def test_a_linked_round_whose_digest_left_part_of_the_settled_chatter_out_does_not_vouch_for_it(self):
        ds = DeepSeek()
        engine, llm, transport, clock = world(ds)
        await say(engine, clock, 1, f"我随口说了{RISKY}", channel=HALL, author=21, name="小红", call=False)
        for i in range(39):  # 小蓝又说了 39 句长的（一共三千多字），都没人叫它
            await say(engine, clock, 2 + i, f"第{i}句闲聊：{LONG}", channel=HALL, author=22, name="小蓝", call=False)
        connect(engine)  # 连进来：这 40 条一起记进 #大厅自己那份，还没问过模型
        await say(engine, clock, 50, "@小D 在吗", channel=HALL, author=21, name="小红")  # 连通期间的一轮：过了
        self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))  # 对照：“连通之前的概要”只带了最后三千字，没带到小红那句
        disconnect(engine, HALL)  # 组散了：#大厅又用它自己那份（整份带着）
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 51, "@小D 还在吗", channel=HALL, author=21, name="小红")  # 带着整份记录：被拒了
        await say(engine, clock, 52, "@小D 在不在", channel=HALL, author=21, name="小红")
        self.assertEqual(ds.results, ["回了", "拒了", "回了"])  # 连通时那一轮过了不算数：被拒一次就换掉，之后照常回
        self.assertEqual(users(engine, HALL, OTHER)[0].split("\n")[1:], [PASSIVE_REJECTED])
        self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))

    async def test_control_a_short_settled_batch_the_digest_carried_is_vetted_by_the_linked_round(self):
        # 对照（control）：“连通之前的概要”带全了的（它前面更早的聊天再长也一样）：连通期间那一轮过了就算数，断开以后别的原因
        # 被拒也不换
        ds = DeepSeek()
        engine, llm, transport, clock = world(ds)
        for i in range(5):  # 连通之前 #大厅自己聊了很多（比概要能带的三千字长）
            await say(engine, clock, 1 + i, f"@小D 以前的事{i}：" + "很长的旧事" * 150, channel=HALL, author=21, name="小红")
        await say(engine, clock, 10, CHATTER, channel=HALL, author=21, name="小红", call=False)
        connect(engine)  # 连进来：那句记进 #大厅自己那份
        await say(engine, clock, 11, "@小D 在吗", channel=HALL, author=21, name="小红")  # 连通期间的一轮：过了
        self.assertIn(CHATTER, chats(llm)[-1]["messages"][0]["content"])  # 对照：概要里带着那句
        disconnect(engine, HALL)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 12, f"@小D {RISKY}", channel=HALL, author=21, name="小红")  # 她这一批被拒了（400）：丢掉
        await say(engine, clock, 13, "@小D 还在吗", channel=HALL, author=21, name="小红")
        self.assertEqual(ds.results[-3:], ["回了", "拒了", "回了"])
        own = own_record(engine, HALL, OTHER)
        self.assertIn(CHATTER, own)  # 已经算数了的不换
        self.assertNotIn(PASSIVE_REJECTED, own)
        self.assertIn(CHATTER, prompt_of(chats(llm)[-1]))


class EditNotesInAWithdrawnPair(unittest.IsolatedAsyncioTestCase):
    async def test_a_withdrawn_pair_keeps_a_line_saying_which_message_was_changed_but_not_what_it_says_now(self):
        for cause, trigger, fixed in (
            ("主服 #闲聊叫它的那句", f"@小D {RISKY}", FIXED),
            ("她改成的那句", "@小D 你好", f"{FIXED}{RISKY}"),
        ):
            with self.subTest(f"被拒是因为{cause}"):
                ds = DeepSeek()
                engine, llm, transport, clock = world(ds)
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                await say(engine, clock, 901, PHONE, channel=HALL, author=21, name="小红", call=False)  # #大厅一直没人叫它
                for i in range(6):  # 主服 #闲聊叫它的时候那条补进来（#1），之后又聊了几轮：成了很早的消息
                    await say(engine, clock, 910 + i, f"@小D 主服第{i}句")
                engine.handle_edit(hers(901, fixed), edited=True)  # 她改了那条：不改旧记录，下次补进来时附一句改成了什么
                await engine.drain()
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await say(engine, clock, 920, trigger)  # 附着那句的那段跟着补进来：这次请求被拒了，那段换掉
                await say(engine, clock, 921, "@小D 刚才说到哪了")
                self.assertEqual(ds.results[-2:], ["拒了", "回了"])
                lost = LOST.format(sid=1, name="小红")
                hall = record(engine, sync, HALL)
                self.assertIn(lost, hall)  # 换掉的那段留着一句：#1 改过了（记成 #大厅的）……
                self.assertNotIn(FIXED, hall)  # ……改成了什么不留
                self.assertIn(lost, history_of(llm))  # 之后的请求照样知道 #1 改过，不当成它现在还是那样
                self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))


class EditsRewrittenIntoTheSharedRecord(unittest.IsolatedAsyncioTestCase):
    async def test_an_edit_deepseek_rejects_becomes_a_deleted_line_and_every_channel_is_answered_again(self):
        ds = DeepSeek()
        engine, llm, transport, clock = world(ds)
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        await say(engine, clock, 1, "@小D 大厅这边好", channel=HALL, author=21, name="小红")
        engine.handle_edit(hers(1, f"@小D {RISKY}", mentions_bot=True), edited=True)  # 刚说的那条：直接改进共用的记录
        await engine.drain()
        self.assertIn(RISKY, record(engine, sync))  # 对照：改进去了（没问过模型）
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 2, "@小D 在吗")  # 主服 #闲聊：记录里有那一行，被拒了
        await say(engine, clock, 3, "@小D 还在吗")
        await say(engine, clock, 4, "@小D 大厅这边呢", channel=HALL, author=22, name="小蓝")
        self.assertEqual(ds.results, ["回了", "拒了", "回了", "回了"])  # 只拒了一次：之后两边都照常回
        whole = record(engine, sync)
        self.assertIn(f"小红：{MESSAGE_DELETED}", whole)  # 那一行当成删了
        self.assertNotIn(RISKY, whole)
        self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))

    async def test_control_an_edit_a_request_already_carried_stays_when_a_later_round_is_rejected(self):
        # 对照（control）：改成的内容请求过了（DeepSeek 接受了）就算数：之后别的原因被拒，那一行也不当成删了
        ds = DeepSeek()
        engine, llm, transport, clock = world(ds)
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 大厅这边好", channel=HALL, author=21, name="小红")
        engine.handle_edit(hers(1, "@小D 大厅这边很好", mentions_bot=True), edited=True)
        await engine.drain()
        await say(engine, clock, 2, "@小D 在吗")  # 带着改过的那一行：过了
        self.assertIn("大厅这边很好", prompt_of(chats(llm)[-1]))
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 3, f"@小D {RISKY}")  # 阿明这一批被拒了（400）：丢掉
        await say(engine, clock, 4, "@小D 还在吗")
        self.assertEqual(ds.results, ["回了", "回了", "拒了", "回了"])
        whole = record(engine, sync)
        self.assertIn("小红：@小D 大厅这边很好", whole)  # 改过的那一行照旧
        self.assertNotIn(MESSAGE_DELETED, whole)


class FilesAndLinksOfEachChannel(unittest.IsolatedAsyncioTestCase):
    async def test_a_busier_linked_channel_does_not_push_out_a_quiet_channels_files_or_links(self):
        ask = "我刚才贴的报告你看看"
        for kind in ("链接", "文件"):
            with self.subTest(kind):
                engine, llm, transport, clock, reader = reading_world(
                    kind, reading(ask, ["阿明：报告在这", "小红：大厅第0个", "小红：大厅第1个"])
                )
                connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                await engine.handle_message(posted(kind, 501, "report", "报告在这"))  # 阿明在主服 #闲聊贴的（没叫它）
                clock.advance(300)  # 过了“两分钟内同一个人”直接读的时候
                for i in range(31):  # 别的服 #大厅那边大家在分享（没叫它）：31 条，比 30 条多一条
                    there = dict(channel_id=HALL, guild_id=OTHER, author=21, name="小红")
                    await engine.handle_message(posted(kind, 600 + i, f"hall{i}", f"大厅第{i}个", **there))
                    clock.advance(10)
                await say(engine, clock, 502, f"@小D {ask}")  # 它要看阿明那条、#大厅最早的两条
                got = opened(kind, reader)
                self.assertIn("report", got)  # 主服 #闲聊刚贴的照样拿得到：别的频道发得多也挤不掉
                self.assertNotIn("hall0", got)  # #大厅自己的照旧只记最近 30 条：最早那条拿不到了
                self.assertIn("hall1", got)

    async def test_control_an_unlinked_channel_still_keeps_only_its_30_newest(self):
        # 对照（control）：没连通的频道跟以前一样，只记最近 30 条带链接（带文件）的消息
        ask = "最早那两个你看看"
        for kind in ("链接", "文件"):
            with self.subTest(kind):
                engine, llm, transport, clock, reader = reading_world(kind, reading(ask, ["小雨：主服第0个", "小雨：主服第1个"]))
                for i in range(31):
                    await engine.handle_message(posted(kind, 600 + i, f"chat{i}", f"主服第{i}个", author=12, name="小雨"))
                    clock.advance(10)
                await say(engine, clock, 700, f"@小D {ask}")
                got = opened(kind, reader)
                self.assertNotIn("chat0", got)  # 最早那条拿不到了
                self.assertIn("chat1", got)


class ReviewFailuresOfEachChannel(unittest.IsolatedAsyncioTestCase):
    def reviewing_world(self, failing):
        """记人设卡、长期记忆的 world()：聊过一轮、安静 10 分钟就整理（失败过的等两倍、四倍……）。"""
        cards, memory = stores()
        cfg = {"review_min_rounds": 1, "review_idle_seconds": 600}
        return world(reviewing(failing), cards=cards, memory=memory, engine_cfg=cfg)

    async def test_each_channels_part_survives_one_failure_even_when_three_channels_fail_together(self):
        failing = set()
        engine, llm, transport, clock = self.reviewing_world(failing)
        three_linked(engine)  # 主服 #闲聊、别的服 #大厅、主服 #大厅
        said = ["大厅那边的事", "闲聊这边的事", "主服大厅的事"]
        await say(engine, clock, 1, f"@小D {said[0]}", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, f"@小D {said[1]}")
        await say(engine, clock, 3, f"@小D {said[2]}", channel=LOBBY, author=12, name="小雨")
        failing.update(said)  # DeepSeek 慢了一阵：每个频道那段都超时
        with self.assertLogs("dsbot.engine", "WARNING"):
            await quiet(engine, clock, 700)
        self.assertEqual(len(reviews(llm)), 3)  # 对照：三段都问了，都没整理成
        failing.clear()
        await quiet(engine, clock)
        for words in said:
            self.assertEqual(times_reviewed(llm, words), 2, words)  # 每个频道那段都只失败了一次：再问一次，不放弃

    async def test_a_failed_review_waits_once_however_many_channels_failed_in_it(self):
        failing = set()
        engine, llm, transport, clock = self.reviewing_world(failing)
        connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        said = ["闲聊这边的事", "大厅那边的事"]
        await say(engine, clock, 1, f"@小D {said[0]}")
        await say(engine, clock, 2, f"@小D {said[1]}", channel=HALL, author=21, name="小红")
        failing.update(said)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await quiet(engine, clock, 700)  # 两个频道那段都超时
        self.assertEqual(len(reviews(llm)), 2)
        failing.clear()
        await quiet(engine, clock, 1100)  # 失败过：要等安静时间的两倍（1200 秒）才再整理
        self.assertEqual(len(reviews(llm)), 2)
        await quiet(engine, clock, 200)  # 一次整理只算失败一次（不是没整理成的两段各算一次：那样要等 2400 秒）
        for words in said:
            self.assertEqual(times_reviewed(llm, words), 2, words)

    async def test_control_each_channel_gets_three_tries_in_a_row_again_after_its_part_was_given_up_or_went_through(self):
        # 对照（control）：一个频道那段连着失败三次才放弃；放弃了、整理成了，它后来的聊天都从头数（不接着之前的次数）
        failing = {"闲聊这边的事"}
        engine, llm, transport, clock = self.reviewing_world(failing)
        connect(engine)
        await say(engine, clock, 1, "@小D 闲聊这边的事")
        await say(engine, clock, 2, "@小D 大厅那边的事", channel=HALL, author=21, name="小红")
        with self.assertLogs("dsbot.engine", "WARNING"):
            await quiet(engine, clock, times=MAX_REVIEW_FAILURES + 1)
        self.assertEqual(times_reviewed(llm, "闲聊这边的事"), MAX_REVIEW_FAILURES)  # 连着失败三次：放弃了，不再问
        self.assertEqual(times_reviewed(llm, "大厅那边的事"), 1)  # 别的服那段照常整理
        # 放弃以后它又聊了一件（失败一次），整理成了以后又聊了一件（失败两次）：都从头数，没到三次不放弃
        for later, fails in (("闲聊这边的新事", 1), ("闲聊这边的第三件事", MAX_REVIEW_FAILURES - 1)):
            await say(engine, clock, 3 + fails, f"@小D {later}")
            failing.add(later)
            with self.assertLogs("dsbot.engine", "WARNING"):
                await quiet(engine, clock, times=fails)
            failing.discard(later)
            await quiet(engine, clock)
            self.assertEqual(times_reviewed(llm, later), fails + 1, later)  # 失败几次以后整理成了


if __name__ == "__main__":
    unittest.main()
