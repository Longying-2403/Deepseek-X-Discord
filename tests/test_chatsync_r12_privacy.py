"""2.8.0 连通的频道，第十二轮评审修掉的问题（隐私）：库里记着的、以前哪一轮写完以后没查成的检查，被后来一轮只照内存里的那点
查完就划掉了；连通组的记录里，很早的消息改了又改回原样，那句“改成了什么”还等着附上。

- 一轮写完以后再查一遍（服务器退出服务器组时那一轮正在跑）出错了：库里记着的留到下次启动再查。之后哪一轮写完以后又要查一遍，
  连同库里记着的一起查，查完才划掉：不会因为后来那一遍查得少，就把该拿掉的翻记录、查人设卡的动作永远留在组里的记录里。
- 连通组的记录里，很早的消息改了（等着附一句“改成了什么”）又改回了记录里的样子：那句作废。别的服务器的频道不会知道她收回的话，
  她的频道断开（组还在）时也不会把那句写进组里的记录。频道自己那份照 2.7.1。"""

import sqlite3
import unittest

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, replies, say, text_of, world
from test_chatsync_privacy import ADMINS, elsewhere, record
from test_chatsync_r2_privacy import same_group
from test_chatsync_r5_scrub import its_replies
from test_chatsync_r9_privacy import restart
from test_chatsync_r11_privacy import stop
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review

THIRD = 3  # 第三服：别的服退组以后和主服连成一组（它后来也退组）
LOOKUPS = ACTIONS({"type": "recall", "channel": "管理组", "about": "封禁名单"}, {"type": "who", "name": "小雨"})
ADDRESS = "我家在幸福路三号"  # 小红改进去、又马上删掉的那句


class StoredCheckOfAnEarlierRound(unittest.IsolatedAsyncioTestCase):
    async def first_rounds_check_fails(self):
        """主服 #闲聊 + 别的服 #大厅连通（同一个服务器组）。主服 #闲聊叫它的这一轮问模型的时候，别的服退出了服务器组（跨服务器了，
        马上拿掉了上一轮的名单：库里记下这一轮写完以后要拿掉它的翻记录、查人设卡的动作）。模型照原来的组要翻 #管理组、查小雨；
        这一轮写完以后再查的那一遍碰上数据库出错（被别的程序锁住了）：动作还在记录里，库里记着的留着。之后主服和第三服连成一组。"""
        engine, llm, transport, clock = world(
            replies(ACTIONS(), ACTIONS(REPLY(None, "在")), LOOKUPS, ACTIONS(REPLY(None, "这次没翻成")), ACTIONS(REPLY(None, "好")))
        )
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        same_group(engine, GUILD, OTHER)
        group = engine.store.group_of(GUILD)
        await say(engine, clock, 1, "@小D 管理组开会", channel=ADMINS)
        sync = connect(engine)
        await say(engine, clock, 2, "@小D 在吗")  # 能翻的频道名单写在这一轮
        left = []

        async def leave_while_asking(call):
            if not left and call["json_mode"] and not is_review(call):
                left.append(True)
                engine.store.unmap(OTHER)
                engine.groups_changed([GUILD, OTHER], left=True)

        llm.hook = leave_while_asking
        real_scrub_now, failed = engine._scrub_now, []

        def locked_once(cs):
            if failed:
                return real_scrub_now(cs)
            failed.append(cs.key)
            real_update = engine.store.update_turns

            def locked(pairs):
                raise sqlite3.OperationalError("database is locked")

            engine.store.update_turns = locked
            try:
                real_scrub_now(cs)
            finally:
                engine.store.update_turns = real_update

        engine._scrub_now = locked_once
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 3, "@小D 帮我翻一下那个私密频道的封禁名单")
        llm.hook = None
        self.assertTrue(left)
        self.assertEqual(failed, [sync])  # 对照：这一轮写完以后查了，出错了
        self.assertIn("管理组", its_replies(engine, sync))  # 对照：那几个动作还在记录里
        self.assertEqual(engine.store.pending_scrubs(), {sync: (True, False)})  # 对照：库里记着的留着
        engine.store.join_group(THIRD, group, now=5.0)  # 之后主服和第三服连成一组（别的服不在里面：还是跨服务器）
        return engine, llm, clock, sync

    async def test_a_later_end_of_round_check_also_runs_what_an_earlier_round_left_stored(self):
        engine, llm, clock, sync = await self.first_rounds_check_fails()
        second = []

        async def third_leaves_while_asking(call):
            if not second and call["json_mode"] and not is_review(call):
                second.append(True)
                engine.store.unmap(THIRD)
                engine.groups_changed([GUILD, THIRD], left=True)  # 主服的组又变了：这一轮写完以后再查一遍（这次查不出什么）

        llm.hook = third_leaves_while_asking
        await say(engine, clock, 4, "@小D 好的")
        llm.hook = None
        self.assertTrue(second)
        replied = its_replies(engine, sync)
        self.assertNotIn("管理组", replied)  # 连同库里记着的一起查了：要翻 #管理组、查小雨的卡的动作拿掉了
        self.assertNotIn("小雨", replied)
        self.assertEqual(engine.store.pending_scrubs(), {})  # 查过了才划掉
        stop(engine)
        restart(engine)
        self.assertNotIn("管理组", its_replies(engine, sync))

    async def test_control_without_a_later_check_the_stored_one_is_run_at_the_next_startup(self):
        # 对照（control）：之后没有哪一轮再查，库里记着的在下次启动时查
        engine, llm, clock, sync = await self.first_rounds_check_fails()
        stop(engine)
        restart(engine)
        replied = its_replies(engine, sync)
        self.assertNotIn("管理组", replied)
        self.assertNotIn("小雨", replied)
        self.assertEqual(engine.store.pending_scrubs(), {})


