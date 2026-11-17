"""2.8.0 连通的频道，第十一轮评审修掉的问题（记录）：点歌以后，把结果交回给它的那一步没问成（DeepSeek 忙、超时、太多请求、
太长），记进共用记录的点歌结果（歌名）照样没问过模型，以前却只有那一步被内容审核拒了才记着；断开时跟着频道回到它自己那份的
“改成了什么”，之后那条消息又改了（那一行在它自己那份里是最近的，直接改），那句旧的还留着。

- 连通组的记录里，那一步不管为什么没问成，记下的点歌结果都等带着它的请求过了才算数：之后的请求被 DeepSeek 拒了（不是太长）
  就换成一句“没记下”，几个频道照常回；请求过了就算数，以后别的原因被拒也不动。
- 没连通的频道照旧（2.7.1 一样记下点歌结果，见 test_chatsync_r10_records.RejectedMusicResult 的对照）。
- 一条消息那一行被直接改成了最新的样子，还没附上的“改成了什么”就作废：不再照旧的说；那句被拒了的话，改好消息就照常回；
  再连通时也不会把那一行改回旧的。没断开过的照旧（2.7.1 里只有一直很早的那一行才会附一句）。"""

import json
import unittest

from helpers import CHANNEL, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, say, text_of, world
from test_chatsync_lifecycle import prompt_of, sent
from test_chatsync_privacy import record
from test_chatsync_r6_privacy import own_record
from test_chatsync_r9_records import RISKY, DeepSeek
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review

from dsbot.llm import LLMError
from dsbot.prompts import PASSIVE_REJECTED

RISKY_SONG = "禁曲"  # 内容审核会拦下的歌名：请求里有它，DeepSeek 就拒（400）
HARMLESS_SONG = "晴天"
PLAY = json.dumps({"actions": [{"type": "music", "op": "play", "query": "随便放一首"}]}, ensure_ascii=False)
NOTHING = ([], set())  # 库里没有还没问过模型的


class Jukebox:
    """假的点歌：点什么都真的放了，结果里写着歌名。"""

    def __init__(self, title):
        self.title = title

    def round_state(self, guild_id):
        return None

    def status(self, guild_id):
        return None

    async def tick(self):
        return None

    async def play(self, guild_id, channel_id, user_id, user_name, query, *, can_search, paid, pay, state=None):
        return f"开始播放：《{self.title}》（UP主：某某）"


def busy_tool_step(error):
    """假的 DeepSeek：第一次聊天要点歌（歌真的放了）；第二次（把点歌结果交回给它的那一步）还没看内容就失败了（error）；之后的
    请求里有 RISKY_SONG 的像内容审核那样拒掉（400），别的回一句“在的”。results 按先后记着每次聊天请求怎么样了。"""
    results = []

    def responder(call):
        if is_review(call):
            return '{"cards": [], "memory": []}'
        if not call["json_mode"]:
            return "摘要"
        if not results:
            results.append("点歌")
            return PLAY
        if len(results) == 1:
            results.append("没问成")
            return error
        if RISKY_SONG in prompt_of(call):
            results.append("拒了")
            return LLMError("HTTP 400：Content Exists Risk", status=400)
        results.append("回了")
        return ACTIONS(REPLY(None, "在的"))

    return responder, results


class MusicResultAfterAFailedToolStep(unittest.IsolatedAsyncioTestCase):
    async def play_and_talk(self, title, error):
        """主服 #闲聊 + 别的服 #大厅连通；阿明在主服 #闲聊点了歌（真的放了），交回结果的那一步没问成；之后两边都叫它。"""
        responder, results = busy_tool_step(error)
        engine, llm, transport, clock = world(responder)
        engine.music = Jukebox(title)
        sync = connect(engine)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 1, "@小D 放首歌")
            clock.advance(120)  # 过了出错以后的冷却
            await say(engine, clock, 2, "@小D 大厅这边呢", channel=HALL, author=21, name="小红")
            await say(engine, clock, 3, "@小D 还在吗", channel=HALL, author=22, name="小蓝")
            await say(engine, clock, 4, "@小D 主服也问一句")
        return engine, llm, transport, sync, results

    async def test_a_song_title_deepseek_rejects_is_withdrawn_whatever_made_the_tool_step_fail(self):
        for error in (
            LLMError("HTTP 503：Service Unavailable", status=503),  # DeepSeek 忙（重试了也没成）
            LLMError("HTTP 429：Too Many Requests", status=429),
            LLMError("超时", None, timeout=True),
            LLMError("HTTP 413：context too long", status=413),  # 太长：先压缩，再记下点歌结果
        ):
            with self.subTest(status=error.status, timeout=error.timeout):
                engine, llm, transport, sync, results = await self.play_and_talk(RISKY_SONG, error)
                self.assertEqual(results, ["点歌", "没问成", "拒了", "回了", "回了"])  # 只拒了一次：之后两边都照常回
                self.assertIn((HALL, "在的"), sent(transport))
                self.assertIn((CHANNEL, "在的"), sent(transport))
                whole = record(engine, sync)
                self.assertNotIn(RISKY_SONG, whole)  # 记下的点歌结果换成了一句“没记下”
                self.assertIn(PASSIVE_REJECTED, whole)
                self.assertNotIn(RISKY_SONG, prompt_of(chats(llm)[-1]))
                self.assertEqual(engine.store.load_unvetted(sync), NOTHING)

    async def test_control_a_harmless_title_stays_and_counts_once_a_request_carried_it(self):
        # 对照（control）：歌名没问题的，之后的请求带着它过了就算数（库里也划掉），一直留在记录里
        engine, llm, transport, sync, results = await self.play_and_talk(HARMLESS_SONG, LLMError("忙", status=503))
        self.assertEqual(results, ["点歌", "没问成", "回了", "回了", "回了"])
        whole = record(engine, sync)
        self.assertIn(HARMLESS_SONG, whole)  # 歌真的在放：结果留着
        self.assertNotIn(PASSIVE_REJECTED, whole)
        self.assertEqual(engine.store.load_unvetted(sync), NOTHING)


