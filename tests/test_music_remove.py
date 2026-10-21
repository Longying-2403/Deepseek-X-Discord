"""2.2.1：从播放队列里删歌、清空队列，/ds-music 删除 输入时列出队列里的歌，模型也知道有哪些斜杠指令。"""

import asyncio
import json
import unittest

import test_music as tm
from helpers import CHANNEL, GUILD

import dsbot.music as music
from dsbot.actions import parse_actions
from dsbot.music import Track, positions, title_matches
from dsbot.prompts import SYSTEM_RULES
from dsbot.render import assistant_text

LABEL = "（UP主：UP） [3:20]"  # FakeSource 默认的 UP主和时长


def url(i: int) -> str:
    return f"https://www.bilibili.com/video/av{910000 + i}"


class RemoveCase(tm.PlayerCase):
    def setUp(self):
        super().setUp()
        self.voice.users[(GUILD, 13)] = tm.VOICE

    async def queue_up(self, titles, users=None):
        """第一首在放，后面的排着。users：每一首是谁点的（默认都是阿明）。"""
        names = {11: "阿明", 12: "小雨", 13: "阿杰"}
        urls = []
        for i, title in enumerate(titles):
            urls.append(self.source.add(url(i), title))
            user = users[i] if users else 11
            await self.play(urls[-1], user=user, name=names[user])
        await tm.settle()
        return urls

    def queue_titles(self):
        g = self.player._guilds[GUILD]
        return [t.title for t in g.queue]


