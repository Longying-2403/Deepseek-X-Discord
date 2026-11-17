"""2.8.0 连通的频道，第五轮评审修掉的问题：跨服务器的连通里，记录里不留只有原来那边能看的东西，以后再查也不用改记录。

- 跨服务器的连通里它要翻别的频道的记录：不翻，记下的那句不写想翻的是哪个频道。服务器退出了服务器组、组里正好有一轮在跑的
  时候（问模型之前按原来的组带上了能翻的频道名单，名单上有主服单独开了共享的私密频道），它要翻的就是名单上的名字。那一轮
  跑完再查时，名单和要翻的那个动作都拿掉了，“不翻”的那句照旧留着（跨服务器的连通自己也会写）：别的服务器的频道（以后才
  连进来的也一样）看不到那个名字。
- 人设卡各服务器一张：跨服务器的连通里它写卡的动作，写错了没认出来的也算，记下它的回话之前就拿掉（它说的话照旧）；查完资料
  再问它的那一步也一样。以后再查（每次启动、每次连通断开）不用改记录，bot 主人也不会看到“有 N 条拿掉了”。人设卡所有服务器
  共用的话照常写卡，写卡的动作照旧记在它的回话里。"""

import unittest

from helpers import GUILD
from test_chatsync import HALL, chats, connect, replies, say, text_of, world
from test_chatsync_privacy import ADMINS, elsewhere, history_of, last_user, record
from test_chatsync_r2_privacy import same_group
from test_chatsync_r3_privacy import turns
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review, stores

from dsbot.memory import SHARED
from dsbot.prompts import RECALL_SYNC_OFF, WHO_SYNC_OFF

CARD = "吸血鬼领主"  # 阿明的人设卡
NEW_CARD = f"{CARD}，喜欢吃辣"  # 他要改成的
# 它写了写卡的动作，可是 to 写成了名字：没认出来，卡没写成；它说的话照常发
WRONG = ACTIONS({"type": "card", "to": "阿明", "text": NEW_CARD}, REPLY(1, "这里改不了卡，请用 /ds-rp"))


def its_replies(engine, sync):
    """共用记录里它的每条回话，照记下的原话（写错了没认出来的动作也在里面）。"""
    return "\n".join(text_of(t.content) for t in engine.store.load_conversation(sync, 0).turns if t.role == "assistant")


class RefusedRecall(unittest.IsolatedAsyncioTestCase):
    async def test_a_recall_refused_after_the_group_left_mid_round_does_not_name_the_channel(self):
        recall = ACTIONS({"type": "recall", "channel": "管理组", "about": "封禁名单"})
        engine, llm, transport, clock = world(replies(ACTIONS(), recall, ACTIONS(REPLY(None, "这次没翻成"))))
        elsewhere(engine, transport)  # 主服开了记忆共享；#管理组 是单独开了共享的私密频道
        same_group(engine, GUILD, OTHER)
        await say(engine, clock, 1, "@小D 管理组开会", channel=ADMINS)
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：同一个服务器组，不算跨服务器（能翻的频道名单照常写进共用的记录）
        left = []

        async def leave_while_asking(call):
            if not left and call["json_mode"] and not is_review(call):
                left.append(True)  # #闲聊 这一轮正在等模型回话：别的服退出了服务器组（/dsowner 服务器组 的退组做完了）
                engine.store.unmap(OTHER)
                engine.groups_changed([GUILD, OTHER], left=True)

        llm.hook = leave_while_asking
        await say(engine, clock, 2, "@小D 帮我翻一下那个私密频道的封禁名单")  # 它要翻名单上的 #管理组：这时候已经跨服务器了，不翻
        llm.hook = None
        self.assertTrue(left)
        self.assertIn("#管理组", text_of(chats(llm)[-2]["messages"][-1]["content"]))  # 对照：这一轮按原来的组带上了名单
        await say(engine, clock, 3, "@小D 在吗", channel=HALL, author=21, name="小红")
        self.assertNotIn("管理组", history_of(llm))  # 别的服 #大厅 被叫到时看不到主服私密频道的名字
        whole = record(engine, sync)
        self.assertNotIn("管理组", whole)  # 记录里也没有：以后才连进来的频道也看不到
        self.assertIn(RECALL_SYNC_OFF, whole)  # 记下的是“不翻”（没写想翻的是哪个频道）


class CardItemsInItsReplies(unittest.IsolatedAsyncioTestCase):
    async def test_a_card_item_the_parser_did_not_accept_is_not_kept_in_its_reply(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(replies(WRONG), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", CARD, via="command")
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：一开始就跨服务器，人设卡各服务器一张
        await say(engine, clock, 1, f"@小D 我的人设卡改成：{NEW_CARD}")
        self.assertEqual(cards.get(GUILD, 11).text, CARD)  # 对照：没认出来，什么都没写
        replied = its_replies(engine, sync)
        self.assertNotIn(NEW_CARD, replied)  # 写错了的写卡动作（里面是整张卡）没留在它的回话里
        self.assertIn("这里改不了卡", replied)  # 它说的话照旧
        before = turns(engine, sync)
        # 以后再查（每次启动、每次连通断开）：什么都不用拿（bot 主人连通、断开时不会看到“有 1 条拿掉了”）
        self.assertEqual(engine.scrub_cross(sync), 0)
        self.assertEqual(turns(engine, sync), before)  # 记录原样（不改中间那条，缓存不失效）

    async def test_a_card_item_the_parser_did_not_accept_after_a_lookup_is_not_kept_either(self):
        lookup = ACTIONS({"type": "who", "name": "小雨"})  # 先要查小雨的人设卡（跨服务器的连通里不查）
        cards, memory = stores()
        engine, llm, transport, clock = world(replies(lookup, WRONG), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", CARD, via="command")
        sync = connect(engine)
        await say(engine, clock, 1, f"@小D 小雨是谁？我的人设卡改成：{NEW_CARD}")
        self.assertIn(WHO_SYNC_OFF, last_user(llm))  # 对照：查的那一步没查，再问它的时候它写错了写卡的动作
        self.assertEqual(cards.get(GUILD, 11).text, CARD)
        replied = its_replies(engine, sync)
        self.assertNotIn(NEW_CARD, replied)  # 查完再问它的那一步记下的回话里也不留
        self.assertIn("这里改不了卡", replied)
        before = turns(engine, sync)
        self.assertEqual(engine.scrub_cross(sync), 0)
        self.assertEqual(turns(engine, sync), before)

    async def test_control_with_shared_cards_the_card_it_wrote_stays_in_its_reply(self):
        # 对照（control）：人设卡所有服务器共用的话，跨服务器的连通里照常写卡，写卡的动作照旧记在它的回话里（以后核对写卡时
        # 认得出那张是它替他写过的）
        cards, memory = stores()
        write = ACTIONS({"type": "card", "to": 1, "text": NEW_CARD}, REPLY(1, "记下了"))
        engine, llm, transport, clock = world(replies(write), cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", CARD, via="command")
        cards.set_shared(True, scope_of=engine.store.memory_scope, alive=set())
        sync = connect(engine)  # 主服 #闲聊 + 别的服 #大厅：跨服务器，人设卡所有服务器共用
        await say(engine, clock, 1, f"@小D 我的人设卡改成：{NEW_CARD}")
        self.assertEqual(cards.get(SHARED, 11).text, NEW_CARD)  # 写成了
        self.assertIn(NEW_CARD, its_replies(engine, sync))  # 写卡的动作留在它的回话里


if __name__ == "__main__":
    unittest.main()