def hers(mid, text):
    """小红在别的服 #大厅发的一条（叫了它）。"""
    return msg(mid, text, channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)


class EditNoteThatWentHomeWithItsChannel(unittest.IsolatedAsyncioTestCase):
    async def leave_then_edit_again(self, first, second, responder=None):
        """主服 #闲聊、别的服 #大厅、主服 #大厅连通。小红在别的服 #大厅说的“我的号码是 138”（#5）回过以后，主服 #闲聊又聊了五轮：
        #5 在共用的记录里成了很早的消息，她改成 first，只记下一句“改成了什么”等 #大厅 下一轮附上。#大厅 还没说话就断开了：
        那句跟着它回到它自己那份，#5 那一轮接在它自己那份最后（是最近的）。second：她又改了一次（不是 None 的话）。"""
        engine, llm, transport, clock = world(responder)
        sync = connect(engine)
        connect(engine, LOBBY, CHANNEL)
        await say(engine, clock, 5, "@小D 我的号码是 138", channel=HALL, author=21, name="小红")
        for i in range(5):
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")
        engine.handle_edit(hers(5, first), edited=True)
        await engine.drain()
        self.assertIn(5, engine.convs[sync].edit_notes)  # 对照：等着别的服 #大厅下一轮附上
        disconnect(engine, HALL)
        self.assertIn(5, engine.convs[HALL].edit_notes)  # 对照：跟着它回到它自己那份
        if second is not None:
            engine.handle_edit(hers(5, second), edited=True)  # 那一行在它自己那份里是最近的：直接改
            await engine.drain()
        return engine, llm, clock

    async def test_a_later_edit_replaces_the_note_that_went_home(self):
        engine, llm, clock = await self.leave_then_edit_again("@小D 我的号码改成 139", "@小D 我的号码改成 137")
        self.assertIn("我的号码改成 137", own_record(engine, HALL, OTHER))  # 对照：那一行直接改了
        await say(engine, clock, 50, "@小D 在吗", channel=HALL, author=21, name="小红")
        request = prompt_of(chats(llm)[-1])
        self.assertIn("我的号码改成 137", request)
        self.assertNotIn("我的号码改成 139", request)  # 不再附那句旧的“现在是：……139”

    async def test_a_later_edit_unblocks_a_channel_whose_note_deepseek_rejects(self):
        ds = DeepSeek()
        engine, llm, clock = await self.leave_then_edit_again(f"@小D {RISKY}", None, ds)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 50, "@小D 在吗", channel=HALL, author=21, name="小红")  # 附着那句：被拒了
        self.assertEqual(ds.results[-1], "拒了")  # 对照：和 2.7.1 一样，那句一直附着，直到那条消息再改、被删
        engine.handle_edit(hers(5, "@小D 我的号码改成 137"), edited=True)  # 她改好了
        await engine.drain()
        await say(engine, clock, 51, "@小D 还在吗", channel=HALL, author=21, name="小红")
        self.assertEqual(ds.results[-1], "回了")  # 改好了就照常回
        self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))

    async def test_a_later_edit_is_not_reverted_when_the_channel_links_again(self):
        engine, llm, clock = await self.leave_then_edit_again("@小D 我的号码改成 139", "@小D 我的号码改成 137")
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅还没说话就又连进来了
        own = own_record(engine, HALL, OTHER)
        self.assertIn("我的号码改成 137", own)  # 它自己那份留着最新的那次
        self.assertNotIn("我的号码改成 139", own)

    async def test_control_without_a_later_edit_the_note_goes_home_and_is_told(self):
        # 对照（control）：没再改的话，那句跟着它回到它自己那份，它下一轮照样附上
        engine, llm, clock = await self.leave_then_edit_again("@小D 我的号码改成 139", None)
        await say(engine, clock, 50, "@小D 在吗", channel=HALL, author=21, name="小红")
        self.assertIn("我的号码改成 139", text_of(chats(llm)[-1]["messages"][-1]["content"]))


if __name__ == "__main__":
    unittest.main()