class Removing(RemoveCase):
    async def test_by_number(self):
        urls = await self.queue_up(["歌0", "歌1", "歌2", "歌3"])
        self.assertEqual(self.source.downloads, urls[:3])  # 正在放的 + 接下来两首
        out = await self.player.remove(GUILD, 12, ["2"])
        self.assertEqual(out, f"从队列里删掉了《歌2》{LABEL}（阿明 点的），后面还有 2 首")
        self.assertEqual(self.queue_titles(), ["歌1", "歌3"])
        self.assertIn("/cache/av910002-3.m4a", self.source.discarded)  # 下好的删掉
        await tm.settle()
        self.assertEqual(self.source.downloads, urls)  # 往前挪了：后面那首接着下载
        self.assertEqual(self.said, [])

    async def test_several_numbers_count_from_before(self):
        await self.queue_up(["歌0", "歌1", "歌2", "歌3", "歌4"])
        out = await self.player.remove(GUILD, 11, ["2", "4"])
        self.assertEqual(out, f"从队列里删掉了 2 首：《歌2》{LABEL}（阿明 点的）、《歌4》{LABEL}（阿明 点的），后面还有 2 首")
        self.assertEqual(self.queue_titles(), ["歌1", "歌3"])  # 不是删完第 2 首再数第 4 首
        await self.queue_up_more(["歌5", "歌6"])
        self.assertEqual(self.queue_titles(), ["歌1", "歌3", "歌5", "歌6"])
        out = await self.player.remove(GUILD, 11, ["第1首和第三首"])
        self.assertEqual(self.queue_titles(), ["歌3", "歌6"])
        self.assertTrue(out.startswith("从队列里删掉了 2 首：《歌1》"), out)

    async def queue_up_more(self, titles):
        for i, title in enumerate(titles, 50):
            await self.play(self.source.add(url(i), title))
        await tm.settle()

    async def test_own_latest_when_nothing_is_said(self):
        await self.queue_up(["歌0", "歌1", "歌2", "歌3"], users=[11, 11, 12, 11])
        out = await self.player.remove(GUILD, 11, [])
        self.assertEqual(out, f"从队列里删掉了《歌3》{LABEL}（阿明 点的），后面还有 2 首")
        self.assertEqual(await self.player.remove(GUILD, 11, [""]), f"从队列里删掉了《歌1》{LABEL}（阿明 点的），后面还有 1 首")
        out = await self.player.remove(GUILD, 11)  # 队列里没有他点的了：正在放的是他点的 → 切掉
        self.assertEqual(out, f"跳过了正在放的《歌0》{LABEL}，接下来放《歌2》{LABEL}")
        self.assertEqual(await self.player.remove(GUILD, 11), "队列里没有你点的歌；要删别人点的，说是第几首或者歌名")
        await tm.settle()
        self.assertEqual(self.said, [])  # 回复里已经说了接下来放什么

    async def test_by_title(self):
        await self.queue_up(["周杰伦 - 晴天", "稻香", "七里香"])
        self.assertEqual(await self.player.remove(GUILD, 12, ["《稻香》"]), f"从队列里删掉了《稻香》{LABEL}（阿明 点的），后面还有 1 首")
        self.assertEqual(await self.player.remove(GUILD, 12, ["不存在的歌"]), "队列里没有对得上「不存在的歌」的歌")
        self.assertEqual(self.queue_titles(), ["七里香"])
        self.assertTrue(title_matches("周杰伦 七里香", Track(url="x", title="七里香", uploader="周杰伦")))
        self.assertTrue(title_matches("晴天", Track(url="x", title="周杰伦 - 晴天")))
        self.assertFalse(title_matches("周杰伦 稻香", Track(url="x", title="七里香", uploader="周杰伦")))
        self.assertFalse(title_matches("《》", Track(url="x", title="七里香")))

    async def test_several_titles_match(self):
        urls = [self.source.add(url(i), t) for i, t in enumerate(["晴天 MV", "晴天 现场版", "晴天 钢琴版"])]
        await self.play(urls[0])
        await self.play(urls[1], user=12, name="小雨")
        await self.play(urls[2])
        out = await self.player.remove(GUILD, 13, ["晴天"])
        self.assertEqual(
            out,
            f"有好几首对得上「晴天」：正在放的《晴天 MV》{LABEL}、第 1 首《晴天 现场版》{LABEL}、第 2 首《晴天 钢琴版》{LABEL}，说一下是第几首",
        )
        self.assertEqual(len(self.queue_titles()), 2)  # 没删
        out = await self.player.remove(GUILD, 12, ["晴天"])  # 其中只有一首是她点的：就是那首
        self.assertTrue(out.startswith("从队列里删掉了《晴天 现场版》"), out)

    async def test_same_song_twice_removes_the_later_one(self):
        a = self.source.add(url(0), "晴天")
        b = self.source.add(url(1), "稻香")
        for link in (a, b, a, b):
            await self.play(link)
        await tm.settle()
        out = await self.player.remove(GUILD, 12, ["稻香"])
        self.assertTrue(out.startswith("从队列里删掉了《稻香》"), out)
        g = self.player._guilds[GUILD]
        self.assertEqual([t.title for t in g.queue], ["稻香", "晴天"])  # 删的是后点的那次
        out = await self.player.remove(GUILD, 12, ["晴天"])  # 正在放一首晴天、后面还排着一首：删排着的那首
        self.assertTrue(out.startswith("从队列里删掉了《晴天》"), out)
        self.assertEqual(g.current.title, "晴天")

    async def test_number_that_is_a_title(self):
        await self.queue_up(["晴天", "1874", "稻香"])
        self.assertTrue((await self.player.remove(GUILD, 12, ["1874"])).startswith("从队列里删掉了《1874》"))
        self.assertEqual(self.queue_titles(), ["稻香"])

    async def test_bad_numbers(self):
        await self.queue_up(["歌0", "歌1", "歌2", "歌3"])
        self.assertEqual(await self.player.remove(GUILD, 12, ["5"]), "队列里只有 3 首，没有第 5 首")
        self.assertEqual(await self.player.remove(GUILD, 12, ["0"]), "编号从 1 开始（1 是下一首；正在放的这首要用切歌）")
        out = await self.player.remove(GUILD, 12, ["2 9"])
        self.assertEqual(out, f"从队列里删掉了《歌2》{LABEL}（阿明 点的），后面还有 2 首；队列里只有 3 首，没有第 9 首")
        self.assertTrue((await self.player.remove(GUILD, 12, ["最后一首"])).startswith("从队列里删掉了《歌3》"))
        self.assertTrue((await self.player.remove(GUILD, 12, ["-1"])).startswith("从队列里删掉了《歌1》"))
        self.assertEqual(await self.player.remove(GUILD, 12, ["1"]), "后面没有排着的歌了（正在放的这首要用切歌）")

    async def test_needs_the_same_voice_channel(self):
        self.assertEqual(await self.player.remove(GUILD, 11, ["1"]), "现在没在放歌")
        self.assertEqual(await self.player.clear(GUILD, 11), "现在没在放歌")
        await self.queue_up(["歌0", "歌1"])
        self.voice.users[(GUILD, 13)] = tm.OTHER
        self.assertEqual(await self.player.remove(GUILD, 13, ["1"]), "要在我放歌的「语音500」里才能操作")
        self.assertEqual(await self.player.clear(GUILD, 13), "要在我放歌的「语音500」里才能操作")
        self.assertEqual(await self.player.remove(GUILD, 99), "要在我放歌的「语音500」里才能操作")
        self.assertEqual(self.queue_titles(), ["歌1"])

    async def test_first_song_still_downloading(self):
        a, b = self.source.add(url(0), "歌0"), self.source.add(url(1), "歌1")
        self.source.gates[a] = asyncio.Event()  # 很长的视频，还在下载
        self.assertEqual(await self.play(a), f"正在下载《歌0》{LABEL}，下好了马上开始放")
        await self.play(b)
        out = await self.player.remove(GUILD, 12, ["1"])
        self.assertEqual(out, f"从队列里删掉了《歌0》{LABEL}（阿明 点的），下一首《歌1》{LABEL}还在下载，下好了就放")
        self.source.gates[a].set()  # 下载被中断
        await asyncio.sleep(0.02)
        await tm.settle()
        self.assertEqual([p for _, p in self.voice.played], ["/cache/av910001-2.m4a"])
        self.assertEqual(self.said, [(CHANNEL, f"🎵 现在放：《歌1》{LABEL}（阿明 点的）")])

    async def test_removing_the_only_song_that_is_downloading(self):
        a = self.source.add(url(0), "歌0")
        self.source.gates[a] = asyncio.Event()
        await self.play(a)
        out = await self.player.remove(GUILD, 11)
        self.assertEqual(out, f"从队列里删掉了《歌0》{LABEL}（阿明 点的），后面没有歌了")
        self.source.gates[a].set()
        await tm.settle()
        self.assertEqual(self.voice.played, [])
        self.assertEqual(self.player.queue_text(GUILD), "现在没在放歌")
        self.clock.advance(music.IDLE_LEAVE + 1)
        await self.player.tick()
        await self.player.tick()
        self.assertNotIn(GUILD, self.voice.bot)  # 没歌了：过一阵安静地离开

    async def test_removed_while_downloads_are_held(self):
        urls = [self.source.add(url(i), f"歌{i}") for i in range(4)]
        await self.play(urls[0])
        self.source.fail[urls[1]] = music.TryLater("B站暂时把请求拦下来了（风控），过几分钟再试", retry=False)
        for link in urls[1:]:
            await self.play(link)
        await tm.settle()
        g = self.player._guilds[GUILD]
        last = g.queue[-1]
        self.assertFalse(last.fetching)
        await self.player.remove(GUILD, 12, ["3"])
        self.assertEqual((last.state, last.done.is_set(), last.cancel.is_set()), ("cancelled", True, True))
        self.clock.advance(music.HOLD_SECONDS)
        await self.player.tick()
        await tm.settle()
        self.assertNotIn(urls[3], self.source.downloads)  # 删掉的不会再下载

    async def test_current_song_by_choice_is_a_skip(self):
        await self.queue_up(["歌0", "歌1"])
        value = self.player.choices(GUILD)[0][1]
        self.assertEqual(await self.player.remove(GUILD, 12, [value]), f"跳过了正在放的《歌0》{LABEL}，接下来放《歌1》{LABEL}")
        self.assertEqual(await self.player.remove(GUILD, 12, [value]), "这首已经不在队列里了（可能刚放完，或者被删了）")


class Clearing(RemoveCase):
    async def test_clear_keeps_the_current_song(self):
        await self.queue_up(["歌0", "歌1", "歌2"])
        self.assertEqual(await self.player.clear(GUILD, 12), f"清掉了后面排着的 2 首，正在放的《歌0》{LABEL}放完就没了")
        self.assertEqual(self.player.queue_text(GUILD), f"正在放：《歌0》{LABEL}（阿明 点的）")
        self.assertEqual(sorted(self.source.discarded), ["/cache/av910001-2.m4a", "/cache/av910002-3.m4a"])
        self.assertEqual(await self.player.clear(GUILD, 12), "后面没有排着的歌")
        self.voice.finish(GUILD)
        await asyncio.sleep(0.05)
        await tm.settle()
        self.assertEqual(self.player.queue_text(GUILD), "现在没在放歌")
        self.assertEqual(len(self.voice.played), 1)

    async def test_clear_while_the_first_song_downloads(self):
        a, b = self.source.add(url(0), "歌0"), self.source.add(url(1), "歌1")
        self.source.gates[a] = asyncio.Event()
        await self.play(a)
        await self.play(b)
        self.assertEqual(await self.player.clear(GUILD, 11), "清掉了队列里的 2 首，现在没有歌了")
        self.source.gates[a].set()
        await tm.settle()
        self.assertEqual(self.voice.played, [])
        self.assertEqual(self.source.downloads, [a])


