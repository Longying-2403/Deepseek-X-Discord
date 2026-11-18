"""2.8.2：一轮里互不相干的几件事同时办（看图、读文件、打开链接、上网搜、找图）。
结果、名额、先后顺序要和一件一件办时一模一样，只是不用等前一件办完。"""

import asyncio
import random
import unittest

from helpers import CHANNEL, GUILD, FakeSearcher, make_engine, msg
from test_links_images import ACTIONS, IMAGE, REPLY, FakePictures, FakeReader, user_text
from test_music import LINK, VOICE, FakeSource, FakeVoice

import dsbot.music as music
from dsbot.engine import _settle, _together
from dsbot.models import FileAttachment, FileText, ImageAttachment, LinkRef
from dsbot.music import MusicPlayer

READ = lambda to: {"type": "read", "to": to}  # noqa: E731
SEARCH = lambda q: {"type": "search", "query": q}  # noqa: E731
PLAY = lambda q: {"type": "music", "op": "play", "query": q}  # noqa: E731


def scripted(*outputs):
    left = iter(outputs)
    return lambda call: next(left)


class Meeting:
    """几件事要同时在办才等得到：每件事进来报个到，到齐了才放行。一件一件办的话，第一件就等不到别的（超时出错）。"""

    def __init__(self, size: int):
        self.size, self.here, self.everyone = size, 0, asyncio.Event()

    async def join(self) -> None:
        self.here += 1
        if self.here >= self.size:
            self.everyone.set()
        await asyncio.wait_for(self.everyone.wait(), 2)


def image_urls(call) -> list[str]:
    content = call["messages"][-1]["content"]
    return [p["image_url"]["url"] for p in content if p["type"] == "image_url"] if isinstance(content, list) else []


def one_by_one(shown, triggers, budget, load, sides):
    """2.8.1 以前一张一张下载时的选法（照抄原来的写法）：返回 (选中的图, 下载过哪几张)。"""
    out, asked = {}, []
    trigger_ids = {m.message_id for m in triggers}
    for previews in (False, True):
        for m in reversed(shown):
            for att in m.images:
                if (att.kind == "preview") != previews:
                    continue
                if budget <= 0:
                    return out, asked
                wanted = m.message_id in trigger_ids
                side = sides[0] if wanted else sides[1]
                asked.append((att.filename, side))
                uri = load(att, side)
                if uri is None:
                    continue
                budget -= 1
                out.setdefault(m.message_id, []).append((uri, "auto" if wanted else "low"))
    return out, asked


