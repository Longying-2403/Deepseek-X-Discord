"""2.6.0：私信记忆（每人一份，跟着人走；敏感的只在私信里用）和跨服务器跟着人走。"""

import json
import unittest
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, msg
from test_dm import DM, OTHER, BotCase, FakeGuild, FakeResponse, chats, dm, dm_engine
from test_memory import ACTIONS, REPLY, chat, is_review, stores, system_text, user_text

from dsbot.actions import parse_actions
from dsbot.memory import MemoryStore, parse_memory_op, parse_review
from dsbot.models import ChannelPlace
from dsbot.render import own_text, review_text

HALL, SECRET_ROOM = 700, 701  # 另一个服务器的公开频道、私密频道


def DM_MEM(**kw):
    return {"type": "dm_memory", **kw}


def setup(responder=None, **kw):
    """私信挂在 GUILD 的阿明（11），带着人设卡和记忆。另一个服务器 OTHER 有公开频道 #大厅 和私密频道 #秘密基地。"""
    cards, memory = stores()
    engine, llm, transport, clock = dm_engine(responder, cards=cards, memory=memory, **kw)
    transport.dm_users[DM] = 11
    transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=True)
    transport.places[SECRET_ROOM] = ChannelPlace(guild_id=OTHER, name="秘密基地", public=False)
    transport.guild_names[OTHER] = "别的服"
    engine.store.set_dm_home(11, GUILD, DM)
    return engine, llm, transport, clock, memory


def entries(memory, user=11):
    return [(e.text, e.sensitive) for e in memory.dm_entries(user)]


class Store(unittest.TestCase):
    def test_add_edit_delete_clear(self):
        m = MemoryStore(":memory:")
        self.assertEqual(m.dm_add(11, " 下周   考试 ", sensitive=False, via="chat", now=1), 1)
        self.assertEqual(m.dm_add(11, "住在台北", sensitive=True, via="chat", now=2), 2)
        self.assertEqual(m.dm_add(12, "别人的", sensitive=False, via="chat", now=2), 1)  # 每人自己编号
        self.assertEqual(entries(m), [("下周 考试", False), ("住在台北", True)])
        self.assertTrue(m.dm_edit(11, 1, None, sensitive=True, via="command", now=3))  # 只改敏感标记
        self.assertFalse(m.dm_edit(11, 1, None, sensitive=True, via="command", now=4))  # 没变
        self.assertTrue(m.dm_edit(11, 1, "下周三考英语", via="chat", now=5))  # 只改内容：敏感标记不变
        self.assertEqual(m.dm_get(11, 1).text, "下周三考英语")
        self.assertTrue(m.dm_get(11, 1).sensitive)
        self.assertFalse(m.dm_edit(11, 9, "没有这条", via="chat"))
        self.assertTrue(m.dm_delete(11, 2))
        self.assertEqual(m.dm_add(11, "新的一条", sensitive=False, via="chat"), 3)  # 删掉的编号不再用
        self.assertIsNone(m.dm_cleared_at(11))
        self.assertEqual(m.dm_clear(11, now=9), 2)
        self.assertEqual((entries(m), m.dm_cleared_at(11)), ([], 9))
        self.assertEqual(entries(m, 12), [("别人的", False)])  # 别人的不受影响


class Parse(unittest.TestCase):
    def test_ops_with_sensitive(self):
        self.assertTrue(parse_memory_op({"add": "x", "sensitive": True}).sensitive)
        self.assertFalse(parse_memory_op({"add": "x", "sensitive": "false"}).sensitive)
        self.assertTrue(parse_memory_op({"add": "x", "敏感": "是"}).sensitive)
        self.assertIsNone(parse_memory_op({"add": "x"}).sensitive)
        op = parse_memory_op({"edit": "(3)", "sensitive": True})  # 只改敏感标记
        self.assertEqual((op.op, op.no, op.text, op.sensitive), ("edit", 3, None, True))
        self.assertIsNone(parse_memory_op({"edit": 3}))

    def test_actions_and_review(self):
        actions, ok = parse_actions(
            ACTIONS(DM_MEM(add="下周考试", sensitive=False), DM_MEM(edit=2, sensitive=True), DM_MEM(delete=1), DM_MEM(add="多的"))
        )
        self.assertTrue(ok)
        self.assertEqual([(a.kind, a.op, a.index, a.sensitive) for a in actions], [
            ("dm_memory", "add", None, False), ("dm_memory", "edit", 2, True), ("dm_memory", "delete", 1, None)
        ])  # 一轮最多 3 条
        self.assertEqual([a.to_json() for a in actions], [
            DM_MEM(add="下周考试", sensitive=False), DM_MEM(edit=2, sensitive=True), DM_MEM(delete=1)
        ])
        cards, ops, dm_ops = parse_review('{"dm_memory": [{"add": "学吉他", "sensitive": false}, {"delete": 2}]}')
        self.assertEqual((cards, ops), ([], []))
        self.assertEqual([(o.op, o.text, o.no, o.sensitive) for o in dm_ops], [("add", "学吉他", None, False), ("delete", None, 2, None)])


