"""2.1.0：它话里的 @名字、#频道名 变成真的 @ 和频道链接；被 @ 的人什么时候真的收到提醒。另有表情回应的提示词。"""

import json
import unittest

from helpers import CHANNEL, GUILD, make_engine, msg

from dsbot.memory import CardStore, MemoryStore
from dsbot.mentions import link, merge_tiers
from dsbot.prompts import SYSTEM_RULES

ACTIONS = lambda *items: json.dumps({"actions": list(items)}, ensure_ascii=False)  # noqa: E731
REPLY = lambda to, *texts: {"type": "reply", "to": to, "messages": list(texts)}  # noqa: E731


def people(**names):
    return {name: frozenset(ids) for name, ids in names.items()}


class Link(unittest.TestCase):
    def test_names(self):
        p = merge_tiers([{"阿明": {11}, "阿明的猫": {13}, "Tom": {21}, "everyone": {9}, "小雨": {12, 14}, "大": {15}}])
        cases = [
            ("@阿明 你好", "<@11> 你好"),
            ("＠阿明，快来", "<@11>，快来"),
            ("@阿明你好", "@阿明你好"),  # 名字后面要隔开：说的可能是别人
            ("@大家 开会了", "@大家 开会了"),  # 有人叫“大”也不会把“@大家”当成 @ 他
            ("@阿明的猫 好可爱", "<@13> 好可爱"),  # 先对最长的
            ("@Tom, @tom 和 @Tommy", "<@21>, <@21> 和 @Tommy"),
            ("发到 a@Tom.com", "发到 a@Tom.com"),  # 邮箱
            ("看 https://www.youtube.com/@Tom 和 www.tiktok.com/@Tom", "看 https://www.youtube.com/@Tom 和 www.tiktok.com/@Tom"),  # 网址里的不动
            ("去 youtube.com/@Tom 看看，threads.net/@阿明 也有", "去 youtube.com/@Tom 看看，threads.net/@阿明 也有"),  # 不带 https:// 的也一样
            ("路径 a/@Tom 和 x/#游戏", "路径 a/@Tom 和 x/#游戏"),
            ("`@Tom` 和 ```\n@Tom\n``` 外面 @Tom", "`@Tom` 和 ```\n@Tom\n``` 外面 <@21>"),  # 代码里的不动
            ("@everyone 开会", "@everyone 开会"),
            ("@小雨 在吗", "@小雨 在吗"),  # 两个人都叫小雨：分不清，不换
        ]
        for text, want in cases:
            self.assertEqual(link(text, p, {})[0], want, text)
        self.assertEqual(link("@阿明 @Tom @阿明", p, {})[1], [11, 21, 11])
        self.assertEqual(link("@阿杰 上线 @everyone", p, {})[2], ["阿杰 上线 @everyone"])  # 对不上的交给按名字找人
        self.assertEqual(link("@小雨的猫呢", merge_tiers([{"小雨": {12}}]), {})[2], ["小雨的猫呢"])

    def test_channels(self):
        ch = people(游戏={300}, general={301})
        self.assertEqual(link("去 #游戏 说，或者 ＃General", {}, ch)[0], "去 <#300> 说，或者 <#301>")
        self.assertEqual(link("#游戏区 那边", {}, ch)[0], "#游戏区 那边")  # 后面没隔开：是别的频道
        self.assertEqual(link("#12 说得对，C#游戏", {}, {**ch, "12": frozenset({5})})[0], "#12 说得对，C#游戏")

    def test_earlier_tiers_win(self):
        merged = merge_tiers([{"阿明": {11}}, {"阿明": {13}, "老王": {15}}])
        self.assertEqual(merged, {"阿明": frozenset({11}), "老王": frozenset({15})})


async def chat(engine, clock, mid, text, author=11, name="阿明", **kw):
    await engine.handle_message(msg(mid, text, author=author, name=name, **kw))
    await engine.drain()
    clock.advance(30)


