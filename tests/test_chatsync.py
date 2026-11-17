"""2.8.0：连通的频道（/dsowner chatsync）：几个服务器的频道共用一份聊天记录。

它只在被叫（或者主动发言）的那个频道说话；记录里每段写明是哪个服务器、哪个频道的；连通之前的旧聊天只给自己的频道看；
断开时每个频道拿回自己那部分；清空记忆只清自己那部分；整理记忆按每段是哪个服务器的记进那个服务器的那份。"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from helpers import CHANNEL, GUILD, OWNER, make_engine, msg
from test_dm import OTHER
from test_memory import ACTIONS, REPLY, is_review, stores

from dsbot.engine import PASSIVE_REPLY
from dsbot.models import ChannelPlace
from dsbot.prompts import HINT_PASSIVE
from dsbot.store import Store, SyncError

HALL = 700  # OTHER 的公开频道 #大厅
LOBBY = 101  # GUILD 里另一个频道 #大厅（没连通）
SECRET = 701  # OTHER 的私密频道
THREAD = 702  # 不是文字频道的东西（帖子之类）
HERE_LABEL = "#闲聊（「主服」服务器）"
HALL_LABEL = "#大厅（「别的服」服务器）"


def world(responder=None, **kw):
    engine, llm, transport, clock = make_engine(responder, **kw)
    transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=True)
    transport.places[LOBBY] = ChannelPlace(guild_id=GUILD, name="大厅", public=True)
    transport.places[SECRET] = ChannelPlace(guild_id=OTHER, name="秘密基地", public=False)
    transport.names.update({HALL: "大厅", LOBBY: "大厅", SECRET: "秘密基地"})
    transport.guild_names[GUILD] = "主服"
    transport.guild_names[OTHER] = "别的服"
    transport.joined = [OTHER]
    return engine, llm, transport, clock


def connect(engine, a=CHANNEL, b=HALL):
    """像 /dsowner chatsync 连通 那样连起来（不经过命令）。"""
    pa, pb = engine.transport.locate(a), engine.transport.locate(b)
    sync, joined = engine.store.link_channels((a, pa.guild_id, pa.name), (b, pb.guild_id, pb.name), now=1.0)
    engine.sync_changed(joined)
    return sync


def disconnect(engine, channel):
    sync, left, rest = engine.store.unlink_channel(channel, now=2.0)
    engine.sync_changed(left, dissolved=sync if not rest else None)
    return left, rest


async def say(engine, clock, mid, text, *, channel=CHANNEL, author=11, name="阿明", call=True):
    guild = engine.transport.locate(channel).guild_id
    await engine.handle_message(msg(mid, text, channel_id=channel, guild_id=guild, author=author, name=name, mentions_bot=call))
    await engine.drain()
    clock.advance(60)


def text_of(content):
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)


def chats(llm):
    return [c for c in llm.calls if c["json_mode"] and not is_review(c)]


def replies(*outputs):
    """按顺序给出聊天的输出（用完了就不说话）；整理记忆、压缩摘要另外回。"""
    queue = list(outputs)

    def responder(call):
        if is_review(call):
            return '{"cards": [], "memory": []}'
        if not call["json_mode"]:
            return "摘要"
        return queue.pop(0) if queue else ACTIONS()

    return responder


class SyncStore(unittest.TestCase):
    def test_link_join_unlink_and_dissolve(self):
        store = Store(":memory:")
        conv_a = store.load_conversation(CHANNEL, GUILD)
        conv_a.next_short_id = 10
        store.append_round(conv_a, "#9 阿明：连通之前", "{}", [(9, 9009, 11, "阿明")], now=1)
        conv_b = store.load_conversation(HALL, OTHER)
        conv_b.next_short_id = 20
        store.append_round(conv_b, "#19 小红：那边以前", "{}", [(19, 9019, 21, "小红")], now=1)
        sync, joined = store.link_channels((CHANNEL, GUILD, "闲聊"), (HALL, OTHER, "大厅"), now=5)
        self.assertEqual((sync, joined), (-1, [HALL, CHANNEL]))  # 连到的那个排在前面
        self.assertEqual((store.conversation_key(CHANNEL), store.conversation_key(HALL), store.conversation_key(LOBBY)), (-1, -1, LOBBY))
        self.assertEqual(store.sync_members(-1), [HALL, CHANNEL])
        shared = store.load_conversation(-1, 0)
        self.assertEqual((shared.turns, shared.next_short_id), ([], 20))  # 编号接在两边用过的后面
        self.assertEqual(store.sync_place(HALL), (OTHER, "大厅"))
        # 共用的记录里各写一轮
        store.append_round(shared, "#20 阿明：这边", "{}", [(20, 9020, 11, "阿明")], now=6, origin=CHANNEL)
        store.append_round(shared, "#21 小红：那边", "{}", [(21, 9021, 21, "小红")], now=7, origin=HALL)
        self.assertEqual(store.load_msg_origins(-1), {20: CHANNEL, 21: HALL})
        self.assertEqual(store.recent_authors(-1, 5, CHANNEL), [(11, "阿明")])
        # 第三个频道加进来：编号再往后挪
        conv_c = store.load_conversation(LOBBY, GUILD)
        conv_c.next_short_id = 50
        store.append_round(conv_c, "#49 小雨：第三个", "{}", [(49, 9049, 12, "小雨")], now=8)
        self.assertEqual(store.link_channels((LOBBY, GUILD, "大厅"), (CHANNEL, GUILD, "闲聊"), now=9), (-1, [LOBBY]))
        self.assertEqual(store.load_conversation(-1, 0).next_short_id, 50)
        with self.assertRaises(SyncError):
            store.link_channels((LOBBY, GUILD, "大厅"), (HALL, OTHER, "大厅"))  # 已经在同一组里
        with self.assertRaises(SyncError):
            store.link_channels((LOBBY, GUILD, "大厅"), (LOBBY, GUILD, "大厅"))  # 自己连自己
        # LOBBY 断开：它在共用记录里没说过话，拿回来的是空的；组里还剩两个
        self.assertEqual(store.unlink_channel(LOBBY, now=10), (-1, [LOBBY], [HALL, CHANNEL]))
        self.assertEqual(store.conversation_key(LOBBY), LOBBY)
        self.assertEqual(store.load_conversation(LOBBY, GUILD).next_short_id, 50)
        # 只剩两个：一个断开组就散了，两个都拿回自己那部分（别人的不带走），共用的记录删掉
        self.assertEqual(store.unlink_channel(CHANNEL, now=11), (-1, [CHANNEL, HALL], []))
        own_a = store.load_conversation(CHANNEL, GUILD)
        self.assertEqual([t.content for t in own_a.turns if t.role == "user"], ["#9 阿明：连通之前", "#20 阿明：这边"])
        self.assertTrue(all(t.origin is None for t in own_a.turns))
        self.assertEqual(own_a.next_short_id, 50)
        self.assertEqual(store.load_msgmap(CHANNEL), {9: 9009, 20: 9020})
        own_b = store.load_conversation(HALL, OTHER)
        self.assertEqual([t.content for t in own_b.turns if t.role == "user"], ["#19 小红：那边以前", "#21 小红：那边"])
        self.assertEqual(store.load_conversation(-1, 0).turns, [])
        self.assertEqual((store.syncs(), store.load_msgmap(-1)), ({}, {}))
        with self.assertRaises(SyncError):
            store.unlink_channel(CHANNEL)  # 没连通
        self.assertEqual(store.link_channels((CHANNEL, GUILD, "闲聊"), (HALL, OTHER, "大厅"))[0], -2)  # 散掉的组的编号不再用

    def test_group_numbers_never_collide_even_if_the_counter_is_lost(self):
        store = Store(":memory:")
        self.assertEqual(store.link_channels((CHANNEL, GUILD, "闲聊"), (HALL, OTHER, "大厅"))[0], -1)
        store.db.execute("DELETE FROM chat_sync_seq")  # 编号表丢了（只还原了一部分之类）
        self.assertEqual(store.link_channels((LOBBY, GUILD, "大厅"), (SECRET, OTHER, "秘密基地"))[0], -2)

    def test_reviewed_parts_stay_reviewed_when_taken_back(self):
        store = Store(":memory:")
        sync, _ = store.link_channels((CHANNEL, GUILD, "闲聊"), (HALL, OTHER, "大厅"), now=1)
        shared = store.load_conversation(sync, 0)
        store.append_round(shared, "#1 阿明：一", "{}", [(1, 1, 11, "阿明")], now=2, origin=CHANNEL)
        store.append_round(shared, "#2 小红：二", "{}", [(2, 2, 21, "小红")], now=3, origin=HALL)
        store.set_reviewed(shared, shared.turns[1].id)  # 第一轮整理过了
        store.append_round(shared, "#3 阿明：三", "{}", [(3, 3, 11, "阿明")], now=4, origin=CHANNEL)
        store.unlink_channel(CHANNEL, now=5)
        own = store.load_conversation(CHANNEL, GUILD)
        self.assertEqual(own.reviewed_upto, own.turns[1].id)  # 拿回来的第一轮算整理过，第三轮还没
        self.assertGreater(own.turns[2].id, own.reviewed_upto)

    def test_drop_origin_keeps_the_others(self):
        store = Store(":memory:")
        sync, _ = store.link_channels((CHANNEL, GUILD, "闲聊"), (HALL, OTHER, "大厅"), now=1)
        shared = store.load_conversation(sync, 0)
        store.append_round(shared, "#1 阿明：一", "{}", [(1, 1, 11, "阿明")], now=2, origin=CHANNEL)
        store.append_round(shared, "#2 小红：二", "{}", [(2, 2, 21, "小红")], now=3, origin=HALL)
        store.db.execute("UPDATE conversations SET summary = '两边的摘要' WHERE channel_id = ?", (sync,))
        store.drop_origin(sync, CHANNEL)
        again = store.load_conversation(sync, 0)
        self.assertEqual([t.origin for t in again.turns], [HALL, HALL])
        self.assertIsNone(again.summary)  # 摘要里有它的事：一起删掉
        self.assertEqual(store.load_msgmap(sync), {2: 2})

    def test_survives_a_restart_and_upgrades_old_files(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "dsbot.sqlite3")
            store = Store(path)
            # 旧版本的库：turns、msgmap 没有 origin 这一列
            store.db.executescript(
                "DROP TABLE turns; CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL, "
                "role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL);"
                "DROP TABLE msgmap; CREATE TABLE msgmap (channel_id INTEGER NOT NULL, short_id INTEGER NOT NULL, "
                "message_id INTEGER NOT NULL, author_id INTEGER, author_name TEXT, PRIMARY KEY (channel_id, short_id));"
                "INSERT INTO turns (channel_id, role, content, created_at) VALUES (100, 'user', '\"旧的\"', 1);"
            )
            store.close()
            store = Store(path)
            self.assertEqual([(t.content, t.origin) for t in store.load_conversation(CHANNEL, GUILD).turns], [("旧的", None)])
            store.link_channels((CHANNEL, GUILD, "闲聊"), (HALL, OTHER, "大厅"), now=5)
            store.link_channels((LOBBY, GUILD, "大厅"), (HALL, OTHER, "大厅"), now=5)
            store.close()
            again = Store(path)
            self.assertEqual(again.syncs(), {-1: [HALL, CHANNEL, LOBBY]})  # 顺序不变
            again.close()


class SharedRecord(unittest.IsolatedAsyncioTestCase):
    async def test_linked_channels_share_one_record(self):
        engine, llm, transport, clock = world(replies(ACTIONS(REPLY(None, "主服好")), ACTIONS(REPLY(None, "别的服好"))))
        connect(engine)
        await say(engine, clock, 1, "@小D 主服这边好")
        await say(engine, clock, 2, "@小D 别的服这边呢", channel=HALL, author=21, name="小红")
        call = chats(llm)[-1]
        system = call["messages"][0]["content"]
        self.assertIn("## 连通的频道", system)
        self.assertIn(f"bot 主人把这个频道和{HERE_LABEL}连通了", system)
        self.assertIn(f"你现在在 {HALL_LABEL}。", system)
        self.assertIn("这几个频道不全在同一个服务器", system)  # 跨服务器：翻记录、按名字查卡这里不能用
        history = [text_of(m["content"]) for m in call["messages"][1:-1]]
        self.assertIn(f" · {HERE_LABEL}】", history[0])  # 每段写明是哪个频道的
        self.assertIn("#1 阿明", history[0])
        self.assertIn("主服好", history[1])
        last = text_of(call["messages"][-1]["content"])
        self.assertIn(f" · {HALL_LABEL}】", last)
        self.assertIn("#2 小红", last)  # 编号接着往下
        self.assertEqual([c for c, *_ in transport.sent], [CHANNEL, HALL])  # 各自在被叫的频道回
        self.assertEqual(engine.status(GUILD, CHANNEL, None)["sync"], [HALL])
        self.assertEqual(engine.status(GUILD, LOBBY, None)["sync"], [])

    async def test_unlinked_channels_look_the_same_as_before(self):
        engine, llm, transport, clock = world(replies(ACTIONS(REPLY(None, "好"))))
        await say(engine, clock, 1, "@小D 你好")
        call = chats(llm)[-1]
        self.assertNotIn("连通", call["messages"][0]["content"])
        self.assertNotIn(" · ", text_of(call["messages"][-1]["content"]).split("\n")[0])
        self.assertEqual([t.origin for t in engine.store.load_conversation(CHANNEL, GUILD).turns], [None, None])

    async def test_chatter_elsewhere_is_brought_in_before_answering(self):
        engine, llm, transport, clock = world()
        connect(engine)
        await say(engine, clock, 10, "别的服在聊游戏", channel=HALL, author=21, name="小红", call=False)
        await say(engine, clock, 11, "@小D 那边在聊什么")
        msgs = chats(llm)[-1]["messages"]
        passive = text_of(msgs[-3]["content"])
        self.assertIn("#1 小红：别的服在聊游戏", passive)
        self.assertIn(HALL_LABEL, passive.split("\n")[0])
        self.assertIn(HINT_PASSIVE, passive)
        self.assertEqual(msgs[-2], {"role": "assistant", "content": PASSIVE_REPLY})  # 它没在那边说话
        self.assertIn("#2 阿明", text_of(msgs[-1]["content"]))
        self.assertEqual(engine.states[HALL].pending, [])
        self.assertEqual(len(chats(llm)), 1)  # 补进来不问模型

    async def test_a_call_elsewhere_is_left_for_that_channel(self):
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(REPLY(None, "那边的回答"))))
        connect(engine)
        hall = engine.state(HALL, OTHER)
        before = msg(20, "先聊点别的", channel_id=HALL, guild_id=OTHER, author=21, name="小红")
        asking = msg(21, "@小D 我也在叫你", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)
        after = msg(22, "接着聊", channel_id=HALL, guild_id=OTHER, author=22, name="小绿")
        for m in (before, asking, after):
            m.received_at = clock()
        asking.trigger = True
        hall.pending = [before, asking, after]
        hall.mention_due = True
        hall.first_human_at = clock()
        await say(engine, clock, 30, "@小D 这边先")
        first, second = chats(llm)
        passive = text_of(first["messages"][-3]["content"])
        self.assertIn("先聊点别的", passive)
        self.assertNotIn("我也在叫你", passive)  # 叫它的那条留给那边自己的一轮
        later = text_of(second["messages"][-1]["content"])
        self.assertIn("我也在叫你", later)
        self.assertIn("接着聊", later)
        self.assertEqual(transport.sent[-1][0], HALL)
        self.assertEqual(transport.sent[-1][2], 21)  # 那边的回答挂在那边叫它的消息上

    async def test_replies_and_reactions_only_go_to_this_channel(self):
        engine, llm, transport, clock = world(
            replies(
                ACTIONS(),
                ACTIONS(
                    REPLY(1, "回主服那条"),
                    {"type": "react", "to": 1, "emoji": "👍"},
                    REPLY(2, "回这边那条"),
                    {"type": "react", "to": 2, "emoji": "😀"},
                ),
            )
        )
        connect(engine)
        await say(engine, clock, 1, "@小D 主服问一句")
        await say(engine, clock, 2, "@小D 这边也问", channel=HALL, author=21, name="小红")
        self.assertEqual([(c, t, r) for c, t, r, _ in transport.sent], [(HALL, "回主服那条", None), (HALL, "回这边那条", 2)])
        self.assertEqual(transport.reactions, [(HALL, 2, "😀")])  # 不跑到主服那边加表情

    async def test_deleting_and_editing_go_into_the_shared_record(self):
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 主服的秘密")
        await say(engine, clock, 2, "@小D 别的服说一句", channel=HALL, author=21, name="小红")
        engine.handle_delete(GUILD, CHANNEL, [1])
        edited = msg(2, "@小D 改过了", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)
        engine.handle_edit(edited, edited=True)
        await engine.drain()
        whole = "\n".join(text_of(t.content) for t in engine.store.load_conversation(sync, 0).turns if t.role == "user")
        self.assertNotIn("主服的秘密", whole)
        self.assertIn("#1 阿明：（这条消息已删除）", whole)
        self.assertIn("改过了", whole)

    async def test_old_chat_stays_with_its_own_channel(self):
        engine, llm, transport, clock = world(replies(ACTIONS(REPLY(None, "火锅好吃"))))
        await say(engine, clock, 1, "@小D 昨天吃的火锅")
        await say(engine, clock, 2, "随口一句", call=False)
        connect(engine)
        await say(engine, clock, 3, "@小D 接着说")
        here = chats(llm)[-1]
        system = here["messages"][0]["content"]
        self.assertIn("## 这个频道连通之前", system)
        self.assertIn("阿明：@小D 昨天吃的火锅", system)
        self.assertIn("小D：火锅好吃", system)
        self.assertEqual(len(here["messages"]), 2)  # 共用的记录从连通开始：旧的不在记录里
        await say(engine, clock, 4, "@小D 那边说了啥", channel=HALL, author=21, name="小红")
        there = chats(llm)[-1]
        self.assertNotIn("昨天吃的火锅", there["messages"][0]["content"])  # 别的频道看不到连通之前的
        self.assertNotIn("火锅好吃", there["messages"][0]["content"])
        self.assertNotIn("## 这个频道连通之前", there["messages"][0]["content"])
        # 连通之前的消息被删了：给它自己看的那段也跟着改
        engine.handle_delete(GUILD, CHANNEL, [1])
        await engine.drain()
        await say(engine, clock, 5, "@小D 还记得吗")
        self.assertNotIn("昨天吃的火锅", chats(llm)[-1]["messages"][0]["content"])

    async def test_disconnecting_takes_each_part_back(self):
        engine, llm, transport, clock = world()
        await say(engine, clock, 1, "@小D 连通之前")
        sync = connect(engine)
        await say(engine, clock, 2, "@小D 主服连通时")
        await say(engine, clock, 3, "@小D 别的服连通时", channel=HALL, author=21, name="小红")
        left, rest = disconnect(engine, CHANNEL)
        self.assertEqual((left, rest), ([CHANNEL, HALL], []))
        await say(engine, clock, 4, "@小D 断开以后")
        msgs = chats(llm)[-1]["messages"]
        history = "\n".join(text_of(m["content"]) for m in msgs[1:-1])
        self.assertIn("连通之前", history)
        self.assertIn("主服连通时", history)
        self.assertNotIn("别的服连通时", history)  # 别的频道说的不带走
        self.assertNotIn("连通的频道", msgs[0]["content"])
        last = text_of(msgs[-1]["content"])
        self.assertIn("#4 阿明", last)  # 编号接着用，不和拿回来的撞上
        self.assertNotIn(" · ", last.split("\n")[0])
        self.assertEqual(engine.store.load_conversation(sync, 0).turns, [])
        await say(engine, clock, 5, "@小D 那边也断开了", channel=HALL, author=21, name="小红")
        there = "\n".join(text_of(m["content"]) for m in chats(llm)[-1]["messages"][1:-1])
        self.assertIn("别的服连通时", there)
        self.assertNotIn("主服连通时", there)

    async def test_one_leaving_a_bigger_group_keeps_the_rest_linked(self):
        engine, llm, transport, clock = world()
        sync = connect(engine)
        connect(engine, LOBBY, CHANNEL)
        await say(engine, clock, 1, "@小D 大厅这边", channel=LOBBY)
        self.assertEqual(disconnect(engine, LOBBY), ([LOBBY], [HALL, CHANNEL]))
        await say(engine, clock, 2, "@小D 主服还连着")
        history = "\n".join(text_of(m["content"]) for m in chats(llm)[-1]["messages"][1:-1])
        self.assertIn("大厅这边", history)  # 它连通期间说过的，还连着的频道还记得
        self.assertEqual(engine.store.sync_members(sync), [HALL, CHANNEL])

    async def test_clearing_a_linked_channel_only_drops_its_part(self):
        engine, llm, transport, clock = world()
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 主服说的")
        await say(engine, clock, 2, "@小D 别的服说的", channel=HALL, author=21, name="小红")
        engine.reset_channel(CHANNEL)
        whole = engine.store.load_conversation(sync, 0)
        self.assertEqual({t.origin for t in whole.turns}, {HALL})
        await say(engine, clock, 3, "@小D 还在吗", channel=HALL, author=21, name="小红")
        history = "\n".join(text_of(m["content"]) for m in chats(llm)[-1]["messages"][1:-1])
        self.assertIn("别的服说的", history)
        self.assertNotIn("主服说的", history)
        self.assertEqual(engine.store.sync_members(sync), [HALL, CHANNEL])  # 照样连通

    async def test_restart_keeps_the_link(self):
        engine, llm, transport, clock = world()
        connect(engine)
        await say(engine, clock, 1, "@小D 重启之前")
        engine.states.clear()
        engine.convs.clear()  # 像刚重启：内存里什么都没有
        await say(engine, clock, 2, "@小D 重启之后", channel=HALL, author=21, name="小红")
        history = "\n".join(text_of(m["content"]) for m in chats(llm)[-1]["messages"][1:-1])
        self.assertIn("重启之前", history)
        self.assertIn("#2 小红", text_of(chats(llm)[-1]["messages"][-1]["content"]))
        engine.states.clear()
        engine.convs.clear()
        engine.handle_delete(GUILD, CHANNEL, [1])  # 重启后还没人说话就删了：照样从共用的记录里找到
        await engine.drain()
        whole = "\n".join(text_of(t.content) for t in engine.store.load_conversation(-1, 0).turns)
        self.assertNotIn("重启之前", whole)

    async def test_dm_memory_is_not_attached_in_linked_channels(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(cards=cards, memory=memory, defaults={"dm_memory": True})
        engine.store.set_dm_home(11, GUILD, 5000)
        memory.dm_add(11, "喜欢猫", sensitive=False, via="chat")
        await say(engine, clock, 1, "@小D 没连通时")
        self.assertIn("喜欢猫", text_of(chats(llm)[-1]["messages"][-1]["content"]))
        connect(engine)
        await say(engine, clock, 2, "@小D 连通以后")
        self.assertNotIn("喜欢猫", text_of(chats(llm)[-1]["messages"][-1]["content"]))

    async def test_names_and_channel_links_stay_with_this_server(self):
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(REPLY(None, "@小红 去 #大厅（「别的服」服务器） 看看，或者 #大厅"))))
        transport.channel_map = {"大厅": {LOBBY}}
        connect(engine)
        await say(engine, clock, 1, "@小D 那边有人吗", channel=HALL, author=21, name="小红")
        await say(engine, clock, 2, "@小D 叫一下她")
        text = transport.sent[-1][1]
        self.assertIn("@小红", text)  # 小红只在别的服说过话：这里 @ 不到
        self.assertEqual(transport.mentions[-1], ())
        self.assertIn("#大厅（「别的服」服务器）", text)  # 那边的频道不换成这里同名频道的链接
        self.assertIn(f"<#{LOBBY}>", text)  # 没写服务器的照常换成这里的


class Review(unittest.IsolatedAsyncioTestCase):
    async def test_each_part_is_written_to_its_own_server(self):
        cards, memory = stores()

        def review(call):
            text = text_of(call["messages"][-1]["content"])
            where = "主服" if "主服的事" in text else "别的服"
            sid = 1 if where == "主服" else 2
            return json.dumps({"cards": [{"to": sid, "text": f"{where}的卡"}], "memory": [{"add": f"{where}的约定"}]}, ensure_ascii=False)

        def responder(call):
            if is_review(call):
                return review(call)
            return ACTIONS()

        engine, llm, transport, clock = world(responder, cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1})
        connect(engine)
        await say(engine, clock, 1, "@小D 主服的事")
        await say(engine, clock, 2, "@小D 别的服的事", channel=HALL, author=21, name="小红")
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["主服的约定"])
        self.assertEqual([m.text for m in memory.entries(OTHER)], ["别的服的约定"])
        self.assertEqual([m.origin for m in memory.entries(OTHER)], [OTHER])
        self.assertEqual(cards.get(GUILD, 11).text, "主服的卡")
        self.assertEqual(cards.get(OTHER, 21).text, "别的服的卡")
        self.assertIsNone(cards.get(GUILD, 21))
        self.assertEqual(engine.states[CHANNEL].unreviewed_rounds, 0)

    async def test_a_review_running_while_the_group_dissolves_leaves_nothing_behind(self):
        cards, memory = stores()
        gate = asyncio.Event()

        def responder(call):
            if is_review(call):
                return '{"cards": [], "memory": [{"add": "散掉之后不该记"}]}'
            return ACTIONS()

        engine, llm, transport, clock = world(responder, cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1})
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 主服")
        await say(engine, clock, 2, "@小D 别的服", channel=HALL, author=21, name="小红")

        async def hold(call):
            if is_review(call):
                await gate.wait()

        llm.hook = hold
        clock.advance(600)
        await engine.tick()
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertTrue(any(is_review(c) for c in llm.calls))  # 正在问模型（问的时候不占着记录）
        disconnect(engine, CHANNEL)  # 组散了
        gate.set()
        await engine.drain()
        self.assertIsNone(engine.store.db.execute("SELECT 1 FROM conversations WHERE channel_id = ?", (sync,)).fetchone())
        self.assertEqual(memory.entries(GUILD), [])  # 散掉的那份记录的整理作废了

    async def test_unreviewed_old_chat_is_reviewed_when_linking(self):
        cards, memory = stores()
        reviewed = []

        def responder(call):
            if is_review(call):
                reviewed.append(text_of(call["messages"][-1]["content"]))
                return '{"cards": [], "memory": [{"add": "连通之前的约定"}]}'
            return ACTIONS()

        engine, llm, transport, clock = world(responder, cards=cards, memory=memory)
        await say(engine, clock, 1, "@小D 连通之前说好了")
        connect(engine)
        await engine.drain()
        self.assertEqual(len(reviewed), 1)
        self.assertIn("连通之前说好了", reviewed[0])
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["连通之前的约定"])
        own = engine.store.load_conversation(CHANNEL, GUILD)
        self.assertEqual(own.reviewed_upto, own.turns[-1].id)


class EdgeCases(unittest.IsolatedAsyncioTestCase):
    async def test_proactive_rounds_do_not_pull_in_other_channels(self):
        engine, llm, transport, clock = world()
        engine.settings.set("channel", CHANNEL, "proactive", True)
        connect(engine)
        await say(engine, clock, 1, "那边随口聊", channel=HALL, author=21, name="小红", call=False)
        await say(engine, clock, 2, "这边也随口聊", call=False)
        for _ in range(5):
            clock.advance(120)
            await engine.tick()
            await engine.drain()
        self.assertEqual(len(chats(llm)), 1)
        call = chats(llm)[0]
        self.assertEqual(len(call["messages"]), 2)  # 主动判断时不把别的频道还没进记录的补进来
        self.assertIn("这边也随口聊", text_of(call["messages"][-1]["content"]))
        self.assertEqual([m.text for m in engine.states[HALL].pending], ["那边随口聊"])

    async def test_compacting_refreshes_every_linked_channels_list(self):
        engine, llm, transport, clock = world()
        transport.places[102] = ChannelPlace(guild_id=GUILD, name="游戏", public=True)
        engine.settings.set("guild", GUILD, "share_memory", True)
        await say(engine, clock, 1, "@小D 游戏以前", channel=102)
        connect(engine, CHANNEL, LOBBY)  # 同一个服务器的两个频道
        await say(engine, clock, 2, "@小D 大厅", channel=LOBBY)
        self.assertIn("（能翻记录的别的频道：", text_of(chats(llm)[-1]["messages"][-1]["content"]))
        await say(engine, clock, 3, "@小D 闲聊")
        st = engine.states[CHANNEL]
        await engine._compact(st, st.conversation, transport.channel_info(CHANNEL), force=True)  # 大厅写的名单压掉了
        await say(engine, clock, 4, "@小D 压缩之后", channel=LOBBY)
        self.assertIn("（能翻记录的别的频道：", text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 那边再写一次

    async def test_one_summary_for_all_linked_channels(self):
        engine, llm, transport, clock = world()
        connect(engine)
        for i in range(4):
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")
            await say(engine, clock, 20 + i, f"@小D 别的服第{i}句", channel=HALL, author=21, name="小红")
        st = engine.states[CHANNEL]
        await engine._compact(st, st.conversation, transport.channel_info(CHANNEL), force=True)
        await say(engine, clock, 30, "@小D 压缩之后", channel=HALL, author=21, name="小红")
        system = chats(llm)[-1]["messages"][0]["content"]
        self.assertIn("几个连通的频道共用的聊天记录更早的部分的摘要", system)
        self.assertIn("摘要", system.split("## 更早的聊天摘要")[1])

    async def test_recall_from_an_unlinked_channel_sees_only_that_channels_part(self):
        recall = json.dumps({"actions": [{"type": "recall", "channel": "闲聊"}]}, ensure_ascii=False)
        engine, llm, transport, clock = world(replies(ACTIONS(), ACTIONS(), recall, ACTIONS()))
        engine.settings.set("guild", GUILD, "share_memory", True)
        connect(engine)
        await say(engine, clock, 1, "@小D 主服这边的话")
        await say(engine, clock, 2, "@小D 别的服那边的话", channel=HALL, author=21, name="小红")
        await say(engine, clock, 3, "@小D 闲聊那边在说什么", channel=LOBBY)
        found = text_of(chats(llm)[-1]["messages"][-1]["content"])
        self.assertIn("主服这边的话", found)
        self.assertNotIn("别的服那边的话", found)  # 别的服没开给这里翻

    async def test_linked_channels_are_not_listed_for_recall(self):
        engine, llm, transport, clock = world()
        engine.settings.set("guild", GUILD, "share_memory", True)
        await say(engine, clock, 1, "@小D 大厅以前", channel=LOBBY)
        self.assertEqual([c for c, _ in engine.shared_sources(GUILD, CHANNEL)], [LOBBY])
        connect(engine, CHANNEL, LOBBY)  # 同一个服务器里的两个频道也能连通
        self.assertEqual(engine.shared_sources(GUILD, CHANNEL), [])  # 本来就在共用的记录里

    async def test_linking_waits_for_a_running_round(self):
        engine, llm, transport, clock = world()
        gate = asyncio.Event()

        async def hold(call):
            await gate.wait()

        llm.hook = hold
        await engine.handle_message(msg(1, "@小D 在吗", mentions_bot=True))
        for _ in range(20):
            await asyncio.sleep(0)
        with self.assertRaises(asyncio.TimeoutError):
            async with engine.sync_guard([CHANNEL, HALL], timeout=0.01):
                pass
        order = []

        async def link_now():
            async with engine.sync_guard([CHANNEL, HALL]):
                order.append("link")
                connect(engine)

        task = asyncio.create_task(link_now())
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(order, [])  # 等这一轮写完记录
        gate.set()
        await engine.drain()
        await task
        self.assertEqual(order, ["link"])
        self.assertEqual(len(engine.store.load_conversation(CHANNEL, GUILD).turns), 2)  # 那一轮写进了它自己原来那份

    async def test_threads_under_a_linked_channel_are_not_linked(self):
        engine, llm, transport, clock = world()
        transport.places[900] = ChannelPlace(guild_id=GUILD, name="帖子", parent_id=CHANNEL, public=True)
        connect(engine)
        await engine.handle_message(msg(1, "@小D 帖子里", channel_id=900, parent_id=CHANNEL, mentions_bot=True))
        await engine.drain()
        call = chats(llm)[-1]
        self.assertNotIn("## 连通的频道", call["messages"][0]["content"])
        self.assertNotIn(" · ", text_of(call["messages"][-1]["content"]).splitlines()[0])

    async def test_talk_in_a_linked_channel_is_not_repeated_as_recent_talk(self):
        engine, llm, transport, clock = world()
        engine.settings.set("guild", GUILD, "cross_server", True)
        engine.settings.set("guild", OTHER, "cross_server", True)
        connect(engine)
        await say(engine, clock, 1, "@小D 我在那边", channel=HALL)
        await say(engine, clock, 2, "@小D 我来这边了")
        self.assertNotIn("跟你聊过", text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 本来就在共用的记录里

    async def test_talk_in_a_same_server_linked_channel_is_not_repeated_as_recent_talk(self):
        engine, llm, transport, clock = world()
        engine.settings.set("guild", GUILD, "share_memory", True)
        connect(engine, CHANNEL, LOBBY)  # 同一个服务器的两个频道：近况照常带，只是不带连通的那个
        await say(engine, clock, 1, "@小D 我在大厅", channel=LOBBY)
        await say(engine, clock, 2, "@小D 我来闲聊了")
        self.assertNotIn("跟你聊过", text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 本来就在共用的记录里

    async def test_talk_in_a_linked_channel_of_a_grouped_server_is_not_repeated(self):
        engine, llm, transport, clock = world()
        group = engine.store.new_group_id()
        engine.store.join_group(GUILD, group, now=1.0)
        engine.store.join_group(OTHER, group, now=1.0)  # 两个服务器连成一组：连通这两个频道不算跨服务器
        for guild in (GUILD, OTHER):
            engine.settings.set("guild", guild, "cross_server", True)
        connect(engine)
        await say(engine, clock, 1, "@小D 我在大厅", channel=HALL)
        await say(engine, clock, 2, "@小D 我来闲聊了")
        self.assertNotIn("跟你聊过", text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 本来就在共用的记录里

    async def test_cards_across_servers_only_when_cards_are_shared(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(cards=cards, memory=memory)
        cards.put(OTHER, 11, "阿明", "别的服的阿明卡", via="command")
        connect(engine)
        await say(engine, clock, 1, "@小D 我在别的服", channel=HALL)
        self.assertNotIn("别的服的阿明卡", text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 卡是各服务器自己的：不附进共用的记录
        await say(engine, clock, 2, "@小D 我在主服")
        self.assertNotIn("人设卡已经删掉了", text_of(chats(llm)[-1]["messages"][-1]["content"]))

    async def test_shared_cards_are_attached_in_a_cross_server_link(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(cards=cards, memory=memory)
        cards.put(GUILD, 11, "阿明", "大家都知道的阿明卡", via="command")
        cards.set_shared(True, scope_of=engine.store.memory_scope, alive=set())
        connect(engine)
        await say(engine, clock, 1, "@小D 我在别的服", channel=HALL)
        self.assertIn("大家都知道的阿明卡", text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 所有服务器共用的卡本来就到处都用

    async def test_each_channel_keeps_its_own_music_line(self):
        engine, llm, transport, clock = world()

        async def tick():
            return None

        engine.music = NS(status=lambda guild: "语音频道「音乐厅」正在放：晴天" if guild == GUILD else None, tick=tick)
        connect(engine)
        await say(engine, clock, 1, "@小D 主服")
        here = text_of(chats(llm)[-1]["messages"][-1]["content"])
        self.assertIn("（现在「主服」的语音频道「音乐厅」正在放：晴天）", here)  # 写明是哪个服务器的语音频道
        await say(engine, clock, 2, "@小D 别的服", channel=HALL, author=21, name="小红")
        self.assertNotIn("语音频道", text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 别的服没在放：不用说
        engine.states.clear()
        engine.convs.clear()  # 重启：只认这个频道自己写过的那句
        await say(engine, clock, 3, "@小D 重启之后", channel=HALL, author=21, name="小红")
        self.assertNotIn("没在放歌", text_of(chats(llm)[-1]["messages"][-1]["content"]))

    async def test_editing_old_chat_from_before_linking_shows_up(self):
        engine, llm, transport, clock = world()
        for i in range(6):
            await say(engine, clock, 1 + i, f"@小D 原来的第{i}句")
        connect(engine)
        edited = msg(1, "@小D 改过的第0句", mentions_bot=True)
        engine.handle_edit(edited, edited=True)
        await engine.drain()
        await say(engine, clock, 20, "@小D 接着聊")
        system = chats(llm)[-1]["messages"][0]["content"]
        self.assertIn("改过的第0句", system)  # 只给它自己看的那段，很早的消息改了也直接改
        self.assertNotIn("原来的第0句", system)

    async def test_unknown_server_names_stay_plain_in_a_cross_server_link(self):
        engine, llm, transport, clock = world(replies(ACTIONS(REPLY(None, "去「生存服」服务器的 #大厅 看看")), ACTIONS(REPLY(None, "去「生存服」服务器的 #大厅 看看"))))
        transport.channel_map = {"大厅": {LOBBY}}
        await say(engine, clock, 1, "@小D 没连通时")
        self.assertIn(f"<#{LOBBY}>", transport.sent[-1][1])  # 它没见过别的服务器：写的多半就是这里
        connect(engine)
        await say(engine, clock, 2, "@小D 连通以后")
        self.assertNotIn(f"<#{LOBBY}>", transport.sent[-1][1])  # 记录里有别的服务器的名字：认不出的不换成这里的链接

    async def test_review_waits_until_every_linked_channel_is_quiet(self):
        cards, memory = stores()
        engine, llm, transport, clock = world(cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1})
        connect(engine)
        await say(engine, clock, 1, "@小D 主服")
        clock.advance(600)
        await say(engine, clock, 2, "那边还在聊", channel=HALL, author=21, name="小红", call=False)
        await engine.tick()
        await engine.drain()
        self.assertFalse(any(is_review(c) for c in llm.calls))  # 那边刚有人说话：还没安静
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertTrue(any(is_review(c) for c in llm.calls))


class FoundWhileTesting(unittest.IsolatedAsyncioTestCase):
    """写回归测试时发现的（G1–G7）。"""

    async def test_chatter_settled_at_linking_keeps_numbers_and_taking_back_working(self):
        engine, llm, transport, clock = world()
        await say(engine, clock, 1, "@小D 连通之前")  # 自己那份的 #1
        await say(engine, clock, 2, "连通前最后一句", call=False)  # 还在缓冲区里
        sync = connect(engine)  # 那句记进自己那份（#2）
        self.assertEqual(engine.store.stale_syncs(), [])  # 不是旧版本写的
        await say(engine, clock, 3, "@小D 连通期间说的")
        shared = "\n".join(text_of(t.content) for t in engine.store.load_conversation(sync, 0).turns)
        self.assertIn("#3 阿明：@小D 连通期间说的", shared)  # 接在自己用过的编号后面，不重复
        with self.assertNoLogs("dsbot.store", "WARNING"):
            disconnect(engine, CHANNEL)
        own = "\n".join(text_of(t.content) for t in engine.store.load_conversation(CHANNEL, GUILD).turns)
        self.assertIn("连通前最后一句", own)
        self.assertIn("连通期间说的", own)  # 断开时照常拿回来

    async def test_rejoining_does_not_review_the_earlier_part_again(self):
        cards, memory = stores()
        reviewed = []

        def responder(call):
            if is_review(call):
                reviewed.append(text_of(call["messages"][-1]["content"]))
                return '{"cards": [], "memory": []}'
            return ACTIONS()

        engine, llm, transport, clock = world(responder, cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1})
        connect(engine)
        connect(engine, LOBBY, CHANNEL)
        await say(engine, clock, 1, "@小D 第一次连通时大厅说的", channel=LOBBY, author=12, name="小雨")
        disconnect(engine, LOBBY)  # 拿回去，在它自己那份里整理
        connect(engine, LOBBY, CHANNEL)  # 又连回来
        await engine.drain()
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual(sum("第一次连通时大厅说的" in r for r in reviewed), 1)  # 组的记录里不再整理一遍

    async def test_clearing_a_channel_keeps_the_others_review_progress(self):
        cards, memory = stores()
        reviewed = []

        def responder(call):
            if is_review(call):
                reviewed.append(text_of(call["messages"][-1]["content"]))
                return '{"cards": [], "memory": []}'
            return ACTIONS()

        engine, llm, transport, clock = world(responder, cards=cards, memory=memory, engine_cfg={"review_min_rounds": 1})
        connect(engine)
        connect(engine, LOBBY, CHANNEL)
        await say(engine, clock, 1, "@小D 主服的事")
        await say(engine, clock, 2, "@小D 别的服的事", channel=HALL, author=21, name="小红")
        await say(engine, clock, 3, "@小D 大厅的事", channel=LOBBY, author=12, name="小雨")
        engine.settings.set("guild", GUILD, "daily_budget_usd", 0)  # 主服今天的预算用完了
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual([sum(w in r for r in reviewed) for w in ("别的服的事", "主服的事")], [1, 0])
        await engine.reset_linked(LOBBY)  # 清空大厅那部分（组的记录换成新的状态）
        engine.settings.set("guild", GUILD, "daily_budget_usd", 10)
        clock.advance(3600)
        await engine.tick()
        await engine.drain()
        self.assertEqual([sum(w in r for r in reviewed) for w in ("别的服的事", "主服的事")], [1, 1])  # 别的服那部分不再整理一遍

    async def test_an_edit_note_waiting_in_the_group_goes_home_with_its_channel(self):
        engine, llm, transport, clock = world()
        sync = connect(engine)
        connect(engine, LOBBY, CHANNEL)
        await say(engine, clock, 5, "@小D 我的号码是 138", channel=HALL, author=21, name="小红")
        for i in range(5):
            await say(engine, clock, 10 + i, f"@小D 主服第{i}句")  # 那条变成了很早的消息
        engine.handle_edit(msg(5, "@小D 我的号码改成 139", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True), edited=True)
        await engine.drain()
        self.assertIn(5, engine.convs[sync].edit_notes)  # 等着别的服下一轮告诉它
        disconnect(engine, HALL)  # 别的服没再说话就断开了
        self.assertNotIn(5, engine.convs[sync].edit_notes)
        await say(engine, clock, 50, "@小D 在吗", channel=HALL, author=21, name="小红")
        self.assertIn("我的号码改成 139", text_of(chats(llm)[-1]["messages"][-1]["content"]))  # 那句跟着它走

    async def test_pausing_again_is_logged_again(self):
        engine, llm, transport, clock = world()
        connect(engine)
        transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=False)
        with self.assertLogs("dsbot.engine", "WARNING"):
            await say(engine, clock, 1, "私密的时候", channel=HALL, author=21, name="小红", call=False)
        transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=True)
        await say(engine, clock, 2, "又公开了", channel=HALL, author=21, name="小红", call=False)
        transport.places[HALL] = ChannelPlace(guild_id=OTHER, name="大厅", public=False)
        with self.assertLogs("dsbot.engine", "WARNING"):  # 一小时之内又变成私密的：照样提醒
            await say(engine, clock, 3, "又私密了", channel=HALL, author=21, name="小红", call=False)


def text_channel(guild, cid, name, position=0):
    import discord

    ch = MagicMock(spec=discord.TextChannel)
    ch.id, ch.name, ch.guild, ch.position, ch.category_id = cid, name, guild, position, None
    ch.permissions_for = lambda member: NS(view_channel=True, send_messages=True)
    guild.text_channels.append(ch)
    return ch


class FakeResponse:
    def __init__(self):
        self.sent, self.edited = [], []

    def is_done(self):
        return False

    async def send_message(self, text, view=None, ephemeral=False):
        self.sent.append((text, view, ephemeral))

    async def edit_message(self, content=None, view=None):
        self.edited.append(content)


class OwnerCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")
        self.engine, self.llm, self.transport, self.clock = world()
        main = NS(id=GUILD, name="主服", me=NS(), text_channels=[])
        other = NS(id=OTHER, name="别的服", me=NS(), text_channels=[])
        channels = {
            CHANNEL: text_channel(main, CHANNEL, "闲聊"),
            LOBBY: text_channel(main, LOBBY, "大厅", 1),
            HALL: text_channel(other, HALL, "大厅"),
            SECRET: text_channel(other, SECRET, "秘密基地", 1),
            THREAD: NS(id=THREAD, name="帖子", guild=main),
        }
        guilds = {GUILD: main, OTHER: other}
        self.bot = NS(
            store=self.engine.store,
            engine=self.engine,
            guilds=[main, other],
            get_guild=guilds.get,
            get_channel=channels.get,
            secrets=NS(owner_id=OWNER),
        )

    def interaction(self, user=OWNER):
        done = []

        async def edit_original_response(content=None, view=None):
            done.append(content)

        return NS(user=NS(id=user), response=FakeResponse(), followup=NS(send=None), edit_original_response=edit_original_response, done=done)

    async def test_link_list_and_unlink(self):
        from dsbot.commands import owner_channel_choices, sync_command, syncs_text

        bot = self.bot
        self.assertIn("还没有连通的频道", syncs_text(bot))
        self.assertEqual([c.value for c in owner_channel_choices(bot, OWNER, "别的服")], [str(HALL), str(SECRET)])
        self.assertEqual(owner_channel_choices(bot, 99, ""), [])  # 不是 bot 主人：频道名单不给看
        ask = self.interaction()
        await sync_command(bot, ask, "link", str(CHANNEL), str(HALL))
        preview, view, _ = ask.response.sent[0]
        self.assertIn("把「主服」#闲聊和「别的服」#大厅连通？", preview)
        self.assertIn("连通之前各频道的旧聊天不给别的频道看", preview)
        self.assertIn("连通是 Beta 功能", preview)
        click = self.interaction()
        await view.confirm(click)
        self.assertIn("已经连通：连通组 1 现在有 「别的服」#大厅、「主服」#闲聊。", click.done[-1])
        notices = {c: t for c, t, _, _ in self.transport.sent}
        self.assertIn("bot 主人把这个频道和「主服」#闲聊连通了", notices[HALL])
        self.assertIn("bot 主人把这个频道和「别的服」#大厅连通了", notices[CHANNEL])
        self.assertIn("连通组 1：「别的服」#大厅、「主服」#闲聊", syncs_text(bot))
        self.assertIn("大厅 · 连通组 1", [c.name for c in owner_channel_choices(bot, OWNER, "别的服")][0])
        # 第三个频道加进来：原来的两个频道也说一声
        ask = self.interaction()
        await sync_command(bot, ask, "link", str(LOBBY), str(CHANNEL))
        self.assertIn("把「主服」#大厅加进连通组 1（「别的服」#大厅、「主服」#闲聊）？", ask.response.sent[0][0])
        await ask.response.sent[0][1].confirm(self.interaction())
        notices = [(c, t) for c, t, _, _ in self.transport.sent[2:]]
        self.assertTrue(any(c == HALL and "「主服」#大厅也连进来了" in t for c, t in notices))
        # 断开
        ask = self.interaction()
        await sync_command(bot, ask, "unlink", str(LOBBY), None)
        self.assertIn("组里还剩「别的服」#大厅、「主服」#闲聊，照样共用", ask.response.sent[0][0])
        click = self.interaction()
        await ask.response.sent[0][1].confirm(click)
        self.assertIn("「主服」#大厅已经断开连通组 1", click.done[-1])
        ask = self.interaction()
        await sync_command(bot, ask, "unlink", str(CHANNEL), None)
        self.assertIn("组里只剩「别的服」#大厅：组就散了", ask.response.sent[0][0])
        click = self.interaction()
        await ask.response.sent[0][1].confirm(click)
        self.assertIn("连通组 1散了", click.done[-1])
        last = {c: t for c, t, _, _ in self.transport.sent[-2:]}
        self.assertIn("不再连通了", last[CHANNEL])
        self.assertIn("不再连通了", last[HALL])
        self.assertIn("还没有连通的频道", syncs_text(bot))

    async def test_refusals(self):
        from dsbot.commands import sync_command

        bot = self.bot
        cases = [
            ((str(CHANNEL), str(SECRET)), "私密频道不能连通"),
            ((str(CHANNEL), str(THREAD)), "只能连文字频道"),
            ((str(CHANNEL), str(CHANNEL)), "不能把一个频道连到它自己"),
            ((str(CHANNEL), "9999"), "bot 看不到频道 9999"),
            (("大厅", str(HALL)), "从弹出的列表里选一个频道"),
            ((str(CHANNEL), None), "在「连到」一栏"),
        ]
        for (a, b), wanted in cases:
            ask = self.interaction()
            await sync_command(bot, ask, "link", a, b)
            self.assertIn(wanted, ask.response.sent[0][0], (a, b))
            self.assertIsNone(ask.response.sent[0][1])
        ask = self.interaction()
        await sync_command(bot, ask, "unlink", str(CHANNEL), None)
        self.assertIn("这个频道没有连通", ask.response.sent[0][0])

    async def test_confirming_after_things_changed_does_nothing(self):
        from dsbot.commands import sync_command

        bot = self.bot
        ask = self.interaction()
        await sync_command(bot, ask, "link", str(CHANNEL), str(HALL))
        connect(self.engine, LOBBY, HALL)  # 给主人看过之后，HALL 先被连到别处了
        click = self.interaction()
        await ask.response.sent[0][1].confirm(click)
        self.assertIn("情况变了", click.response.edited[-1])
        self.assertEqual(self.engine.store.sync_of(CHANNEL), None)


if __name__ == "__main__":
    unittest.main()