def hers(mid, text):
    """小红在别的服 #大厅发的一条（叫了它）。"""
    return msg(mid, text, channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)


def whole_request(llm):
    """最近一次聊天请求的全部内容。"""
    return "\n".join(text_of(m["content"]) for m in chats(llm)[-1]["messages"])


class RevertedEditOfAnOldLine(unittest.IsolatedAsyncioTestCase):
    async def edit_and_take_back(self, engine, clock, *, take_back=True):
        """小红在别的服 #大厅说的“周末去聚会”（#1）回过以后，主服 #闲聊又聊了五轮：#1 在共用的记录里成了很早的消息。她改成加上
        家里地址（等着附一句“改成了什么”），又马上改回了原样（take_back）。"""
        await say(engine, clock, 1, "@小D 周末去聚会", channel=HALL, author=21, name="小红")
        for i in range(5):
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")
        engine.handle_edit(hers(1, f"@小D 周末去聚会，{ADDRESS}"), edited=True)
        await engine.drain()
        if take_back:
            engine.handle_edit(hers(1, "@小D 周末去聚会"), edited=True)
            await engine.drain()

    async def test_the_other_server_is_not_told_the_text_she_took_back(self):
        engine, llm, transport, clock = world()
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅
        await self.edit_and_take_back(engine, clock)
        await say(engine, clock, 20, "@小D 主服接着聊")  # 主服这一轮会补进 #大厅 还没附上的“改成了什么”
        self.assertNotIn(ADDRESS, whole_request(llm))
        self.assertNotIn(ADDRESS, record(engine, sync))

    async def test_the_text_she_took_back_is_not_written_into_the_group_when_her_channel_leaves(self):
        engine, llm, transport, clock = world()
        sync = connect(engine)
        connect(engine, LOBBY, CHANNEL)  # 主服 #大厅也连着：别的服 #大厅断开以后组还在
        await self.edit_and_take_back(engine, clock)
        disconnect(engine, HALL)  # 断开时它还没附上的“改成了什么”直接改进组里的记录
        self.assertNotIn(ADDRESS, record(engine, sync))

    async def test_control_an_edit_she_keeps_is_told_as_before(self):
        # 对照（control）：没改回去的，照旧跟着 #大厅 补进来的那段附上
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await self.edit_and_take_back(engine, clock, take_back=False)
        await say(engine, clock, 20, "@小D 主服接着聊")
        self.assertIn(ADDRESS, record(engine, sync))


if __name__ == "__main__":
    unittest.main()