class InDm(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_lists_everything_with_marks(self):
        engine, llm, transport, clock, memory = setup()
        memory.dm_add(11, "下周三考英语", sensitive=False, via="chat")
        memory.dm_add(11, "住在台北市大安区", sensitive=True, via="chat")
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        system = system_text(llm.calls[0])
        self.assertIn("## 私信记忆", system)
        self.assertIn("(1) 下周三考英语\n(2) 住在台北市大安区〔敏感〕", system)

    async def test_turned_off(self):
        engine, llm, transport, clock, memory = setup(defaults={"dm_memory": False})
        memory.dm_add(11, "下周三考英语", sensitive=False, via="chat")
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertNotIn("## 私信记忆", system_text(llm.calls[0]))

    async def test_remember_in_chat(self):
        out = ACTIONS(DM_MEM(add="答应帮他想乐队名字", sensitive=False), DM_MEM(add="他最近失眠"), REPLY(1, "记下了"))
        engine, llm, transport, clock, memory = setup(lambda call: out)
        await engine.handle_message(dm(1, "记住你答应帮我想乐队名字，还有我最近失眠"))
        await engine.drain()
        # 没写敏感不敏感的：当成敏感
        self.assertEqual(entries(memory), [("答应帮他想乐队名字", False), ("他最近失眠", True)])
        self.assertEqual([e.via for e in memory.dm_entries(11)], ["chat", "chat"])
        self.assertIn((DM, 1, "📝"), transport.reactions)
        self.assertEqual(transport.sent[-1][1], "记下了")

    async def test_limits(self):
        engine, llm, transport, clock, memory = setup(
            lambda call: ACTIONS(DM_MEM(add="太长" * 200), DM_MEM(add="已经有了"), REPLY(1, "好")), engine_cfg={"dm_memory_max_entries": 2}
        )
        memory.dm_add(11, "已经有了", sensitive=False, via="chat")
        await engine.handle_message(dm(1, "记住"))
        await engine.drain()
        self.assertEqual(entries(memory), [("已经有了", False)])  # 太长的、重复的都不记

    async def test_others_cards_are_not_copied_in(self):
        """私信记忆本人用 /ds-dm 记忆 看得到：查过的别人的人设卡，原文不能抄进来。"""
        card = "小雨的设定：她是一只会说话的狐狸，最喜欢吃油豆腐，害怕打雷，说话结尾喜欢加个喵字"
        outputs = [
            ACTIONS({"type": "who", "name": "小雨"}),
            ACTIONS(REPLY(1, "她是狐狸")),
            ACTIONS(DM_MEM(add=card, sensitive=False), DM_MEM(add="他想跟小雨交朋友", sensitive=False), REPLY(2, "记下了")),
        ]
        engine, llm, transport, clock, memory = setup(chat(outputs))
        engine.cards.put(GUILD, 12, "小雨", card, via="command")
        await engine.handle_message(dm(1, "小雨是什么设定"))
        await engine.drain()
        clock.advance(60)
        await engine.handle_message(dm(2, "把她的设定原样记住"))
        await engine.drain()
        self.assertEqual(entries(memory), [("他想跟小雨交朋友", False)])  # 照抄卡的那条没记，别的照记

    async def test_not_in_server_channels(self):
        engine, llm, transport, clock, memory = setup(lambda call: ACTIONS(DM_MEM(add="想偷偷记一下"), REPLY(1, "好")))
        await engine.handle_message(msg(1, "@小D 记住这个", mentions_bot=True))
        await engine.drain()
        self.assertEqual(entries(memory), [])

    async def test_review_writes_it(self):
        review = json.dumps({
            "cards": [], "memory": [],
            "dm_memory": [{"add": "最近在学吉他", "sensitive": False}, {"add": "下个月要做手术", "sensitive": True}],
        }, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "好"))], review=review))
        memory.dm_add(11, "早就记着的", sensitive=False, via="chat")
        for i in range(3):
            await engine.handle_message(dm(1 + i, f"第{i}句"))
            await engine.drain()
            clock.advance(30)
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        reviews = [c for c in llm.calls if is_review(c)]
        self.assertEqual(len(reviews), 1)
        self.assertIn("私信记忆（这个人自己的，跟着他走）", system_text(reviews[0]))
        self.assertIn("【他的私信记忆】\n(1) 早就记着的", user_text(reviews[0]))
        self.assertEqual(entries(memory), [("早就记着的", False), ("最近在学吉他", False), ("下个月要做手术", True)])
        self.assertEqual([e.via for e in memory.dm_entries(11)][1:], ["review", "review"])

    async def test_review_does_not_bring_back_what_was_cleared(self):
        review = json.dumps({"cards": [], "memory": [], "dm_memory": [{"add": "清掉之前聊的", "sensitive": False}]}, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "好"))], review=review))
        for i in range(3):
            await engine.handle_message(dm(1 + i, f"第{i}句"))
            await engine.drain()
            clock.advance(30)
        memory.dm_clear(11, now=clock())  # 整理之前他清空了
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        reviews = [c for c in llm.calls if is_review(c)]
        self.assertEqual(len(reviews), 1)  # 人设卡、长期记忆照样整理
        self.assertNotIn("【他的私信记忆】", user_text(reviews[0]))
        self.assertEqual(entries(memory), [])


