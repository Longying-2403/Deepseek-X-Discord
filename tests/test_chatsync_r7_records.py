"""2.8.0 连通的频道，第七轮评审修掉的问题：删掉、改过的消息照实进记录。

- 组散了，压缩前挑出来、交给不在组里的一份接着整理的那批：只记下它那份记录里有的消息被删了。别的地方（不相干的频道、别的
  服务器）一次删很多条，不会把它记着的删除挤掉（挤掉了，就会照她删掉的那条写人设卡）。散组之前删的、散了以后才删的都一样。
- 连通的频道暂停着（变成私密的、被关掉了）时改过的、连通期间才说的消息（还在回的那一轮里的也算）：组里那份当成删了；它断开
  （或者组散了）拿回去的那一行照改过的写，只有它自己那份有，组里、别的频道拿回去的都没有。连通期间重启过、之后才改的也一样
  （改过的样子只记在内存里：重启之前改的，重启以后就不记得了）。
- 改过以后又删了：拿回去的还是删掉的那一行（组里记着的删除被别的删除挤掉了也一样）。又公开了、再改了一次：照后来那次。"""

import asyncio
import json
import re
import unittest

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, replies, say, text_of, world
from test_chatsync_lifecycle import hold, spin
from test_chatsync_privacy import record
from test_chatsync_r2_records import compact, hold_reviews, times_reviewed
from test_chatsync_r5_records import HEAVY
from test_chatsync_records import three_linked
from test_dm import OTHER
from test_memory import is_review, stores

from dsbot.engine import DELETED_KEEP
from dsbot.models import ChannelPlace
from dsbot.prompts import MESSAGE_DELETED

HER_CARD = "小红说她家住在幸福路三号"  # 整理时照小红那条消息给她写的卡
HERS = 9  # 小红在别的服 #大厅说她家住址的那条（记录里是 #1：消息 ID 和编号不一样）
ELSEWHERE = 555  # 主服里一个不相干的频道：没连通过，也没跟它聊过
SAID = "@小D 明天开会"  # 小红在别的服 #大厅说的（消息 2，记录里是 #1）
HIDDEN = "改到周五"  # #大厅变成私密的以后，她把那条改成了“明天开会：改到周五”
LATER = "改到周六"  # #大厅又公开以后，她又改成了“明天开会：改到周六”


def writing_her_card():
    """整理到小红说“我家住在幸福路三号”的那一段：照那条消息的编号给她写人设卡，再记一条长期记忆，写着是第几次问到这一段
    （看得出照着改的是哪一次）；聊天、压缩摘要照 replies() 回。"""
    others = replies()
    asked = []

    def responder(call):
        if is_review(call):
            hit = re.search(r"#(\d+) 小红：[^\n]*我家住在幸福路三号", text_of(call["messages"][-1]["content"]))
            if hit is None:
                return '{"cards": [], "memory": []}'
            asked.append(hit)
            card = {"to": int(hit.group(1)), "text": HER_CARD}
            note = {"add": f"小红那段整理过了（第{len(asked)}次问到）"}
            return json.dumps({"cards": [card], "memory": [note]}, ensure_ascii=False)
        return others(call)

    return responder


def purge(engine, guild, channel, start=50_000):
    """管理员在这个频道一次删了很多条（Discord 一批最多删 100 条）：一共 DELETED_KEEP 条，都不在这几份记录里。"""
    end = start + DELETED_KEEP
    for first in range(start, end, 100):
        engine.handle_delete(guild, channel, range(first, min(first + 100, end)))


def set_public(transport, public):
    """别的服把 #大厅 改成私密的（连通暂停：它不看那里的消息），或者又改回公开的。"""
    transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=public)


def edited(words):
    """小红把她在别的服 #大厅说的那条（消息 2）改成了“明天开会：words”。"""
    return msg(2, f"{SAID}：{words}", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)


