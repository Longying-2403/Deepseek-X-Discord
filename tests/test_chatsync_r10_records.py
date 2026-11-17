"""2.8.0 连通的频道，第十轮评审修掉的问题：连通组整理记忆时每个频道连着失败的次数、整理失败以后等多久；点歌的结果被 DeepSeek
拒了以后卡住组里的频道；同一个服务器的几个连通频道各认各的“在放什么”、能翻的频道名单。

- 连通组安静时整理记忆，一个频道那部分连着失败了两次：那批聊天后来压缩时整理了（整理成没成都一样），或者这个频道断开以后又连回
  同一个组，它以后的聊天都从头数，失败一次不会就放弃。
- 一次整理里一个频道那部分连着失败三次、放弃了：这次别的频道那部分也没整理成的（排在它前面、后面都一样），下次照样多等一等，
  不会刚过安静时间就又整理。
- 连通组里点了歌、歌放上了，回话的那一步被 DeepSeek 拒了（不是太长）：记下的点歌结果（歌名是 B站上来的，没问过模型）跟别的频道
  补进来的闲聊一样，带着它的请求被拒时换成一句“没有记下来”，之后每个频道照常回。没连通的频道跟以前一样。
- 同一个服务器的几个频道连通（跨服务器的连通里同一个服务器的那几个也一样）：语音频道在放什么、能翻的频道名单是同一个，谁写的
  最新那句都算（已经断开了的频道写的也算）：歌停了、换回原来那首、名单变回原来那样，下一个被叫到的频道照样写一句。别的服务器的
  照旧各是各的。"""

import unittest

from helpers import CHANNEL, GUILD
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, say, text_of, world
from test_chatsync_lifecycle import prompt_of
from test_chatsync_privacy import GAMES, PLAYING, history_of, jukebox, record
from test_chatsync_r2_records import compact, times_reviewed
from test_chatsync_r2_review import quiet
from test_chatsync_r6_privacy import own_record
from test_chatsync_r9_records import RISKY, DeepSeek, reviewing
from test_chatsync_records import three_linked
from test_chatsync_review import reviews
from test_memory import ACTIONS, is_review, stores

from dsbot.engine import MAX_REVIEW_FAILURES
from dsbot.llm import LLMError
from dsbot.models import ChannelPlace
from dsbot.prompts import MUSIC_IDLE, MUSIC_IDLE_AT, MUSIC_NOW, MUSIC_NOW_AT, MUSIC_UNANSWERED, PASSIVE_REJECTED

IDLE = 600  # 安静多久就整理记忆（review_idle_seconds；失败过的等两倍、四倍……）
ART = 107  # 主服的公开频道 #画画
PLAY = ACTIONS({"type": "music", "op": "play", "query": "随便放一首"})  # 它替叫它的人点一首歌
OTHER_SONG = "语音频道「音乐厅」正在放：稻香"  # 换的那首（PLAYING 是“晴天”）
SPEAKER = {CHANNEL: (11, "阿明"), LOBBY: (12, "小雨")}  # 主服 #闲聊、主服 #大厅 里说话的人


def reviewing_world(responder):
    """记人设卡、长期记忆的 world()：聊过一轮、安静 10 分钟就整理（失败过的等两倍、四倍……）。"""
    cards, memory = stores()
    cfg = {"review_min_rounds": 1, "review_idle_seconds": IDLE}
    return world(responder, cards=cards, memory=memory, engine_cfg=cfg)


def reviewing_with(slow, busy):
    """整理记忆：给它看的聊天里有 slow 里的话，那一段等太久没写完（超时：模型那边的问题，连着三次就放弃）；有 busy 里的话，
    DeepSeek 忙（503：暂时的问题，不算进放弃的次数）；别的什么都不记。聊天照 replies() 回。两个集合都可以中途改。"""
    timing_out = reviewing(slow)

    def responder(call):
        if is_review(call) and any(words in text_of(call["messages"][-1]["content"]) for words in busy):
            return LLMError("HTTP 503：Service Unavailable", status=503)
        return timing_out(call)

    return responder


class Jukebox:
    """假的点歌：点什么都放得上，放的是 title 那首（B站上的歌名）；记着点过什么。语音频道在放什么不说（status 是 None）。"""

    def __init__(self, title):
        self.title = title
        self.played = []

    def round_state(self, guild_id):
        return None

    def status(self, guild_id):
        return None

    async def tick(self):
        return None

    async def play(self, guild_id, channel_id, user_id, user_name, query, *, can_search, paid, pay, state=None):
        self.played.append((guild_id, query))
        return f"开始播放：《{self.title}》（UP主：某某）"