class Choosing(RemoveCase):
    async def test_choices_follow_the_queue(self):
        await self.queue_up(["晴天", "稻香", "七里香"], users=[11, 12, 11])
        self.assertEqual(
            self.player.choices(GUILD),
            [("正在放：晴天（阿明 点的）", "id:1"), ("1. 稻香（小雨 点的）", "id:2"), ("2. 七里香（阿明 点的）", "id:3")],
        )
        self.assertEqual(self.player.choices(GUILD, "2"), [("2. 七里香（阿明 点的）", "id:3")])
        self.assertEqual(self.player.choices(GUILD, "稻"), [("1. 稻香（小雨 点的）", "id:2")])
        self.assertEqual(self.player.choices(GUILD, "小雨"), [("1. 稻香（小雨 点的）", "id:2")])
        self.assertEqual(self.player.choices(GUILD, "不存在"), [])
        self.assertEqual(self.player.choices(GUILD + 1), [])
        self.voice.finish(GUILD)  # 选好之后队列往前走了一首
        await asyncio.sleep(0.05)
        await tm.settle()
        out = await self.player.remove(GUILD, 12, ["id:3"])
        self.assertEqual(out, f"从队列里删掉了《七里香》{LABEL}（阿明 点的），后面没有排着的歌了")
        self.assertEqual(self.player._guilds[GUILD].current.title, "稻香")

    async def test_choice_limits(self):
        long = "很长的标题" * 40
        await self.queue_up(["歌0"] + [long] * 30)
        rows = self.player.choices(GUILD)
        self.assertEqual(len(rows), music.QUEUE_CHOICES)
        self.assertTrue(all(0 < len(name) <= 100 and len(value) <= 100 for name, value in rows))


class Parsing(unittest.TestCase):
    def parse(self, *items):
        actions, ok = parse_actions(json.dumps({"actions": list(items)}, ensure_ascii=False))
        self.assertTrue(ok)
        return actions

    def test_remove_shapes(self):
        actions = self.parse(
            {"type": "music", "op": "remove", "index": 2},
            {"type": "music", "op": "删除", "index": [3, "第4首", 3]},
            {"type": "music", "op": "remove", "query": "稻香"},
            {"type": "music", "op": "remove", "index": "七里香"},
            {"type": "music", "op": "delete", "query": "晴天", "index": 5},
            {"type": "music", "op": "remove", "to": "#7"},
        )
        self.assertEqual(
            [(a.op, a.index, a.query, a.to) for a in actions],
            [
                ("remove", 2, None, None),
                ("remove", 3, None, None),
                ("remove", None, "第4首", None),  # 原样交给点歌那边认（还是第 4 首，但不会被当成歌名《4》）
                ("remove", None, "稻香", None),
                ("remove", None, "七里香", None),
                ("remove", None, "晴天", None),  # 写了歌名就按歌名删（编号可能是猜的）
                ("remove", None, None, 7),
            ],
        )
        self.assertEqual(actions[0].to_json(), {"type": "music", "op": "remove", "index": 2})
        self.assertEqual(actions[3].to_json(), {"type": "music", "op": "remove", "query": "稻香"})
        self.assertEqual(actions[6].to_json(), {"type": "music", "op": "remove", "to": 7})
        said = assistant_text(json.dumps({"actions": [a.to_json() for a in actions[:1] + actions[3:4] + actions[6:]]}, ensure_ascii=False), "小D")
        self.assertEqual(said.split("\n"), ["小D 从播放队列里删了第 2 首", "小D 从播放队列里删了「稻香」", "小D 从播放队列里删了刚点的歌"])

    def test_limits_and_clear(self):
        actions = self.parse(*[{"type": "music", "op": "remove", "index": list(range(1, 9))}] * 2, {"type": "music", "op": "remove", "index": list(range(9, 20))})
        self.assertEqual([a.index for a in actions], list(range(1, 9)) + [9, 10])  # 一轮最多删 10 首，重复的不算
        actions = self.parse({"type": "music", "op": "clear"}, {"type": "music", "op": "清空"}, {"type": "music", "op": "remove"}, {"type": "music", "op": "remove"})
        self.assertEqual([a.op for a in actions], ["clear", "remove"])
        self.assertEqual(actions[0].to_json(), {"type": "music", "op": "clear"})
        self.assertEqual(assistant_text(json.dumps({"actions": [actions[0].to_json()]}), "小D"), "小D 清空了播放队列")

    def test_positions(self):
        cases = {
            "2": [2], "第2首": [2], "#3": [3], "2号": [2], "2.": [2], "第二首": [2], "第十二首": [12], "二十首": [20],
            "2 3": [2, 3], "2、5和7": [2, 5, 7], "第1首和第3首": [1, 3], "最后一首": [-1], "-1": [-1], "２": [2],
            "稻香": None, "七": None, "我和你": None, "": None,
            "2-3": [2, 3], "4~2": [2, 3, 4], "2到4": [2, 3, 4], "第 2 到第 4 首": [2, 3, 4], "2 - 3": [2, 3],
            "1-0": None, "1-200": None, "2.3": None,
        }
        for text, want in cases.items():
            self.assertEqual(positions(text), want, text)

    def test_prompt(self):
        self.assertIn('{"type": "music", "op": "remove"}', SYSTEM_RULES)
        self.assertIn('{"type": "music", "op": "remove", "index": 2}', SYSTEM_RULES)
        self.assertIn("clear（清空后面排着的，正在放的接着放）", SYSTEM_RULES)
        self.assertIn("/ds-music 点歌：播放、切歌、暂停、继续、停止、队列、删除、清空", SYSTEM_RULES)
        self.assertIn("## 斜杠指令", SYSTEM_RULES)
        self.assertIn("会照你写的顺序一个一个办", SYSTEM_RULES)
        self.assertIn("/ds-rp", SYSTEM_RULES.split("## 斜杠指令", 1)[1].split("##", 1)[0])


class PromptExample(RemoveCase):
    async def test_example_matches_the_real_result(self):
        x, y = self.source.add(url(0), "晴天"), self.source.add(url(2), "七里香")
        z = self.source.add(url(1), "稻香", "周杰伦", 223)
        await self.play(x)
        await self.play(z, user=12, name="小雨")
        await self.play(y)
        out = await self.player.remove(GUILD, 12)
        self.assertIn(f"（点歌：{out}）", SYSTEM_RULES)