def own_record(engine, channel, guild):
    """这个频道自己那份记录。"""
    return "\n".join(text_of(t.content) for t in engine.store.load_conversation(channel, guild).turns)


class DissolvedGroupsReview(unittest.IsolatedAsyncioTestCase):
    async def test_a_purge_elsewhere_does_not_make_the_dissolved_groups_review_forget_her_delete(self):
        for after in (False, True):
            with self.subTest("散了以后才删" if after else "散组之前删的"):
                cards, memory = stores()
                engine, llm, transport, clock = world(
                    writing_her_card(), cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1000}
                )
                connect(engine)  # 主服 #闲聊 + 别的服 #大厅：断开一个，组就散了
                await say(engine, clock, HERS, f"@小D 我家住在幸福路三号{HEAVY}", channel=HALL, author=21, name="小红")
                for i in range(3):
                    await say(engine, clock, 10 + i, f"@小D 主服最近{i}")
                gate = asyncio.Event()
                hold_reviews(llm, "我家住在幸福路三号", gate)  # 整理小红那一段的时候模型回得慢
                await compact(engine, CHANNEL)  # 压掉小红那一轮：挑出来一批，正在问
                await spin()
                if not after:
                    engine.handle_delete(OTHER, HALL, [HERS])  # 这时候小红删了那条（组里记着）
                self.assertEqual(disconnect(engine, CHANNEL), ([CHANNEL, HALL], []))  # 组散了：那批交给不在组里的一份接着整理
                await spin()
                self.assertEqual(times_reviewed(llm, "我家住在幸福路三号"), 2)  # 对照：接着整理的那份又在问那一段
                if after:
                    engine.handle_delete(OTHER, HALL, [HERS])  # 散了以后小红才删
                purge(engine, GUILD, ELSEWHERE)  # 主服的管理员在一个不相干的频道一次删了很多条
                gate.set()
                await engine.drain()
                llm.hook = None
                # 对照：问到一半的那次作废，接着整理的那份问完照着改了
                self.assertEqual([m.text for m in memory.entries(OTHER)], ["小红那段整理过了（第2次问到）"])
                self.assertIsNone(cards.get(OTHER, 21))  # 照她删掉的那条写的卡不写：她删的还记着


