"""2.8.0 连通的频道，第十一轮评审修掉的问题（隐私）：服务器退出服务器组时正好有一轮在跑，那一轮写完以后要再查一遍记录（整份查、
拿掉它按原来的组要翻记录、查人设卡的动作），这件事以前只记在内存里：那一轮写完、还没跑完（还在发回话）时 bot 停了（断电、
强制关掉），下次启动时就不查了，只有退出的那个服务器能看的留在了组里的记录里。

- 那一轮写完以后要怎么查也记在数据库里（chat_sync_scrub）：停在中间的话，下次启动时照那时记下的查（整份查、拿掉那几个动作），
  查完就划掉。
- 没停的话照旧在那一轮跑完、放开锁之前查，库里也划掉。"""

import asyncio
import unittest

from helpers import CHANNEL, GUILD, msg
from test_chatsync import connect, replies, say, world
from test_chatsync_privacy import ADMINS, elsewhere, record
from test_chatsync_r2_privacy import same_group
from test_chatsync_r5_scrub import its_replies
from test_chatsync_r6_privacy import leave_group_while_asking
from test_chatsync_r9_privacy import BACK, WHISPER, grouped_with_third, restart, third_leaves
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review, stores


async def stuck_after_writing(engine, transport, message, written):
    """叫它的这一轮写完记录、正在发回话的时候 bot 停了（断电、强制关掉）：回话发不出去，这一轮跑不到最后（放开锁之前的那一遍
    查不了）。written()：这一轮写完了没有。"""
    hang = asyncio.Event()
    real_send = transport.send

    async def stuck_send(*a, **kw):
        await hang.wait()
        return await real_send(*a, **kw)

    transport.send = stuck_send
    await engine.handle_message(message)
    for _ in range(500):
        await asyncio.sleep(0)
        if written():
            break


def stop(engine):
    """bot 停了：还在跑的那一轮就此作废（同一个数据库，重启见 restart）。"""
    for task in list(engine._tasks):
        task.cancel()


class RoundInFlightAtAServerGroupLeaveWhenTheBotStops(unittest.IsolatedAsyncioTestCase):
    async def third_server_leaves_during_a_round(self, *, stops):
        """主服 #闲聊 + 别的服 #大厅连通；第三服和它们在一个服务器组里（主服、第三服开了记忆共享）。阿明在第三服 #后台跟它说了
        句话；主服 #闲聊叫它的这一轮问模型的时候（带着 #后台 的近况），第三服退出了服务器组。stops：这一轮写完、正在发回话时
        bot 停了，然后重启。返回之后的共用记录。"""
        cards, memory = stores()
        quiet = replies()

        def answering(call):  # 叫它的每一轮都回一句（写完记录以后还要发回话）
            if call["json_mode"] and not is_review(call):
                return ACTIONS(REPLY(None, "好的"))
            return quiet(call)

        engine, llm, transport, clock = world(answering, cards=cards, memory=memory)
        sync = grouped_with_third(engine, transport, cards, memory)
        await say(engine, clock, 1, f"@小D {WHISPER}", channel=BACK)
        left = []

        async def leave_while_asking(call):
            if not left and call["json_mode"] and not is_review(call):
                left.append(True)
                third_leaves(engine, cards, memory)

        llm.hook = leave_while_asking
        message = msg(2, "@小D 我回主服了", mentions_bot=True)
        if stops:
            await stuck_after_writing(engine, transport, message, lambda: "我回主服了" in record(engine, sync))
        else:
            await engine.handle_message(message)
            await engine.drain()
        llm.hook = None
        self.assertTrue(left)
        self.assertIn("我回主服了", record(engine, sync))  # 对照：这一轮写完了
        if stops:
            stop(engine)
            restart(engine)  # 同一个数据库重启：启动时查一遍连通组的记录
        return engine, sync

    async def test_what_only_the_departed_server_could_see_is_gone_after_a_hard_stop_in_the_middle_of_the_round(self):
        engine, sync = await self.third_server_leaves_during_a_round(stops=True)
        whole = record(engine, sync)
        self.assertNotIn(WHISPER, whole)  # 那一轮带进来的第三服 #后台 的近况：启动时整份查，拿掉了
        self.assertNotIn("后台", whole)
        self.assertIn("阿明：@小D 我回主服了", whole)
        self.assertEqual(engine.store.pending_scrubs(), {})  # 查过了：库里划掉

    async def test_control_without_a_stop_the_round_is_checked_whole_once_it_ends(self):
        # 对照（control）：没停的话，那一轮跑完、放开锁之前就整份查了，库里也划掉
        engine, sync = await self.third_server_leaves_during_a_round(stops=False)
        whole = record(engine, sync)
        self.assertNotIn(WHISPER, whole)
        self.assertIn("阿明：@小D 我回主服了", whole)
        self.assertEqual(engine.store.pending_scrubs(), {})

    async def test_the_rounds_lookups_are_gone_after_a_hard_stop_in_the_middle_of_the_round(self):
        lookups = ACTIONS({"type": "recall", "channel": "管理组", "about": "封禁名单"}, {"type": "who", "name": "小雨"})
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(REPLY(None, "在")), lookups, ACTIONS(REPLY(None, "这次没翻成"))))
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        same_group(engine, GUILD, OTHER)
        await say(engine, clock, 1, "@小D 管理组开会", channel=ADMINS)
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：同一个服务器组，不算跨服务器
        await say(engine, clock, 2, "@小D 在吗")  # 能翻的频道名单写在这一轮
        left = leave_group_while_asking(engine, llm)  # 别的服退出了服务器组：跨服务器了，马上拿掉了上一轮的名单
        message = msg(3, "@小D 帮我翻一下那个私密频道的封禁名单", channel_id=CHANNEL, guild_id=GUILD, mentions_bot=True)
        await stuck_after_writing(engine, transport, message, lambda: "这次没翻成" in its_replies(engine, sync))
        llm.hook = None
        self.assertTrue(left)
        self.assertIn("管理组", its_replies(engine, sync))  # 对照：停的时候，这一轮要翻 #管理组 的动作还在记录里
        stop(engine)
        restart(engine)
        replied = its_replies(engine, sync)
        self.assertNotIn("管理组", replied)  # 启动时照记下的查：这一轮要翻 #管理组、查小雨的卡的动作拿掉了
        self.assertNotIn("小雨", replied)
        self.assertEqual(engine.store.pending_scrubs(), {})


if __name__ == "__main__":
    unittest.main()