class InChatRemoveCase(tm.InChatCase):
    async def fill(self, engine, n=5):
        for i in range(n):
            await engine.music.play(GUILD, CHANNEL, 11, "阿明", self.source.add(url(i), f"歌{i}"))
        await tm.settle()

    def make(self, outputs, **kw):
        made = super().make(outputs, **kw)
        self.source = made[5]
        return made


class InChatRemoving(InChatRemoveCase):

    async def test_two_numbers_in_one_round(self):
        remove = json.dumps({"actions": [{"type": "music", "op": "remove", "index": 2}, {"type": "music", "op": "remove", "index": 3}]})
        reply = json.dumps({"actions": [{"type": "reply", "to": 1, "messages": ["删好了"]}]}, ensure_ascii=False)
        engine, llm, transport, clock, voice, source = self.make([remove, reply])
        await self.fill(engine)
        await self.chat(engine, clock, 501, "@小D 把第2首和第3首删了")
        result = llm.calls[1]["messages"][-1]["content"]
        self.assertIn(
            f"（点歌：从队列里删掉了 2 首：《歌2》{LABEL}（阿明 点的）、《歌3》{LABEL}（阿明 点的），后面还有 2 首）", result
        )
        self.assertEqual(transport.sent[-1][1], "删好了")
        self.assertEqual([t.title for t in engine.music._guilds[GUILD].queue], ["歌1", "歌4"])

    async def test_without_saying_which(self):
        remove = json.dumps({"actions": [{"type": "music", "op": "remove"}]})
        nothing = json.dumps({"actions": []})
        engine, llm, transport, clock, voice, source = self.make([remove, nothing])
        voice.users[(GUILD, 12)] = tm.VOICE
        await self.fill(engine, 2)
        await engine.music.play(GUILD, CHANNEL, 12, "小雨", source.add(url(7), "点错的歌"))
        await engine.music.play(GUILD, CHANNEL, 11, "阿明", source.add(url(8), "阿明的歌"))
        await self.chat(engine, clock, 501, "@小D 刚才点错了，删掉", author=12, name="小雨")
        self.assertIn(f"（点歌：从队列里删掉了《点错的歌》{LABEL}（小雨 点的），后面还有 2 首）", llm.calls[1]["messages"][-1]["content"])

    async def test_numbers_count_from_the_start_of_the_round(self):
        mixed = json.dumps({"actions": [
            {"type": "music", "op": "skip"},
            {"type": "music", "op": "play", "query": url(9)},
            {"type": "music", "op": "remove", "index": 2},
        ]})
        engine, llm, transport, clock, voice, source = self.make([mixed, json.dumps({"actions": []})])
        await self.fill(engine, 4)  # 在放歌0，后面排着歌1、歌2、歌3
        source.add(url(9), "新歌")
        await self.chat(engine, clock, 501, f"@小D 切歌，把第2首删了，再放 {url(9)}")
        g = engine.music._guilds[GUILD]
        self.assertEqual(g.current.title, "歌1")  # 删的是他说的时候的第 2 首（歌2），不是切歌之后的第 2 首
        self.assertEqual([t.title for t in g.queue], ["歌3", "新歌"])

    async def test_replace_a_wrong_song(self):
        swap = json.dumps({"actions": [{"type": "music", "op": "remove"}, {"type": "music", "op": "play", "query": url(9)}]})
        engine, llm, transport, clock, voice, source = self.make([swap, json.dumps({"actions": []})])
        await self.fill(engine, 3)
        source.add(url(9), "七里香")
        await self.chat(engine, clock, 501, f"@小D 刚才那首点错了，换成 {url(9)}")
        self.assertEqual([t.title for t in engine.music._guilds[GUILD].queue], ["歌1", "七里香"])  # 先删点错的歌2，再放新的

    async def test_play_then_remove_removes_the_new_song(self):
        """截图里那次：机器人刚离开语音，一句话“放一首歌，然后删除”。"""
        both = json.dumps({"actions": [{"type": "music", "op": "play", "query": url(9)}, {"type": "music", "op": "remove"}]})
        engine, llm, transport, clock, voice, source = self.make([both, json.dumps({"actions": []})])
        source.add(url(9), "新歌")
        await self.chat(engine, clock, 501, f"@小D 测试一下，放一首歌，然后删除 {url(9)}")
        results = [line for line in llm.calls[1]["messages"][-1]["content"].split("\n") if line.startswith("（点歌")]
        self.assertEqual(results, [f"（点歌：开始播放：《新歌》{LABEL}）", f"（点歌：跳过了正在放的《新歌》{LABEL}，后面没有歌了）"])
        self.assertEqual(voice.playing, {})

    async def test_play_then_remove_while_another_song_plays(self):
        both = json.dumps({"actions": [{"type": "music", "op": "play", "query": url(9)}, {"type": "music", "op": "remove"}]})
        engine, llm, transport, clock, voice, source = self.make([both, json.dumps({"actions": []})])
        await self.fill(engine, 2)  # 他之前点的歌0 在放、歌1 排着
        source.add(url(9), "新歌")
        await self.chat(engine, clock, 501, f"@小D 放 {url(9)}，然后删掉")
        g = engine.music._guilds[GUILD]
        self.assertEqual((g.current.title, [t.title for t in g.queue]), ("歌0", ["歌1"]))  # 删的是刚点的新歌

    async def test_failed_play_then_remove_removes_nothing(self):
        both = json.dumps({"actions": [{"type": "music", "op": "play", "query": url(9)}, {"type": "music", "op": "remove"}]})
        engine, llm, transport, clock, voice, source = self.make([both, json.dumps({"actions": []})])
        await self.fill(engine, 2)  # url(9) 没有加进假的 B站：点不上
        await self.chat(engine, clock, 501, f"@小D 放 {url(9)}，然后删掉")
        g = engine.music._guilds[GUILD]
        self.assertEqual((g.current.title, [t.title for t in g.queue]), ("歌0", ["歌1"]))
        self.assertIn("（点歌：这次点的歌没点上，没有要删的）", llm.calls[1]["messages"][-1]["content"])

    async def test_remove_the_playing_song_then_skip(self):
        out = json.dumps({"actions": [{"type": "music", "op": "remove"}, {"type": "music", "op": "skip"}]})
        engine, llm, transport, clock, voice, source = self.make([out, json.dumps({"actions": []})])
        await self.fill(engine, 3)
        engine.music._guilds[GUILD].queue[1].requester_id = 12  # 阿明自己只剩正在放的歌0
        engine.music._guilds[GUILD].queue[0].requester_id = 12
        await self.chat(engine, clock, 501, "@小D 我点的这首删了，再切一首")
        g = engine.music._guilds[GUILD]
        self.assertEqual((g.current.title, [t.title for t in g.queue]), ("歌1", ["歌2"]))  # 只切掉了一首
        self.assertIn("（点歌：要切的那首已经不在放了）", llm.calls[1]["messages"][-1]["content"])

    async def test_slash_command_path(self):
        engine, llm, transport, clock, voice, source = self.make([])
        await self.fill(engine, 4)
        view = engine.settings.view(GUILD, CHANNEL, None, None)
        out = await engine.music_request(GUILD, CHANNEL, 11, "阿明", "remove", "2", view=view)
        self.assertEqual(out, f"从队列里删掉了《歌2》{LABEL}（阿明 点的），后面还有 2 首")
        out = await engine.music_request(GUILD, CHANNEL, 11, "阿明", "remove", view=view)  # 不填：他自己最近点的
        self.assertEqual(out, f"从队列里删掉了《歌3》{LABEL}（阿明 点的），后面还有 1 首")
        self.assertEqual(await engine.music_request(GUILD, CHANNEL, 11, "阿明", "clear", view=view), f"清掉了后面排着的 1 首，正在放的《歌0》{LABEL}放完就没了")
        self.assertEqual(engine.music.queued(GUILD), 0)



