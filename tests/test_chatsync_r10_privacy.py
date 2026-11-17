"""2.8.0 连通的频道，第十轮评审修掉的隐私方面的问题：服务器退出服务器组以后，组里的频道写完一轮再整份查一遍记录的，只是退组时
真在跑的那一轮。

- 连通组的频道还在一个服务器组里，组里原来有的服务器退出了服务器组：退组时整份记录照变成跨服务器时那样查一遍；那时正好有一轮
  在跑的（在问模型、在等查完资料的第二步回话……），那一轮写进去以后、放开锁之前再整份查一遍：它按原来的组带上的、翻到的，只有
  退出的那个服务器能看的东西不留下。
- 那时只是 /dsowner chatsync 连通、断开的命令占着这份记录的锁（在等别的频道正在回的那一轮说完），没有哪一轮在跑：之后才开始的
  那一轮带的、翻到的都是照退组以后的组来的，写进去的（能翻的频道名单、翻到的组里频道的记录、要翻的那个动作）照旧留着，不会又被
  整份查掉。"""

import asyncio
import unittest

from helpers import CHANNEL, GUILD
from test_chatsync import LOBBY, chats, say, text_of, world
from test_chatsync_lifecycle import hold, spin
from test_chatsync_privacy import GAMES, record
from test_chatsync_r2_privacy import RECALL_GAMES
from test_chatsync_r3_privacy import when_asked
from test_chatsync_r4_scrub import kinds_in_replies
from test_chatsync_r6_records import link_like_the_command
from test_chatsync_r9_privacy import BACK, WHISPER, grouped_with_third, third_leaves
from test_memory import ACTIONS, stores

from dsbot.models import ChannelPlace

PLAN = "周六一起打副本"  # 小雨在主服 #游戏 跟它说的（主服一直在服务器组里）
RECALL_BACK = ACTIONS({"type": "recall", "channel": "后台"})  # 翻第三服 #后台 的记录
SAYING = {"帮我翻一下游戏": RECALL_GAMES, "帮我翻一下后台": RECALL_BACK}


class ServerGroupLeaveWhileTheSharedRecordIsLocked(unittest.IsolatedAsyncioTestCase):
    async def test_a_round_started_after_the_leave_keeps_what_it_looked_up_when_only_a_link_command_held_the_lock(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(when_asked(SAYING), cards=cards, memory=memory)
        # 主服、别的服、第三服一个服务器组；主服 #闲聊 + 别的服 #大厅连通（主服开了记忆共享）
        sync = grouped_with_third(engine, transport, cards, memory)
        transport.places[GAMES] = ChannelPlace(guild_id=GUILD, name="游戏", public=True)
        transport.names[GAMES] = "游戏"
        await say(engine, clock, 1, f"@小D {PLAN}", channel=GAMES, author=12, name="小雨")  # 主服 #游戏（它自己那份记录）
        gate = asyncio.Event()
        hold(llm, {"你好": gate})
        busy = asyncio.create_task(say(engine, clock, 2, "@小D 你好", channel=LOBBY, author=12, name="小雨"))
        await spin()  # 主服 #大厅（还没连通）有人叫它：正在等模型回话
        linking = asyncio.create_task(link_like_the_command(engine, LOBBY, CHANNEL))
        await spin()  # bot 主人把它连进来：命令先拿了共用记录的锁，在等它那一轮说完
        self.assertTrue(engine.convs[sync].lock.locked())  # 对照：锁被命令占着（组里没有哪一轮在跑）
        third_leaves(engine, cards, memory)  # 这时第三服退出了服务器组（主服、别的服还在一个组里）
        gate.set()
        await busy
        self.assertEqual(await linking, sync)  # 对照：连上了
        # 退组以后才开始的一轮：阿明在主服 #闲聊要翻主服 #游戏（退组前后都在组里）
        await say(engine, clock, 3, "@小D 帮我翻一下游戏")
        whole = record(engine, sync)
        self.assertIn("（能翻记录的别的频道：", whole)  # 这一轮写的能翻的频道名单留着
        self.assertIn(PLAN, whole)  # 翻到的记录留着
        self.assertEqual(kinds_in_replies(engine, sync), ["recall"])  # 回话里要翻的那个动作也留着

    async def test_control_a_round_in_flight_at_the_leave_is_still_checked_whole_once_it_is_written(self):
        # 对照（control）：退组时真有一轮在跑（主服 #闲聊翻了第三服 #后台的记录，正在等查完以后那一步的回话）：那一轮写进去以后、
        # 放开锁之前照样整份查一遍，翻到的只有第三服能看的记录不留下
        cards, memory = stores()
        engine, llm, transport, clock = world(when_asked(SAYING), cards=cards, memory=memory)
        sync = grouped_with_third(engine, transport, cards, memory)
        await say(engine, clock, 1, f"@小D {WHISPER}", channel=BACK, author=12, name="小雨")  # 第三服 #后台（它自己那份记录）
        left = []

        async def leave_while_answering(call):
            if not left and call["json_mode"] and "（你翻了" in text_of(call["messages"][-1]["content"]):
                left.append(True)  # 第二步问模型时（带着翻到的记录）：第三服退出了服务器组
                third_leaves(engine, cards, memory)

        llm.hook = leave_while_answering
        await say(engine, clock, 2, "@小D 帮我翻一下后台")
        llm.hook = None
        self.assertTrue(left)
        self.assertIn(WHISPER, text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 对照：第二步问模型时带着翻到的记录
        whole = record(engine, sync)
        self.assertNotIn(WHISPER, whole)  # 写进去以后整份又查了一遍：翻到的记录拿掉了
        self.assertNotIn("你翻了", whole)
        self.assertEqual(kinds_in_replies(engine, sync), [])  # 要翻的那个动作也拿掉
        self.assertIn("阿明：@小D 帮我翻一下后台", whole)  # 说过的话照旧


if __name__ == "__main__":
    unittest.main()