class InChannels(unittest.IsolatedAsyncioTestCase):
    async def mention(self, engine, mid, text="@小D 在吗", **kw):
        await engine.handle_message(msg(mid, text, mentions_bot=True, **kw))
        await engine.drain()

    async def test_home_server_gets_the_non_sensitive_ones(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")
        await self.mention(engine, 1)
        text = user_text(chats(llm)[-1])
        self.assertIn("（阿明 私信里跟你聊过、记在私信记忆里的：\n  最近在学吉他）", text)
        self.assertNotIn("手术", text)  # 敏感的只在私信里用
        clock.advance(60)
        await self.mention(engine, 2)
        self.assertNotIn("私信记忆", user_text(chats(llm)[-1]))  # 附过了、没变：不再附
        memory.dm_add(11, "养了一只猫", sensitive=False, via="chat")
        clock.advance(60)
        await self.mention(engine, 3)
        self.assertIn("  最近在学吉他\n  养了一只猫）", user_text(chats(llm)[-1]))  # 变了：再附一次
        memory.dm_clear(11)
        clock.advance(60)
        await self.mention(engine, 4)
        self.assertIn("（阿明 的私信记忆清空了，之前附着的那些别再用。）", user_text(chats(llm)[-1]))
        clock.advance(60)
        await self.mention(engine, 5)
        self.assertNotIn("私信记忆", user_text(chats(llm)[-1]))  # 说过一次就够了
        # 压缩、整理这个频道时不算这个频道自己的内容：服务器的长期记忆、人设卡不会把它抄过去
        rounds = "\n".join(own_text(t.content) for t in engine.states[CHANNEL].conversation.turns if t.role == "user")
        self.assertNotIn("学吉他", rounds)
        self.assertNotIn("清空了", rounds)

    async def test_other_servers_need_cross_server(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        await self.mention(engine, 1, guild_id=OTHER, channel_id=HALL)
        self.assertNotIn("学吉他", user_text(chats(llm)[-1]))  # 私信挂在 GUILD；OTHER 没开跨服务器
        engine.settings.set("guild", OTHER, "cross_server", True)
        clock.advance(60)
        await self.mention(engine, 2, guild_id=OTHER, channel_id=HALL)
        self.assertIn("学吉他", user_text(chats(llm)[-1]))

    async def test_server_can_turn_it_off(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        engine.settings.set("guild", GUILD, "dm_memory", False)
        await self.mention(engine, 1)
        self.assertNotIn("学吉他", user_text(chats(llm)[-1]))

    def test_review_text_drops_it(self):
        content = (
            "【11-05 20:00】\n（阿明 私信里跟你聊过、记在私信记忆里的：\n  最近在学吉他\n  养了一只猫）\n"
            "（小雨 的私信记忆清空了，之前附着的那些别再用。）\n#1 阿明：@小D 在吗 〔@你〕"
        )
        self.assertEqual(review_text(content), ["【11-05 20:00】", "#1 阿明：@小D 在吗 〔@你〕"])


class CrossServer(unittest.IsolatedAsyncioTestCase):
    async def talk(self, engine, clock, mid, text, **kw):
        await engine.handle_message(msg(mid, text, mentions_bot=True, **kw))
        await engine.drain()
        clock.advance(60)

    async def test_follows_only_when_both_servers_allow(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "加油"))]))
        await self.talk(engine, clock, 1, "@小D 我在准备期末考试", guild_id=OTHER, channel_id=HALL)
        await self.talk(engine, clock, 2, "@小D 在吗")
        self.assertNotIn("期末考试", user_text(chats(llm)[-1]))  # 两边都没开
        engine.settings.set("guild", GUILD, "cross_server", True)
        await self.talk(engine, clock, 3, "@小D 在吗")
        self.assertNotIn("期末考试", user_text(chats(llm)[-1]))  # 那边没开：不带出来
        engine.settings.set("guild", OTHER, "cross_server", True)
        await self.talk(engine, clock, 4, "@小D 在吗")
        text = user_text(chats(llm)[-1])
        self.assertIn("在「别的服」服务器的 #大厅 跟你聊过：", text)
        self.assertIn("阿明：@小D 我在准备期末考试", text)
        self.assertIn("你：加油", text)
        await self.talk(engine, clock, 5, "@小D 还在吗")
        self.assertNotIn("期末考试", user_text(chats(llm)[-1]))  # 带过一次就不再带
        rounds = "\n".join(own_text(t.content) for t in engine.states[CHANNEL].conversation.turns if t.role == "user")
        self.assertNotIn("期末考试", rounds)  # 不算这个频道自己的内容

    async def test_private_channels_and_secrets_stay_there(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "好"))]))
        for guild in (GUILD, OTHER):
            engine.settings.set("guild", guild, "cross_server", True)
        await self.talk(engine, clock, 1, "@小D 我们的暗号是芝麻开门", guild_id=OTHER, channel_id=SECRET_ROOM)
        await self.talk(engine, clock, 2, "@小D 这件事你要保密：我要转学了", guild_id=OTHER, channel_id=HALL)
        await self.talk(engine, clock, 3, "@小D 在吗")
        text = user_text(chats(llm)[-1])
        self.assertNotIn("芝麻开门", text)
        self.assertNotIn("转学", text)

    async def test_dm_gets_other_servers(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "加油"))]))
        engine.settings.set("guild", OTHER, "cross_server", True)
        await self.talk(engine, clock, 1, "@小D 我在准备期末考试", guild_id=OTHER, channel_id=HALL)
        await engine.handle_message(dm(2, "在吗"))
        await engine.drain()
        # 私信挂的服务器没开：不带（私信里聊的会记进那个服务器的人设卡、长期记忆）
        self.assertNotIn("期末考试", user_text(chats(llm)[-1]))
        engine.settings.set("guild", GUILD, "cross_server", True)
        clock.advance(30)
        await engine.handle_message(dm(3, "还在吗"))
        await engine.drain()
        self.assertIn("在「别的服」服务器的 #大厅 跟你聊过：", user_text(chats(llm)[-1]))

    async def test_dm_lines_stay_with_the_home_server(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "加油"))]))
        for guild in (GUILD, OTHER):
            engine.settings.set("guild", guild, "cross_server", True)
        await engine.handle_message(dm(1, "我在准备期末考试"))
        await engine.drain()
        clock.advance(60)
        await self.talk(engine, clock, 2, "@小D 在吗", guild_id=OTHER, channel_id=HALL)
        self.assertNotIn("期末考试", user_text(chats(llm)[-1]))  # 私信的原话只带到私信挂着的那个服务器
        await self.talk(engine, clock, 3, "@小D 在吗")
        self.assertIn("期末考试", user_text(chats(llm)[-1]))