class InChat(unittest.IsolatedAsyncioTestCase):
    async def test_people_who_spoke_get_pinged_when_it_was_called(self):
        outputs = iter([ACTIONS(), ACTIONS(REPLY(2, "@小雨 副本要开了，快上线")), ACTIONS(REPLY(3, "@小雨 再叫一次"))])
        engine, llm, transport, clock = make_engine(lambda call: next(outputs))
        await chat(engine, clock, 500, "@小D 我先下了", author=12, name="小雨", mentions_bot=True)
        await chat(engine, clock, 501, "@小D 帮我叫一下小雨", mentions_bot=True)
        self.assertEqual(transport.sent[-1][1], "<@12> 副本要开了，快上线")
        self.assertEqual(transport.mentions[-1], (12,))
        await chat(engine, clock, 502, "@小D 再叫她一次", mentions_bot=True)
        self.assertEqual(transport.sent[-1][1], "<@12> 再叫一次")  # 5 分钟内：照样显示成 @，但不再提醒
        self.assertEqual(transport.mentions[-1], ())
        self.assertIn("@小雨 副本要开了", json.dumps(engine.states[CHANNEL].conversation.turns[3].content, ensure_ascii=False))

    async def test_no_pings_when_nobody_called_it(self):
        engine, llm, transport, clock = make_engine(lambda call: ACTIONS(REPLY(None, "@小雨 早")), defaults={"proactive": True})
        await engine.handle_message(msg(500, "早", author=12, name="小雨"))
        clock.advance(200)
        await engine.tick()
        await engine.drain()
        self.assertEqual(transport.sent[-1][1], "<@12> 早")
        self.assertEqual(transport.mentions[-1], ())

    async def test_at_most_three_pings_per_round(self):
        out = ACTIONS(REPLY(5, "@甲 @乙 集合", "@丙 @丁 也来"))
        engine, llm, transport, clock = make_engine(lambda call: out if len(llm.calls) == 5 else ACTIONS())
        for i, name in enumerate("甲乙丙丁"):
            await chat(engine, clock, 500 + i, "@小D 我来了", author=30 + i, name=name, mentions_bot=True)
        await chat(engine, clock, 504, "@小D 叫大家集合", mentions_bot=True)
        self.assertEqual([s[1] for s in transport.sent[-2:]], ["<@30> <@31> 集合", "<@32> <@33> 也来"])
        self.assertEqual(transport.mentions[-2:], [(30, 31), (32,)])  # 分成两条发也只提醒 3 个人

    async def test_server_wide_ping_limit(self):
        engine, llm, transport, clock = make_engine()
        st = engine.state(CHANNEL, GUILD)
        names = {f"人{i}": frozenset({100 + i}) for i in range(12)}
        pinged = []
        for i in range(12):
            _, pings = engine._link_mentions(st, f"@人{i} 来", names, {}, "mention", {"pings_left": 3})
            pinged += pings
        self.assertEqual(len(pinged), 10)  # 10 分钟内整个服务器最多提醒 10 次
        clock.advance(61)
        engine._housekeep(clock())  # 定时清理不能把还在 10 分钟窗口里的记录清掉
        _, pings = engine._link_mentions(st, "@人11 来", names, {}, "mention", {"pings_left": 3})
        self.assertEqual(pings, [])
        clock.advance(600)
        _, pings = engine._link_mentions(st, "@人11 来", names, {}, "mention", {"pings_left": 3})
        self.assertEqual(pings, [111])

    async def test_mentioned_users_cards_and_member_search(self):
        cards, memory = CardStore(":memory:"), MemoryStore(":memory:")
        cards.put(GUILD, 15, "老王", "喜欢钓鱼", via="command")
        out = ACTIONS(REPLY(1, "@阿杰 @老王 @小绿 @路人 开黑了"))
        engine, llm, transport, clock = make_engine(lambda call: out if call["json_mode"] else "摘要", cards=cards, memory=memory)
        transport.members = {"小绿": {77}}
        await chat(engine, clock, 501, "@小D 叫一下 @阿杰 他们", mentions_bot=True, mentioned=[(55, "阿杰")])
        self.assertEqual(transport.sent[-1][1], "<@55> <@15> <@77> @路人 开黑了")
        self.assertEqual(transport.member_searches, [["小绿 @路人 开黑了", "路人 开黑了"]])

    async def test_channel_links_and_same_names(self):
        out = ACTIONS(REPLY(3, "@阿明 去 #游戏 说"))
        engine, llm, transport, clock = make_engine(lambda call: out if len(llm.calls) == 3 else ACTIONS())
        transport.channel_map = {"游戏": {300}}
        await chat(engine, clock, 501, "@小D 嗨", mentions_bot=True)
        await chat(engine, clock, 502, "@小D 我也叫阿明", author=13, mentions_bot=True)
        await chat(engine, clock, 503, "@小D 阿明在哪", author=12, name="小雨", mentions_bot=True)
        self.assertEqual(transport.sent[-1][1], "@阿明 去 <#300> 说")  # 两个阿明：不 @；频道照样换


class Reactions(unittest.TestCase):
    def test_prompt_prefers_reactions_when_words_are_not_needed(self):
        self.assertIn("加个表情就够了，别硬回一句", SYSTEM_RULES)
        self.assertIn('{"type": "react", "to": 45, "emoji": "👍"}', SYSTEM_RULES)
        self.assertIn("想叫某个人就写 @名字", SYSTEM_RULES)
        self.assertIn("「〔已编辑〕」", SYSTEM_RULES)


if __name__ == "__main__":
    unittest.main()