class Pictures(unittest.IsolatedAsyncioTestCase):
    async def test_pictures_of_one_round_load_together(self):
        meeting = Meeting(3)

        async def loader(att, max_side):
            await meeting.join()
            return "data:image/jpeg;base64," + att.filename

        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "看到了")), image_loader=loader)
        images = [ImageAttachment(f"p{i}", "image/png", 1) for i in range(3)]
        await engine.handle_message(msg(501, "@小D 看图", mentions_bot=True, images=images))
        await engine.drain()
        self.assertEqual(image_urls(llm.calls[0]), [f"data:image/jpeg;base64,p{i}" for i in range(3)])
        self.assertEqual(len(transport.sent), 1)

    async def test_same_pictures_as_one_by_one(self):
        """随便什么样的一批消息：选中哪几张、先后顺序、缩到多大，都和一张一张下载时一样；下载过的也是同样那几张（不多下）。"""
        engine, llm, transport, clock = make_engine()
        sides = (engine.ec.image_max_side, engine.ec.low_detail_max_side)
        rng = random.Random(282)

        def load(att, side):
            return None if att.filename.endswith("x") else f"{att.filename}@{side}"  # 名字以 x 结尾的下不成

        for case in range(400):
            n = 0
            shown = []
            for k in range(rng.randint(1, 5)):
                images = []
                for _ in range(rng.randint(0, 4)):
                    n += 1
                    name = f"i{n}" + ("x" if rng.random() < 0.35 else "")
                    images.append(ImageAttachment(name, "image/png", 1, kind=rng.choice(["image", "gif", "preview", "sticker"])))
                shown.append(msg(600 + k, "图", images=images))
            triggers = [m for m in shown if rng.random() < 0.5]
            budget = rng.randint(0, 6)
            asked = []

            async def loader(att, side):
                asked.append((att.filename, side))
                for _ in range(rng.randint(0, 3)):  # 有快有慢：先下完的不一定排在前面
                    await asyncio.sleep(0)
                return load(att, side)

            engine.image_loader = loader
            engine.ec.max_images_per_round = budget
            got = await engine._images(shown, triggers)
            want, want_asked = one_by_one(shown, triggers, budget, load, sides)
            self.assertEqual(list(got.items()), list(want.items()), case)
            self.assertEqual(sorted(asked), sorted(want_asked), case)

    async def test_a_broken_picture_stops_the_others(self):
        stopped = []

        async def loader(att, max_side):
            if att.filename == "bad":
                await asyncio.sleep(0)
                raise ValueError("坏图")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.append(att.filename)
                raise

        engine, llm, transport, clock = make_engine(image_loader=loader)
        m = msg(501, "图", images=[ImageAttachment(name, "image/png", 1) for name in ("a", "bad", "b")])
        with self.assertRaises(ValueError):  # 还是它本来的错，不是包了一层的
            await asyncio.wait_for(engine._images([m], [m]), 5)
        self.assertEqual(sorted(stopped), ["a", "b"])  # 别的不留在后台接着下

    async def test_pictures_and_files_are_fetched_together(self):
        meeting = Meeting(2)

        async def loader(att, max_side):
            await meeting.join()
            return "data:image/jpeg;base64,AAAA"

        async def reader(att, max_chars):
            await meeting.join()
            return FileText(kind="文本", text="文件内容")

        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "好")), image_loader=loader)
        engine.file_reader = reader
        await engine.handle_message(
            msg(501, "@小D 看看", mentions_bot=True, images=[ImageAttachment("p", "image/png", 1)], files=[FileAttachment("a.txt", None, 10)])
        )
        await engine.drain()
        self.assertEqual(image_urls(llm.calls[0]), ["data:image/jpeg;base64,AAAA"])
        self.assertIn("文件内容", user_text(llm.calls[0]))
        self.assertEqual(len(transport.sent), 1)

    async def test_with_seeing_pictures_off_nothing_is_downloaded(self):
        asked = []

        async def loader(att, max_side):
            asked.append(att.filename)
            return "data:image/jpeg;base64,AAAA"

        async def reader(att, max_chars):
            return FileText(kind="文本", text="文件内容")

        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(1, "好")), image_loader=loader, defaults={"vision": False})
        engine.file_reader = reader
        await engine.handle_message(
            msg(501, "@小D 看看", mentions_bot=True, images=[ImageAttachment("p", "image/png", 1)], files=[FileAttachment("a.txt", None, 10)])
        )
        await engine.drain()
        self.assertEqual(asked, [])
        self.assertIn("文件内容", user_text(llm.calls[0]))