class ReviewFixes(RemoveCase):
    """审查时发现的问题。"""

    async def test_numbers_past_the_end_are_not_titles(self):
        await self.queue_up(["晴天 2005 演唱会", "【4K修复】稻香 MV", "七里香", "Live 1080P 晴天"])
        self.assertEqual(await self.player.remove(GUILD, 12, ["5"]), "队列里只有 3 首，没有第 5 首")  # 不是切掉“2005”那首
        self.assertEqual(await self.player.remove(GUILD, 12, ["4"]), "队列里只有 3 首，没有第 4 首")  # 也不是删掉“4K”那首
        self.assertEqual(await self.player.clear(GUILD, 12), f"清掉了后面排着的 3 首，正在放的《晴天 2005 演唱会》{LABEL}放完就没了")
        self.assertEqual(await self.player.remove(GUILD, 12, ["1"]), "后面没有排着的歌了（正在放的这首要用切歌）")
        self.assertEqual(self.player._guilds[GUILD].current.title, "晴天 2005 演唱会")

    async def test_long_numbers_can_still_be_titles(self):
        await self.queue_up(["晴天", "陈奕迅《1874》MV", "18745 不是这首", "稻香"])
        self.assertTrue((await self.player.remove(GUILD, 12, ["1874"])).startswith("从队列里删掉了《陈奕迅《1874》MV》"))
        self.assertEqual(await self.player.remove(GUILD, 12, ["第1874首"]), "队列里只有 2 首，没有第 1874 首")
        self.assertEqual(await self.player.remove(GUILD, 12, ["187"]), "队列里只有 2 首，没有第 187 首")

    async def test_repeated_numbers(self):
        await self.queue_up(["歌0", "歌1", "歌2", "歌3"])
        self.assertEqual(await self.player.remove(GUILD, 12, ["2 2"]), f"从队列里删掉了《歌2》{LABEL}（阿明 点的），后面还有 2 首")
        self.assertEqual(await self.player.remove(GUILD, 12, ["2 最后一首"]), f"从队列里删掉了《歌3》{LABEL}（阿明 点的），后面还有 1 首")

    async def test_repeated_numbers_while_the_first_song_downloads(self):
        urls = [self.source.add(url(i), f"歌{i}") for i in range(4)]
        self.source.gates[urls[0]] = asyncio.Event()
        for link in urls:
            await self.play(link)
        out = await self.player.remove(GUILD, 12, ["1 2 2"])
        self.assertTrue(out.startswith("从队列里删掉了 2 首：《歌0》"), out)
        self.source.gates[urls[0]].set()
        await asyncio.sleep(0.02)
        await tm.settle()
        self.assertEqual(self.player._guilds[GUILD].current.title, "歌2")  # 没卡住：接着下载、接着放

    async def test_mine_together_with_a_number(self):
        await self.queue_up(["歌0", "歌1", "歌2", "歌3"], users=[11, 11, 12, 11])
        out = await self.player.remove(GUILD, 12, ["", "1"])
        self.assertEqual(out, f"从队列里删掉了 2 首：《歌2》{LABEL}（小雨 点的）、《歌1》{LABEL}（阿明 点的），后面还有 1 首")

    async def test_spaced_positions(self):
        for text, want in {"第 2 首": [2], "2 号": [2], "第 二 首": [2], "第 2 和第 4 首": [2, 4]}.items():
            self.assertEqual(positions(text), want, text)
        await self.queue_up(["歌0", "稻香", "七里香", "【首发】周杰伦 第2张专辑 主打歌"])
        self.assertTrue((await self.player.remove(GUILD, 12, ["第 2 首"])).startswith("从队列里删掉了《七里香》"))
        self.assertFalse(title_matches("第 2 首", Track(url="x", title="【首发】周杰伦 第2张专辑 主打歌")))
        self.assertFalse(title_matches("雨", Track(url="x", title="雨夜")))  # 一个字的要整个标题一样
        self.assertTrue(title_matches("雨", Track(url="x", title="《雨》")))
        self.assertTrue(title_matches("雨 周杰伦", Track(url="x", title="【周杰伦】雨 MV")))
        self.assertEqual(self.player.choices(GUILD, "首发"), [("2. 【首发】周杰伦 第2张专辑 主打歌（阿明 点的）", "id:4")])

    async def test_play_reply_when_removed_while_waiting(self):
        a, b = self.source.add(url(0), "歌0"), self.source.add(url(1), "歌1")
        self.source.gates[a] = asyncio.Event()
        await self.play(a)
        await self.player.skip(GUILD, 11)  # A 被切掉了，可它还没下载完：B 要排在它后面下载
        waiting = asyncio.ensure_future(self.play(b))
        await tm.settle()
        self.assertEqual(await self.player.remove(GUILD, 12, ["1"]), f"从队列里删掉了《歌1》{LABEL}（阿明 点的），后面没有歌了")
        self.assertEqual(await waiting, f"《歌1》{LABEL}还没开始放就被切掉了")

    async def test_clear_checks_the_channel_first_and_skip_mentions_the_hold(self):
        urls = [self.source.add(url(i), f"歌{i}") for i in range(3)]
        await self.play(urls[0])
        self.voice.users[(GUILD, 13)] = tm.OTHER
        self.assertEqual(await self.player.clear(GUILD, 13), "要在我放歌的「语音500」里才能操作")
        self.source.fail[urls[1]] = music.TryLater("B站暂时把请求拦下来了（风控），过几分钟再试", retry=False)
        for link in urls[1:]:
            await self.play(link)
        await tm.settle()
        out = await self.player.skip(GUILD, 12)
        self.assertEqual(out, f"跳过了《歌0》{LABEL}，B站暂时把请求拦下来了（风控），下一首《歌1》{LABEL}过几分钟再开始下载")

    async def test_round_state(self):
        await self.queue_up(["歌0", "歌1", "稻香", "歌3"], users=[11, 11, 12, 11])
        state = self.player.round_state(GUILD)
        self.assertEqual(([t.title for t in state.queue], state.playing), (["歌1", "稻香", "歌3"], 1))
        self.assertEqual(await self.player.remove(GUILD, 11, ["1"], state=state), f"从队列里删掉了《歌1》{LABEL}（阿明 点的），后面还有 2 首")
        self.assertEqual(await self.player.remove(GUILD, 11, ["3"], state=state), f"从队列里删掉了《歌3》{LABEL}（阿明 点的），后面还有 1 首")
        self.assertEqual(await self.player.remove(GUILD, 11, ["1"], state=state), f"第 1 首《歌1》{LABEL}已经不在队列里了")
        self.assertEqual(await self.player.remove(GUILD, 11, ["歌1"], state=state), "这首已经不在队列里了（可能刚放完，或者被删了）")
        self.assertTrue((await self.player.skip(GUILD, 12, state=state)).startswith("跳过了《歌0》"))
        self.assertEqual(await self.player.skip(GUILD, 12, state=state), "要切的那首已经不在放了")
        self.assertEqual(await self.player.clear(GUILD, 12, state=state), "清掉了队列里的 1 首，现在没有歌了")  # 稻香那时还排着

    async def test_round_state_remembers_new_songs(self):
        await self.queue_up(["歌0", "歌1"])
        state = self.player.round_state(GUILD)
        await self.play(self.source.add(url(9), "新歌"), state=state)
        self.assertEqual(await self.player.remove(GUILD, 11, [], state=state), f"从队列里删掉了《新歌》{LABEL}（阿明 点的），后面还有 1 首")
        state = self.player.round_state(GUILD)
        await self.play(url(8), state=state)  # 点不上
        self.assertEqual(await self.player.remove(GUILD, 11, [], state=state), "这次点的歌没点上，没有要删的")
        state = self.player.round_state(GUILD)
        await self.play(url(9), state=state)
        self.assertEqual(await self.player.clear(GUILD, 11, state=state), f"清掉了之前排着的 1 首，这次点的《新歌》{LABEL}留着")


