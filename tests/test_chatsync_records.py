"""2.8.0：连通的频道（/dsowner chatsync）的记录和消息编号。

连进一个正在聊的组，编号接在它自己用过的后面；断开、又连回、再断开，拿回来的每句只有一次；断开时编号跳了一大段，连通之前的
编号映射照样留着；旧版本往它自己那份里写过的不拿回；断开以后删改消息、清空记忆，它离开的那个组的记录也跟着改。"""

import unittest
from unittest.mock import patch

from helpers import CHANNEL, GUILD, msg
from test_chatsync import HALL, LOBBY, chats, connect, disconnect, say, text_of, world
from test_dm import OTHER

from dsbot.store import Store


def said(conv):
    """记录里一条条的消息（#编号 开头的那几行），按先后。"""
    return [line for t in conv.turns if t.role == "user" for line in text_of(t.content).split("\n") if line.startswith("#")]


def msgmap(store, key):
    """库里这份记录所有的编号映射（load_msgmap 只读最近 MSGMAP_KEEP 个，看不出删没删）。"""
    rows = store.db.execute("SELECT short_id, message_id FROM msgmap WHERE channel_id = ?", (key,))
    return {r["short_id"]: r["message_id"] for r in rows}


def three_linked(engine):
    """「主服」#闲聊、「别的服」#大厅、「主服」#大厅 连成一组：断开一个，组还在。"""
    sync = connect(engine)
    connect(engine, LOBBY, CHANNEL)
    return sync


class Numbers(unittest.IsolatedAsyncioTestCase):
    async def test_joining_an_active_group_numbers_after_its_own_record(self):
        engine, llm, transport, clock = world()
        for i in range(5):  # 「主服」#大厅自己原来的记录：#1～#5（消息 500～504）
            await say(engine, clock, 500 + i, f"@小D 大厅以前第{i}句", channel=LOBBY, author=12, name="小雨")
        sync = connect(engine)
        await say(engine, clock, 1, "@小D 主服这边")  # 组里正在聊：共用的记录已经在内存里，下一个编号是 #2
        connect(engine, LOBBY, CHANNEL)
        await say(engine, clock, 600, "@小D 大厅连进来以后说的", channel=LOBBY, author=12, name="小雨")
        self.assertEqual(engine.store.message_rows(sync, [600]), {600: [(6, "小雨")]})  # 接在它自己用过的后面，不重用 #2
        self.assertEqual(disconnect(engine, LOBBY), ([LOBBY], [HALL, CHANNEL]))
        old = [f"#{i + 1} 小雨：@小D 大厅以前第{i}句 〔@你〕" for i in range(5)]
        new = "#6 小雨：@小D 大厅连进来以后说的 〔@你〕"
        self.assertEqual(said(engine.store.load_conversation(LOBBY, GUILD)), [*old, new])  # 拿回来的不和原来的撞号
        self.assertEqual(msgmap(engine.store, LOBBY), {1: 500, 2: 501, 3: 502, 4: 503, 5: 504, 6: 600})
        engine.handle_delete(GUILD, LOBBY, [501])  # 删掉原来的 #2：只改那一行
        await engine.drain()
        gone = "#2 小雨：（这条消息已删除）"
        self.assertEqual(said(engine.store.load_conversation(LOBBY, GUILD)), [old[0], gone, *old[2:], new])
        engine.handle_delete(GUILD, LOBBY, [600])  # 删掉连通时说的：也只改那一行
        await engine.drain()
        self.assertEqual(said(engine.store.load_conversation(LOBBY, GUILD)), [old[0], gone, *old[2:], "#6 小雨：（这条消息已删除）"])

    async def test_leaving_rejoining_and_leaving_again_takes_each_line_back_once(self):
        engine, llm, transport, clock = world()
        three_linked(engine)
        await say(engine, clock, 1, "@小D 第一次连通时说的", channel=LOBBY, author=12, name="小雨")
        disconnect(engine, LOBBY)  # 组里还剩两个：照样连通，还留着它说过的
        await say(engine, clock, 2, "@小D 断开期间说的", channel=LOBBY, author=12, name="小雨")
        connect(engine, LOBBY, CHANNEL)  # 又连回同一个组
        await say(engine, clock, 3, "@小D 第二次连通时说的", channel=LOBBY, author=12, name="小雨")
        disconnect(engine, LOBBY)
        own = engine.store.load_conversation(LOBBY, GUILD)
        self.assertEqual(  # 第一次连通时说的上次已经拿回来了，这次不再拿
            said(own),
            ["#1 小雨：@小D 第一次连通时说的 〔@你〕", "#2 小雨：@小D 断开期间说的 〔@你〕", "#3 小雨：@小D 第二次连通时说的 〔@你〕"],
        )
        self.assertEqual(msgmap(engine.store, LOBBY), {1: 1, 2: 2, 3: 3})

    async def test_numbers_from_before_linking_survive_a_big_jump(self):
        with patch("dsbot.store.MSGMAP_KEEP", 5):  # 代替 3000：连通期间组里用掉了 3000 多个编号
            engine, llm, transport, clock = world()
            await say(engine, clock, 1, "@小D 连通之前：我的手机号 13900001111")
            await say(engine, clock, 2, "@小D 连通之前第二句")
            connect(engine)
            for i in range(8):  # 连通期间主要是那边在聊：断开时编号跳到 #11
                await say(engine, clock, 100 + i, f"@小D 那边第{i}句", channel=HALL, author=21, name="小红")
            disconnect(engine, CHANNEL)
            self.assertEqual(msgmap(engine.store, CHANNEL), {1: 1, 2: 2})  # 按条数留，不按编号差删掉
            await say(engine, clock, 3, "@小D 断开以后")  # 接着往自己那份里写：照样按条数留
            self.assertEqual(msgmap(engine.store, CHANNEL), {1: 1, 2: 2, 11: 3})
            engine.handle_delete(GUILD, CHANNEL, [1])  # 连通之前的消息被删了：记录改得到
            await engine.drain()
            self.assertEqual(said(engine.store.load_conversation(CHANNEL, GUILD))[0], "#1 阿明：（这条消息已删除）")
            for i in range(3):
                await say(engine, clock, 4 + i, f"@小D 断开以后第{i}句")
            self.assertEqual(sorted(msgmap(engine.store, CHANNEL)), [2, 11, 12, 13, 14])  # 多了还是只留最近 5 个


