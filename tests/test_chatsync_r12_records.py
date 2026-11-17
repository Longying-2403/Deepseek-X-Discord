"""2.8.0 连通的频道，第十二轮评审修掉的问题（群友看到的）：和别的服务器的频道连通、人设卡各服务器一张时，这个频道里不附人设卡
（模型会请群友自己用 /ds-rp 改），可是在这里用 /ds-rp 存卡，回复却说“下次你说话时它就会用上”；断开时跟着频道回来的那句
“改成了什么”，她把消息改回了原来的样子（就是那一行写的），那句还留着。

- 这样的频道里存卡：回复照实说这个频道不会用（这个服务器别的频道照常用），和这里关掉了人设卡时一样。
- 没连通的频道、同一个服务器（组）里的连通、所有服务器共用一张卡时照旧。
- 那一行是最近的（断开时拿回来的那几轮）时改回了原来的样子：那句作废，不再告诉它更早的那次改成了什么，那句被拒了的话改回去就
  照常回，再连通时也不会把那一行改成更早的那次。频道自己那份里很早的一行照 2.7.1（那句照旧附一次）。"""

import unittest
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, FakeResponse, chats, connect, disconnect, say, text_of, world
from test_chatsync_lifecycle import prompt_of
from test_chatsync_privacy import record
from test_chatsync_r6_privacy import own_record
from test_chatsync_r9_records import RISKY, DeepSeek
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, chat, stores, user_text

CARD = "自称猫娘，说话爱带喵"
PROMISE = "下次你说话时它就会用上"


def member_bot(engine):
    """CardModal 用到的那几样（commands.py 只看 bot.cards / memory / cfg / store / settings / engine）。"""
    return NS(cards=engine.cards, memory=engine.memory, cfg=engine.cfg, store=engine.store, settings=engine.settings, engine=engine)


def member_interaction(channel=CHANNEL, guild=GUILD, user=11, name="阿明"):
    """阿明在频道里用 /ds-rp、交了编辑框（只认 commands 用到的那几个属性）。"""
    return NS(
        user=NS(id=user, display_name=name, name=name),
        guild_id=guild,
        channel_id=channel,
        channel=NS(id=channel, category_id=None),
        response=FakeResponse(),
        followup=NS(send=None),
    )