class ReviewParsing(unittest.TestCase):
    def test_mixed_index_list(self):
        actions, _ = parse_actions(json.dumps({"actions": [{"type": "music", "op": "remove", "index": [2, "稻香", True]}]}, ensure_ascii=False))
        self.assertEqual([(a.index, a.query) for a in actions], [(2, None), (None, "稻香")])


class ReviewChat(InChatRemoveCase):
    async def test_two_people_remove_by_number_in_one_round(self):
        remove = json.dumps({"actions": [
            {"type": "music", "op": "remove", "index": 2, "to": 1},
            {"type": "music", "op": "remove", "index": 3, "to": 2},
        ]})
        engine, llm, transport, clock, voice, source = self.make([remove, json.dumps({"actions": []})])
        voice.users[(GUILD, 12)] = tm.VOICE
        await self.fill(engine)  # 在放歌0，后面排着歌1~歌4
        await engine.handle_message(tm.msg(501, "@小D 删掉第2首", mentions_bot=True))
        await engine.handle_message(tm.msg(502, "@小D 我要删第3首", author=12, name="小雨", mentions_bot=True))
        await engine.drain()
        self.assertEqual(len(llm.calls), 2)  # 两个人在同一轮里
        self.assertEqual([t.title for t in engine.music._guilds[GUILD].queue], ["歌1", "歌4"])  # 第 3 首是说的时候的歌3

    async def test_mine_and_a_number_in_one_round(self):
        remove = json.dumps({"actions": [{"type": "music", "op": "remove"}, {"type": "music", "op": "remove", "index": 1}]})
        engine, llm, transport, clock, voice, source = self.make([remove, json.dumps({"actions": []})])
        voice.users[(GUILD, 12)] = tm.VOICE
        await self.fill(engine, 3)
        await engine.music.play(GUILD, CHANNEL, 12, "小雨", source.add(url(7), "她点的"))
        await self.chat(engine, clock, 501, "@小D 我刚点的不要了，第1首也删掉", author=12, name="小雨")
        self.assertEqual([t.title for t in engine.music._guilds[GUILD].queue], ["歌2"])

    async def test_play_then_clear_keeps_the_new_song(self):
        mixed = json.dumps({"actions": [{"type": "music", "op": "play", "query": url(9)}, {"type": "music", "op": "clear"}]})
        engine, llm, transport, clock, voice, source = self.make([mixed, json.dumps({"actions": []})])
        await self.fill(engine, 3)
        source.add(url(9), "新歌")
        await self.chat(engine, clock, 501, f"@小D 放 {url(9)}，后面那些都不要了")
        self.assertEqual([t.title for t in engine.music._guilds[GUILD].queue], ["新歌"])



