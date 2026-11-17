"""2.8.0 连通的频道，第十一轮修掉的问题：没问模型就记进记录的那几条（别的连通频道补进来的闲聊、直接改进共用记录的编辑、
连通时记进它自己那份的闲聊）只记在内存里，重启（部署新版本、NAS 重启）以后就忘了：之后带着它们的请求被 DeepSeek 拒了，
换不掉，组里的频道（或者断开以后的那个频道）从此每一轮都被拒。

- 这几条也记在数据库里（chat_unvetted）：重启以后照样认得，被拒了（不是太长）照样换掉，之后照常回。
- 带着它们的请求过了、换掉了：库里那一行也删掉（以后别的原因被拒，过了的不动）。
- 清空记忆、重启以后清空一个频道在组里的那部分：别的频道的照旧记着。"""

import unittest

from test_chatsync import CHANNEL, HALL, LOBBY, chats, connect, disconnect, say, world
from test_chatsync_lifecycle import prompt_of, users
from test_chatsync_privacy import record
from test_chatsync_r6_privacy import own_record
from test_chatsync_r9_records import LONG, RISKY, DeepSeek, hers
from test_dm import OTHER

from dsbot.engine import Engine
from dsbot.prompts import MESSAGE_DELETED, PASSIVE_REJECTED

NOTHING = ([], set())  # 库里没有还没问过模型的


def reboot(engine):
    """重启了（同一个数据库，新的 Engine，内存里什么都没有），照样登录着 Discord。"""
    return Engine(
        engine.cfg,
        engine.settings,
        engine.store,
        engine.llm,
        engine.ledger,
        engine.transport,
        clock=engine.clock,
        sleep=engine.sleep,
        cards=engine.cards,
        memory=engine.memory,
    )