def asking_for_a_song(ds):
    """照 ds（test_chatsync_r9_records.DeepSeek：请求里有 RISKY 就拒）回，只是叫它“放首歌”的那一轮它要点歌。"""

    def responder(call):
        out = ds(call)
        if isinstance(out, str) and call["json_mode"] and not is_review(call):
            if "放首歌" in text_of(call["messages"][-1]["content"]):
                return PLAY
        return out

    return responder


def last_music(text):
    """一次请求里最后写的“现在在放什么”那一句（模型当成现在的情况）；没写过是 None。"""
    lines = [line for line in text.split("\n") if line.startswith("（现在") and "语音频道" in line]
    return lines[-1] if lines else None


def last_list(text):
    """一次请求里最后写的能翻记录的频道名单：频道名的 tuple（写的是“没有”就是空的；没写过是 None）。"""
    lines = text.split("\n")
    found = None
    for i, line in enumerate(lines):
        if line == "（现在没有能翻记录的别的频道）":
            found = ()
        elif line == "（能翻记录的别的频道：":
            names = []
            for later in lines[i + 1 :]:
                if not later.startswith("  "):
                    break
                names.append(later[2:])
            found = tuple(names)
    return found


class EarlierFailuresOfAChannelsPart(unittest.IsolatedAsyncioTestCase):
    async def test_a_channels_next_part_gets_its_three_tries_once_the_part_that_failed_was_compacted_or_taken_back(self):
        for how in ("压缩时整理了那一段", "压缩时整理那一段又失败了", "断开以后又连回同一个组"):
            with self.subTest(how):
                failing = {"大厅第一件事"}
                engine, llm, transport, clock = reviewing_world(reviewing(failing))
                if how.startswith("断开"):
                    three_linked(engine)  # 主服 #闲聊、别的服 #大厅、主服 #大厅：别的服 #大厅走了组还在
                else:
                    connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                await say(engine, clock, 1, "@小D 大厅第一件事", channel=HALL, author=21, name="小红")
                await say(engine, clock, 2, "@小D 闲聊的事")
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await quiet(engine, clock, 2 * IDLE)  # 别的服 #大厅那段超时了……
                    await quiet(engine, clock, 4 * IDLE)  # ……两次（再失败一次就放弃）
                    self.assertEqual(times_reviewed(llm, "大厅第一件事"), 2)  # 对照
                    if how.startswith("压缩"):
                        if how == "压缩时整理了那一段":
                            failing.clear()
                        for i in range(3):
                            await say(engine, clock, 10 + i, f"@小D 闲聊第{i}句")
                        await compact(engine, CHANNEL)  # 共用的记录压缩了：那一段压掉之前挑出来整理（这一批就算了结了）
                        await engine.drain()
                        self.assertEqual(times_reviewed(llm, "大厅第一件事"), 3)  # 对照：压缩前整理了它
                    else:
                        disconnect(engine, HALL)  # 它那部分拿回它自己那份，在那里整理
                        connect(engine, HALL, CHANNEL)  # 又连回同一个组
                    failing.clear()
                    failing.add("大厅第二件事")
                    await say(engine, clock, 3, "@小D 大厅第二件事", channel=HALL, author=21, name="小红")
                    await quiet(engine, clock, 40 * IDLE)  # 它后来的这一段也超时了一次
                failing.clear()
                await say(engine, clock, 4, "@小D 闲聊又一件事")
                await quiet(engine, clock, 40 * IDLE)  # DeepSeek 好了
                # 只失败了一次：照样再整理一次，没有接着以前那批的次数放弃
                self.assertEqual(times_reviewed(llm, "大厅第二件事"), 2)