class SecondReviewFixes(RemoveCase):
    async def test_ranges_and_digits(self):
        await self.queue_up(["【2024跨年】周杰伦 晴天", "歌1", "歌2", "歌3", "Live 2013 青花瓷"])
        out = await self.player.remove(GUILD, 12, ["2-3"])
        self.assertEqual(out, f"从队列里删掉了 2 首：《歌2》{LABEL}（阿明 点的）、《歌3》{LABEL}（阿明 点的），后面还有 2 首")
        self.assertEqual(self.player._guilds[GUILD].current.title, "【2024跨年】周杰伦 晴天")  # 没有切掉“2024”那首
        self.assertEqual(await self.player.remove(GUILD, 12, ["2.3"]), "队列里没有对得上「2.3」的歌")
        self.assertEqual(await self.player.remove(GUILD, 12, ["1-0"]), "队列里没有对得上「1-0」的歌")
        self.assertEqual(await self.player.remove(GUILD, 12, ["1~5"]), f"从队列里删掉了 2 首：《歌1》{LABEL}（阿明 点的）、《Live 2013 青花瓷》{LABEL}（阿明 点的），后面没有排着的歌了；队列里只有 2 首，没有第 3、4、5 首")
        self.assertTrue(title_matches("2013", Track(url="x", title="Live 2013 青花瓷")))
        self.assertFalse(title_matches("1-0", Track(url="x", title="Live 1080P 晴天")))

    async def test_skip_only_what_was_playing(self):
        await self.queue_up(["歌0", "歌1", "歌2"])
        state = self.player.round_state(GUILD)
        await self.player.remove(GUILD, 12, [f"id:{state.playing}"], state=state)  # 删歌时已经切掉了正在放的
        self.assertEqual(await self.player.skip(GUILD, 12, state=state), "要切的那首已经不在放了")
        self.assertEqual(self.player._guilds[GUILD].current.title, "歌1")
        self.assertTrue((await self.player.skip(GUILD, 12)).startswith("跳过了《歌1》"))

    async def test_choices_by_position_words(self):
        await self.queue_up(["歌0", "歌1", "歌2", "歌3"])
        self.assertEqual(self.player.choices(GUILD, "#2"), [("2. 歌2（阿明 点的）", "id:3")])
        self.assertEqual(self.player.choices(GUILD, "第2"), [("2. 歌2（阿明 点的）", "id:3")])
        self.assertEqual(self.player.choices(GUILD, "最后一首"), [("3. 歌3（阿明 点的）", "id:4")])
        self.assertEqual(self.player.choices(GUILD, "2 3"), [])


class SecondReviewParsing(unittest.TestCase):
    def test_decorated_index_is_not_a_title_number(self):
        actions, _ = parse_actions(json.dumps({"actions": [{"type": "music", "op": "remove", "index": "第1874首"}]}, ensure_ascii=False))
        self.assertEqual([(a.index, a.query) for a in actions], [(None, "第1874首")])


class SecondReviewChat(InChatRemoveCase):
    async def round(self, engine, *messages):
        for mid, text, author, name in messages:
            await engine.handle_message(tm.msg(mid, text, author=author, name=name, mentions_bot=True))
        await engine.drain()

    def state(self, engine):
        g = engine.music._guilds.get(GUILD)
        return (g.current.title if g and g.current else None), [t.title for t in g.queue] if g else []

    async def test_skip_and_remove_of_the_same_song(self):
        out = json.dumps({"actions": [{"type": "music", "op": "skip"}, {"type": "music", "op": "remove"}]})
        engine, llm, transport, clock, voice, source = self.make([out, json.dumps({"actions": []})])
        voice.users[(GUILD, 12)] = tm.VOICE
        await engine.music.play(GUILD, CHANNEL, 12, "小雨", source.add(url(0), "她的歌"))
        await self.fill(engine, 3)  # 后面排着阿明的歌0、歌1、歌2
        await self.round(engine, (501, "@小D 切了吧，我刚点错了，删掉", 12, "小雨"))
        self.assertEqual(self.state(engine), ("歌0", ["歌1", "歌2"]))  # 只切掉了她那首
        self.assertIn("（点歌：这首已经不在队列里了（可能刚放完，或者被删了））", llm.calls[1]["messages"][-1]["content"])

    async def test_skip_and_remove_while_the_first_song_downloads(self):
        out = json.dumps({"actions": [{"type": "music", "op": "skip"}, {"type": "music", "op": "remove", "index": 1}]})
        engine, llm, transport, clock, voice, source = self.make([out, json.dumps({"actions": []})])
        source.gates[url(0)] = asyncio.Event()
        await self.fill(engine, 3)  # 歌0 还在下载，后面排着歌1、歌2
        await self.round(engine, (501, "@小D 切歌，第1首删了", 11, "阿明"))
        self.assertEqual([t.title for t in engine.music._guilds[GUILD].queue], ["歌1", "歌2"])  # 只少了歌0

    async def test_skip_then_clear_means_silence(self):
        out = json.dumps({"actions": [{"type": "music", "op": "skip"}, {"type": "music", "op": "clear"}]})
        engine, llm, transport, clock, voice, source = self.make([out, json.dumps({"actions": []})])
        await self.fill(engine, 3)
        await self.round(engine, (501, "@小D 这首切了，后面排的也都清掉", 11, "阿明"))
        self.assertEqual(self.state(engine), (None, []))

    async def test_titles_are_pinned_too(self):
        out = json.dumps({"actions": [
            {"type": "music", "op": "remove", "index": 3, "to": 1},
            {"type": "music", "op": "remove", "query": "晴天", "to": 2},
        ]}, ensure_ascii=False)
        engine, llm, transport, clock, voice, source = self.make([out, json.dumps({"actions": []})])
        voice.users[(GUILD, 12)] = tm.VOICE
        sunny, rice = source.add(url(1), "晴天"), source.add(url(2), "稻香")
        await self.fill(engine, 1)
        for link in (sunny, rice, sunny):
            await engine.music.play(GUILD, CHANNEL, 11, "阿明", link)
        await self.round(engine, (501, "@小D 删掉第3首", 11, "阿明"), (502, "@小D 重复的晴天删一个", 12, "小雨"))
        self.assertEqual(self.state(engine), ("歌0", ["晴天", "稻香"]))  # 两个人说的是同一首：只删掉一首晴天

    async def test_ambiguous_title_stays_ambiguous(self):
        out = json.dumps({"actions": [
            {"type": "music", "op": "remove", "index": 1, "to": 1},
            {"type": "music", "op": "remove", "query": "晴天", "to": 2},
        ]}, ensure_ascii=False)
        engine, llm, transport, clock, voice, source = self.make([out, json.dumps({"actions": []})])
        voice.users[(GUILD, 12)] = tm.VOICE
        await self.fill(engine, 1)
        for i, title in ((1, "晴天 MV"), (2, "晴天 钢琴版")):
            await engine.music.play(GUILD, CHANNEL, 11, "阿明", source.add(url(i), title))
        await self.round(engine, (501, "@小D 删掉第1首", 11, "阿明"), (502, "@小D 把晴天删了", 12, "小雨"))
        self.assertEqual(self.state(engine), ("歌0", ["晴天 钢琴版"]))  # 小雨说的“晴天”本来就有两首：不替她猜
        self.assertIn("有好几首对得上「晴天」", llm.calls[1]["messages"][-1]["content"])

    async def test_a_later_stop_still_wins(self):
        out = json.dumps({"actions": [{"type": "music", "op": "play", "query": url(9), "to": 1}, {"type": "music", "op": "stop", "to": 2}]})
        engine, llm, transport, clock, voice, source = self.make([out, json.dumps({"actions": []})])
        voice.users[(GUILD, 12)] = tm.VOICE
        source.add(url(9), "新歌")
        await self.fill(engine, 2)
        await self.round(engine, (501, f"@小D 放 {url(9)}", 12, "小雨"), (502, "@小D 别放了，停了吧", 11, "阿明"))
        self.assertNotIn(GUILD, voice.bot)  # 照说话的顺序：后说的“停了吧”算数
        self.assertEqual(self.state(engine), (None, []))