class StoreRecords(unittest.TestCase):
    def test_a_record_that_never_took_back_trims_by_number_distance(self):
        with patch("dsbot.store.MSGMAP_KEEP", 5):
            store = Store(":memory:")
            conv = store.load_conversation(CHANNEL, GUILD)
            conv.next_short_id = 4
            store.append_round(conv, "#1 阿明：一\n#2 阿明：二\n#3 阿明：三", "{}", [(1, 1001), (2, 1002), (3, 1003)], now=1)
            conv.next_short_id = 8  # 编号跳了一段
            store.append_round(conv, "#7 阿明：七", "{}", [(7, 1007)], now=2)
            self.assertEqual(msgmap(store, CHANNEL), {3: 1003, 7: 1007})  # 和以前一样：比 8 - 5 小的删掉（按条数会都留着）

    def test_unlinking_does_not_take_back_over_what_an_older_version_wrote(self):
        store = Store(":memory:")
        own = store.load_conversation(CHANNEL, GUILD)
        own.next_short_id = 3
        store.append_round(own, "#1 阿明：连通之前\n#2 阿明：连通之前第二句", "{}", [(1, 1001, 11, "阿明"), (2, 1002, 11, "阿明")], now=1)
        sync, _ = store.link_channels((CHANNEL, GUILD, "闲聊"), (HALL, OTHER, "大厅"), now=2)
        shared = store.load_conversation(sync, 0)
        shared.next_short_id = 4
        store.append_round(shared, "#3 阿明：连通时这边", "{}", [(3, 1003, 11, "阿明")], now=3, origin=CHANNEL)
        shared.next_short_id = 5
        store.append_round(shared, "#4 小红：连通时那边", "{}", [(4, 1004, 21, "小红")], now=4, origin=HALL)
        self.assertEqual(store.stale_syncs(), [])
        # 换回旧版本跑了一阵：它不认连通组，照常往这个频道自己那份里写（#3 和共用记录里的撞号）
        own = store.load_conversation(CHANNEL, GUILD)
        own.next_short_id = 4
        store.append_round(own, "#3 阿明：旧版本写的", "{}", [(3, 1999, 11, "阿明")], now=5)
        self.assertEqual(store.stale_syncs(), [CHANNEL])  # 启动时提醒一声
        with self.assertLogs("dsbot.store", "WARNING") as logs:
            store.unlink_channel(CHANNEL, now=6)
        self.assertEqual(len(logs.output), 1)
        self.assertIn(f"频道 {CHANNEL} 连通期间它自己那份记录被写过", logs.output[0])
        own = store.load_conversation(CHANNEL, GUILD)
        self.assertEqual(said(own), ["#1 阿明：连通之前", "#2 阿明：连通之前第二句", "#3 阿明：旧版本写的"])  # 共用记录里的 #3 不拿回来
        self.assertEqual(msgmap(store, CHANNEL), {1: 1001, 2: 1002, 3: 1999})
        self.assertEqual(said(store.load_conversation(HALL, OTHER)), ["#4 小红：连通时那边"])  # 那边照常拿回自己的
        self.assertEqual(store.stale_syncs(), [])  # 断开了：不用再提醒