class Tuning(unittest.IsolatedAsyncioTestCase):
    """2.6.0 试用后的调整：上限 150 条、自己管；人设卡优先；列出来的有上限；敏感标记好改。"""

    def test_defaults(self):
        from dsbot.config import EngineConfig

        cfg = EngineConfig()
        self.assertEqual((cfg.dm_memory_max_entries, cfg.dm_memory_attach_entries), (150, 20))

    async def test_dm_prompt_lists_the_newest_within_the_cap(self):
        engine, llm, transport, clock, memory = setup(engine_cfg={"dm_memory_prompt_chars": 25})
        for i, text in enumerate(["最早的一条，十个字", "第二条，也是十个字", "第三条，十个字的事", "最新的一条，十个字"]):
            memory.dm_add(11, text, sensitive=False, via="chat", now=100 + i)
        memory.dm_edit(11, 1, None, sensitive=True, via="command", now=200)  # 刚改过：算最近的
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        system = system_text(llm.calls[0])
        self.assertIn("现在 4 条，最多 150 条", system)
        self.assertIn("比较早的 2 条没列在这里", system)
        self.assertIn("(1) 最早的一条，十个字〔敏感〕\n(4) 最新的一条，十个字\n", system)  # 按编号排
        self.assertNotIn("第二条", system)

    async def test_channels_get_only_the_newest_few(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]), engine_cfg={"dm_memory_attach_entries": 2})
        for i in range(4):
            memory.dm_add(11, f"第{i + 1}件事", sensitive=False, via="chat", now=100 + i)
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        text = user_text(chats(llm)[-1])
        self.assertIn("  第3件事\n  第4件事）", text)
        self.assertNotIn("第1件事", text)

    async def test_card_comes_first(self):
        engine, llm, transport, clock, memory = setup()
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        system = system_text(llm.calls[0])
        self.assertIn("先用 card 写进他的人设卡；写不进人设卡的才记进私信记忆", system)
        self.assertIn("照他说的用 edit 改 sensitive", system)
        engine, llm, transport, clock, memory = setup(defaults={"cards": False})
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertNotIn("先用 card", system_text(llm.calls[0]))  # 没开人设卡：都记进私信记忆

    async def test_review_puts_the_card_first(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "好"))]))
        for i in range(3):
            await engine.handle_message(dm(1 + i, f"第{i}句：我是猫娘"))
            await engine.drain()
            clock.advance(30)
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        review = next(c for c in llm.calls if is_review(c))
        self.assertIn("先写进他的人设卡；写不进人设卡的才记进私信记忆", system_text(review))

    def test_numbers_can_be_picked(self):
        from dsbot.commands import dm_memory_choices, dm_memory_text

        bot = NS(memory=MemoryStore(":memory:"), cfg=NS(engine=NS(dm_memory_max_entries=150)))
        bot.memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        bot.memory.dm_add(11, "下个月要动手术", sensitive=True, via="chat")
        bot.memory.dm_add(11, "长" * 150, sensitive=False, via="chat")
        bot.memory.dm_add(12, "别人的吉他", sensitive=False, via="chat")
        choices = dm_memory_choices(bot, 11, "")
        self.assertEqual([c.value for c in choices], [1, 2, 3])
        self.assertEqual(choices[1].name, "(2) 下个月要动手术〔敏感〕")
        self.assertLessEqual(len(choices[2].name), 100)
        self.assertEqual([c.value for c in dm_memory_choices(bot, 11, "吉他")], [1])  # 只有自己的
        self.assertIn("判错了就改", dm_memory_text(bot, 11))


def all_text(call):
    out = []
    for m in call["messages"]:
        c = m["content"]
        out.append(c if isinstance(c, str) else "".join(p.get("text", "") for p in c))
    return "\n".join(out)