class LookingThingsUp(unittest.IsolatedAsyncioTestCase):
    """查资料那一步：上网搜、找图和读文件、打开链接同时办。"""

    def make(self, first, *, searcher=None, **kw):
        defaults = kw.pop("defaults", None)
        engine, llm, transport, clock = make_engine(
            scripted(ACTIONS(*first), ACTIONS(REPLY(2, "好"))), searcher=searcher, defaults=defaults, **kw
        )
        return engine, llm, transport, clock

    async def ask(self, engine, clock, *, links=()):
        """#1：别人早先发的文件（和链接），不会自动读；#2：叫它。"""
        await engine.handle_message(
            msg(500, "", author=12, name="小雨", files=[FileAttachment("a.txt", None, 10)], links=[LinkRef(u) for u in links])
        )
        clock.advance(300)
        await engine.handle_message(msg(501, "@小D 看看上面那个，再查一下", mentions_bot=True))
        await engine.drain()

    async def test_search_runs_while_the_file_is_read(self):
        meeting = Meeting(2)

        class Searcher(FakeSearcher):
            async def search(self, query, **kw):
                await meeting.join()
                return await FakeSearcher.search(self, query, **kw)

        async def reader(att, max_chars):
            await meeting.join()
            return FileText(kind="文本", text="文件内容")

        searcher = Searcher()
        engine, llm, transport, clock = self.make([READ(1), SEARCH("a")], searcher=searcher)
        engine.file_reader = reader
        await self.ask(engine, clock)
        text = user_text(llm.calls[1])
        self.assertIn("（你要看的文件：）\n【#1 的文件「a.txt」· 文本】", text)
        self.assertIn("（你搜了「a」：）", text)
        self.assertLess(text.index("文件内容"), text.index("（你搜了「a」：）"))  # 写给它看的顺序没变
        self.assertEqual(transport.sent[0][1], "好")

    async def test_finding_pictures_runs_while_the_file_is_read(self):
        meeting = Meeting(2)

        class Finder(FakePictures):
            async def find(self, query):
                await meeting.join()
                return await FakePictures.find(self, query)

        async def reader(att, max_chars):
            await meeting.join()
            return FileText(kind="文本", text="文件内容")

        pictures = Finder()
        engine, llm, transport, clock = self.make([READ(1), IMAGE("水豚")], pictures=pictures)
        engine.file_reader = reader
        await self.ask(engine, clock)
        self.assertEqual(pictures.calls, ["水豚"])
        self.assertIn("文件内容", user_text(llm.calls[1]))
        self.assertEqual(len(image_urls(llm.calls[1])), 2)  # 两张候选图照样给它看

    async def test_two_searches_run_together_and_keep_their_order(self):
        meeting = Meeting(2)
        finished = []

        class Searcher(FakeSearcher):
            async def search(self, query, **kw):
                await meeting.join()
                if query == "a":
                    for _ in range(6):  # 先写的那个回来得晚
                        await asyncio.sleep(0)
                finished.append(query)
                return await FakeSearcher.search(self, query, **kw)

        searcher = Searcher()
        engine, llm, transport, clock = self.make([SEARCH("a"), SEARCH("b")], searcher=searcher)
        await engine.handle_message(msg(501, "@小D 比较一下 a 和 b", mentions_bot=True))
        await engine.drain()
        self.assertEqual(finished, ["b", "a"])
        text = user_text(llm.calls[1])
        self.assertLess(text.index("（你搜了「a」：）"), text.index("（你搜了「b」：）"))  # 结果还是照它写的顺序放
        day = engine.ledger.day(clock())
        self.assertEqual(engine.store.count_usage(day, guild_id=GUILD, kind="search"), 2)

    async def test_daily_limit_goes_to_the_first_one(self):
        searcher = FakeSearcher()
        engine, llm, transport, clock = self.make(
            [SEARCH("a"), SEARCH("b")], searcher=searcher, defaults={"daily_search_limit": 1}
        )
        await engine.handle_message(msg(501, "@小D 比较一下 a 和 b", mentions_bot=True))
        await engine.drain()
        self.assertEqual(searcher.calls, ["a"])
        text = user_text(llm.calls[1])
        self.assertIn("（你搜了「a」：）", text)
        self.assertIn("想搜「b」，但今天这个服务器的搜索次数用完了（1 次）", text)

    async def test_paying_for_links_is_settled_before_the_searches(self):
        """打开链接用不用要花钱的读法，和以前（先读完文件、打开完链接才搜）一样，不算同一轮里同时在搜的那几次的钱。"""
        url = "https://example.com/post"
        reader = FakeReader()
        searcher = FakeSearcher()  # 搜一次算一次钱：搜完这个服务器今天就超预算了
        engine, llm, transport, clock = self.make(
            [READ(1), SEARCH("a")], searcher=searcher, link_reader=reader, defaults={"daily_budget_usd": 0.0005}
        )

        async def slow_file(att, max_chars):
            for _ in range(12):  # 文件读得慢：这时候搜索已经搜完、记上账了
                await asyncio.sleep(0)
            return FileText(kind="文本", text="文件内容")

        engine.file_reader = slow_file
        await self.ask(engine, clock, links=[url])
        self.assertEqual(searcher.calls, ["a"])
        self.assertTrue(engine._guild_over(GUILD, engine.settings.view(GUILD, CHANNEL), clock())[0])  # 搜完确实超了
        self.assertEqual([c[0] for c in reader.calls], [url])
        self.assertEqual(reader.pays, [True])

    async def test_a_crash_while_reading_stops_the_search(self):
        """读文件的地方出了意料之外的错：同时在搜的那一次停下，记账的那一笔不会一直“进行中”。"""
        started = []

        class Hang(FakeSearcher):
            async def search(self, query, **kw):
                started.append(query)
                await asyncio.Event().wait()

        async def reader(att, max_chars):
            for _ in range(3):
                await asyncio.sleep(0)
            raise RuntimeError("读文件的地方坏了")

        engine, llm, transport, clock = self.make([READ(1), SEARCH("a")], searcher=Hang())
        engine.file_reader = reader
        with self.assertLogs("dsbot.engine", "ERROR"):
            await asyncio.wait_for(self.ask(engine, clock), 5)
        self.assertEqual(started, ["a"])
        rows = engine.store.db.execute("SELECT outcome FROM usage WHERE kind = 'search'").fetchall()
        self.assertEqual([r["outcome"] for r in rows], ["error"])
        self.assertEqual(len(llm.calls), 1)  # 第二步没问成