class AfterLeaving(unittest.IsolatedAsyncioTestCase):
    async def test_deleting_after_leaving_also_scrubs_the_group_record(self):
        engine, llm, transport, clock = world()
        sync = three_linked(engine)
        await say(engine, clock, 5, "@小D 我的手机号是 13800001111", channel=HALL, author=21, name="小红")
        await say(engine, clock, 6, "@小D 我家住在幸福路八号", channel=HALL, author=21, name="小红")
        self.assertEqual(disconnect(engine, HALL), ([HALL], [CHANNEL, LOBBY]))  # 组里还剩两个，照样看得到它说过的
        engine.handle_delete(OTHER, HALL, [5])
        await engine.drain()
        gone = ["#1 小红：（这条消息已删除）", "#2 小红：（这条消息已删除）"]
        self.assertEqual(said(engine.store.load_conversation(sync, 0)), [gone[0], "#2 小红：@小D 我家住在幸福路八号 〔@你〕"])
        self.assertEqual(said(engine.store.load_conversation(HALL, OTHER))[0], gone[0])
        engine.states.clear()
        engine.convs.clear()  # 像刚重启：内存里什么都没有
        engine.handle_delete(OTHER, HALL, [6])
        await engine.drain()
        self.assertEqual(said(engine.store.load_conversation(sync, 0)), gone)
        self.assertEqual(said(engine.store.load_conversation(HALL, OTHER)), gone)

    async def test_editing_after_leaving_also_fixes_the_group_record(self):
        engine, llm, transport, clock = world()
        sync = three_linked(engine)
        await say(engine, clock, 5, "@小D 周六下午三点集合", channel=HALL, author=21, name="小红")
        await say(engine, clock, 6, "@小D 地点在公园东门", channel=HALL, author=21, name="小红")
        disconnect(engine, HALL)
        edited = msg(5, "@小D 周日上午十点集合", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)
        engine.handle_edit(edited, edited=True)
        await engine.drain()
        first = "#1 小红：@小D 周日上午十点集合 〔@你〕 〔已编辑〕"
        self.assertEqual(said(engine.store.load_conversation(sync, 0)), [first, "#2 小红：@小D 地点在公园东门 〔@你〕"])
        engine.states.clear()
        engine.convs.clear()  # 重启以后再改另一条
        edited = msg(6, "@小D 地点改在公园西门", channel_id=HALL, guild_id=OTHER, author=21, name="小红", mentions_bot=True)
        engine.handle_edit(edited, edited=True)
        await engine.drain()
        both = [first, "#2 小红：@小D 地点改在公园西门 〔@你〕 〔已编辑〕"]
        self.assertEqual(said(engine.store.load_conversation(sync, 0)), both)
        self.assertEqual(said(engine.store.load_conversation(HALL, OTHER)), both)

    async def test_after_a_restart_a_delete_known_only_to_the_group_still_reaches_it(self):
        with patch("dsbot.store.MSGMAP_KEEP", 5):
            engine, llm, transport, clock = world()
            sync = three_linked(engine)
            await say(engine, clock, 5, "@小D 我的手机号是 13800001111", channel=HALL, author=21, name="小红")
            disconnect(engine, HALL)
            for i in range(5):  # 断开以后自己那份又聊了很多：编号映射只留最近 5 个，连通时那条的已经不在了
                await say(engine, clock, 10 + i, f"@小D 断开以后第{i}句", channel=HALL, author=21, name="小红")
            self.assertNotIn(5, msgmap(engine.store, HALL).values())
            engine.states.clear()
            engine.convs.clear()  # 重启：只有组的记录还认得这条消息
            engine.handle_delete(OTHER, HALL, [5])
            await engine.drain()
            self.assertEqual(said(engine.store.load_conversation(sync, 0)), ["#1 小红：（这条消息已删除）"])

    async def test_clearing_after_leaving_also_drops_its_part_from_the_group(self):
        engine, llm, transport, clock = world()
        sync = three_linked(engine)
        await say(engine, clock, 1, "@小D 主服闲聊说的")
        await say(engine, clock, 2, "@小D 别的服大厅说的", channel=HALL, author=21, name="小红")
        await say(engine, clock, 3, "@小D 主服大厅说的", channel=LOBBY, author=12, name="小雨")
        disconnect(engine, HALL)
        self.assertEqual(engine.store.origin_syncs(HALL), [sync])  # 组的记录里还留着它说过的
        engine.reset_channel(HALL)  # 断开以后 /ds 清空记忆
        group = engine.store.load_conversation(sync, 0)
        self.assertEqual(said(group), ["#1 阿明：@小D 主服闲聊说的 〔@你〕", "#3 小雨：@小D 主服大厅说的 〔@你〕"])  # 还在组里的说的不动
        self.assertEqual({t.origin for t in group.turns}, {CHANNEL, LOBBY})
        self.assertEqual(engine.store.load_conversation(HALL, OTHER).turns, [])  # 它自己那份（拿回来的）也清空了
        self.assertEqual(engine.store.origin_syncs(HALL), [])
        await say(engine, clock, 4, "@小D 刚才都说了什么")
        history = "\n".join(text_of(m["content"]) for m in chats(llm)[-1]["messages"][1:-1])
        self.assertIn("主服大厅说的", history)
        self.assertNotIn("别的服大厅说的", history)  # 组里的频道接着聊，也看不到了
        self.assertEqual(engine.store.sync_members(sync), [CHANNEL, LOBBY])  # 照样连通


if __name__ == "__main__":
    unittest.main()