def history(engine, channel=CHANNEL):
    """这个频道记录里所有的内容（内存里的和库里的）。"""
    st = engine.states[channel]
    mem = "\n".join(json.dumps(t.content, ensure_ascii=False) for t in st.conversation.turns)
    db = "\n".join(json.dumps(engine.store.turn_content(t.id), ensure_ascii=False) for t in st.conversation.turns)
    return mem + "\n" + db


class ReviewFixes(unittest.IsolatedAsyncioTestCase):
    """复查找到的问题（每条都先复现过）。"""

    async def dm_rounds(self, engine, clock, start=1, n=3):
        for i in range(n):
            await engine.handle_message(dm(start + i, f"第{i}句"))
            await engine.drain()
            clock.advance(30)

    async def review(self, engine, clock):
        clock.advance(600)
        await engine.tick()
        await engine.drain()

    async def test_review_cannot_lower_the_sensitive_mark(self):
        review = json.dumps({"cards": [], "memory": [], "dm_memory": [
            {"edit": 1, "text": "最近和女朋友吵架了，想分手", "sensitive": False}, {"edit": 2, "sensitive": False},
        ]}, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "好"))], review=review))
        memory.dm_add(11, "最近和女朋友吵架了", sensitive=False, via="review")
        memory.dm_add(11, "住在台北", sensitive=True, via="review")
        await self.dm_rounds(engine, clock)
        memory.dm_edit(11, 1, None, sensitive=True, via="command")  # 本人用 /ds-dm 记忆 标成敏感
        await self.review(engine, clock)
        self.assertEqual(entries(memory), [("最近和女朋友吵架了，想分手", True), ("住在台北", True)])

    async def test_text_edits_never_lower_it_and_default_to_sensitive(self):
        outputs = [
            ACTIONS(DM_MEM(edit=1, text="学吉他，因为抑郁症想转移注意力"), REPLY(1, "改好了")),
            ACTIONS(DM_MEM(edit=2, text="还住在台北", sensitive=False), REPLY(2, "改好了")),
            ACTIONS(DM_MEM(edit=2, sensitive=False), REPLY(3, "好，第 2 条不算敏感了")),
        ]
        engine, llm, transport, clock, memory = setup(chat(outputs))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_add(11, "住在台北", sensitive=True, via="chat")
        await engine.handle_message(dm(1, "第1条改一下"))
        await engine.drain()
        clock.advance(30)
        await engine.handle_message(dm(2, "第2条改一下"))
        await engine.drain()
        self.assertEqual(entries(memory), [("学吉他，因为抑郁症想转移注意力", True), ("还住在台北", True)])
        clock.advance(30)
        await engine.handle_message(dm(3, "第2条不算敏感"))  # 他自己说的、只改标记：可以往下调
        await engine.drain()
        self.assertEqual(entries(memory)[1], ("还住在台北", False))

    async def test_new_entries_about_a_sensitive_thing_stay_sensitive(self):
        engine, llm, transport, clock, memory = setup(lambda call: ACTIONS(DM_MEM(add="最近和女朋友吵架了，有点烦", sensitive=False), REPLY(1, "嗯")))
        memory.dm_add(11, "和女朋友吵架了", sensitive=True, via="chat")
        await engine.handle_message(dm(1, "记一下"))
        await engine.drain()
        self.assertEqual(entries(memory)[1], ("最近和女朋友吵架了，有点烦", True))

    async def test_sensitive_things_stay_out_of_cards_and_long_term_memory(self):
        review = json.dumps({
            "cards": [{"to": 1, "text": "阿明，下个月要做手术"}],
            "memory": [{"add": "阿明下个月要做手术"}, {"add": "周五晚上打副本"}],
            "dm_memory": [],
        }, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "好"))], review=review))
        memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")
        await self.dm_rounds(engine, clock)
        await self.review(engine, clock)
        self.assertIsNone(engine.cards.get(GUILD, 11))
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["周五晚上打副本"])  # 不敏感的照记
        reviews = [c for c in llm.calls if is_review(c)]
        self.assertIn("就算他明确要求记住也不行", system_text(reviews[0]))

    async def test_in_chat_card_and_memory_with_sensitive_things_are_refused(self):
        out = ACTIONS(
            {"type": "card", "to": 1, "text": "阿明，下个月要做手术"},
            {"type": "memory", "add": "阿明住在台北市大安区", "sensitive": True},
            {"type": "memory", "add": "阿明下个月要做手术"},
            REPLY(1, "记下了"),
        )
        engine, llm, transport, clock, memory = setup(lambda call: out)
        memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")
        await engine.handle_message(dm(1, "记住"))
        await engine.drain()
        self.assertIsNone(engine.cards.get(GUILD, 11))
        self.assertEqual(memory.entries(GUILD), [])
        self.assertIn(("阿明住在台北市大安区", True), entries(memory))  # 标着敏感的长期记忆改记进私信记忆

    async def test_marking_sensitive_withdraws_what_channels_already_saw(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_add(11, "和女朋友分手了", sensitive=False, via="review")  # 判错了
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        self.assertIn("分手", history(engine))
        bot = NS(memory=memory, engine=engine, cfg=NS(engine=NS(dm_memory_max_entries=150)))
        from dsbot.commands import settings_changed, withdraw_dm_memory

        # 和 /ds-dm 记忆 设为敏感 一样：改标记，再撤回
        memory.dm_edit(11, 2, None, sensitive=True, via="command")
        withdraw_dm_memory(bot, 11)
        self.assertNotIn("分手", history(engine))
        self.assertNotIn("学吉他", history(engine))  # 整段拿掉：下次叫它时照现在的重新附
        clock.advance(60)
        await engine.handle_message(msg(2, "@小D 你好", author=12, name="小雨", mentions_bot=True))
        await engine.drain()
        self.assertNotIn("分手", all_text(chats(llm)[-1]))  # 别人那一轮也看不到了
        clock.advance(60)
        await engine.handle_message(msg(3, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        text = all_text(chats(llm)[-1])
        self.assertIn("学吉他", text)
        self.assertNotIn("分手", text)
        # 服务器关掉私信记忆（/ds 设置）：附过的也撤回
        engine.settings.set("guild", GUILD, "dm_memory", False)
        settings_changed(bot, "dm_memory", "guild", GUILD)
        self.assertNotIn("学吉他", history(engine))

    async def test_chat_and_review_changes_withdraw_too(self):
        outputs = [ACTIONS(REPLY(1, "在")), ACTIONS(DM_MEM(delete=1), REPLY(2, "删了"))]
        engine, llm, transport, clock, memory = setup(chat(outputs))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        self.assertIn("学吉他", history(engine))
        clock.advance(60)
        await engine.handle_message(dm(2, "把学吉他那条删了"))
        await engine.drain()
        self.assertEqual(entries(memory), [])
        self.assertNotIn("学吉他", history(engine))

    async def test_changed_during_the_round_is_not_left_in_the_record(self):
        engine, llm, transport, clock, memory = setup()
        memory.dm_add(11, "和女朋友分手了", sensitive=False, via="review")

        def responder(call):  # 这一轮问模型的时候，他用命令把它标成了敏感
            memory.dm_edit(11, 1, None, sensitive=True, via="command")
            engine.withdraw_dm_memory(11)
            return ACTIONS(REPLY(1, "在"))

        engine.llm.responder = responder
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        self.assertNotIn("分手", history(engine))

    async def test_dm_replies_and_sensitive_lines_stay_in_the_dm(self):
        outputs = [ACTIONS(REPLY(1, "在！手术准备得怎么样了？")), ACTIONS(REPLY(2, "加油")), ACTIONS(REPLY(3, "在"))]
        engine, llm, transport, clock, memory = setup(chat(outputs))
        memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        clock.advance(30)
        await engine.handle_message(dm(2, "我下个月要做手术，有点怕"))
        await engine.drain()
        clock.advance(60)
        await engine.handle_message(msg(3, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        text = user_text(chats(llm)[-1])
        self.assertNotIn("手术", text)  # 它的回话、说到敏感的事的那句都不带
        self.assertIn("（阿明 刚才在私信里跟你聊过：\n  阿明：在吗）", text)  # 别的照带，只带他自己的话

    async def test_cross_server_talk_does_not_reach_the_home_server_through_the_dm(self):
        outputs = [ACTIONS(REPLY(1, "加油")), ACTIONS(REPLY(1, "期末考试复习得怎么样了？")), ACTIONS(REPLY(1, "在"))]
        engine, llm, transport, clock, memory = setup(chat(outputs))
        engine.settings.set("guild", OTHER, "cross_server", True)  # 只有 OTHER 开了
        await engine.handle_message(msg(1, "@小D 我在准备期末考试", mentions_bot=True, guild_id=OTHER, channel_id=HALL))
        await engine.drain()
        clock.advance(60)
        await engine.handle_message(dm(2, "在吗"))
        await engine.drain()
        self.assertNotIn("期末考试", user_text(chats(llm)[-1]))  # 私信挂的服务器没开：私信里也不带
        clock.advance(60)
        await engine.handle_message(msg(3, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        self.assertNotIn("期末", user_text(chats(llm)[-1]))

    async def test_clearing_during_a_round_wins(self):
        engine, llm, transport, clock, memory = setup()

        def responder(call):  # 问模型的时候他清空了私信记忆
            memory.dm_clear(11, now=clock())
            return ACTIONS(DM_MEM(add="清空之前说的事", sensitive=False), REPLY(1, "好"))

        engine.llm.responder = responder
        await engine.handle_message(dm(1, "记住这个"))
        await engine.drain()
        self.assertEqual(entries(memory), [])

    async def test_reasoning_is_not_logged_in_dms(self):
        engine, llm, transport, clock, memory = setup()
        memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")
        with self.assertLogs("dsbot.engine", "INFO") as logs:
            await engine.handle_message(dm(1, "在吗"))
            await engine.drain()
        self.assertIn("想法略", "\n".join(logs.output))


class SecondReview(unittest.IsolatedAsyncioTestCase):
    """第二次复查找到的问题。"""

    async def dm_rounds(self, engine, clock, start=1, n=3):
        for i in range(n):
            await engine.handle_message(dm(start + i, f"第{i}句"))
            await engine.drain()
            clock.advance(30)

    async def review(self, engine, clock):
        clock.advance(600)
        await engine.tick()
        await engine.drain()

    async def test_delete_first_cannot_hide_a_sensitive_merge(self):
        review = json.dumps({"cards": [], "memory": [], "dm_memory": [
            {"delete": 2}, {"delete": 1}, {"add": "最近在学吉他，刚和女朋友分手", "sensitive": False},
        ]}, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "好"))], review=review))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_add(11, "和女朋友分手了", sensitive=True, via="command")
        await self.dm_rounds(engine, clock)
        await self.review(engine, clock)
        self.assertEqual(entries(memory), [("最近在学吉他，刚和女朋友分手", True)])  # 并进了敏感的那件事：照样敏感

    async def test_same_in_chat(self):
        out = ACTIONS(DM_MEM(delete=1), DM_MEM(add="和女朋友分手了，最近在学吉他散心", sensitive=False), REPLY(1, "好"))
        engine, llm, transport, clock, memory = setup(lambda call: out)
        memory.dm_add(11, "和女朋友分手了", sensitive=True, via="command")
        await engine.handle_message(dm(1, "合并一下"))
        await engine.drain()
        self.assertEqual(entries(memory), [("和女朋友分手了，最近在学吉他散心", True)])

    async def test_saying_something_sensitive_stops_dm_lines_from_following(self):
        outputs = [
            ACTIONS(REPLY(1, "在")),
            ACTIONS(DM_MEM(add="他下个月要做手术", sensitive=True), REPLY(2, "记下了，只在私信里用")),
            ACTIONS(REPLY(3, "在")),
        ]
        engine, llm, transport, clock, memory = setup(chat(outputs))
        await engine.handle_message(dm(1, "最近好累，医院跑了好几趟"))
        await engine.drain()
        clock.advance(30)
        await engine.handle_message(dm(2, "下个月我要去做个手术，有点怕，记一下"))  # 换了说法：认不出原话
        await engine.drain()
        clock.advance(60)
        await engine.handle_message(msg(3, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        text = user_text(chats(llm)[-1])
        self.assertNotIn("手术", text)
        self.assertNotIn("医院", text)  # 之前那段私信也不带了

    async def test_dm_review_screens_even_after_a_clear_or_with_dm_memory_off(self):
        review = json.dumps({
            "cards": [{"to": 1, "text": "阿明，下个月要做手术"}], "memory": [{"add": "阿明下个月要做手术"}], "dm_memory": [],
        }, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "好"))], review=review), defaults={"dm_memory": False})
        memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")  # 关掉之前记的
        await self.dm_rounds(engine, clock)
        await self.review(engine, clock)
        self.assertIsNone(engine.cards.get(GUILD, 11))
        self.assertEqual(memory.entries(GUILD), [])
        review_call = next(c for c in llm.calls if is_review(c))
        self.assertIn("就算他明确要求记住也不行", system_text(review_call))  # 私信里一律从严

    async def test_merging_public_text_into_a_sensitive_entry_withdraws_it(self):
        review = json.dumps({"cards": [], "memory": [], "dm_memory": [
            {"delete": 1}, {"add": "因为抑郁症在学吉他", "sensitive": True},
        ]}, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))], review=review))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        self.assertIn("学吉他", history(engine))
        clock.advance(60)
        await self.dm_rounds(engine, clock, start=2)
        await self.review(engine, clock)
        self.assertEqual(entries(memory), [("因为抑郁症在学吉他", True)])
        self.assertNotIn("学吉他", history(engine))

    async def test_their_own_edit_withdraws_the_old_wording(self):
        outputs = [ACTIONS(REPLY(1, "在")), ACTIONS(DM_MEM(edit=1, text="和女朋友分手了", sensitive=False), REPLY(2, "改好了"))]
        engine, llm, transport, clock, memory = setup(chat(outputs))
        memory.dm_add(11, "和女朋友小美分手了", sensitive=False, via="chat")
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        self.assertIn("小美", history(engine))
        clock.advance(60)
        await engine.handle_message(dm(2, "把名字去掉"))
        await engine.drain()
        self.assertNotIn("小美", history(engine))

    async def test_lowering_needs_a_round_with_only_their_words(self):
        from helpers import FakeSearcher

        outputs = [
            ACTIONS({"type": "search", "query": "天气"}),
            ACTIONS(DM_MEM(edit=1, sensitive=False), REPLY(1, "好")),  # 搜索结果里的话让它这么做的
        ]
        engine, llm, transport, clock, memory = setup(chat(outputs), searcher=FakeSearcher())
        memory.dm_add(11, "下个月要做手术", sensitive=True, via="chat")
        await engine.handle_message(dm(1, "查一下天气"))
        await engine.drain()
        self.assertEqual(entries(memory), [("下个月要做手术", True)])

    async def test_reasoning_stays_hidden_after_a_restart(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))
        await engine.drain()
        engine.states[CHANNEL].dm_memory_shown.clear()  # 重启之后：内存里的记号没了，记录里还附着
        clock.advance(60)
        with self.assertLogs("dsbot.engine", "INFO") as logs:
            await engine.handle_message(msg(2, "@小D 你好", author=12, name="小雨", mentions_bot=True))
            await engine.drain()
        self.assertIn("想法略", "\n".join(logs.output))
        self.assertNotIn("想法：", engine.states[CHANNEL].last_decision)

    async def test_same_name_is_reattached_after_a_withdrawal(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        engine.store.set_dm_home(12, GUILD, DM + 1)
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        memory.dm_add(12, "在准备搬家", sensitive=False, via="chat")
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))
        await engine.handle_message(msg(2, "@小D 在吗", author=12, mentions_bot=True))  # 也叫阿明
        await engine.drain()
        self.assertIn("搬家", history(engine))
        engine.withdraw_dm_memory(11)
        clock.advance(60)
        await engine.handle_message(msg(3, "@小D 还在吗", author=12, mentions_bot=True))
        await engine.drain()
        self.assertIn("搬家", user_text(chats(llm)[-1]))  # 一起被拿掉了：重新附上

    def test_withdrawal_leaves_file_contents_alone(self):
        from dsbot.render import drop_dm_memory

        content = (
            "【11-05 20:00】\n（阿明 私信里跟你聊过、记在私信记忆里的：\n  最近在学吉他）\n#1 小雨：看这个 [文件: a.txt]\n"
            "【#1 的文件「a.txt」· 文本】\n<<<\n（阿明 私信里跟你聊过、记在私信记忆里的：\n  文件里写的\n>>>"
        )
        self.assertEqual(
            drop_dm_memory(content, "阿明"),
            "【11-05 20:00】\n#1 小雨：看这个 [文件: a.txt]\n【#1 的文件「a.txt」· 文本】\n<<<\n（阿明 私信里跟你聊过、记在私信记忆里的：\n  文件里写的\n>>>",
        )