class AfterMusic(unittest.IsolatedAsyncioTestCase):
    """按歌名找歌和搜索共用每天的搜索次数：谁先占，和一件一件办时一样（排在点歌后面的搜索不提前）。"""

    def setUp(self):
        self._ready_wait = music.READY_WAIT
        music.READY_WAIT = 0.05

    def tearDown(self):
        music.READY_WAIT = self._ready_wait

    def make(self, first, searcher):
        engine, llm, transport, clock = make_engine(
            scripted(ACTIONS(*first), ACTIONS(REPLY(1, "好")), ACTIONS()),
            searcher=searcher,
            defaults={"daily_search_limit": 1},
        )
        voice, source = FakeVoice(), FakeSource()
        voice.users[(GUILD, 11)] = VOICE
        source.found["周杰伦 晴天"] = [LINK]
        source.add(LINK, "晴天", "周杰伦", 270)
        engine.music = MusicPlayer(source, voice)
        self.addAsyncCleanup(engine.music.close)
        return engine, llm, source

    async def test_the_search_still_goes_before_the_song(self):
        """模型不管先写哪个，查资料那一步都是先搜、后点歌（_interpret 排的）：只剩一次搜索时归搜索，和以前一样。"""
        for first in ([PLAY("周杰伦 晴天"), SEARCH("a")], [SEARCH("a"), PLAY("周杰伦 晴天")]):
            searcher = FakeSearcher()
            engine, llm, source = self.make(first, searcher)
            await engine.handle_message(msg(501, "@小D 查一下 a，再放首晴天", mentions_bot=True))
            await engine.drain()
            text = user_text(llm.calls[1])
            self.assertEqual(searcher.calls, ["a"])
            self.assertIn("（你搜了「a」：）", text)
            self.assertEqual(source.searches, [])
            self.assertIn("搜索次数用完了（1 次），按歌名找不了", text)

    async def test_a_search_placed_after_a_song_is_not_started_early(self):
        """保险：哪天点歌排到了搜索前面，搜索就不能提前去占名额（提前的话，读文件的工夫它就把本该归点歌的最后一次抢走了）。"""
        searcher = FakeSearcher()
        engine, llm, source = self.make([READ(1), PLAY("周杰伦 晴天"), SEARCH("a")], searcher)
        real = engine._interpret

        def song_before_search(*args, **kwargs):
            actions, stored = real(*args, **kwargs)
            return sorted(actions, key=lambda a: ("read", "music").index(a.kind) if a.kind in ("read", "music") else 2), stored

        async def reader(att, max_chars):
            for _ in range(6):
                await asyncio.sleep(0)
            return FileText(kind="文本", text="文件内容")

        engine._interpret = song_before_search
        engine.file_reader = reader
        await engine.handle_message(msg(500, "", author=12, name="小雨", files=[FileAttachment("a.txt", None, 10)]))
        clock = engine.clock
        clock.advance(300)
        await engine.handle_message(msg(501, "@小D 看看上面那个，放首晴天，再查一下 a", mentions_bot=True))
        await engine.drain()
        text = user_text(llm.calls[1])
        self.assertIn("文件内容", text)
        self.assertEqual(source.searches, ["周杰伦 晴天"])
        self.assertIn("开始播放：《晴天》", text)
        self.assertEqual(searcher.calls, [])
        self.assertIn("想搜「a」，但今天这个服务器的搜索次数用完了（1 次）", text)
        self.assertLess(text.index("开始播放"), text.index("想搜「a」"))


class Helpers(unittest.IsolatedAsyncioTestCase):
    async def test_together_keeps_the_order(self):
        async def job(value, waits):
            for _ in range(waits):
                await asyncio.sleep(0)
            return value

        self.assertEqual(await _together(job("a", 3), job("b", 0), job("c", 1)), ["a", "b", "c"])
        self.assertEqual(await _together(), [])

    async def test_together_stops_the_rest_on_an_error(self):
        stopped = []

        async def hang(name):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.append(name)
                raise

        async def fail():
            await asyncio.sleep(0)
            raise KeyError("x")

        with self.assertRaises(KeyError):
            await asyncio.wait_for(_together(hang("a"), fail(), hang("b")), 5)
        self.assertEqual(sorted(stopped), ["a", "b"])

    async def test_together_stops_everything_when_cancelled(self):
        stopped = []

        async def hang(name):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.append(name)
                raise

        task = asyncio.ensure_future(_together(hang("a"), hang("b")))
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        done, _ = await asyncio.wait([task], timeout=5)
        self.assertTrue(done and task.cancelled())
        self.assertEqual(sorted(stopped), ["a", "b"])

    async def test_settle_stops_what_is_left_and_takes_errors(self):
        async def hang():
            await asyncio.Event().wait()

        async def fail():
            raise ValueError("没人要的错")

        async def fine():
            return 1

        tasks = [asyncio.ensure_future(job) for job in (hang(), fail(), fine())]
        await asyncio.sleep(0)
        await asyncio.wait_for(_settle(tasks), 5)
        self.assertTrue(tasks[0].cancelled())
        self.assertTrue(all(t.done() for t in tasks))
        self.assertEqual(tasks[2].result(), 1)
        await _settle([])


if __name__ == "__main__":
    unittest.main()
