"""2.15.0 更新公告：bot 主人用 /dsowner 公告 发，程序原样发出去（不问模型）。

- 三种范围：服务器（发到设了公告频道的服务器）、私信（发给最近 30 天私信过它、没拒收的人）、全部（两边都发）。
- 没设公告频道的服务器不收（管理员用 /dsadmin 公告频道 设为 / 取消）；每个人可以拒收私信的那份（/ds-dm 更新公告 值:关）。
- 先看预览，点了“发送”才发；一条一条发，中间隔一会儿；结果只说人数，不说是谁。
- 发出去的公告不是模型写的：下一轮在那个频道（私信）里告诉它一声，重启了也还在。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, OWNER, T0, make_engine, msg
from test_bubble import text
from test_engine import REPLY

from dsbot import __version__
from dsbot import announce as an
from dsbot.models import ChannelPlace
from dsbot.whatsnew import NOTES

OTHER = 2000  # 别的服务器
NEWS = 150  # 主服的公告频道
OTHER_NEWS = 2150
DM_A, DM_B, DM_C = 5001, 5002, 5003
DAY = 86400.0


def setup(**kw):
    engine, llm, transport, clock = make_engine(lambda call: REPLY(1, "好"), **kw)
    transport.dms, transport.blocked = [], set()
    transport.names[NEWS] = "公告"
    return engine, llm, transport, clock


def person(engine, transport, clock, uid, channel, *, guild=GUILD, days_ago=1.0):
    """一个私信过它的人：私信挂在 guild，最近一次私信是 days_ago 天前。"""
    transport.places[channel] = ChannelPlace(guild_id=guild, name="私信", dm=True)
    transport.dm_users[channel] = uid
    engine.store.set_dm_home(uid, guild, channel)
    with engine.store.db:
        engine.store.db.execute("UPDATE dm_homes SET updated_at = ? WHERE user_id = ?", (clock.t - days_ago * DAY, uid))


class Texts(unittest.TestCase):
    def test_compose(self):
        self.assertEqual(an.compose("更新公告 · v9", "- 一\n- 二"), "📢 **更新公告 · v9**\n- 一\n- 二")
        self.assertEqual(an.compose("  标题\n换行  ", " 正文 ", dm=True), "📢 **标题 换行**\n正文\n" + an.DM_FOOT)
        self.assertIn("/ds-dm 更新公告 值:关", an.DM_FOOT)  # 私信的那份写明怎么拒收
        self.assertEqual(an.compose("", "正文"), f"📢 **更新公告 · v{__version__}**\n正文")  # 标题清空了：用默认的

    def test_never_longer_than_one_message(self):
        out = an.compose("题" * 500, "字" * 5000, dm=True)
        self.assertLessEqual(len(out), 2000)
        self.assertTrue(out.endswith(an.DM_FOOT))

    def test_draft_by_scope(self):
        notes = {"9.9.9": [("server", "服务器的事"), ("dm", "私信的事"), ("both", "两边的事")]}
        try:
            NOTES.update(notes)
            self.assertEqual(an.draft("server", "9.9.9"), "- 服务器的事\n- 两边的事")
            self.assertEqual(an.draft("dm", "9.9.9"), "- 私信的事\n- 两边的事")
            self.assertEqual(an.draft("all", "9.9.9"), "- 服务器的事\n- 私信的事\n- 两边的事")
        finally:
            del NOTES["9.9.9"]
        self.assertEqual(an.draft("server", "0.0.1"), "")  # 这一版没写说明：空着让主人自己写

    def test_this_version_has_notes(self):
        self.assertIn(__version__, NOTES)
        self.assertEqual(next(iter(NOTES)), __version__)  # 新的一版写在最前面
        for scope in an.SCOPES:
            self.assertTrue(an.draft(scope), scope)
        for lines in NOTES.values():
            for where, line in lines:
                self.assertIn(where, ("server", "dm", "both"))
                self.assertTrue(line.strip())


class Plans(unittest.IsolatedAsyncioTestCase):
    async def test_server_without_channel_is_skipped(self):
        engine, _, transport, clock = setup()
        transport.joined = [OTHER]
        engine.store.set_announce_channel(GUILD, NEWS)
        person(engine, transport, clock, 11, DM_A)
        plan = engine.announce_plan("server")
        self.assertEqual((plan.channels, plan.unset, plan.gone), ([(GUILD, NEWS)], [OTHER], []))
        self.assertEqual(plan.people, [])  # 只发服务器：私信一个都不发
        self.assertFalse(plan.empty)
        engine.store.set_announce_channel(GUILD, None)  # 取消了：不收
        plan = engine.announce_plan("server")
        self.assertEqual((plan.channels, plan.unset), ([], [GUILD, OTHER]))
        self.assertTrue(plan.empty)

    async def test_channel_gone_or_not_in_that_server(self):
        engine, _, transport, _ = setup()
        transport.joined = [OTHER]
        engine.store.set_announce_channel(GUILD, NEWS)
        engine.store.set_announce_channel(OTHER, OTHER_NEWS)
        transport.places[OTHER_NEWS] = ChannelPlace(guild_id=GUILD, name="公告", public=True)  # 记着的频道不在那个服务器里
        plan = engine.announce_plan("server")
        self.assertEqual((plan.channels, plan.unset, plan.gone), ([(GUILD, NEWS)], [], [OTHER]))
        transport.locate = lambda cid: None  # 频道没了
        plan = engine.announce_plan("server")
        self.assertEqual((plan.channels, plan.gone), ([], [GUILD, OTHER]))

    async def test_left_server_is_not_listed(self):
        engine, _, _, _ = setup()
        engine.store.set_announce_channel(OTHER, OTHER_NEWS)  # bot 已经不在那个服务器了
        plan = engine.announce_plan("server")
        self.assertEqual((plan.channels, plan.unset, plan.gone), ([], [GUILD], []))

    async def test_dm_people(self):
        engine, _, transport, clock = setup()
        engine.store.set_announce_channel(GUILD, NEWS)
        person(engine, transport, clock, 11, DM_A, days_ago=1)
        person(engine, transport, clock, 22, DM_B, days_ago=29.9)
        person(engine, transport, clock, 33, DM_C, days_ago=30.1)  # 太久没私信
        plan = engine.announce_plan("dm")
        self.assertEqual((plan.people, plan.stale, plan.refused, plan.closed), ([(11, DM_A), (22, DM_B)], 1, 0, 0))
        self.assertEqual(plan.channels, [])  # 只发私信：频道一个都不发
        engine.store.set_announce_off(22, True)
        plan = engine.announce_plan("dm")
        self.assertEqual((plan.people, plan.stale, plan.refused), ([(11, DM_A)], 1, 1))
        engine.store.set_announce_off(22, False)
        self.assertEqual(engine.announce_plan("dm").people, [(11, DM_A), (22, DM_B)])

    async def test_recent_chat_counts_even_if_home_is_old(self):
        engine, _, transport, clock = setup()
        person(engine, transport, clock, 11, DM_A, days_ago=200)  # 很久以前定的服务器
        self.assertEqual(engine.announce_plan("dm").stale, 1)
        await engine.handle_message(msg(1, "在吗", channel_id=DM_A, mentions_bot=True, private=True))
        await engine.drain()
        self.assertEqual(engine.announce_plan("dm").people, [(11, DM_A)])  # 刚私信过
        clock.advance(31 * DAY)
        self.assertEqual(engine.announce_plan("dm").stale, 1)

    async def test_dm_closed(self):
        engine, _, transport, clock = setup()
        person(engine, transport, clock, 11, DM_A)
        person(engine, transport, clock, 22, DM_B, guild=OTHER)  # 私信挂的服务器，bot 已经不在了
        plan = engine.announce_plan("dm")
        self.assertEqual((plan.people, plan.closed), ([(11, DM_A)], 1))
        engine.settings.set("guild", GUILD, "dm", "关")  # 那个服务器关掉了私信
        plan = engine.announce_plan("dm")
        self.assertEqual((plan.people, plan.closed), ([], 2))

    async def test_never_dmed_is_not_a_target(self):
        engine, _, _, _ = setup()
        engine.store.set_dm_home(11, GUILD, None)  # 只用 /ds-dm 服务器 定过，没私信过
        plan = engine.announce_plan("all")
        self.assertEqual((plan.people, plan.stale, plan.refused, plan.closed), ([], 0, 0, 0))


class Sending(unittest.IsolatedAsyncioTestCase):
    def both(self):
        engine, llm, transport, clock = setup()
        transport.joined = [OTHER]
        engine.store.set_announce_channel(GUILD, NEWS)
        person(engine, transport, clock, 11, DM_A)
        person(engine, transport, clock, 22, DM_B)
        return engine, llm, transport, clock

    async def test_server_only(self):
        engine, llm, transport, _ = self.both()
        result = await engine.announce("server", "更新公告 · v9", "- 新功能")
        self.assertEqual(transport.sent, [(NEWS, "📢 **更新公告 · v9**\n- 新功能", None, False)])
        self.assertEqual(transport.mentions, [()])  # 不 @ 任何人
        self.assertEqual(transport.dms, [])
        self.assertEqual((result.channels, result.failed_channels, result.dms, result.dm_failed), ([(GUILD, NEWS)], [], 0, 0))
        self.assertEqual(llm.calls, [])  # 原样发，不问模型

    async def test_dm_only(self):
        engine, llm, transport, _ = self.both()
        result = await engine.announce("dm", "更新公告 · v9", "- 新功能")
        want = "📢 **更新公告 · v9**\n- 新功能\n" + an.DM_FOOT
        self.assertEqual(transport.dms, [(11, want), (22, want)])
        self.assertEqual(transport.sent, [])
        self.assertEqual((result.channels, result.dms, result.dm_failed), ([], 2, 0))
        self.assertEqual(llm.calls, [])

    async def test_all(self):
        engine, _, transport, _ = self.both()
        result = await engine.announce("all", "更新公告 · v9", "- 新功能")
        self.assertEqual([s[:2] for s in transport.sent], [(NEWS, "📢 **更新公告 · v9**\n- 新功能")])  # 频道里那份没有拒收的那一行
        self.assertEqual([uid for uid, _ in transport.dms], [11, 22])
        self.assertTrue(all(words.endswith(an.DM_FOOT) for _, words in transport.dms))
        self.assertEqual((len(result.channels), result.dms), (1, 2))

    async def test_refused_gets_nothing(self):
        engine, _, transport, _ = self.both()
        engine.store.set_announce_off(11, True)
        await engine.announce("all", "标题", "正文")
        self.assertEqual([uid for uid, _ in transport.dms], [22])

    async def test_one_at_a_time_with_a_gap(self):
        engine, _, _, _ = self.both()
        await engine.announce("all", "标题", "正文")  # 1 个频道 + 2 个人
        self.assertEqual(engine.test_sleeps, [an.GAP_SECONDS, an.GAP_SECONDS])  # 第一条之前不等
        del engine.test_sleeps[:]
        await engine.announce("server", "标题", "正文")
        self.assertEqual(engine.test_sleeps, [])

    async def test_failures_are_counted(self):
        engine, _, transport, _ = self.both()
        transport.blocked = {22}  # 关了私信、屏蔽了它
        result = await engine.announce("dm", "标题", "正文")
        self.assertEqual((result.dms, result.dm_failed), (1, 1))
        self.assertEqual([c for c, _ in engine.store.channel_notes(DM_B)], [])  # 没发出去的：不告诉它“发过”
        self.assertEqual(len(engine.store.channel_notes(DM_A)), 1)
        transport.can_send = False  # 频道里没有发言权限
        result = await engine.announce("server", "标题", "正文")
        self.assertEqual((result.channels, result.failed_channels), ([], [(GUILD, NEWS)]))
        self.assertEqual(engine.store.channel_notes(NEWS), [])

    async def test_send_error_does_not_stop_the_rest(self):
        engine, _, transport, _ = self.both()
        real = transport.send_dm

        async def flaky(uid, words):
            if uid == 11:
                raise RuntimeError("断线了")
            return await real(uid, words)

        transport.send_dm = flaky
        result = await engine.announce("dm", "标题", "正文")
        self.assertEqual((result.dms, result.dm_failed), (1, 1))
        self.assertFalse(engine.announcing)

    async def test_one_announcement_at_a_time(self):
        engine, _, transport, _ = self.both()
        engine.announcing = True
        self.assertIsNone(await engine.announce("all", "标题", "正文"))
        self.assertEqual((transport.sent, transport.dms), ([], []))
        engine.announcing = False
        self.assertIsNotNone(await engine.announce("all", "标题", "正文"))
        self.assertFalse(engine.announcing)

    async def test_logged_with_counts_only(self):
        engine, _, transport, _ = self.both()
        transport.blocked = {22}
        await engine.announce("all", " 标题 ", " 正文 ")
        row = dict(engine.store.db.execute("SELECT * FROM announcements").fetchone())
        self.assertEqual(
            (row["scope"], row["title"], row["text"], row["channels"], row["dms"], row["failed"]), ("all", "标题", "正文", 1, 1, 1)
        )
        self.assertEqual(row["ts"], T0)

    async def test_long_text_is_cut(self):
        engine, _, transport, _ = self.both()
        await engine.announce("all", "标题", "字" * 5000)
        for words in [s[1] for s in transport.sent] + [w for _, w in transport.dms]:
            self.assertLessEqual(len(words), 2000)


class Told(unittest.IsolatedAsyncioTestCase):
    """发出去的公告不是模型写的：下一轮告诉它一声。"""

    async def test_channel_hears_about_it_once(self):
        engine, llm, transport, _ = setup()
        engine.store.set_announce_channel(GUILD, CHANNEL)
        await engine.announce("server", "更新公告 · v9", "- 可以设提醒了\n- 别的")
        await engine.handle_message(msg(1, "@小D 刚才那条公告说了什么", mentions_bot=True))
        await engine.drain()
        seen = text(llm.calls[0])
        self.assertIn("替 bot 主人在这里发了一条更新公告，不是你写的，原文：📢 **更新公告 · v9** - 可以设提醒了 - 别的。", seen)
        self.assertEqual(engine.store.channel_notes(CHANNEL), [])  # 告诉过了
        await engine.handle_message(msg(2, "@小D 再说一遍", mentions_bot=True, author=22, name="小红"))
        await engine.drain()
        self.assertNotIn("原文：", text(llm.calls[1]).split("#2")[-1])  # 不再重复告诉（上一轮的还在记录里）
        self.assertEqual(sum("替 bot 主人在这里发了" in str(m["content"]) for m in llm.calls[1]["messages"]), 1)

    async def test_other_channel_does_not_hear(self):
        engine, llm, transport, _ = setup()
        engine.store.set_announce_channel(GUILD, NEWS)
        await engine.announce("server", "标题", "正文")
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))  # 在别的频道叫它
        await engine.drain()
        self.assertNotIn("更新公告", text(llm.calls[0]))
        self.assertEqual(len(engine.store.channel_notes(NEWS)), 1)  # 公告频道那条还留着

    async def test_dm_hears_about_it(self):
        engine, llm, transport, clock = setup()
        person(engine, transport, clock, 11, DM_A)
        transport.dm_with[DM_A] = "阿明"
        await engine.announce("dm", "标题", "私信里的事")
        await engine.handle_message(msg(1, "这是什么", channel_id=DM_A, mentions_bot=True, private=True))
        await engine.drain()
        self.assertIn("原文：📢 **标题** 私信里的事。", text(llm.calls[0]))
        self.assertNotIn("值:关", text(llm.calls[0]))  # 拒收的那一行是程序加的，不算公告的内容

    async def test_survives_restart_and_expires(self):
        engine, _, _, clock = setup()
        engine.store.set_announce_channel(GUILD, NEWS)
        await engine.announce("server", "标题", "正文")
        self.assertNotIn(NEWS, engine.states)  # 记在数据库里，不靠内存里的频道状态
        self.assertEqual(len(engine.store.channel_notes(NEWS)), 1)
        engine.store.tidy_channel_notes(clock.t - an.NOTE_KEEP_SECONDS)
        self.assertEqual(len(engine.store.channel_notes(NEWS)), 1)
        clock.advance(an.NOTE_KEEP_SECONDS + 1)
        engine._housekeep(clock.t)  # 一直没人在那里说话：过期了就删掉
        self.assertEqual(engine.store.channel_notes(NEWS), [])


class Reply:
    def __init__(self):
        self.said, self.views, self.modal = [], [], None

    def is_done(self):
        return bool(self.said)

    async def send_message(self, words, view=None, ephemeral=False):
        assert ephemeral
        self.said.append(words)
        self.views.append(view)

    async def send_modal(self, modal):
        self.modal = modal

    async def edit_message(self, content=None, view=None):
        self.said.append(content)
        self.views.append(view)


def ask(user=OWNER, channel=CHANNEL, **extra):
    reply = Reply()

    async def followup(words, view=None, ephemeral=False):
        assert ephemeral
        reply.said.append(words)
        reply.views.append(view)

    async def edit_original(content=None):
        reply.said.append(content)

    return NS(
        guild_id=GUILD, guild=NS(id=GUILD, name="主服"), channel_id=channel, channel=NS(id=channel), user=NS(id=user),
        response=reply, followup=NS(send=followup), edit_original_response=edit_original, **extra,
    )


class Commands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import discord

            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")
        from helpers import FakeTransport
        from test_admin_commands import make_bot

        global server_label
        from dsbot.commands import server_label

        self.discord = discord
        self.bot = make_bot(self)
        self.transport = FakeTransport()
        self.transport.dms, self.transport.blocked = [], set()
        self.bot.engine.transport = self.transport
        self.sleeps = []

        async def sleep(seconds):
            self.sleeps.append(seconds)

        self.bot.engine.sleep = sleep
        tree = self.bot.tree
        self.admin = next(c for c in tree.get_commands() if c.name == "dsadmin")
        self.dm = next(c for c in tree.get_commands() if c.name == "ds-dm")
        self.owner = tree.get_commands(guild=discord.Object(id=GUILD))[0]

    def person(self, uid, channel):
        self.transport.places[channel] = ChannelPlace(guild_id=GUILD, name="私信", dm=True)
        self.bot.store.set_dm_home(uid, GUILD, channel)

    async def said(self, command, *args, **where):
        i = ask(**where)
        await command.callback(i, *args)
        return "\n".join(t for t in i.response.said if t), i

    # ---- 管理员：公告频道
    async def test_admin_sets_and_clears_channel(self):
        from dsbot.commands import announce_line

        self.assertIn("更新公告：不收", announce_line(self.bot, GUILD))
        words, _ = await self.said(self.admin.get_command("公告频道").get_command("设为"), None)
        self.assertIn(f"会发到 <#{CHANNEL}>", words)  # 不填：就是这个频道
        self.assertEqual(self.bot.store.announce_channel(GUILD), CHANNEL)
        words, _ = await self.said(self.admin.get_command("公告频道").get_command("设为"), NS(id=NEWS))
        self.assertEqual(self.bot.store.announce_channel(GUILD), NEWS)
        self.assertEqual(announce_line(self.bot, GUILD), f"更新公告：发到 <#{NEWS}>（`/dsadmin 公告频道 设为` 换一个，`取消` 就不再收）")
        words, _ = await self.said(self.admin.get_command("公告频道").get_command("取消"))
        self.assertIn("不再收更新公告", words)
        self.assertIsNone(self.bot.store.announce_channel(GUILD))
        words, _ = await self.said(self.admin.get_command("公告频道").get_command("取消"))
        self.assertIn("本来就没设", words)

    async def test_admin_cannot_pick_a_channel_it_cannot_speak_in(self):
        muted = NS(id=NEWS, permissions_for=lambda me: NS(send_messages=False))
        i = ask()
        i.guild.me = NS(id=1)
        await self.admin.get_command("公告频道").get_command("设为").callback(i, muted)
        self.assertIn("没有发言权限", i.response.said[0])
        self.assertIsNone(self.bot.store.announce_channel(GUILD))

    async def test_status_shows_it(self):
        from test_admin_commands import ask as admin_ask

        from dsbot.commands import status_text

        self.assertIn("更新公告：不收（没设公告频道", status_text(self.bot, admin_ask()))
        self.bot.store.set_announce_channel(GUILD, NEWS)
        self.assertIn(f"更新公告：发到 <#{NEWS}>", status_text(self.bot, admin_ask()))

    # ---- 每个人：拒收
    async def test_dm_opt_out(self):
        news = self.dm.get_command("更新公告")
        words, _ = await self.said(news, None, user=11)
        self.assertIn("更新公告：开", words)
        self.assertFalse(self.bot.store.announce_off(11))
        words, _ = await self.said(news, NS(value="off"), user=11)
        self.assertIn("更新公告：关", words)
        self.assertTrue(self.bot.store.announce_off(11))
        self.assertFalse(self.bot.store.announce_off(22))  # 只管自己的
        words, _ = await self.said(news, None, user=11)  # 只看：不改
        self.assertIn("更新公告：关", words)
        words, _ = await self.said(news, NS(value="on"), user=11)
        self.assertIn("更新公告：开", words)
        self.assertFalse(self.bot.store.announce_off(11))

    # ---- 主人：发
    async def open(self, scope, user=OWNER):
        i = ask(user=user)
        await self.owner.get_command("公告").callback(i, NS(value=scope))
        return i

    async def test_only_owner(self):
        i = await self.open("all", user=11)
        self.assertIsNone(i.response.modal)
        self.assertEqual(i.response.said, ["只有 bot 的主人能用这个命令。"])

    async def test_modal_is_prefilled(self):
        i = await self.open("dm")
        modal = i.response.modal
        self.assertEqual(modal.head.default, f"更新公告 · v{__version__}")
        self.assertEqual(modal.text.default, an.draft("dm"))
        self.assertLessEqual(modal.text.max_length, an.MAX_CHARS)

    async def fill(self, scope, title="更新公告 · v9", words="- 新功能"):
        modal = (await self.open(scope)).response.modal
        modal.head._value, modal.text._value = title, words
        i = ask()
        await modal.on_submit(i)
        return i

    async def test_nowhere_to_send(self):
        i = await self.fill("server")
        self.assertIn("现在没有地方可发，没发。", i.response.said[0])
        self.assertIn("没有一个服务器设了公告频道", i.response.said[0])
        self.assertEqual(i.response.views, [None])  # 没有“发送”按钮

    async def test_preview_then_send(self):
        self.bot.store.set_announce_channel(GUILD, NEWS)
        self.person(11, DM_A)
        self.person(22, DM_B)
        self.bot.store.set_announce_off(22, True)
        i = await self.fill("all")
        self.assertEqual(i.response.said[0], "📢 **更新公告 · v9**\n- 新功能")  # 预览：发出去的样子
        summary, view = i.response.said[1], i.response.views[1]
        self.assertIn(f"服务器：发到 1 个公告频道（{server_label(self.bot, GUILD)} <#{NEWS}>）", summary)
        self.assertIn("私信：发给 1 个人", summary)
        self.assertIn("1 人拒收", summary)
        self.assertNotIn("22", summary)  # 私信只说人数，不说是谁
        self.assertEqual((self.transport.sent, self.transport.dms), ([], []))  # 还没点：什么都没发
        send, drop = view.children
        self.assertEqual((send.label, drop.label), ("发送", "不发了"))
        stranger = ask(user=11)
        await send.callback(stranger)
        self.assertEqual(stranger.response.said, ["只有发起的人能点。"])
        self.assertEqual(self.transport.sent, [])
        done = ask()
        await send.callback(done)
        self.assertEqual([s[:2] for s in self.transport.sent], [(NEWS, "📢 **更新公告 · v9**\n- 新功能")])
        self.assertEqual(self.transport.dms, [(11, "📢 **更新公告 · v9**\n- 新功能\n" + an.DM_FOOT)])
        self.assertIn("发送中", done.response.said[0])
        self.assertIn("服务器：发到了 1 个公告频道", done.response.said[1])
        self.assertIn("私信：发出去 1 人", done.response.said[1])

    async def test_cancel_sends_nothing(self):
        self.bot.store.set_announce_channel(GUILD, NEWS)
        i = await self.fill("server")
        _, drop = i.response.views[1].children
        done = ask()
        await drop.callback(done)
        self.assertEqual(done.response.said, ["没发。"])
        self.assertEqual(self.transport.sent, [])

    async def test_result_names_what_failed(self):
        self.bot.store.set_announce_channel(GUILD, NEWS)
        self.person(11, DM_A)
        i = await self.fill("all")
        self.transport.blocked = {11}
        real = self.transport.send

        async def no_send(*a, **kw):
            return None

        self.transport.send = no_send
        done = ask()
        await i.response.views[1].children[0].callback(done)
        self.transport.send = real
        self.assertIn(f"没发出去的（检查 bot 在那个频道的发言权限）：{server_label(self.bot, GUILD)} <#{NEWS}>", done.response.said[1])
        self.assertIn("私信：发出去 0 人，没发出去 1 人", done.response.said[1])

    async def test_dm_preview_shows_the_opt_out_line(self):
        self.person(11, DM_A)
        i = await self.fill("dm")
        self.assertTrue(i.response.said[0].endswith(an.DM_FOOT))

    async def test_busy(self):
        self.bot.engine.announcing = True
        i = await self.open("all")
        self.assertIsNone(i.response.modal)
        self.assertIn("上一条公告还在发", i.response.said[0])

    def test_layout(self):
        from test_admin_commands import shape

        payload = self.owner.to_dict(self.bot.tree)
        self.assertEqual(shape(payload)["公告"], ["范围"])
        self.assertEqual(shape(self.dm.to_dict(self.bot.tree))["更新公告"], ["值?"])
        self.assertEqual(shape(self.admin.to_dict(self.bot.tree))["公告频道"], {"设为": ["频道?"], "取消": []})


if __name__ == "__main__":
    unittest.main()