class Notice(BotCase):
    def test_existing_dm_users_hear_about_it_once(self):
        from dsbot.discord_app import DM_MEMORY_NOTE

        bot = self.make([FakeGuild(GUILD, "A服", {11})])
        bot.memory = MemoryStore(":memory:")
        here = NS(id=GUILD)
        notice = bot._memory_notice(11, here, None)
        self.assertTrue(notice.startswith("📒 新功能："))
        self.assertIn("/ds-dm 记忆", notice)
        self.assertIsNone(bot._memory_notice(11, here, None))  # 只说一次
        self.assertEqual(bot._memory_notice(12, here, "第一次私信的说明" + DM_MEMORY_NOTE), "第一次私信的说明" + DM_MEMORY_NOTE)
        self.assertIsNone(bot._memory_notice(12, here, None))  # 第一次私信的说明里说过了
        bot.settings.set("guild", GUILD, "dm_memory", False)
        self.assertIsNone(bot._memory_notice(13, here, None))  # 没开私信记忆：先不说
        bot.settings.set("guild", GUILD, "dm_memory", True)
        self.assertIsNotNone(bot._memory_notice(13, here, None))


class Commands(BotCase):
    def commands(self, bot):
        from dsbot.commands import register_dm_commands
        from dsbot.ledger import Ledger

        added = []
        bot.tree = NS(add_command=added.append)
        bot.ledger = Ledger(bot.store, "Asia/Taipei")
        bot.secrets = NS(owner_id=0)
        bot.memory = MemoryStore(":memory:")
        bot.cfg = NS(engine=NS(dm_memory_max_entries=30))
        register_dm_commands(bot)
        return added[0].get_command("记忆").callback

    def interaction(self):
        self.sent = []

        async def followup(text, ephemeral=False):
            self.sent.append((text, ephemeral))

        return NS(user=NS(id=11), guild_id=None, channel_id=DM, response=FakeResponse(self.sent), followup=NS(send=followup))

    async def test_view_delete_mark_clear(self):
        bot = self.make([FakeGuild(GUILD, "A服", {11})])
        command = self.commands(bot)
        await command(self.interaction())
        self.assertIn("还没有", self.sent[-1][0])
        bot.memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        bot.memory.dm_add(11, "住在台北", sensitive=True, via="chat")
        bot.memory.dm_add(12, "别人的", sensitive=False, via="chat")
        await command(self.interaction())
        text, ephemeral = self.sent[-1]
        self.assertTrue(ephemeral)  # 只有自己看得到
        self.assertIn("（2 / 30 条）\n(1) 最近在学吉他\n(2) 住在台北〔敏感〕", text)
        self.assertNotIn("别人的", text)
        await command(self.interaction(), NS(value="private"), 1)
        self.assertTrue(bot.memory.dm_get(11, 1).sensitive)
        await command(self.interaction(), NS(value="public"), 2)
        self.assertFalse(bot.memory.dm_get(11, 2).sensitive)
        await command(self.interaction(), NS(value="delete"))
        self.assertIn("填上编号", self.sent[-1][0])
        await command(self.interaction(), NS(value="delete"), 9)
        self.assertIn("没有第 9 条", self.sent[-1][0])
        await command(self.interaction(), NS(value="delete"), 2)
        self.assertEqual(entries(bot.memory), [("最近在学吉他", True)])
        await command(self.interaction(), NS(value="clear"))
        self.assertIn("已清空你的私信记忆（1 条）", self.sent[-1][0])
        self.assertEqual((entries(bot.memory), entries(bot.memory, 12)), ([], [("别人的", False)]))


if __name__ == "__main__":
    unittest.main()