class BackoffAfterAChannelsPartIsGivenUp(unittest.IsolatedAsyncioTestCase):
    async def test_a_part_that_failed_in_the_review_where_another_channels_part_was_given_up_still_waits_longer(self):
        for order in ("排在放弃的那段前面", "排在放弃的那段后面"):
            with self.subTest(order):
                slow, busy = {"大厅那件事"}, set()
                engine, llm, transport, clock = reviewing_world(reviewing_with(slow, busy))
                connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                if order == "排在放弃的那段前面":
                    busy.add("闲聊那件事")  # 主服 #闲聊那段 DeepSeek 一直忙（暂时的问题，不放弃）
                    await say(engine, clock, 1, "@小D 闲聊那件事")  # 先说的：每次整理都先问它
                await say(engine, clock, 2, "@小D 大厅那件事", channel=HALL, author=21, name="小红")
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await quiet(engine, clock, 2 * IDLE)  # 别的服 #大厅那段超时（第一次）
                    await quiet(engine, clock, 4 * IDLE)  # （第二次）
                    if order == "排在放弃的那段后面":
                        await say(engine, clock, 3, "@小D 闲聊那件事")  # 后说的：排在 #大厅那段后面
                        busy.add("闲聊那件事")
                    await quiet(engine, clock, 8 * IDLE)  # 第三次：#大厅那段放弃了；主服 #闲聊那段这次也没整理成
                self.assertEqual(times_reviewed(llm, "大厅那件事"), MAX_REVIEW_FAILURES)  # 对照：放弃了，不再问
                busy.clear()
                asked = len(reviews(llm))
                await quiet(engine, clock, IDLE + 60)  # 刚过安静时间
                # 这次整理有频道没整理成（放弃了一个频道那段不算从头来）：要等安静时间的两倍以上才再整理
                self.assertEqual(reviews(llm)[asked:], [])


class RejectedMusicResult(unittest.IsolatedAsyncioTestCase):
    async def test_a_song_title_deepseek_rejects_is_withdrawn_and_every_channel_is_answered_again(self):
        ds = DeepSeek()
        engine, llm, transport, clock = world(asking_for_a_song(ds))
        engine.music = player = Jukebox(RISKY)  # B站上那首歌的名字里有 DeepSeek 拒的词
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 1, "@小D 放首歌")  # 主服 #闲聊：歌放上了，带着点歌结果回话的那一步被拒
            self.assertEqual(player.played, [(GUILD, "随便放一首")])  # 对照：歌真的放上了
            self.assertIn(RISKY, record(engine, sync))  # 对照：点歌的结果（连歌名）记进了共用的记录
            await say(engine, clock, 2, "@小D 大厅这边呢", channel=HALL, author=21, name="小红")  # 带着它：被拒了
        await say(engine, clock, 3, "@小D 还在吗", channel=HALL, author=21, name="小红")
        await say(engine, clock, 4, "@小D 主服也问一句")
        self.assertEqual(ds.results, ["回了", "拒了", "拒了", "回了", "回了"])  # 只多拒了一次：之后两边都照常回
        whole = record(engine, sync)
        self.assertNotIn(RISKY, whole)
        self.assertIn(f"{MUSIC_UNANSWERED}\n{PASSIVE_REJECTED}", whole)  # 点过歌的那句留着，歌名换成一句“没有记下来”
        self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))

    async def test_control_an_unlinked_channel_keeps_its_music_result_as_before(self):
        # 对照（control）：没连通的频道跟 2.7.1 一样：回话的那一步被拒以后，点歌的结果照旧记在它自己那份里，之后被拒也不换掉
        ds = DeepSeek()
        engine, llm, transport, clock = world(asking_for_a_song(ds))
        engine.music = Jukebox(RISKY)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 1, "@小D 放首歌")
            await say(engine, clock, 2, "@小D 在吗")
        self.assertEqual(ds.results, ["回了", "拒了", "拒了"])
        own = own_record(engine, CHANNEL, GUILD)
        self.assertIn(RISKY, own)
        self.assertNotIn(PASSIVE_REJECTED, own)