class CardSavedInACrossServerLink(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")

    async def save_card(self, *, linked):
        """主服 #闲聊（linked：和别的服 #大厅连通，跨服务器）：阿明用 /ds-rp 存了卡，接着在 #闲聊 叫它。返回存卡的回复、那一轮的请求。"""
        from dsbot.commands import CardModal

        cards, memory = stores()
        engine, llm, transport, clock = world(chat([ACTIONS(REPLY(None, "在"))]), cards=cards, memory=memory)
        if linked:
            connect(engine)
        modal = CardModal(member_bot(engine), GUILD, "")
        modal.text._value = CARD  # 编辑框里填的
        interaction = member_interaction()
        await modal.on_submit(interaction)
        reply = interaction.response.sent[-1][0]
        self.assertEqual(engine.cards.get(GUILD, 11).text, CARD)  # 卡真的存了
        await say(engine, clock, 1, "@小D 我是谁？")
        return reply, user_text(chats(llm)[-1])

    async def test_the_save_reply_says_the_card_is_not_used_in_this_linked_channel(self):
        reply, request = await self.save_card(linked=True)
        self.assertNotIn(CARD, request)  # 对照：照设计，这个连通的频道里不附他的卡
        self.assertIn("连通", reply)  # 照实说这个频道不会用
        self.assertIn("不会用", reply)

    async def test_control_an_unlinked_channel_keeps_the_promise(self):
        # 对照（control）：没连通的频道照常这么说，下一轮也真的附上了
        reply, request = await self.save_card(linked=False)
        self.assertIn(PROMISE, reply)
        self.assertNotIn("连通", reply)
        self.assertIn(CARD, request)


FIRST = "@小D 我的号码是 138"  # 她原来说的（拿回来以后她自己那份那一行就是这样写的）
EDITED = "@小D 我的号码改成 139"  # 她先改成的（那时那一行在共用的记录里很早了：只记下一句“改成了什么”）


def hers(mid, text):
    """小红在别的服 #大厅发的一条（叫了它）。"""
    return msg(mid, text, channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)


def current(call):
    """这一轮给它看的（不算记录）。"""
    return text_of(call["messages"][-1]["content"])


class EditBackOfALineThatIsRecentAgain(unittest.IsolatedAsyncioTestCase):
    async def note_went_home_then_edit_back(self, first_edit=EDITED, responder=None):
        """主服 #闲聊、别的服 #大厅、主服 #大厅连通。小红在别的服 #大厅说的 FIRST（#5）回过以后，主服 #闲聊又聊了五轮，她改成
        first_edit：只记下一句“改成了什么”。别的服 #大厅还没说话就断开了：那句跟着它回到它自己那份，#5 那一轮在它自己那份里是最近的。
        然后她又改回了原来那句。"""
        engine, llm, transport, clock = world(responder)
        sync = connect(engine)
        connect(engine, LOBBY, CHANNEL)
        await say(engine, clock, 5, FIRST, channel=HALL, author=21, name="小红")
        for i in range(5):
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")
        engine.handle_edit(hers(5, first_edit), edited=True)
        await engine.drain()
        disconnect(engine, HALL)
        self.assertIn(5, engine.convs[HALL].edit_notes)  # 对照：那句跟着它回到它自己那份
        engine.handle_edit(hers(5, FIRST), edited=True)
        await engine.drain()
        return engine, llm, clock

    async def test_the_note_is_not_told_after_she_edited_the_message_back(self):
        engine, llm, clock = await self.note_went_home_then_edit_back()
        self.assertIn("我的号码是 138", own_record(engine, HALL, OTHER))  # 对照：那一行就是现在的样子
        await say(engine, clock, 50, "@小D 在吗", channel=HALL, author=21, name="小红")
        self.assertNotIn("我的号码改成 139", current(chats(llm)[-1]))  # 不再告诉它“现在是：……139”

    async def test_editing_the_message_back_unblocks_her_channel_when_the_note_was_rejected(self):
        ds = DeepSeek()
        engine, llm, clock = await self.note_went_home_then_edit_back(f"@小D {RISKY}", ds)
        await say(engine, clock, 50, "@小D 在吗", channel=HALL, author=21, name="小红")
        self.assertEqual(ds.results, ["回了"] * 7)  # 她已经改回去了：照常回（没有一直被拒）
        self.assertNotIn(RISKY, prompt_of(chats(llm)[-1]))

    async def test_a_relink_does_not_write_the_older_edit_back_into_her_own_record(self):
        engine, llm, clock = await self.note_went_home_then_edit_back()
        connect(engine, HALL, CHANNEL)  # 别的服 #大厅还没说话就又连进来了
        own = own_record(engine, HALL, OTHER)
        self.assertIn("我的号码是 138", own)
        self.assertNotIn("我的号码改成 139", own)  # 不会把那一行改成更早的那次

    async def test_control_an_unlinked_channels_old_line_keeps_the_2_7_1_note(self):
        # 对照（control）：没连通的频道，很早的一行改了又改回原来那句：和 2.7.1 一样，那句“改成了什么”照旧附一次
        engine, llm, transport, clock = world()
        await say(engine, clock, 5, "@小D 我的号码是 138")
        for i in range(5):
            await say(engine, clock, 10 + i, f"@小D 第{i}句")
        engine.handle_edit(msg(5, "@小D 我的号码改成 139", mentions_bot=True), edited=True)
        await engine.drain()
        engine.handle_edit(msg(5, "@小D 我的号码是 138", mentions_bot=True), edited=True)
        await engine.drain()
        await say(engine, clock, 50, "@小D 在吗")
        self.assertIn("我的号码改成 139", current(chats(llm)[-1]))
        self.assertIn("我的号码是 138", record(engine, CHANNEL))


if __name__ == "__main__":
    unittest.main()