class EditsMadeWhilePaused(unittest.IsolatedAsyncioTestCase):
    async def test_an_edit_made_while_paused_is_in_its_own_record_after_it_leaves(self):
        for how in ("断开它", "组散了", "关掉了允许回应", "重启以后才改的"):
            with self.subTest(how):
                engine, llm, transport, clock = world(replies())
                dissolve = how == "组散了"
                # 组散了：主服 #闲聊 + 别的服 #大厅；别的：再加上主服 #大厅，别的服 #大厅断开以后组还在
                sync = connect(engine) if dissolve else three_linked(engine)
                await say(engine, clock, 2, SAID, channel=HALL, author=21, name="小红")  # 连通期间说的：只在组里那份
                if how == "重启以后才改的":
                    engine.states.clear()
                    engine.convs.clear()  # 像刚重启：内存里什么都没有（还连通着）
                if how == "关掉了允许回应":
                    engine.settings.set("channel", HALL, "enabled", False)  # 别的服的管理员在 #大厅关了“允许回应”（/ds 区域）
                else:
                    set_public(transport, False)  # 连通暂停
                engine.handle_edit(edited(HIDDEN), edited=True)
                await engine.drain()
                self.assertIn(f"#1 小红：{MESSAGE_DELETED}", record(engine, sync))  # 对照：组里那份当成删了
                if dissolve:
                    self.assertEqual(disconnect(engine, CHANNEL), ([CHANNEL, HALL], []))  # 主服 #闲聊断开：组散了
                else:
                    # bot 主人照 /ds 状态 说的把暂停的频道断开
                    self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
                await engine.drain()
                own = own_record(engine, HALL, OTHER)
                self.assertIn(f"{SAID}：{HIDDEN}", own)  # 它自己那份拿回去的那一行照改过的写
                self.assertNotIn(MESSAGE_DELETED, own)
                if dissolve:
                    self.assertNotIn(HIDDEN, own_record(engine, CHANNEL, GUILD))  # 主服 #闲聊拿回去的没有它
                else:
                    self.assertNotIn(HIDDEN, record(engine, sync))  # 组里还在的频道照样看不到

    async def test_an_edit_made_while_paused_during_its_round_is_in_its_own_record_after_it_leaves(self):
        engine, llm, transport, clock = world(replies())
        sync = three_linked(engine)  # 主服 #闲聊、别的服 #大厅、主服 #大厅：别的服 #大厅断开以后组还在
        gate = asyncio.Event()
        hold(llm, {"明天开会": gate})
        await engine.handle_message(msg(2, SAID, channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True))
        await spin()
        self.assertEqual(len(chats(llm)), 1)  # 对照：别的服 #大厅这一轮在等模型回话
        set_public(transport, False)  # 连通暂停
        engine.handle_edit(edited(HIDDEN), edited=True)  # 这时候改了：模型看到的是改之前的，写进记录的也是
        gate.set()
        await engine.drain()
        llm.hook = None
        whole = record(engine, sync)
        self.assertIn(f"#1 小红：{MESSAGE_DELETED}", whole)  # 对照：写完以后组里那份当成删了
        self.assertNotIn(HIDDEN, whole)
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
        await engine.drain()
        own = own_record(engine, HALL, OTHER)
        self.assertIn(f"{SAID}：{HIDDEN}", own)  # 它自己那份拿回去的那一行照改过的写
        self.assertNotIn(MESSAGE_DELETED, own)

    async def test_control_a_message_deleted_after_its_hidden_edit_stays_deleted_in_its_own_record(self):
        # 对照（control）：暂停时改过、后来又删了：拿回去的还是删掉的那一行。改过的样子删的时候就不记了，不靠组里记着的删除
        # （那些被别的删除挤掉了也一样）
        engine, llm, transport, clock = world(replies())
        three_linked(engine)
        await say(engine, clock, 2, SAID, channel=HALL, author=21, name="小红")
        set_public(transport, False)
        engine.handle_edit(edited(HIDDEN), edited=True)
        await engine.drain()
        engine.handle_delete(OTHER, HALL, [2])  # 小红又删了那条
        purge(engine, OTHER, HALL)  # 别的服的管理员又在 #大厅 一次删了很多条：组里记着的删除里没有那条了
        await engine.drain()
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
        await engine.drain()
        own = own_record(engine, HALL, OTHER)
        self.assertIn(f"#1 小红：{MESSAGE_DELETED}", own)  # 删掉的没有又冒出来
        self.assertNotIn(HIDDEN, own)

    async def test_control_an_edit_made_after_it_is_public_again_replaces_the_hidden_one(self):
        # 对照（control）：暂停时改过，又公开以后再改了一次：组里、它自己那份都照后来那次（暂停时改的那次不再写回去）
        engine, llm, transport, clock = world(replies())
        sync = three_linked(engine)
        await say(engine, clock, 2, SAID, channel=HALL, author=21, name="小红")
        set_public(transport, False)
        engine.handle_edit(edited(HIDDEN), edited=True)
        await engine.drain()
        set_public(transport, True)  # 别的服又把 #大厅 改回公开的
        engine.handle_edit(edited(LATER), edited=True)
        await engine.drain()
        self.assertIn(f"{SAID}：{LATER}", record(engine, sync))  # 对照：公开以后改的，组里照常改
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))
        await engine.drain()
        own = own_record(engine, HALL, OTHER)
        self.assertIn(f"{SAID}：{LATER}", own)
        self.assertNotIn(HIDDEN, own)


if __name__ == "__main__":
    unittest.main()