class NowPlayingInASameServerLink(unittest.IsolatedAsyncioTestCase):
    async def test_a_stop_is_told_although_another_channel_of_the_server_wrote_that_the_song_was_playing(self):
        for how in ("同一个服务器的两个频道连通", "写那句的频道后来断开了"):
            with self.subTest(how):
                engine, llm, transport, clock = world()
                playing = jukebox(engine)
                if how == "同一个服务器的两个频道连通":
                    connect(engine, CHANNEL, LOBBY)  # 主服 #闲聊 + 主服 #大厅：同一个语音频道
                    first, then, stopped = CHANNEL, LOBBY, MUSIC_IDLE
                else:
                    three_linked(engine)  # 主服 #闲聊、别的服 #大厅、主服 #大厅（跨服务器了：写明是哪个服务器的语音频道）
                    first, then, stopped = LOBBY, CHANNEL, MUSIC_IDLE_AT.format(server="主服")
                playing[GUILD] = PLAYING
                author, name = SPEAKER[first]
                await say(engine, clock, 1, "@小D 在放什么", channel=first, author=author, name=name)  # 写了“正在放：晴天”
                if how == "写那句的频道后来断开了":
                    disconnect(engine, LOBBY)  # 它说过的留在组里（组里还有主服 #闲聊、别的服 #大厅）
                del playing[GUILD]  # 歌停了
                author, name = SPEAKER[then]
                await say(engine, clock, 2, "@小D 现在还在放吗", channel=then, author=author, name=name)
                # 这一轮告诉它停了：记录里最后那句不再是“正在放：晴天”
                self.assertEqual(last_music(history_of(llm)), stopped)

    async def test_a_song_that_changes_back_is_told_after_another_channel_of_the_server_wrote_a_newer_line(self):
        engine, llm, transport, clock = world()
        playing = jukebox(engine)
        connect(engine, CHANNEL, LOBBY)  # 主服 #闲聊 + 主服 #大厅
        playing[GUILD] = PLAYING
        await say(engine, clock, 1, "@小D 在放什么", channel=LOBBY, author=12, name="小雨")  # 主服 #大厅写了“晴天”
        playing[GUILD] = OTHER_SONG
        await say(engine, clock, 2, "@小D 换歌了吗")  # 主服 #闲聊写了“稻香”
        playing[GUILD] = PLAYING  # 又放回晴天
        await say(engine, clock, 3, "@小D 现在放的什么", channel=LOBBY, author=12, name="小雨")
        # 主服 #大厅自己上次写的也是晴天，可是记录里最新那句是稻香：照样写一句
        self.assertEqual(last_music(history_of(llm)), MUSIC_NOW.format(text=PLAYING))

    async def test_control_each_server_of_a_cross_server_link_keeps_its_own_line(self):
        # 对照（control）：跨服务器的连通里每个服务器的语音频道是各自的：别的服 #大厅不照主服写的那几句比（别的服一直没在放歌，
        # 它一句都不用写）
        engine, llm, transport, clock = world()
        playing = jukebox(engine)
        connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        playing[GUILD] = PLAYING
        await say(engine, clock, 1, "@小D 在放什么")  # 主服 #闲聊写了“「主服」的语音频道……晴天”
        await say(engine, clock, 2, "@小D 别的服呢", channel=HALL, author=21, name="小红")
        playing[GUILD] = OTHER_SONG
        await say(engine, clock, 3, "@小D 换歌了吗")  # 主服 #闲聊又写了一句（稻香）
        await say(engine, clock, 4, "@小D 那别的服呢", channel=HALL, author=21, name="小红")
        seen = history_of(llm)
        self.assertIn(MUSIC_NOW_AT.format(server="主服", text=OTHER_SONG), seen)  # 对照：主服写的在记录里
        self.assertNotIn("「别的服」的语音频道", seen)


class ChannelListInASameServerLink(unittest.IsolatedAsyncioTestCase):
    async def test_a_list_that_changes_back_is_written_again_after_another_channel_of_the_server_wrote_a_newer_one(self):
        engine, llm, transport, clock = world()
        engine.settings.set("guild", GUILD, "share_memory", True)
        for cid, name in ((GAMES, "游戏"), (ART, "画画")):
            transport.places[cid] = ChannelPlace(guild_id=GUILD, name=name, public=True)
            transport.names[cid] = name
        await say(engine, clock, 1, "@小D 游戏频道这边", channel=GAMES)  # #游戏 有了记录：能翻
        connect(engine, CHANNEL, LOBBY)  # 主服 #闲聊 + 主服 #大厅
        await say(engine, clock, 2, "@小D 大厅这边", channel=LOBBY, author=12, name="小雨")  # 主服 #大厅写了名单：#游戏
        await say(engine, clock, 3, "@小D 画画频道这边", channel=ART)  # #画画 也有了记录
        await say(engine, clock, 4, "@小D 闲聊这边")  # 主服 #闲聊写了名单：#游戏、#画画
        self.assertEqual(last_list(history_of(llm)), ("#游戏", "#画画"))  # 对照
        engine.reset_channel(ART)  # #画画 清空了记忆：翻不到了，名单又只有 #游戏
        await say(engine, clock, 5, "@小D 大厅又来了", channel=LOBBY, author=12, name="小雨")
        # 主服 #大厅自己上次写的也是只有 #游戏，可是记录里最新的名单还有 #画画：照样再写一遍
        self.assertEqual(last_list(history_of(llm)), ("#游戏",))


if __name__ == "__main__":
    unittest.main()