class ThirdReviewFixes(RemoveCase):
    """v2.2.2 改成照写的顺序办之后，审查时发现的问题。"""

    async def test_number_titles_follow_the_title_rules(self):
        x, y = self.source.add(url(0), "歌0"), self.source.add(url(2), "歌2")
        twin = self.source.add(url(1), "陈奕迅《1874》")
        for link in (x, twin, y, twin):
            await self.play(link)
        await tm.settle()
        state = self.player.round_state(GUILD)
        await self.player.remove(GUILD, 11, ["3"], state=state)  # 阿明删了后一首 1874
        self.assertEqual(await self.player.remove(GUILD, 12, ["1874"], state=state), "这首已经不在队列里了（可能刚放完，或者被删了）")
        self.assertEqual(self.queue_titles(), ["陈奕迅《1874》", "歌2"])  # 小雨说的就是那一首：不会再删掉另一首

    async def test_number_titles_stay_ambiguous(self):
        await self.queue_up(["歌0", "1874 MV", "1874 钢琴版"], users=[11, 11, 13])
        state = self.player.round_state(GUILD)
        await self.player.remove(GUILD, 11, ["1"], state=state)
        out = await self.player.remove(GUILD, 12, ["1874"], state=state)
        self.assertTrue(out.startswith("有好几首对得上「1874」"), out)
        self.assertIn("已经不在队列里的《1874 MV》", out)
        self.assertEqual(self.queue_titles(), ["1874 钢琴版"])

    async def test_songs_that_drop_out_mid_round_are_still_known(self):
        a, old, new = self.source.add(url(0), "歌0"), self.source.add(url(1), "她的旧歌"), self.source.add(url(2), "她的新歌")
        self.source.gates[new] = asyncio.Event()
        await self.play(a)
        await self.play(old, user=12, name="小雨")
        await self.play(new, user=12, name="小雨")
        await tm.settle()
        state = self.player.round_state(GUILD)
        self.source.fail[new] = music.MusicError("这个视频不存在，或者已经被删了")
        self.source.gates[new].set()  # 这一轮还在办别的事的时候，她新点的那首下载失败了
        await tm.settle()
        self.assertEqual(await self.player.remove(GUILD, 12, [], state=state), "这首已经不在队列里了（可能刚放完，或者被删了）")
        self.assertEqual(self.queue_titles(), ["她的旧歌"])  # 不会改删她更早点的

    async def test_a_song_that_ended_mid_round_keeps_titles_ambiguous(self):
        await self.queue_up(["晴天 MV", "晴天 钢琴版"], users=[11, 13])
        state = self.player.round_state(GUILD)
        self.voice.finish(GUILD)  # MV 放完了，钢琴版开始放
        await asyncio.sleep(0.05)
        await tm.settle()
        out = await self.player.remove(GUILD, 12, ["晴天"], state=state)
        self.assertTrue(out.startswith("有好几首对得上「晴天」"), out)
        self.assertEqual(self.player._guilds[GUILD].current.title, "晴天 钢琴版")  # 没有切掉阿杰点的

    async def test_skip_when_nothing_was_playing(self):
        state = self.player.round_state(GUILD)
        await self.play(self.source.add(url(9), "新歌"), state=state)
        self.assertEqual(await self.player.skip(GUILD, 12, state=state), "叫我切歌的时候还没在放歌，刚点的这首照放")
        self.assertEqual(self.player._guilds[GUILD].current.title, "新歌")

    async def test_last_attempt_decides_mine(self):
        await self.queue_up(["歌0"])
        state = self.player.round_state(GUILD)
        await self.play(self.source.add(url(9), "新歌"), state=state)
        await self.play(url(8), state=state)  # 第二首点不上
        self.assertEqual(await self.player.remove(GUILD, 11, [], state=state), "这次点的歌没点上，没有要删的")
        self.assertEqual(self.queue_titles(), ["新歌"])

    async def test_numbers_and_clear_messages_after_the_queue_changed(self):
        await self.queue_up(["歌0", "歌1", "歌2"])
        state = self.player.round_state(GUILD)
        await self.play(self.source.add(url(9), "新歌"), state=state)
        self.assertEqual(await self.player.remove(GUILD, 11, ["3"], state=state), "说的时候队列里只有 2 首，没有第 3 首")
        await self.play(self.source.add(url(8), "别人的歌"), user=12, name="小雨")  # 别的地方（/ds-music）刚点的
        out = await self.player.clear(GUILD, 11, state=state)
        self.assertEqual(out, f"清掉了之前排着的 2 首，这次点的《新歌》{LABEL}留着，后面还有别人刚点的 1 首")


class ThirdReviewChat(InChatRemoveCase):
    async def test_play_then_skip_when_nothing_was_playing(self):
        out = json.dumps({"actions": [{"type": "music", "op": "play", "query": url(9)}, {"type": "music", "op": "skip"}]})
        engine, llm, transport, clock, voice, source = self.make([out, json.dumps({"actions": []})])
        source.add(url(9), "新歌")
        await self.chat(engine, clock, 501, f"@小D 放 {url(9)}，这首切了吧")
        self.assertEqual(engine.music._guilds[GUILD].current.title, "新歌")

    async def test_queue_view_shows_the_final_queue(self):
        out = json.dumps({"actions": [{"type": "music", "op": "queue"}, {"type": "music", "op": "remove", "index": 2}]})
        engine, llm, transport, clock, voice, source = self.make([out, json.dumps({"actions": []})])
        await self.fill(engine, 4)
        await self.chat(engine, clock, 501, "@小D 看看队列，把第2首删了")
        results = [line for line in llm.calls[1]["messages"][-1]["content"].split("\n") if line.startswith("（点歌")]
        self.assertTrue(results[0].startswith("（点歌：从队列里删掉了《歌2》"), results)
        self.assertNotIn("歌2", results[1])  # 看到的是删完之后的队列


if __name__ == "__main__":
    unittest.main()