class UnvettedAcrossARestart(unittest.IsolatedAsyncioTestCase):
    async def test_an_edit_rewritten_into_the_shared_record_is_still_withdrawn_after_a_restart(self):
        ds = DeepSeek()
        engine, llm, transport, clock = world(ds)
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        await say(engine, clock, 1, "@小D 大厅这边好", channel=HALL, author=21, name="小红")
        engine.handle_edit(hers(1, f"@小D {RISKY}", mentions_bot=True), edited=True)  # 刚说的那条：直接改进共用的记录
        await engine.drain()
        engine = reboot(engine)  # 还没人再叫它就重启了
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 2, "@小D 在吗")  # 主服 #闲聊：记录里有那一行，被拒了
        await say(engine, clock, 3, "@小D 还在吗")
        await say(engine, clock, 4, "@小D 大厅这边呢", channel=HALL, author=22, name="小蓝")
        self.assertEqual(ds.results, ["回了", "拒了", "回了", "回了"])  # 只拒了一次：之后两边都照常回
        whole = record(engine, sync)
        self.assertIn(f"小红：{MESSAGE_DELETED}", whole)  # 那一行当成删了
        self.assertNotIn(RISKY, whole)
        self.assertEqual(engine.store.load_unvetted(sync), NOTHING)  # 换掉了：库里也划掉

    async def test_chatter_another_channel_brought_in_is_still_withdrawn_after_a_restart(self):
        ds = DeepSeek()
        engine, llm, transport, clock = world(ds)
        sync = connect(engine)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 1, f"我随口说了{RISKY}", channel=HALL, author=21, name="小红", call=False)  # 没人叫它
            ds.busy = 1
            await say(engine, clock, 2, "@小D 在吗")  # 主服 #闲聊：补进了那条，DeepSeek 忙（503），没问成也没被拒
            self.assertIn(RISKY, record(engine, sync))  # 对照：那条在共用的记录里
            engine = reboot(engine)
            await say(engine, clock, 3, "@小D 大厅这边呢", channel=HALL, author=22, name="小蓝")  # 带着那条：被拒了
        await say(engine, clock, 4, "@小D 还在吗", channel=HALL, author=22, name="小蓝")
        self.assertEqual(ds.results, ["503", "拒了", "回了"])  # 只拒了一次：之后照常回
        hall = record(engine, sync, HALL)
        self.assertNotIn(RISKY, hall)
        self.assertIn(PASSIVE_REJECTED, hall)  # 补进来的那几条换成一句“没有记下来”
        self.assertEqual(engine.store.load_unvetted(sync), NOTHING)

    async def test_settled_chatter_is_still_withdrawn_when_a_restart_came_between_the_link_and_the_unlink(self):
        for how in ("还没叫过它", "连通期间那一轮的概要没带全"):
            with self.subTest(how):
                ds = DeepSeek()
                engine, llm, transport, clock = world(ds)
                await say(engine, clock, 1, f"我随口说了{RISKY}", channel=HALL, author=21, name="小红", call=False)
                if how.startswith("连通期间"):
                    for i in range(39):  # 小蓝又说了 39 句长的（一共三千多字），都没人叫它
                        await say(engine, clock, 2 + i, f"第{i}句闲聊：{LONG}", channel=HALL, author=22, name="小蓝", call=False)
                connect(engine, HALL, CHANNEL)  # 连进来：那几条记进 #大厅自己那份（不进共用的），还没问过模型
                self.assertIn(RISKY, own_record(engine, HALL, OTHER))
                if how.startswith("连通期间"):
                    await say(engine, clock, 50, "@小D 在吗", channel=HALL, author=21, name="小红")  # 过了，可概要没带到那句
                    self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))
                engine = reboot(engine)  # 连通期间重启了（部署了新版本）
                disconnect(engine, HALL)  # 组散了：#大厅又用它自己那份（整份带着）
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await say(engine, clock, 51, "@小D 还在吗", channel=HALL, author=21, name="小红")  # 带着整份记录：被拒了
                await say(engine, clock, 52, "@小D 在不在", channel=HALL, author=21, name="小红")
                self.assertEqual(ds.results[-2:], ["拒了", "回了"])  # 只拒了一次：之后照常回
                self.assertEqual(users(engine, HALL, OTHER)[0].split("\n")[1:], [PASSIVE_REJECTED])
                self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))
                self.assertEqual(engine.store.load_unvetted(HALL), NOTHING)

    async def test_a_clear_after_a_restart_keeps_what_the_other_channels_left_unvetted(self):
        for how in ("/ds 清空记忆（等正在回的说完）", "直接清空（reset_channel）"):
            with self.subTest(how):
                ds = DeepSeek()
                engine, llm, transport, clock = world(ds)
                sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
                connect(engine, LOBBY, CHANNEL)  # 主服 #大厅也连进来
                with self.assertLogs("dsbot.engine", "WARNING"):
                    await say(engine, clock, 1, f"我随口说了{RISKY}", channel=HALL, author=21, name="小红", call=False)
                    ds.busy = 1
                    await say(engine, clock, 2, "@小D 在吗")  # 主服 #闲聊：补进了那条，DeepSeek 忙（503）
                    engine = reboot(engine)
                    # 重启以后，有人在主服 #闲聊清空记忆（别的频道说的不动）
                    if how.startswith("/ds"):
                        await engine.reset_linked(CHANNEL)
                    else:
                        engine.reset_channel(CHANNEL)  # 共用的那份还没载入过
                    await say(engine, clock, 3, "@小D 大厅这边呢", channel=HALL, author=22, name="小蓝")  # 带着那条：被拒了
                await say(engine, clock, 4, "@小D 还在吗", channel=HALL, author=22, name="小蓝")
                self.assertEqual(ds.results[-3:], ["503", "拒了", "回了"])
                hall = record(engine, sync, HALL)
                self.assertNotIn(RISKY, hall)
                self.assertIn(PASSIVE_REJECTED, hall)

    async def test_control_what_a_request_carried_is_crossed_off_in_the_database_too(self):
        # 对照（control）：带着它们的请求过了就算数，库里也划掉：重启以后别的原因被拒，过了的那一行也不当成删了
        ds = DeepSeek()
        engine, llm, transport, clock = world(ds)
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 大厅这边好", channel=HALL, author=21, name="小红")
        engine.handle_edit(hers(1, "@小D 大厅这边很好", mentions_bot=True), edited=True)
        await engine.drain()
        self.assertNotEqual(engine.store.load_unvetted(sync), NOTHING)  # 对照：改进去了、还没问过模型，库里记着
        await say(engine, clock, 2, "@小D 在吗")  # 带着改过的那一行：过了
        self.assertEqual(engine.store.load_unvetted(sync), NOTHING)
        engine = reboot(engine)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 3, f"@小D {RISKY}")  # 阿明这一批被拒了（400）：丢掉
        await say(engine, clock, 4, "@小D 还在吗")
        self.assertEqual(ds.results, ["回了", "回了", "拒了", "回了"])
        whole = record(engine, sync)
        self.assertIn("小红：@小D 大厅这边很好", whole)  # 改过的那一行照旧
        self.assertNotIn(MESSAGE_DELETED, whole)

    async def test_control_clearing_a_channel_forgets_its_own_unvetted_chatter(self):
        # 对照（control）：清空记忆以后，它自己那份里连通时记进去的那几条也没了，库里不留
        ds = DeepSeek()
        engine, llm, transport, clock = world(ds)
        await say(engine, clock, 1, "周末去不去爬山", channel=HALL, author=21, name="小红", call=False)
        connect(engine, HALL, CHANNEL)
        self.assertNotEqual(engine.store.load_unvetted(HALL), NOTHING)  # 对照：连通时记进它自己那份，库里记着
        disconnect(engine, HALL)
        await engine.reset_linked(HALL)
        self.assertEqual(engine.store.load_unvetted(HALL), NOTHING)
        await say(engine, clock, 2, "@小D 在吗", channel=HALL, author=21, name="小红")
        self.assertEqual(ds.results, ["回了"])


if __name__ == "__main__":
    unittest.main()
