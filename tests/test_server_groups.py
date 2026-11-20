"""2.7.0：服务器组（/dsowner 服务器组）：连成一组的服务器当成一个服务器来记，共用人设卡和长期记忆，
开了记忆共享的频道也能翻组里别的服务器的记录；设置、预算、人设还是各管各的。"""

import json
import os
import tempfile
import unittest
from collections import deque
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, OWNER, make_engine, msg
from test_dm import OTHER, chats, dm
from test_dm_memory import HALL, setup
from test_memory import ACTIONS, REPLY, chat, is_review, stores, system_text, three_rounds, user_text

from dsbot.groups import GroupError, leftovers, link, plan_link, plan_unlink, recover, unlink
from dsbot.memory import CardStore, MemoryStore
from dsbot.models import ChannelPlace
from dsbot.store import Store

THIRD = 3
ARENA = 800  # 第三个服务器的公开频道
LOBBY = 101  # GUILD 里另一个叫“大厅”的频道


def texts(memory, scope):
    return [(m.no, m.text) for m in memory.entries(scope)]


def card_texts(cards, scope):
    return {uid: cards.get(scope, uid).text for uid in sorted(cards.users(scope)) if cards.get(scope, uid) is not None}


def world():
    store = Store(":memory:")
    cards, memory = stores()
    return store, cards, memory


async def talk(engine, clock, mid, text="@小D 在吗", **kw):
    await engine.handle_message(msg(mid, text, mentions_bot=True, **kw))
    await engine.drain()
    clock.advance(60)


class GroupStore(unittest.TestCase):
    def test_scope_members_and_ids(self):
        store = Store(":memory:")
        self.assertEqual((store.memory_scope(GUILD), store.group_of(GUILD)), (GUILD, None))
        g1, g2 = store.new_group_id(), store.new_group_id()
        self.assertEqual((g1, g2), (-1, -2))  # 负数：和服务器 ID 撞不上
        store.join_group(OTHER, g1, now=1.0)
        store.join_group(GUILD, g1, now=1.0)  # 同一时刻加进来的：也分得出先后
        self.assertEqual((store.memory_scope(GUILD), store.memory_scope(OTHER)), (g1, g1))
        self.assertEqual(store.group_members(g1), [OTHER, GUILD])
        self.assertEqual(store.groups(), {g1: [OTHER, GUILD]})
        epochs = (store.group_epoch(OTHER), store.group_epoch(GUILD), store.group_epoch(THIRD))
        store.start_leaving(OTHER, now=2.0)  # 退出的第一步：记下来，复制完之前还算组里的
        self.assertEqual((store.memory_scope(OTHER), store.leaving()), (g1, [(OTHER, g1)]))
        store.unmap(OTHER)  # 复制完了：不算组里的了
        self.assertEqual((store.memory_scope(OTHER), store.group_members(g1)), (OTHER, [GUILD]))
        store.done_leaving(OTHER)
        self.assertEqual(store.leaving(), [])
        # 组变过几次按服务器记：只有退出的那个变了（组里剩下的照常用组里那份，编号不变），不相干的服务器也不算
        self.assertEqual(
            (store.group_epoch(OTHER), store.group_epoch(GUILD), store.group_epoch(THIRD)), (epochs[0] + 1, epochs[1], epochs[2])
        )

    def test_survives_a_restart_in_the_same_order(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "dsbot.sqlite3")
            store = Store(path)
            g = store.new_group_id()
            for guild in (THIRD, GUILD, OTHER):
                store.join_group(guild, g, now=5.0)  # 时间一样
            store.start_leaving(OTHER, now=5.0)
            store.close()
            again = Store(path)
            self.assertEqual((again.memory_scope(GUILD), again.group_members(g)), (g, [THIRD, GUILD, OTHER]))
            self.assertEqual(again.leaving(), [(OTHER, g)])  # 没做完的退出也还记着
            self.assertEqual(again.new_group_id(), -2)  # 用过的编号不再用
            again.close()


class CardMerge(unittest.TestCase):
    def test_merge_moves_and_keeps_the_other_as_an_old_version(self):
        cards, g = CardStore(":memory:"), -1
        cards.put(OTHER, 11, "阿明", "X旧", via="command", now=10)
        cards.put(OTHER, 11, "阿明", "X卡", via="command", now=11)
        cards.put(g, 11, "阿明", "组里的卡", via="command", now=12)
        cards.put(OTHER, 12, "小雨", "一样", via="command", now=13)
        cards.put(g, 12, "小雨", "一样", via="command", now=14)
        cards.put(OTHER, 13, "小绿", "小绿旧", via="command", now=15)
        cards.put(OTHER, 13, "小绿", "只在这边", via="command", now=16)
        cards.put(OTHER, 14, "小红", "删掉了", via="command", now=17)
        cards.delete(OTHER, 14, now=100)
        self.assertEqual(cards.overlap(OTHER, g), 1)  # 两边都有、内容又不一样的
        self.assertEqual(cards.merge_scope(OTHER, g, now=200), (1, 1))
        self.assertEqual(cards.get(g, 11).text, "组里的卡")  # 两边都有：用组里那张
        self.assertEqual(cards.restore_previous(g, 11), "X卡")  # 这边那张是最新的旧版本：改回上一版就换回来
        moved = cards.get(g, 13)
        self.assertEqual((moved.text, moved.updated_at), ("只在这边", 16))  # 原样搬过去（版本号不变，不用重新附）
        self.assertEqual(cards.restore_previous(g, 13), "小绿旧")  # 旧版本也一起搬过去
        self.assertEqual(cards.deleted_at(g, 14), 100)  # 删卡的记号也带过去（不拿删之前的聊天写回来）
        self.assertEqual(cards.seen_in(g), {11: {OTHER}, 12: {OTHER}, 13: {OTHER}})  # 都记成“在 OTHER 出现过”
        self.assertEqual((cards.count(OTHER), cards.own_texts(OTHER, 11), cards.deleted_at(OTHER, 14)), (0, [], None))
        self.assertEqual(cards.merge_scope(OTHER, g, now=300), (0, 0))  # 并完就空了：再并一次什么也不做

    def test_soft_deleted_card_keeps_its_old_versions(self):
        cards, g = CardStore(":memory:"), -1
        cards.put(OTHER, 11, "阿明", "这边的卡", via="command", now=1)
        cards.put(OTHER, 11, "阿明", "", via="chat", now=2)  # 叫它忘掉：卡没了，旧版本还在
        cards.put(g, 11, "阿明", "组里的卡", via="command", now=3)
        cards.merge_scope(OTHER, g, now=4)
        self.assertEqual(cards.get(g, 11).text, "组里的卡")
        self.assertEqual(cards.restore_previous(g, 11), "这边的卡")  # 还能改回来

    def test_switching_to_the_merged_card_can_be_switched_back(self):
        cards, g = CardStore(":memory:"), -1
        cards.put(g, 11, "阿明", "组卡v1", via="command", now=1)
        cards.put(g, 11, "阿明", "组卡v2", via="command", now=2)
        cards.put(OTHER, 11, "阿明", "这边的卡", via="command", now=3)
        cards.merge_scope(OTHER, g, now=10)
        self.assertEqual(cards.restore_previous(g, 11, now=11), "这边的卡")  # 换成这边那张
        self.assertEqual(cards.restore_previous(g, 11, now=12), "组卡v2")  # 组里原来那张没丢：再改回上一版就换回去
        self.assertEqual(cards.restore_previous(g, 11, now=13), "组卡v1")  # 之后照常一版一版往回改
        self.assertIsNone(cards.restore_previous(g, 11, now=14))

    def test_switching_still_works_after_the_group_card_changed(self):
        cards, g = CardStore(":memory:"), -1
        cards.put(g, 11, "阿明", "组卡v1", via="command", now=1)
        cards.put(OTHER, 11, "阿明", "这边的卡", via="command", now=2)
        cards.merge_scope(OTHER, g, now=10)
        cards.put(g, 11, "阿明", "组卡v2", via="review", now=20)  # 连组以后组里那张又被改过一次
        self.assertEqual(cards.restore_previous(g, 11, now=21), "组卡v1")  # 先照常撤销这次修改
        self.assertEqual(cards.restore_previous(g, 11, now=22), "这边的卡")  # 换成这边那张：组卡v1 不丢
        self.assertEqual(cards.restore_previous(g, 11, now=23), "组卡v1")
        cards.put(OTHER, 12, "小雨", "这边的卡", via="command", now=30)
        cards.put(THIRD, 12, "小雨", "第三服的卡", via="command", now=31)
        cards.put(g, 12, "小雨", "组里的卡", via="command", now=32)
        cards.merge_scope(OTHER, g, now=40)
        cards.merge_scope(THIRD, g, now=41)  # 三个服务器的卡：换哪张，组里那张都还在
        self.assertEqual(cards.restore_previous(g, 12, now=42), "第三服的卡")
        self.assertEqual(cards.restore_previous(g, 12, now=43), "组里的卡")
        self.assertEqual(cards.restore_previous(g, 12, now=44), "这边的卡")
        self.assertEqual(cards.restore_previous(g, 12, now=45), "组里的卡")

    def test_a_card_moving_in_brings_its_own_history(self):
        cards, g = CardStore(":memory:"), -1
        for i in range(1, 7):
            cards.put(g, 11, "阿明", f"组里第{i}版", via="command", now=i)
        cards.put(g, 11, "阿明", "", via="chat", now=10)  # 在组里叫它忘掉了：只剩旧版本
        cards.put(OTHER, 11, "阿明", "这边第1版", via="command", now=20)
        cards.put(OTHER, 11, "阿明", "这边第2版", via="command", now=21)  # 忘掉之后在这边写的：比组里删得晚
        cards.merge_scope(OTHER, g, now=30)
        self.assertEqual(cards.get(g, 11).text, "这边第2版")  # 顶上去成了组里的卡
        self.assertEqual(cards.restore_previous(g, 11, now=31), "这边第1版")  # 它自己的旧版本跟过来了，先回到它们
        self.assertEqual(cards.restore_previous(g, 11, now=32), "组里第6版")  # 再往前是组里忘掉的那张（现在这张存成旧版本）
        self.assertEqual(cards.restore_previous(g, 11, now=33), "这边第1版")  # 换回来
        cards.put(g, 12, "小雨", "组卡", via="command", now=40)
        cards.put(g, 12, "小雨", "", via="chat", now=41)  # 组里忘掉了
        cards.put(THIRD, 12, "小雨", "第三服只写过一次的卡", via="command", now=42)  # 没有旧版本
        cards.merge_scope(THIRD, g, now=50)
        self.assertTrue(cards.swaps_on_restore(g, 12))
        self.assertEqual(cards.restore_previous(g, 12, now=51), "组卡")  # 换成组里忘掉的那张……
        self.assertEqual(cards.restore_previous(g, 12, now=52), "第三服只写过一次的卡")  # ……现在这张没丢，换得回来

    def test_own_forgotten_history_still_walks_back_after_linking(self):
        # 组里还没有他的东西：他自己叫它忘掉的卡的旧版本照原样搬过去，改回上一版照常一版一版往回走（不当成别处来的）
        store, cards, memory = world()
        for i in range(1, 5):
            cards.put(OTHER, 11, "阿明", f"v{i}", via="command", now=i)
        cards.put(OTHER, 11, "阿明", "", via="chat", now=10)
        g = link(store, cards, memory, GUILD, OTHER, now=20).group_id
        walked = [cards.restore_previous(g, 11, now=30 + i) for i in range(5)]
        self.assertEqual(walked, ["v4", "v3", "v2", "v1", None])
        unlink(store, cards, memory, GUILD, now=40)  # 组散了：OTHER 拿回的也照常往回走
        cards.put(OTHER, 12, "小雨", "b1", via="command", now=41)
        cards.put(OTHER, 12, "小雨", "b2", via="command", now=42)
        self.assertEqual([cards.restore_previous(OTHER, 12, now=50 + i) for i in range(2)], ["b1", None])

    def test_a_card_deleted_in_the_group_is_not_brought_back(self):
        cards, g = CardStore(":memory:"), -1
        cards.put(OTHER, 11, "阿明", "这边的卡", via="command", now=1)
        cards.put(g, 11, "阿明", "组里的卡", via="command", now=2)
        cards.delete(g, 11, now=5)  # 在组里清空了自己的卡
        self.assertEqual(cards.overlap(OTHER, g), 1)  # 预览里算进“这边那张存成旧版本”的人
        self.assertEqual(cards.merge_scope(OTHER, g, now=10), (0, 1))
        self.assertIsNone(cards.get(g, 11))  # 不顶上去
        self.assertEqual(cards.restore_previous(g, 11, now=11), "这边的卡")  # 想要的话自己改回来
        cards.put(OTHER, 12, "小雨", "删了以后才写的卡", via="command", now=20)
        cards.delete(g, 12, now=15)
        self.assertEqual(cards.merge_scope(OTHER, g, now=30), (1, 0))  # 组里删得比它早：照常搬过去
        self.assertEqual(cards.get(g, 12).text, "删了以后才写的卡")

    def test_forgotten_cards_do_not_push_out_the_groups_versions(self):
        cards, g = CardStore(":memory:"), -1
        for i in range(1, 7):
            cards.put(OTHER, 11, "阿明", f"这边第{i}版", via="command", now=10 + i)
        cards.put(OTHER, 11, "阿明", "", via="chat", now=20)  # 在这边叫它忘掉了：只剩 5 个旧版本
        for i in range(1, 7):
            cards.put(g, 11, "阿明", f"组里第{i}版", via="command", now=i)  # 组里的旧版本时间更早
        cards.merge_scope(OTHER, g, now=30)
        self.assertEqual(cards.get(g, 11).text, "组里第6版")
        self.assertEqual(cards.restore_previous(g, 11, now=31), "组里第5版")  # 组里自己的旧版本一个没少
        self.assertNotIn("这边第6版", cards.own_texts(g, 11))  # 放不下：那边忘掉的不搬

    def test_forgotten_card_moving_in_stays_behind_the_groups_own_history(self):
        # 加入方叫它忘掉了的卡（只剩旧版本，比组里的还新）搬进来：改回上一版先回到组里自己的，搬来的排在后面
        cards, g = CardStore(":memory:"), -1
        for i in range(1, 4):
            cards.put(g, 11, "阿明", f"组里第{i}版", via="command", now=i)
        cards.put(OTHER, 11, "阿明", "这边第1版", via="command", now=10)
        cards.put(OTHER, 11, "阿明", "这边第2版", via="command", now=11)
        cards.put(OTHER, 11, "阿明", "", via="chat", now=12)
        cards.merge_scope(OTHER, g, now=20)
        self.assertEqual(cards.get(g, 11).text, "组里第3版")
        self.assertEqual(cards.restore_previous(g, 11, now=21), "组里第2版")

    def test_copy_replaces_everything(self):
        cards, g = CardStore(":memory:"), -1
        cards.put(g, 11, "阿明", "旧", via="command", now=1)
        cards.put(g, 11, "阿明", "组里的卡", via="command", now=2)
        cards.put(g, 12, "小雨", "x", via="command", now=3)
        cards.delete(g, 12, now=50)
        cards.put(OTHER, 99, "剩下的", "不该在这里", via="command", now=4)
        self.assertEqual(cards.copy_scope(g, OTHER), 1)
        self.assertEqual((cards.get(OTHER, 11).updated_at, cards.get(OTHER, 99)), (2, None))
        self.assertEqual((cards.restore_previous(OTHER, 11), cards.deleted_at(OTHER, 12)), ("旧", 50))
        self.assertEqual(cards.get(g, 11).text, "组里的卡")  # 组里的不动

    def test_copy_for_some_people_keeps_every_deleted_mark(self):
        cards, g = CardStore(":memory:"), -1
        cards.put(g, 11, "阿明", "以前的", via="command", now=1)
        cards.delete(g, 11, now=30)
        cards.put(g, 11, "阿明", "组里的", via="command", now=31)
        cards.put(g, 12, "小雨", "小雨的", via="command", now=2)
        cards.delete(g, 12, now=40)  # 小雨在组里清空了卡
        cards.put(OTHER, 13, "小绿", "复制到一半的", via="command", now=3)
        self.assertEqual(cards.copy_scope(g, OTHER, users={11}), 1)
        self.assertEqual(card_texts(cards, OTHER), {11: "组里的"})  # 整个换掉
        self.assertEqual(cards.deleted_at(OTHER, 12), 40)  # 删卡的记号总是带过去：整理记忆时不会把卡写回来
        cards.drop_users(g, {11})
        self.assertEqual((cards.get(g, 11), cards.deleted_at(g, 11), cards.deleted_at(g, 12)), (None, 30, 40))  # 拿掉卡，记号留着


class MemoryMerge(unittest.TestCase):
    def test_into_an_empty_group_keeps_the_numbers(self):
        memory = MemoryStore(":memory:")
        for text in ("一", "二", "三"):
            memory.add(OTHER, text, via="command")
        memory.delete(OTHER, 2)
        self.assertEqual(memory.merge_scope(OTHER, -1), 2)
        self.assertEqual(texts(memory, -1), [(1, "一"), (3, "三")])
        self.assertEqual(memory.add(-1, "四", via="command"), 4)  # 删掉的编号不再用
        self.assertEqual(memory.count(OTHER), 0)

    def test_empty_base_keeps_the_joining_numbers(self):
        store, cards, memory = world()
        for text in ("一", "二", "三", "四"):
            memory.add(GUILD, text, via="command")
        memory.delete(GUILD, 1)
        memory.delete(GUILD, 3)
        g = link(store, cards, memory, GUILD, OTHER, now=1).group_id  # OTHER（组里原来的）什么都没记过
        self.assertEqual(texts(memory, g), [(2, "二"), (4, "四")])

    def test_renumbers_and_skips_duplicates(self):
        memory = MemoryStore(":memory:")
        for scope, text in ((-1, "a"), (-1, "b"), (OTHER, "b"), (OTHER, "c")):
            memory.add(scope, text, via="command")
        self.assertEqual(memory.merge_preview(OTHER, -1), (1, 3))
        self.assertEqual(memory.merge_scope(OTHER, -1), 1)
        self.assertEqual(texts(memory, -1), [(1, "a"), (2, "b"), (3, "c")])
        self.assertEqual(memory.add(-1, "d", via="command"), 4)
        self.assertEqual(memory.merge_scope(OTHER, -1), 0)

    def test_copy_keeps_the_numbers(self):
        memory = MemoryStore(":memory:")
        memory.add(-1, "a", via="command")
        memory.add(-1, "b", via="command")
        memory.delete(-1, 1)
        memory.add(OTHER, "剩下的", via="command")
        self.assertEqual(memory.copy_scope(-1, OTHER), 1)
        self.assertEqual(texts(memory, OTHER), [(2, "b")])
        self.assertEqual(memory.add(OTHER, "c", via="command"), 3)
        self.assertEqual(texts(memory, -1), [(2, "b")])


class LinkUnlink(unittest.TestCase):
    def test_two_servers_make_a_group_and_the_target_wins(self):
        store, cards, memory = world()
        memory.add(OTHER, "B服每周五开黑", via="command")
        memory.add(GUILD, "A服的梗", via="command")
        cards.put(OTHER, 11, "阿明", "B服的卡", via="command", now=1)
        cards.put(GUILD, 11, "阿明", "A服的卡", via="command", now=2)
        cards.put(GUILD, 12, "小雨", "小雨的卡", via="command", now=3)
        plan = plan_link(store, cards, memory, GUILD, OTHER)
        self.assertEqual((plan.joining, plan.base, plan.group_id), (GUILD, OTHER, None))
        self.assertEqual((plan.memories_added, plan.memory_total, plan.cards_kept), (1, 2, 1))
        self.assertIsNone(store.group_of(GUILD))  # 只是看看：什么都没改
        result = link(store, cards, memory, GUILD, OTHER, now=100)
        g = result.group_id
        self.assertTrue(result.created)
        self.assertEqual(result.members, [OTHER, GUILD])
        self.assertEqual((store.memory_scope(GUILD), store.memory_scope(OTHER)), (g, g))
        self.assertEqual(texts(memory, g), [(1, "B服每周五开黑"), (2, "A服的梗")])  # “连到”的那个服务器的编号不变
        self.assertEqual(card_texts(cards, g), {11: "B服的卡", 12: "小雨的卡"})
        self.assertEqual(cards.seen_in(g), {11: {OTHER, GUILD}, 12: {GUILD}})
        self.assertEqual(cards.restore_previous(g, 11), "A服的卡")
        self.assertEqual((result.cards_moved, result.cards_kept, result.memories_added, result.memory_total), (1, 1, 1, 2))
        for guild in (GUILD, OTHER):
            self.assertEqual((memory.count(guild), cards.count(guild)), (0, 0))  # 都记在组里了

    def test_joining_an_existing_group_and_what_cannot_be_linked(self):
        store, cards, memory = world()
        g = link(store, cards, memory, GUILD, OTHER, now=1).group_id
        joined = link(store, cards, memory, THIRD, GUILD, now=2)  # 连到组里的服务器：加进那个组
        self.assertEqual((joined.group_id, joined.members, joined.created), (g, [OTHER, GUILD, THIRD], False))
        with self.assertRaisesRegex(GroupError, "同一个服务器"):
            link(store, cards, memory, GUILD, GUILD, now=3)
        with self.assertRaisesRegex(GroupError, "已经在同一组"):
            link(store, cards, memory, THIRD, OTHER, now=3)
        h = link(store, cards, memory, 4, 5, now=4).group_id
        with self.assertRaisesRegex(GroupError, "先让其中一个退出"):
            link(store, cards, memory, 4, GUILD, now=5)
        result = link(store, cards, memory, 4, 6, now=6)  # 在组里的“连到”不在组里的：后者加进前者的组
        self.assertEqual((result.group_id, result.joining), (h, 6))

    def test_leaving_takes_its_own_peoples_cards(self):
        store, cards, memory = world()
        memory.add(OTHER, "组里的事", via="command")
        cards.put(OTHER, 11, "阿明", "阿明的卡", via="command", now=1)  # 在 OTHER
        cards.put(GUILD, 12, "小雨", "小雨的卡", via="command", now=2)  # 在 GUILD
        cards.put(THIRD, 13, "小绿", "小绿的卡", via="command", now=3)  # 在 THIRD
        g = link(store, cards, memory, GUILD, OTHER, now=4).group_id
        link(store, cards, memory, THIRD, GUILD, now=5)
        cards.note_seen(g, 11, THIRD, "阿明")  # 阿明也在 THIRD 跟它聊过
        plan = plan_unlink(store, cards, memory, THIRD)
        self.assertEqual((plan.group_id, plan.rest, plan.cards_taken, plan.cards_moved_out, plan.memories), (g, [OTHER, GUILD], 2, 1, 1))
        left = unlink(store, cards, memory, THIRD, now=6)
        self.assertEqual((left.members, left.dissolved, left.cards_copied, left.cards_moved_out), ([OTHER, GUILD], None, 2, 1))
        self.assertEqual(store.memory_scope(THIRD), THIRD)
        self.assertEqual(card_texts(cards, THIRD), {11: "阿明的卡", 13: "小绿的卡"})  # 只带走在 THIRD 出现过的人的
        self.assertEqual(card_texts(cards, g), {11: "阿明的卡", 12: "小雨的卡"})  # 只在 THIRD 的小绿跟着走了
        self.assertEqual(texts(memory, THIRD), [(1, "组里的事")])
        self.assertNotIn(THIRD, set().union(*cards.seen_in(g).values()))
        last = unlink(store, cards, memory, GUILD, now=7)
        self.assertEqual((last.members, last.dissolved), ([], OTHER))  # 只剩一个：组散了
        self.assertEqual(store.groups(), {})
        self.assertEqual(card_texts(cards, GUILD), {12: "小雨的卡"})
        self.assertEqual(card_texts(cards, OTHER), {11: "阿明的卡"})  # 剩下的那个留着组里剩下的
        for guild in (GUILD, OTHER):
            self.assertEqual((store.memory_scope(guild), texts(memory, guild)), (guild, [(1, "组里的事")]))
        self.assertEqual((memory.count(g), cards.count(g), cards.seen_in(g)), (0, 0, {}))  # 散掉的组不留东西
        with self.assertRaisesRegex(GroupError, "不在任何组里"):
            unlink(store, cards, memory, GUILD, now=8)

    def test_link_rerun_after_a_crash_finishes_the_job(self):
        store, cards, memory = world()
        memory.add(OTHER, "B服的事", via="command")
        cards.put(OTHER, 11, "阿明", "B服的卡", via="command", now=1)
        memory.add(GUILD, "A服的事", via="command")
        g = store.new_group_id()
        store.join_group(OTHER, g, now=2)  # 新建组时记进了组，还没把 B服的记忆搬进去就断了
        self.assertEqual(memory.count(g), 0)
        result = link(store, cards, memory, GUILD, OTHER, now=3)  # 再执行一次同样的命令
        self.assertEqual(result.group_id, g)
        self.assertEqual(texts(memory, g), [(1, "B服的事"), (2, "A服的事")])
        self.assertEqual((cards.get(g, 11).text, memory.count(OTHER)), ("B服的卡", 0))

    def test_interrupted_join_does_not_demote_newer_cards(self):
        store, cards, memory = world()
        g = link(store, cards, memory, OTHER, 4, now=1).group_id
        cards.put(GUILD, 11, "阿明", "旧卡", via="command", now=2)
        store.join_group(GUILD, g, now=3)  # 先记进组……还没把它原来的并进来就断了
        cards.put(g, 11, "阿明", "进组之后写的", via="chat", now=4)  # 这期间写的已经记在组里
        recover(store, cards, memory, now=5)  # 下次执行组命令、重启时
        self.assertEqual(cards.get(g, 11).text, "进组之后写的")  # 新的那张还是现在这张
        self.assertEqual(cards.restore_previous(g, 11), "旧卡")
        self.assertEqual(cards.count(GUILD), 0)

    def test_interrupted_leave_does_not_bring_deleted_things_back(self):
        store, cards, memory = world()
        memory.add(OTHER, "留着的", via="command")
        memory.add(OTHER, "要删的", via="command")
        cards.put(OTHER, 11, "阿明", "阿明的卡", via="command", now=1)
        g = link(store, cards, memory, GUILD, OTHER, now=2).group_id
        link(store, cards, memory, THIRD, OTHER, now=3)
        cards.note_seen(g, 11, THIRD, "阿明")
        store.start_leaving(THIRD, now=4)  # THIRD 退出：刚记成“正在退出”、复制到一半就断了（它还在组里）
        cards.copy_scope(g, THIRD, users={11})
        memory.copy_scope(g, THIRD)
        self.assertEqual(store.memory_scope(THIRD), g)  # 照常用组里那份
        cards.delete(g, 11)  # 这期间：阿明在组里清空了自己的卡，管理员删了一条
        memory.delete(g, 2)
        recover(store, cards, memory, now=5)
        self.assertEqual((store.leaving(), store.memory_scope(THIRD)), ([], THIRD))
        self.assertIsNone(cards.get(g, 11))  # 组里不会被副本“复活”
        self.assertIsNone(cards.get(THIRD, 11))  # 退出的那边也不会
        self.assertEqual(texts(memory, g), [(1, "留着的")])
        self.assertEqual(texts(memory, THIRD), [(1, "留着的")])  # 副本照现在的组里重新拿

    def test_after_switching_over_its_own_new_things_are_kept(self):
        store, cards, memory = world()
        memory.add(OTHER, "组里的事", via="command")
        g = link(store, cards, memory, GUILD, OTHER, now=1).group_id
        link(store, cards, memory, THIRD, OTHER, now=2)
        store.start_leaving(THIRD, now=3)  # 复制完、换成自己那份了，还没收尾就断了
        memory.copy_scope(g, THIRD)
        cards.copy_scope(g, THIRD, users=set())
        store.unmap(THIRD)
        memory.add(THIRD, "退出以后自己记的", via="chat")  # 这之后它已经在用自己那份
        recover(store, cards, memory, now=4)
        self.assertEqual(store.leaving(), [])
        self.assertEqual(texts(memory, THIRD), [(1, "组里的事"), (2, "退出以后自己记的")])  # 不会被重新复制盖掉

    def test_linking_while_a_leave_is_pending_does_not_sweep_its_copy(self):
        store, cards, memory = world()
        cards.put(OTHER, 11, "阿明", "阿明的卡", via="command", now=1)
        g = link(store, cards, memory, GUILD, OTHER, now=2).group_id
        link(store, cards, memory, THIRD, OTHER, now=3)
        cards.note_seen(g, 11, THIRD, "阿明")
        store.start_leaving(THIRD, now=4)  # 复制到一半就断了
        cards.copy_scope(g, THIRD, users={11})
        cards.delete(g, 11)  # 阿明在组里清空了卡
        link(store, cards, memory, 4, OTHER, now=5)  # 这时候又有服务器连进来（会扫一遍组里的服务器）
        self.assertIsNone(cards.get(g, 11))  # 复制到一半的那份不会被当成剩下的扫回组里

    def test_deleted_cards_stay_deleted_after_leaving_and_breaking_up(self):
        store, cards, memory = world()
        cards.put(OTHER, 16, "小白", "小白的卡", via="command", now=1)
        cards.delete(OTHER, 16, now=10)  # 连组之前在 OTHER 删过
        cards.put(GUILD, 12, "小雨", "小雨的卡", via="command", now=2)
        g = link(store, cards, memory, GUILD, OTHER, now=3).group_id
        link(store, cards, memory, THIRD, OTHER, now=4)
        cards.delete(g, 12, now=20)  # 在组里删的
        unlink(store, cards, memory, THIRD, now=5)
        unlink(store, cards, memory, GUILD, now=6)  # 组散了
        for guild in (THIRD, GUILD, OTHER):
            self.assertEqual((cards.deleted_at(guild, 12), cards.deleted_at(guild, 16)), (20, 10))

    def test_recover_breaks_up_a_lonely_group_and_keeps_leftovers(self):
        store, cards, memory = world()
        memory.add(OTHER, "B服的事", via="command")
        g = store.new_group_id()
        store.join_group(OTHER, g, now=1)  # 连到一半：组里只有一个服务器
        memory.merge_scope(OTHER, g)
        dead = store.new_group_id()
        memory.add(dead, "散掉的组剩下的", via="command")  # 文件之间对不上（比如只还原了 memory.sqlite3）
        recover(store, cards, memory, now=2)
        self.assertEqual((store.groups(), store.memory_scope(OTHER)), ({}, OTHER))
        self.assertEqual(texts(memory, OTHER), [(1, "B服的事")])
        self.assertEqual(memory.count(g), 0)  # 散掉的组：退出时最后一步删掉
        self.assertEqual(memory.count(dead), 1)  # 对不上的：不删（可能是唯一的一份），报出来
        self.assertEqual(leftovers(store, cards, memory), {dead: (0, 1)})

    def test_group_operations_wait_for_the_disk(self):
        store, cards, memory = world()
        seen = []
        real = cards.merge_scope

        def spy(*args, **kw):  # 连组做到一半时：三个库都是每次提交都写进磁盘（FULL = 2）
            seen.append([db.execute("PRAGMA synchronous").fetchone()[0] for db in (store.db, cards.db, memory.db)])
            return real(*args, **kw)

        cards.merge_scope = spy
        link(store, cards, memory, GUILD, OTHER, now=1)
        link(store, cards, memory, THIRD, OTHER, now=2)
        real_copy = cards.copy_scope

        def spy_copy(*args, **kw):  # 退出、接着做完时也一样
            seen.append([db.execute("PRAGMA synchronous").fetchone()[0] for db in (store.db, cards.db, memory.db)])
            return real_copy(*args, **kw)

        cards.copy_scope = spy_copy
        unlink(store, cards, memory, THIRD, now=3)
        store.start_leaving(GUILD, now=4)  # 退出记下了、还没做完
        recover(store, cards, memory, now=5)
        self.assertGreaterEqual(len(seen), 5)
        self.assertTrue(all(levels == [2, 2, 2] for levels in seen), seen)
        after = [db.execute("PRAGMA synchronous").fetchone()[0] for db in (store.db, cards.db, memory.db)]
        self.assertEqual(after, [1, 1, 1])  # 做完改回平时的 NORMAL
        self.assertEqual(store.groups(), {})  # 只剩一个服务器：组散了
        link(store, cards, memory, GUILD, OTHER, now=6)
        cards.copy_scope = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("磁盘满了"))
        store.start_leaving(OTHER, now=7)
        with self.assertRaises(RuntimeError):
            recover(store, cards, memory, now=8)
        after = [db.execute("PRAGMA synchronous").fetchone()[0] for db in (store.db, cards.db, memory.db)]
        self.assertEqual(after, [1, 1, 1])  # 出错了也改回去

    def test_breaking_up_drops_the_group_copy_before_finishing(self):
        # 散组时：组里那份删完了才算退出做完（顺序反了的话，中间断掉，那份就永远留着、每次都报“残留”）
        store, cards, memory = world()
        memory.add(OTHER, "B服的事", via="command")
        cards.put(OTHER, 11, "阿明", "阿明的卡", via="command", now=1)
        g = link(store, cards, memory, GUILD, OTHER, now=2).group_id
        real = store.done_leaving
        states = []

        def spy(guild_id):
            states.append((guild_id, memory.count(g), cards.count(g)))
            return real(guild_id)

        store.done_leaving = spy
        unlink(store, cards, memory, GUILD, now=3)
        self.assertEqual(states[-1][1:], (0, 0))
        self.assertEqual(leftovers(store, cards, memory), {})

    def test_restoring_only_some_files_loses_nothing(self):
        # 组散了以后只把 cards / memory 换回连组期间的备份：启动时不会把备份里组的那份当成残留删掉
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            self._restore_some(d)

    def _restore_some(self, d):
        paths = [os.path.join(d, f) for f in ("dsbot.sqlite3", "cards.sqlite3", "memory.sqlite3")]
        store, cards, memory = Store(paths[0]), CardStore(paths[1]), MemoryStore(paths[2])
        memory.add(OTHER, "B服的事", via="command")
        cards.put(GUILD, 11, "阿明", "阿明的卡", via="command", now=1)
        g = link(store, cards, memory, GUILD, OTHER, now=2).group_id
        backup = {}
        for db, path in ((cards, paths[1]), (memory, paths[2])):
            db.db.execute("PRAGMA wal_checkpoint(FULL)")
            with open(path, "rb") as f:
                backup[path] = f.read()
        unlink(store, cards, memory, GUILD, now=3)  # 组散了
        cards.close()
        memory.close()
        for path, data in backup.items():  # 只还原了这两个文件
            for suffix in ("-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)
            with open(path, "wb") as f:
                f.write(data)
        cards, memory = CardStore(paths[1]), MemoryStore(paths[2])
        recover(store, cards, memory, now=4)  # 启动时
        self.assertEqual(texts(memory, g), [(1, "B服的事")])  # 还在
        self.assertEqual(cards.get(g, 11).text, "阿明的卡")
        self.assertEqual(leftovers(store, cards, memory), {g: (1, 1)})  # 报出来给 bot 主人看
        self.assertEqual(link(store, cards, memory, GUILD, THIRD, now=5).group_id, g - 1)  # 新组不会认走它
        for db in (store, cards, memory):
            db.close()

    def test_memories_remember_which_server_wrote_them(self):
        store, cards, memory = world()
        memory.add(OTHER, "B服的事", via="command")  # 旧数据：不知道是谁记的，并进组时算 B服的
        memory.add(GUILD, "A服的事", via="command", origin=GUILD)
        g = link(store, cards, memory, GUILD, OTHER, now=1).group_id
        self.assertEqual([(m.text, m.origin) for m in memory.entries(g)], [("B服的事", OTHER), ("A服的事", GUILD)])
        unlink(store, cards, memory, GUILD, now=2)  # 组散了：两边各带走一份（没在组里时整理都能改），照样记着是谁记的
        self.assertEqual([(m.text, m.origin) for m in memory.entries(GUILD)], [("B服的事", OTHER), ("A服的事", GUILD)])
        self.assertEqual([(m.text, m.origin) for m in memory.entries(OTHER)], [("B服的事", OTHER), ("A服的事", GUILD)])
        g2 = link(store, cards, memory, GUILD, THIRD, now=3).group_id  # 带走的再连进别的组：记的那个服务器不在组里，算 GUILD 记的
        self.assertEqual({m.origin for m in memory.entries(g2)}, {GUILD})

    def test_linking_again_keeps_who_wrote_what(self):
        # 组散了又连回来：各是各的（不会全算成“连到”那边记的，这边自己记的整理时照样改得了）
        store, cards, memory = world()
        memory.add(OTHER, "别的服的约定", via="command", origin=OTHER)
        memory.add(GUILD, "测试服过时的梗", via="command", origin=GUILD)
        link(store, cards, memory, GUILD, OTHER, now=1)
        unlink(store, cards, memory, GUILD, now=2)
        g = link(store, cards, memory, GUILD, OTHER, now=3).group_id
        self.assertEqual([(m.text, m.origin) for m in memory.entries(g)], [("别的服的约定", OTHER), ("测试服过时的梗", GUILD)])
        # 三个服务器的组里有两个退出、再连成一组：第三个服务器记的算带进来的那个服务器的，这两个各是各的
        memory.add(THIRD, "第三服的事", via="command", origin=THIRD)
        link(store, cards, memory, THIRD, OTHER, now=4)
        self.assertEqual(memory.get(g, 3).origin, THIRD)
        unlink(store, cards, memory, GUILD, now=5)
        unlink(store, cards, memory, OTHER, now=6)  # 组散了：OTHER、THIRD 各拿一份
        g2 = link(store, cards, memory, GUILD, OTHER, now=7).group_id
        self.assertEqual(
            [(m.text, m.origin) for m in memory.entries(g2)], [("别的服的约定", OTHER), ("测试服过时的梗", GUILD), ("第三服的事", OTHER)]
        )

    def test_relinking_after_an_interrupted_link_keeps_who_wrote_what(self):
        # 组散了以后再连，连到一半断了（比如数据库被别的程序占着）：接着做完时把半截的组散掉，谁记的照样记着，
        # 再连一次和没断过一样
        import sqlite3

        store, cards, memory = world()
        memory.add(OTHER, "别的服的约定", via="command", origin=OTHER)
        memory.add(GUILD, "测试服过时的梗", via="command", origin=GUILD)
        link(store, cards, memory, GUILD, OTHER, now=1)
        unlink(store, cards, memory, GUILD, now=2)  # 组散了：OTHER 那份里也有 GUILD 记的
        real = memory.merge_scope

        def locked(*args, **kw):
            raise sqlite3.OperationalError("database is locked")

        memory.merge_scope = locked
        with self.assertRaises(sqlite3.OperationalError):
            link(store, cards, memory, GUILD, OTHER, now=3)  # OTHER 记进了新组，它的记忆还没搬进去就断了
        memory.merge_scope = real
        recover(store, cards, memory, now=4)  # 下次执行组命令、重启时：只有一个服务器的组散掉
        self.assertEqual(store.groups(), {})
        mine = [("别的服的约定", OTHER), ("测试服过时的梗", GUILD)]
        self.assertEqual([(m.text, m.origin) for m in memory.entries(OTHER)], mine)
        g = link(store, cards, memory, GUILD, OTHER, now=5).group_id
        self.assertEqual([(m.text, m.origin) for m in memory.entries(g)], mine)

    def test_a_new_group_cut_off_at_the_joiner_keeps_the_bringer_rule(self):
        # 新建组连到一半（两个服务器都记进了组，加入方的记忆还没并进去）就断了：接着做完时组照常留着，加入方带进来的、
        # 组外的服务器记的条目算加入方的（只有它整理时改得了）
        import sqlite3

        store, cards, memory = world()
        memory.add(THIRD, "第三服的约定", via="command", origin=THIRD)
        link(store, cards, memory, GUILD, THIRD, now=1)
        unlink(store, cards, memory, GUILD, now=2)  # GUILD 带走的那份里有 THIRD 记的
        memory.add(OTHER, "别的服的约定", via="command", origin=OTHER)
        real = memory.merge_scope

        def locked(src, dst, **kw):
            if src == GUILD:  # “连到”的那个（OTHER）已经并进去了
                raise sqlite3.OperationalError("database is locked")
            return real(src, dst, **kw)

        memory.merge_scope = locked
        with self.assertRaises(sqlite3.OperationalError):
            link(store, cards, memory, GUILD, OTHER, now=3)
        memory.merge_scope = real
        g = store.group_of(GUILD)
        self.assertEqual(store.group_members(g), [OTHER, GUILD])
        recover(store, cards, memory, now=4)
        self.assertEqual(store.group_members(g), [OTHER, GUILD])  # 两个服务器：组照常留着
        self.assertEqual([(m.text, m.origin) for m in memory.entries(g)], [("别的服的约定", OTHER), ("第三服的约定", GUILD)])
        self.assertEqual(memory.count(GUILD), 0)

    def test_entries_from_servers_outside_the_group_count_as_the_bringers(self):
        # 带进组的条目，记的那个服务器不在组里：算带进来的那个服务器的（只有它整理时改得了）。组里已经有记忆、
        # 重新编号并进去时也一样；中途断了、接着做完时扫进来的也一样
        store, cards, memory = world()
        memory.add(OTHER, "B的约定", via="command", origin=OTHER)
        g = link(store, cards, memory, GUILD, OTHER, now=1).group_id
        memory.add(THIRD, "第四个服务器记的", via="command", origin=4)  # THIRD 从别的组带出来的
        link(store, cards, memory, THIRD, OTHER, now=2)
        self.assertEqual((memory.get(g, 2).text, memory.get(g, 2).origin), ("第四个服务器记的", THIRD))
        memory.add(5, "第六个服务器记的", via="command", origin=6)
        store.join_group(5, g, now=3)  # 第四个加进来：先记进组，还没把它原来的并进来就断了
        recover(store, cards, memory, now=4)
        self.assertEqual((memory.get(g, 3).text, memory.get(g, 3).origin), ("第六个服务器记的", 5))
        memory.add(GUILD, "要加进来的那个记的", via="command", origin=7)  # 组里服务器自己名下剩下的（中途断了）
        memory.add(GUILD, "不相干的服务器记的", via="command", origin=8)
        link(store, cards, memory, 7, OTHER, now=5)  # 连组时先扫一遍：马上要加进来的那个也算组里的
        self.assertEqual([(m.text, m.origin) for m in memory.entries(g)][3:], [("要加进来的那个记的", 7), ("不相干的服务器记的", GUILD)])

    def test_edits_made_apart_belong_to_the_server_that_made_them(self):
        # 组散了以后自己整理时改了别的服务器记的条目：再连回来，改过的那条算这边的；那边改了自己记的，这边没改的
        # 旧版本还算那边的
        store, cards, memory = world()
        memory.add(OTHER, "周五晚上开黑", via="command", origin=OTHER)
        memory.add(OTHER, "B的约定", via="command", origin=OTHER)
        link(store, cards, memory, GUILD, OTHER, now=1)
        unlink(store, cards, memory, GUILD, now=2)
        memory.edit(GUILD, 1, "周五晚上开黑，测试服这边改到周六晚上", via="review")
        memory.edit(OTHER, 2, "B的约定（改过）", via="review")
        g = link(store, cards, memory, GUILD, OTHER, now=3).group_id
        self.assertEqual(
            [(m.text, m.origin) for m in memory.entries(g)],
            [("周五晚上开黑", OTHER), ("B的约定（改过）", OTHER), ("周五晚上开黑，测试服这边改到周六晚上", GUILD), ("B的约定", OTHER)],
        )
        memory.edit(g, 4, "B的约定（又改了）", via="chat")  # 在组里改：照旧算原来记的那个服务器的
        self.assertEqual(memory.get(g, 4).origin, OTHER)

    def test_pending_leaver_memory_copy_is_not_swept_back(self):
        # 退出复制到一半断了：它名下那份半截的副本不会被扫回组里（组里在这期间删掉的那条就不会回来）
        store, cards, memory = world()
        memory.add(OTHER, "留着的", via="command")
        memory.add(OTHER, "要删的", via="command")
        g = link(store, cards, memory, GUILD, OTHER, now=2).group_id
        link(store, cards, memory, THIRD, OTHER, now=3)
        store.start_leaving(THIRD, now=4)  # 复制到一半就断了（还算组里的）
        memory.copy_scope(g, THIRD)
        memory.delete(g, 2)  # 这期间管理员在组里删了第 2 条
        link(store, cards, memory, 4, OTHER, now=5)  # 又有服务器连进来：先扫一遍组里服务器自己名下的
        self.assertEqual(texts(memory, g), [(1, "留着的")])

    def test_interrupted_leave_does_not_take_a_card_rewritten_elsewhere(self):
        # 退出没做完时，他清空了卡、又只在别的服务器重写了一张：不再算在退出的那个服务器出现过，它不带走新卡
        store, cards, memory = world()
        cards.put(OTHER, 11, "阿明", "阿明的卡", via="command", now=1)
        g = link(store, cards, memory, GUILD, OTHER, now=2).group_id
        link(store, cards, memory, THIRD, OTHER, now=3)
        cards.note_seen(g, 11, THIRD, "阿明")
        store.start_leaving(THIRD, now=4)  # 复制到一半就断了（还算组里的）
        cards.copy_scope(g, THIRD, users={11})
        cards.delete(g, 11, now=5)  # 清空了卡（在哪儿出现过的记录也一起去掉）
        cards.put(g, 11, "阿明", "在别的服重写的卡", via="command", now=6)
        cards.note_seen(g, 11, OTHER, "阿明")
        recover(store, cards, memory, now=7)
        self.assertIsNone(cards.get(THIRD, 11))

    def test_old_memory_files_get_the_origin_column(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = os.path.join(d, "memory.sqlite3")
            import sqlite3

            old = sqlite3.connect(path)
            old.executescript(
                "CREATE TABLE memories (guild_id INTEGER NOT NULL, no INTEGER NOT NULL, text TEXT NOT NULL, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL, via TEXT NOT NULL, PRIMARY KEY (guild_id, no));"
                "CREATE TABLE memory_seq (guild_id INTEGER PRIMARY KEY, next_no INTEGER NOT NULL);"
                "INSERT INTO memories VALUES (1, 1, '旧的', 0, 0, 'chat');"
                "INSERT INTO memory_seq VALUES (1, 2);"
            )
            old.commit()
            old.close()
            memory = MemoryStore(path)
            self.assertEqual([(m.text, m.origin) for m in memory.entries(1)], [("旧的", None)])
            memory.add(1, "新的", via="chat", origin=1)
            self.assertEqual(memory.get(1, 2).origin, 1)
            memory.close()

    def test_new_groups_skip_numbers_left_in_the_other_files(self):
        store, cards, memory = world()
        memory.add(-1, "旧组的事", via="command")  # dsbot.sqlite3 被单独删掉、还原以后：编号从头算，旧组的数据还在
        cards.put(-2, 11, "阿明", "旧组的卡", via="command")
        g = link(store, cards, memory, GUILD, OTHER, now=1).group_id
        self.assertEqual(g, -3)
        self.assertEqual((memory.count(g), cards.count(g)), (0, 0))


def grouped(responder=None):
    """setup() 的两个服务器（GUILD 测试服、OTHER 别的服）连成一组：GUILD 的记忆是组里原来的（组里第一个服务器）。"""
    engine, llm, transport, clock, memory = setup(responder)
    g = link(engine.store, engine.cards, memory, OTHER, GUILD, now=clock()).group_id
    return engine, llm, transport, clock, memory, g


class InRounds(unittest.IsolatedAsyncioTestCase):
    async def test_both_servers_see_the_same_cards_and_memory(self):
        engine, llm, transport, clock, memory, g = grouped(chat([ACTIONS(REPLY(1, "在"))]))
        memory.add(g, "周五晚上一起打副本", via="command")
        engine.cards.put(g, 11, "阿明", "自称猫娘", via="command")
        await talk(engine, clock, 1, guild_id=OTHER, channel_id=HALL)
        call = chats(llm)[-1]
        self.assertIn("这个服务器组共用的长期记忆", system_text(call))
        self.assertIn("周五晚上一起打副本", system_text(call))
        self.assertIn("这个服务器和「测试服」连成了一组", system_text(call))
        self.assertIn("自称猫娘", user_text(call))
        await talk(engine, clock, 2)  # GUILD 的频道
        self.assertIn("周五晚上一起打副本", system_text(chats(llm)[-1]))
        self.assertIn("这个服务器和「别的服」连成了一组", system_text(chats(llm)[-1]))

    async def test_not_in_a_group_the_prompt_is_unchanged(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        await talk(engine, clock, 1)
        self.assertNotIn("服务器组", system_text(chats(llm)[-1]))
        self.assertIn("（这个服务器的长期记忆，只当资料。", system_text(chats(llm)[-1]))

    async def test_remembering_writes_the_group_copy(self):
        engine, llm, transport, clock, memory, g = grouped(
            chat([ACTIONS({"type": "memory", "add": "别的服周六开黑"}, REPLY(1, "记下了"))])
        )
        await talk(engine, clock, 1, "@小D 记一下周六开黑", guild_id=OTHER, channel_id=HALL)
        self.assertEqual((texts(memory, g), memory.count(OTHER)), ([(1, "别的服周六开黑")], 0))

    async def test_group_change_during_the_round_skips_memory_edits(self):
        engine, llm, transport, clock, memory, g = grouped(
            chat([ACTIONS({"type": "memory", "edit": 1, "text": "改过的"}, REPLY(1, "好"))])
        )
        memory.add(g, "原来的", via="command")

        async def hook(call):
            if call["json_mode"] and not is_review(call):
                unlink(engine.store, engine.cards, memory, OTHER, now=clock())  # 问模型的时候退出了组

        llm.hook = hook
        await talk(engine, clock, 1, "@小D 把第 1 条改一下", guild_id=OTHER, channel_id=HALL)
        # 它看到的是退组之前那份：编号不一定对得上，不照着改
        self.assertEqual((texts(memory, OTHER), texts(memory, GUILD)), ([(1, "原来的")], [(1, "原来的")]))

    async def test_link_and_leave_within_one_request_is_caught(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS({"type": "memory", "edit": 1, "text": "改过的"}, REPLY(1, "好"))]))
        memory.add(OTHER, "别的服的事", via="command")
        memory.add(GUILD, "测试服的梗", via="command")

        async def hook(call):
            if call["json_mode"] and not is_review(call):  # 问模型的时候连进了组、又退了出来：记在哪一样，编号却变了
                link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())
                unlink(engine.store, engine.cards, memory, GUILD, now=clock())

        llm.hook = hook
        await talk(engine, clock, 1, "@小D 把第 1 条改一下")
        self.assertNotIn("改过的", json.dumps(texts(memory, GUILD), ensure_ascii=False))

    async def test_other_servers_linking_does_not_disturb_this_one(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS({"type": "memory", "add": "周五晚上开黑"}, REPLY(1, "记下了"))]))

        async def hook(call):
            if call["json_mode"] and not is_review(call):
                link(engine.store, engine.cards, memory, THIRD, 4, now=clock())  # 不相干的两个服务器连成一组

        llm.hook = hook
        await talk(engine, clock, 1, "@小D 记一下周五开黑")
        self.assertEqual(texts(memory, GUILD), [(1, "周五晚上开黑")])
        self.assertIn("📝", [r[2] for r in transport.reactions])

    async def test_review_asks_again_for_the_new_group(self):
        review = json.dumps({"cards": [], "memory": [{"add": "整理出来的"}]}, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))], review=review))
        state = {"linked": False}

        async def hook(call):
            if is_review(call) and not state["linked"]:
                state["linked"] = True
                link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())  # 整理的时候连成了一组

        llm.hook = hook
        await three_rounds(engine, clock)
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        g = engine.store.group_of(GUILD)
        self.assertTrue(state["linked"])
        # 第一次看到的是连组之前的那份：不照着写，马上照新的组再问一次（压缩前挑出来的那批也不会白白跳过）
        self.assertEqual((memory.count(GUILD), texts(memory, g)), (0, [(1, "整理出来的")]))
        reviews = [c for c in llm.calls if is_review(c)]
        self.assertEqual(len(reviews), 2)
        self.assertNotIn("这段聊天在服务器「测试服」里", system_text(reviews[0]))
        self.assertIn("这段聊天在服务器「测试服」里", system_text(reviews[1]))

    async def test_others_joining_the_group_keep_this_rounds_memory(self):
        engine, llm, transport, clock, memory, g = grouped(chat([ACTIONS({"type": "memory", "add": "周六晚上开黑"}, REPLY(1, "记下了"))]))
        state = {}

        async def hook(call):
            if call["json_mode"] and not is_review(call) and not state:
                state["joined"] = link(engine.store, engine.cards, memory, THIRD, GUILD, now=clock())  # 问模型时第三个服务器连了进来

        llm.hook = hook
        await talk(engine, clock, 1, "@小D 记一下周六晚上开黑")
        self.assertTrue(state)
        self.assertEqual(texts(memory, g), [(1, "周六晚上开黑")])  # 组里的编号没变：照样记下
        self.assertIn("📝", [r[2] for r in transport.reactions])

    async def test_others_joining_the_group_do_not_block_an_edit(self):
        # 第三个服务器加进同一个组：组里原来的编号不变（并进来的接在后面），这边正在改的照样改
        engine, llm, transport, clock, memory, g = grouped(chat([ACTIONS({"type": "memory", "edit": 1, "text": "周六晚上开黑"}, REPLY(1, "改好了"))]))
        memory.add(g, "周五晚上开黑", via="command")
        memory.add(THIRD, "第三服的事", via="command")

        async def hook(call):
            if call["json_mode"] and not is_review(call) and engine.store.group_of(THIRD) is None:
                link(engine.store, engine.cards, memory, THIRD, GUILD, now=clock())

        llm.hook = hook
        await talk(engine, clock, 1, "@小D 把第 1 条改成周六")
        self.assertEqual(texts(memory, g), [(1, "周六晚上开黑"), (2, "第三服的事")])

    async def test_other_servers_linking_does_not_block_this_ones_edit(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS({"type": "memory", "edit": 1, "text": "周六晚上开黑"}, REPLY(1, "改好了"))]))
        memory.add(GUILD, "周五晚上开黑", via="command")

        async def hook(call):
            if call["json_mode"] and not is_review(call):
                link(engine.store, engine.cards, memory, THIRD, 4, now=clock())  # 不相干的两个服务器连成一组

        llm.hook = hook
        await talk(engine, clock, 1, "@小D 把第 1 条改成周六")
        self.assertEqual(texts(memory, GUILD), [(1, "周六晚上开黑")])

    async def test_other_servers_linking_does_not_redo_this_ones_review(self):
        review = json.dumps({"cards": [], "memory": [{"add": "整理出来的"}]}, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))], review=review))
        state = {"linked": False}

        async def hook(call):
            if is_review(call) and not state["linked"]:
                state["linked"] = True
                link(engine.store, engine.cards, memory, THIRD, 4, now=clock())  # 整理的时候不相干的服务器连成一组

        llm.hook = hook
        await three_rounds(engine, clock)
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertTrue(state["linked"])
        self.assertEqual(len([c for c in llm.calls if is_review(c)]), 1)  # 不用重问
        self.assertEqual(texts(memory, GUILD), [(1, "整理出来的")])

    async def test_a_new_memory_follows_this_server_into_the_group(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS({"type": "memory", "add": "周五开黑"}, REPLY(1, "记下了"))]))
        state = {}

        async def hook(call):
            if call["json_mode"] and not is_review(call) and not state:
                state["g"] = link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock()).group_id  # 问模型时连进了组

        llm.hook = hook
        await talk(engine, clock, 1, "@小D 记一下周五开黑")
        self.assertEqual(texts(memory, state["g"]), [(1, "周五开黑")])  # 新记一条不看编号：记进现在用的这份
        self.assertEqual(memory.get(state["g"], 1).origin, GUILD)

    async def review_in_group(self, ops):
        review = json.dumps({"cards": [], "memory": ops}, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))], review=review))
        memory.add(OTHER, "别的服的约定", via="command")
        memory.add(GUILD, "测试服的约定", via="command")
        g = link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock()).group_id
        await three_rounds(engine, clock)  # 在测试服聊
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        return memory, g, [c for c in llm.calls if is_review(c)]

    async def test_review_leaves_other_servers_memories_alone(self):
        # 整理记忆只合并、删改自己服务器记的；它看不到别的服务器的聊天，那边记的只给看
        memory, g, reviews = await self.review_in_group([{"delete": 1}, {"delete": 2}, {"delete": 9}, {"add": "整理出来的"}])
        self.assertEqual(texts(memory, g), [(1, "别的服的约定"), (3, "整理出来的")])  # 1 是别的服记的；9 没给它看过
        self.assertEqual(memory.get(g, 3).origin, GUILD)
        self.assertIn("(1)〔「别的服」记的〕别的服的约定", user_text(reviews[-1]))
        self.assertIn("(2) 测试服的约定", user_text(reviews[-1]))
        self.assertIn("标着〔「某某」记的〕的是组里别的服务器记的（1 条", system_text(reviews[-1]))

    async def test_review_does_not_half_merge_into_another_servers_memory(self):
        # 想把这里的并进别的服务器那条：那条不让改，这里的也不删（不然内容哪儿都没了）
        memory, g, _ = await self.review_in_group([{"edit": 1, "text": "别的服的约定；测试服的约定"}, {"delete": 2}])
        self.assertEqual(texts(memory, g), [(1, "别的服的约定"), (2, "测试服的约定")])

    async def test_over_the_limit_it_stops_asking_to_delete(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        engine.ec.memory_max_entries = 2
        for i in range(2):
            memory.add(OTHER, f"别的服{i}", via="command")
            memory.add(GUILD, f"测试服{i}", via="command")
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())  # 并完 4 条，超过上限 2 条
        await talk(engine, clock, 1)
        prompt = system_text(chats(llm)[-1])
        self.assertIn("超过上限了，新的记不进：别为了腾地方删条目", prompt)
        from dsbot.prompts import build_review_prompt

        review = build_review_prompt(name="小D", cards=False, memory=True, card_max=1, entry_max=200, count=4, max_entries=2, foreign=2)
        self.assertIn("超过了上限 2 条", review)
        self.assertIn("别为了腾地方删还有用的", review)
        self.assertNotIn("快满了就合并、删掉旧的", review)
        # 没有别的服务器记的（没连组、退组时带走的、上限调小了）：照原来那样自己合并、删旧的
        alone = build_review_prompt(name="小D", cards=False, memory=True, card_max=1, entry_max=200, count=4, max_entries=2)
        self.assertIn("快满了就合并、删掉旧的", alone)
        self.assertNotIn("超过了上限", alone)

    async def test_exactly_full_it_does_not_delete_other_servers_entries(self):
        # 正好记满、里面有别的服务器记的：聊天时别为了腾地方删（分不出是哪边的），整理时只动这里记的
        review = json.dumps({"cards": [], "memory": []}, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))], review=review))
        engine.ec.memory_max_entries = 2
        memory.add(OTHER, "别的服的约定", via="command")
        memory.add(GUILD, "测试服的约定", via="command")
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())  # 并完正好 2 条
        await talk(engine, clock, 1)
        prompt = system_text(chats(llm)[-1])
        self.assertIn("记满了，里面有组里别的服务器记的：别为了腾地方删条目", prompt)
        self.assertNotIn("超过上限了", prompt)
        await three_rounds(engine, clock)
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        review_prompt = system_text([c for c in llm.calls if is_review(c)][-1])
        self.assertIn("记满了（最多 2 条）：组里别的服务器记的这里删不了", review_prompt)
        self.assertNotIn("快满了就合并、删掉旧的", review_prompt)
        from dsbot.prompts import build_review_prompt, build_system_prompt

        # 没连组（全是自己记的）、还没满：照原来那样
        args = dict(name="小D", cards=False, memory=True, card_max=1, entry_max=200, max_entries=2)
        self.assertIn("快满了就合并、删掉旧的", build_review_prompt(count=2, **args))
        self.assertIn("快满了就合并、删掉旧的", build_review_prompt(count=1, foreign=1, **args))
        full = [(1, "a"), (2, "b")]
        self.assertNotIn("记满了，里面有", build_system_prompt(guild="g", channel="c", name="n", persona="", summary=None, memories=full, memory_max=2))
        alone = build_system_prompt(guild="g", channel="c", name="n", persona="", summary=None, memories=full, memory_max=2, group=["别的服"])
        self.assertNotIn("记满了，里面有", alone)  # 组里别的服务器没记过：全是自己的，照常合并、删旧的

    async def test_chat_edits_only_numbers_it_was_shown(self):
        engine, llm, transport, clock, memory, g = grouped(chat([ACTIONS({"type": "memory", "delete": 2}, REPLY(1, "删了"))]))
        memory.add(g, "组里的事", via="command")
        state = {}

        async def hook(call):
            if call["json_mode"] and not is_review(call) and not state:
                memory.add(THIRD, "第三服的事", via="command")
                state["joined"] = link(engine.store, engine.cards, memory, THIRD, GUILD, now=clock())  # 问模型时第三服并了进来

        llm.hook = hook
        await talk(engine, clock, 1, "@小D 把第 2 条删了")
        self.assertEqual(texts(memory, g), [(1, "组里的事"), (2, "第三服的事")])  # 第 2 条它没看过：不删

    async def test_nicknames_in_other_servers_do_not_rename_the_card(self):
        engine, llm, transport, clock, memory, g = grouped(chat([ACTIONS(REPLY(1, "在"))]))
        engine.cards.put(g, 11, "阿明", "自称猫娘", via="command")
        engine.cards.note_seen(g, 11, GUILD, "阿明")  # 在测试服出现过（提示词里有卡的人只列在这里出现过的）
        await talk(engine, clock, 1)  # 组里第一个服务器（测试服）：卡上的名字跟着这里
        before = system_text(chats(llm)[-1])
        await talk(engine, clock, 2, name="Ming", guild_id=OTHER, channel_id=HALL)  # 在别的服叫 Ming
        self.assertEqual(engine.cards.get(g, 11).name, "阿明")
        await talk(engine, clock, 3)
        self.assertEqual(system_text(chats(llm)[-1]), before)  # 提示词没变（缓存照样命中）
        self.assertEqual([c.user_id for c in engine.cards.find(g, "Ming")], [11])  # 按那边的名字也查得到
        await talk(engine, clock, 4, name="大明")  # 在第一个服务器改了昵称：卡上的名字才跟着改
        self.assertEqual(engine.cards.get(g, 11).name, "大明")

    async def test_everyone_who_talks_counts_as_seen_here(self):
        engine, llm, transport, clock, memory, g = grouped(chat([ACTIONS(REPLY(2, "在"))]))
        engine.ec.cards_per_round = 1  # 一轮只附一张卡
        engine.cards.put(g, 11, "阿明", "阿明的卡", via="command")
        engine.cards.put(g, 12, "小雨", "小雨的卡", via="command")
        await engine.handle_message(msg(1, "大家好", author=12, name="小雨", guild_id=OTHER, channel_id=HALL))
        await talk(engine, clock, 2, guild_id=OTHER, channel_id=HALL)
        seen = engine.cards.seen_in(g)
        self.assertIn(OTHER, seen[11])
        self.assertIn(OTHER, seen[12])  # 没附上卡的也算在这里出现过（退组时这边带走他的卡）

    async def test_mentions_only_reach_people_seen_here(self):
        engine, llm, transport, clock, memory, g = grouped(chat([ACTIONS(REPLY(1, "@小明 来了"))]))
        engine.cards.put(g, 21, "小明", "别的服的小明", via="command")
        engine.cards.note_seen(g, 21, OTHER, "小明")  # 只在别的服出现过
        transport.members = {"小明": {31}}  # 测试服里的小明（没有卡）
        await talk(engine, clock, 1, "@小D 叫一下小明")
        self.assertEqual(transport.sent[-1][1], "<@31> 来了")

    async def test_namesakes_in_other_servers_are_kept_apart(self):
        outputs = [ACTIONS({"type": "who", "name": "小明"}), ACTIONS(REPLY(1, "好"))]
        engine, llm, transport, clock, memory, g = grouped(chat(outputs * 3))
        engine.cards.put(g, 21, "小明", "测试服小明：自称猫娘", via="command")
        engine.cards.note_seen(g, 21, GUILD, "小明")  # 只在测试服
        await talk(engine, clock, 1, "@小D 小明是谁", guild_id=OTHER, channel_id=HALL)  # 别的服问
        prompt = system_text(chats(llm)[-1])
        self.assertNotIn("有卡的人：小明", prompt)  # 别的服务器的人不列名字
        self.assertIn("另外还有人有卡，没在这里的频道出现过", prompt)
        text = user_text(chats(llm)[-1])
        self.assertIn("【小明】（还没在这个服务器出现过：是别的服务器记下的人，别当成这里同名的人）", text)
        self.assertNotIn(21, engine.states[HALL].cards_shown)  # 那份写着不在这里：他来这里说话时照常附上他的卡
        engine.cards.put(g, 31, "小明", "别的服小明：自称骑士", via="command")
        engine.cards.note_seen(g, 31, OTHER, "小明")  # 别的服也有一个小明：先认这里的
        await talk(engine, clock, 3, "@小D 小明是谁", guild_id=OTHER, channel_id=HALL)
        text = user_text(chats(llm)[-1])
        self.assertIn("别的服小明：自称骑士", text)
        self.assertNotIn("测试服小明", text)
        engine.cards.note_seen(g, 21, OTHER, "Ming")  # 测试服的小明在别的服叫 Ming：按 Ming 查到他
        self.assertEqual(
            [(c.user_id, shown, bool(note)) for c, shown, note, _ in engine.cards.find_here(g, "Ming", OTHER)], [(21, "Ming", False)]
        )
        # 按他在测试服的名字“小明”在别的服查：这里的小明优先，不会混进来
        self.assertEqual([c.user_id for c, _, _, _ in engine.cards.find_here(g, "小明", OTHER)], [31])

    def test_finding_cards_by_name_in_a_group(self):
        cards, g = CardStore(":memory:"), -1
        cards.put(g, 1, "小明明", "这里的小明明", via="command")
        cards.note_seen(g, 1, OTHER, "小明明")
        cards.put(g, 2, "小明", "测试服的小明", via="command")
        cards.note_seen(g, 2, GUILD, "小明")
        cards.put(g, 3, "阿明", "只在私信里聊过的", via="command")
        cards.note_seen(g, 3, OTHER, "")
        cards.note_seen(g, 3, GUILD, "大明")

        def find(name):
            return [(c.user_id, shown, note[:6], local) for c, shown, note, local in cards.find_here(g, name, OTHER)]

        # 名字完全一样的先找，哪一档都算：这里的“小明明”（包含）盖不过别的服务器的“小明”（完全一样）
        self.assertEqual(find("小明"), [(2, "小明", "（还没在这个", False)])
        self.assertEqual(find("小明明"), [(1, "小明明", "", True)])
        # 只在私信里出现过：不知道他在这里叫什么，单独一档，写明白
        self.assertEqual(find("大明"), [(3, "大明", "（他只在私信", False)])  # 不记成卡已经附过了
        self.assertEqual(find("@阿明"), [(3, "阿明", "（他只在私信", False)])  # 显示的名字不带 @
        self.assertEqual(cards.named_here(g, OTHER), [(1, "小明明")])  # 提示词的名单不列只在私信里出现过的

    def test_dm_only_people_can_still_be_mentioned_by_card_name(self):
        engine, llm, transport, clock, memory, g = grouped()
        engine.cards.put(g, 11, "阿明", "阿明的卡", via="command")
        engine.cards.note_seen(g, 11, GUILD, "")  # 只在私信里出现过（没记名字）
        self.assertEqual(engine.cards.owners_seen(g, GUILD), [(11, "阿明")])

    def test_status_counts_the_group(self):
        engine, llm, transport, clock, memory = setup()
        memory.add(GUILD, "x", via="command")
        link(engine.store, engine.cards, memory, OTHER, GUILD, now=clock())
        status = engine.status(OTHER, HALL, None)
        self.assertEqual((status["memory_count"], status["group"]), (1, [GUILD]))


class Sharing(unittest.IsolatedAsyncioTestCase):
    def share(self, engine):
        for guild in (GUILD, OTHER):
            engine.settings.set("guild", guild, "share_memory", True)

    async def test_shared_channels_across_the_group(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        self.share(engine)
        await talk(engine, clock, 1, "@小D 我们在大厅聊游戏", guild_id=OTHER, channel_id=HALL)
        self.assertEqual(engine.shared_sources(GUILD, CHANNEL), [])  # 没连成组：只翻自己服务器的
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())
        self.assertEqual([cid for cid, _ in engine.shared_sources(GUILD, CHANNEL)], [HALL])
        await talk(engine, clock, 2)
        text = user_text(chats(llm)[-1])
        self.assertIn("#大厅（「别的服」服务器）", text)  # 能翻记录的频道：写着是哪个服务器
        self.assertIn("在「别的服」服务器的 #大厅 跟你聊过", text)  # 他刚才在那边跟它聊的

    async def test_recall_reads_the_other_servers_channel(self):
        outputs = [ACTIONS(REPLY(1, "在")), ACTIONS({"type": "recall", "channel": "#大厅（「别的服」服务器）"}), ACTIONS(REPLY(1, "看到了"))]
        engine, llm, transport, clock, memory = setup(chat(outputs))
        self.share(engine)
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())
        await talk(engine, clock, 1, "@小D 我们在大厅聊游戏", guild_id=OTHER, channel_id=HALL)
        await talk(engine, clock, 2, "@小D 大厅那边聊了什么")
        text = user_text(chats(llm)[-1])
        self.assertIn("（你翻了 #大厅（「别的服」服务器） 的记录", text)
        self.assertIn("我们在大厅聊游戏", text)

    def test_finding_channels_across_servers(self):
        engine, llm, transport, clock, memory = setup()
        transport.places[ARENA] = ChannelPlace(guild_id=THIRD, name="大厅", public=True)
        transport.places[LOBBY] = ChannelPlace(guild_id=GUILD, name="大厅", public=True)
        transport.guild_names[THIRD] = "第三服"
        hall, arena, lobby = (HALL, transport.places[HALL]), (ARENA, transport.places[ARENA]), (LOBBY, transport.places[LOBBY])
        self.assertIsInstance(engine._find_source([hall, arena], "大厅", GUILD), list)  # 不猜
        self.assertEqual(engine._find_source([hall, arena], "大厅（「第三服」服务器）", GUILD), arena)
        self.assertEqual(engine._find_source([hall, arena, lobby], "大厅", GUILD), lobby)  # 先认这个服务器自己的
        self.assertIsNone(engine._find_source([hall, arena], "第三服", GUILD))  # 光写服务器名对不上
        # 照着近况里的写法（“在「别的服」服务器的 #大厅 跟你聊过”）写的：认得是别的服的，不会认成这里的 #大厅
        self.assertEqual(engine._find_source([lobby, hall, arena], "「别的服」服务器的 #大厅", GUILD), hall)

    def test_naming_the_server_never_falls_back_to_this_one(self):
        engine, llm, transport, clock, memory = setup()
        transport.places[LOBBY] = ChannelPlace(guild_id=GUILD, name="大厅", public=True)
        long_name = "The Official XYZ Community Server For Everyone 2026"  # 超过 40 字：给模型看的是截短的
        transport.guild_names[OTHER] = long_name
        hall, lobby = (HALL, transport.places[HALL]), (LOBBY, transport.places[LOBBY])
        label = engine._server_label(OTHER)
        self.assertTrue(label.endswith("…"))
        for wanted in (
            f"「{label}」服务器的 #大厅",  # 近况的写法
            f"在「{label}」服务器的 #大厅",  # 照近况原句连“在”字一起抄的
            f"#大厅（「{label}」服务器）",  # 能翻的频道名单的写法
            f"#大厅（「{label.rstrip('…')}」服务器）",  # 抄的时候丢了“…”
            f"#大厅（「{long_name}」服务器）",  # 全名
        ):
            self.assertEqual(engine._find_source([lobby, hall], wanted, GUILD), hall, wanted)
        # 那边的 #大厅 没开共享（不在能翻的里面）：写了是那边的，就是找不到，不会翻成这里的 #大厅
        self.assertIsNone(engine._find_source([lobby], f"「{label}」服务器的 #大厅", GUILD))
        self.assertIsNone(engine._find_source([lobby, hall], "#大厅（「不存在的服」服务器）", GUILD))
        self.assertEqual(engine._find_source([lobby, hall], "#大厅", GUILD), lobby)  # 没写服务器：先认这里的
        # 能翻的频道名单里的服务器名，和近况、组名单截得一样
        self.assertEqual(engine._source_name(HALL, transport.places[HALL], GUILD), f"#大厅（「{label}」服务器）")
        very_long = "超长的服务器名字" * 9  # 72 字（Discord 最长 100 字）
        transport.guild_names[OTHER] = very_long
        self.assertEqual(engine._find_source([lobby, hall], f"#大厅（「{very_long}」服务器）", GUILD), hall)
        transport.places[LOBBY] = ChannelPlace(guild_id=GUILD, name="公告（「国服」服务器）", public=True)
        lobby = (LOBBY, transport.places[LOBBY])  # 频道自己就叫这个名字：照名字翻得到
        self.assertEqual(engine._find_source([lobby, hall], "#公告（「国服」服务器）", GUILD), lobby)
        # 群友起个“大厅（「…」服务器）”的帖子：冒充不了那边的频道
        transport.places[ARENA] = ChannelPlace(guild_id=GUILD, name=f"大厅（「{very_long}」服务器）", parent_id=CHANNEL, public=True)
        thread = (ARENA, transport.places[ARENA])
        self.assertEqual(engine._find_source([thread, lobby, hall], f"#大厅（「{very_long}」服务器）", GUILD), hall)
        self.assertIsNone(engine._find_source([thread], "#大厅（「不存在的服」服务器）", GUILD))

    def test_other_servers_channels_are_not_linked_to_this_one(self):
        from dsbot.mentions import link as link_names

        channels = {"大厅": frozenset({LOBBY})}
        others = lambda server: server == "别的服"  # noqa: E731  bot 知道的别的服务器
        for text in (
            "#大厅（「别的服」服务器）那边在聊原神",
            "在「别的服」服务器的 #大厅 阿明也说了",
            "#大厅 (「别的服」服务器)",
            "帖子「攻略」（在 #大厅）（「别的服」服务器）",  # 别的服务器的帖子
            "「别的服」服务器的 帖子「攻略」（在 #大厅） 跟你聊过",  # 近况里别的服务器的帖子
        ):
            self.assertNotIn(f"<#{LOBBY}>", link_names(text, {}, channels, other_server=others)[0], text)
        self.assertEqual(link_names("去 #大厅 看看", {}, channels, other_server=others)[0], f"去 <#{LOBBY}> 看看")  # 这里的频道照常换
        for text in ("「测试服」服务器的 #大厅", "#大厅（「测试服」服务器维护通知）", "#大厅（「测试服」服务器）", "去「生存」服务器的 #大厅 看看"):
            self.assertIn(f"<#{LOBBY}>", link_names(text, {}, channels, other_server=others)[0], text)  # 写的是这里、不是哪个服务器

    async def test_this_servers_own_channels_stay_linked(self):
        # 写的是这个服务器（给它看的名字中和、截短过）：照常换成这里的链接。写的是 bot 知道的别的服务器（它在的、组里的、
        # 最近跟它聊过的）：不换。认不出是哪个服务器的（游戏里的“生存服务器”）：没连组、没开跨服务器、不共用人设卡时（它没见过
        # 别的服务器）照常换，连成组以后不换。这里和组里别的服务器同名（分不出是哪个）：不换
        from dsbot.mentions import link as link_names

        replies = [ACTIONS(REPLY(1, "群规在「[原神]提瓦特冒险团」服务器的 #大厅 置顶了")), ACTIONS(REPLY(1, "#大厅（「别的服」服务器）那边在聊原神"))]
        engine, llm, transport, clock, memory = setup(chat(replies))
        channels = {"大厅": frozenset({LOBBY})}

        def linked(text):
            return f"<#{LOBBY}>" in link_names(text, {}, channels, other_server=engine._other_server_named(GUILD))[0]

        transport.guild_names[GUILD] = "【原神】提瓦特冒险团"  # 给它看的是“[原神]提瓦特冒险团”
        transport.channel_map = {"大厅": {LOBBY}}
        await talk(engine, clock, 1, "@小D 群规在哪")
        self.assertIn(f"<#{LOBBY}>", transport.sent[-1][1])
        self.assertTrue(linked("#大厅（「【原神】提瓦特冒险团」服务器）"))
        self.assertTrue(linked("去「生存」服务器的 #大厅 看看"))  # 游戏里的服务器
        long_name = "超长的服务器名字" * 9
        transport.guild_names[GUILD] = long_name  # 提示词里截到 60 字
        self.assertTrue(linked(f"「{long_name[:59]}…」服务器的 #大厅"))
        self.assertTrue(linked(f"「{long_name[:59]}」服务器的 #大厅"))  # 抄的时候丢了“…”
        engine.interactions[(THIRD, 11)] = deque()  # 他在第三服跟它聊过（没开跨服务器，这里看不到）
        transport.guild_names[THIRD] = "第三服"
        self.assertFalse(linked("「第三服」服务器的 #大厅"))
        self.assertTrue(linked("去「生存」服务器的 #大厅 看看"))  # 知道别的服务器了，认不出的照样换
        self.assertTrue(linked(f"「{long_name[:59]}…」服务器的 #大厅"))
        engine.interactions.clear()  # 重启了，或者几个小时没人在那边跟它聊：bot 还在那个服务器，照样认得
        transport.joined.append(THIRD)
        self.assertFalse(linked("「第三服」服务器的 #大厅"))
        for third in (long_name, long_name + "（管理）"):  # 和这里同名、开头一样（比如专门放管理命令的服务器）
            transport.guild_names[THIRD] = third  # 可这里从来看不到那个服务器（没连组、没开跨服务器）：写的就是这里
            for cut in (f"{long_name[:59]}…", f"{long_name[:39]}…", f"{long_name[:20]}…"):
                self.assertTrue(linked(f"「{cut}」服务器的 #大厅"), (third, cut))
        self.assertFalse(linked(f"「{long_name}（管理）」服务器的 #大厅"))  # 写的是那个服务器的全名：不换
        transport.guild_names[THIRD] = long_name[:39]  # 那个服务器的全名正好是这里截到 40 字、丢了“…”的样子：算那边的
        lobby = [(LOBBY, ChannelPlace(guild_id=GUILD, name="大厅", public=True))]
        self.assertFalse(linked(f"「{long_name[:39]}」服务器的 #大厅"))
        self.assertIsNone(engine._find_source(lobby, f"「{long_name[:39]}」服务器的 #大厅", GUILD))
        self.assertTrue(linked(f"「{long_name[:39]}…」服务器的 #大厅"))
        transport.guild_names[THIRD] = long_name + "（管理）"
        self.assertTrue(linked(f"「{long_name[:39]}」服务器的 #大厅"))
        self.assertEqual(engine._find_source(lobby, f"「{long_name[:39]}」服务器的 #大厅", GUILD), lobby[0])
        engine.cards.set_shared(True, scope_of=engine.store.memory_scope, alive=set())  # 共用人设卡：卡上会写着别的服务器
        self.assertFalse(linked(f"「{long_name[:20]}…」服务器的 #大厅"))
        self.assertFalse(linked("去「生存」服务器的 #大厅 看看"))  # 认不出是哪个的：和连了组一样不换
        self.assertTrue(linked(f"「{long_name}」服务器的 #大厅"))
        engine.cards.set_shared(False, scope_of=engine.store.memory_scope, alive=set())
        transport.guild_names[GUILD], transport.guild_names[THIRD] = "【原神】提瓦特冒险团", "第三服"
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())
        self.assertFalse(linked("#大厅（「别的服」服务器）"))
        self.assertFalse(linked("#大厅（「别的服🌸」服务器）"))  # 写得和给它看的不一样：认不出是哪个，就不换
        self.assertFalse(linked("去「生存」服务器的 #大厅 看看"))
        self.assertTrue(linked("#大厅（「[原神]提瓦特冒险团」服务器）"))  # 认得出是这里：照常换
        self.assertTrue(linked("群规在「【原神】提瓦特冒险团」服务器的 #大厅 置顶了"))
        for cut in ("【原神】提瓦…", "[原神]提瓦…"):  # 它自己截短的这里的名字：照原样写【】的、中和过的都认得出
            self.assertTrue(linked(f"群规在「{cut}」服务器的 #大厅 置顶了"), cut)
        await talk(engine, clock, 2, "@小D 别的服在聊什么")
        self.assertEqual(transport.sent[-1][1], "#大厅（「别的服」服务器）那边在聊原神")  # 组里别的服务器的频道：不链到这里的 #大厅
        transport.guild_names[GUILD] = "别的服"  # 和组里别的服务器同名
        self.assertFalse(linked("「别的服」服务器的 #大厅"))
        self.assertTrue(linked("去 #大厅 看看"))
        transport.guild_names[GUILD] = long_name
        self.assertTrue(linked(f"「{long_name[:59]}」服务器的 #大厅"))  # 提示词里截到 60 字的这里的名字，丢了“…”
        self.assertTrue(linked(f"「{long_name[:20]}…」服务器的 #大厅"))  # 它自己截短的这里的名字：认得出
        label = engine._server_label(GUILD)  # 整理记忆时给它看的（截到 40 字），记进组里的长期记忆
        self.assertTrue(linked(f"群规在「{label}」服务器的 #大厅 置顶了"))
        self.assertTrue(linked(f"「{label.rstrip('…')}」服务器的 #大厅"))
        transport.guild_names[OTHER] = long_name[:30] + "二群"
        self.assertFalse(linked(f"「{long_name[:20]}…」服务器的 #大厅"))  # 组里别的服务器开头也一样：分不出是哪个
        self.assertTrue(linked(f"群规在「{label}」服务器的 #大厅 置顶了"))
        transport.guild_names[OTHER] = long_name[:50] + "二群"  # 截到 40 字以后一模一样
        self.assertFalse(linked(f"群规在「{label}」服务器的 #大厅 置顶了"))

    def test_the_cut_shown_in_chat_stays_linked_when_a_known_server_starts_alike(self):
        # 它看得到别的服务器的名字时（连了组、开了跨服务器、共用人设卡），bot 知道的别的服务器开头也一样：聊天里给它看的
        # （截到 60 字的）这里的名字照常换，它自己截短的、截到 40 字的就分不出是哪个，不换
        from dsbot.mentions import link as link_names

        long_name = "超长的服务器名字" * 9
        for mode in ("group", "cross", "shared"):
            engine, llm, transport, clock, memory = setup()
            channels = {"大厅": frozenset({LOBBY})}
            transport.guild_names[GUILD] = long_name
            transport.guild_names[THIRD] = long_name[:59] + "二群"
            transport.joined.append(THIRD)
            if mode == "group":
                link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())
            elif mode == "cross":
                engine.settings.set("guild", GUILD, "cross_server", True)
            else:
                engine.cards.set_shared(True, scope_of=engine.store.memory_scope, alive=set())

            def linked(text):
                return f"<#{LOBBY}>" in link_names(text, {}, channels, other_server=engine._other_server_named(GUILD))[0]

            self.assertTrue(linked(f"「{long_name[:59]}…」服务器的 #大厅"), mode)
            self.assertFalse(linked(f"「{long_name[:58]}…」服务器的 #大厅"), mode)
            self.assertFalse(linked(f"「{engine._server_label(GUILD)}」服务器的 #大厅"), mode)

    def test_cross_server_names_are_not_taken_for_this_one(self):
        # 开了跨服务器（近况里会有别的服务器的名字）：认不出是这里的就不换；和开了跨服务器的那边同名也不换
        from dsbot.mentions import link as link_names

        engine, llm, transport, clock, memory = setup()
        channels = {"大厅": frozenset({LOBBY})}

        def linked(text):
            return f"<#{LOBBY}>" in link_names(text, {}, channels, other_server=engine._other_server_named(GUILD))[0]

        engine.settings.set("guild", GUILD, "cross_server", True)
        self.assertFalse(linked("去「生存」服务器的 #大厅 看看"))
        self.assertTrue(linked("「测试服」服务器的 #大厅"))  # 这里
        transport.guild_names[THIRD] = "测试服"
        transport.joined.append(THIRD)
        self.assertTrue(linked("「测试服」服务器的 #大厅"))  # 同名的那边没开跨服务器：这里看不到它
        engine.settings.set("guild", THIRD, "cross_server", True)
        self.assertFalse(linked("「测试服」服务器的 #大厅"))  # 都开了：那边的近况会带到这里，分不出是哪个

    def test_servers_with_the_same_name_are_told_apart(self):
        # 组里两个服务器同名（或者截短以后一样）：照名单里的写法翻，翻的是名单里那个，不会落到这里同名的频道上
        engine, llm, transport, clock, memory = setup()
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())
        link(engine.store, engine.cards, memory, THIRD, OTHER, now=clock())
        transport.places[LOBBY] = ChannelPlace(guild_id=GUILD, name="大厅", public=True)
        transport.guild_names[GUILD] = transport.guild_names[OTHER] = "猫猫俱乐部"
        hall, lobby = (HALL, transport.places[HALL]), (LOBBY, transport.places[LOBBY])
        self.assertEqual(engine._find_source([lobby, hall], "#大厅（「猫猫俱乐部」服务器）", GUILD), hall)
        self.assertEqual(engine._find_source([lobby, hall], "「猫猫俱乐部」服务器的 #大厅", GUILD), hall)
        self.assertEqual(engine._find_source([lobby, hall], "#大厅", GUILD), lobby)  # 没写是哪个服务器：先认这里的
        for wanted in ("大厅（「猫猫俱乐部」服务器）", "＃大厅（「猫猫俱乐部」服务器）"):  # 没写“#”、写成全角的
            self.assertEqual(engine._find_source([lobby, hall], wanted, GUILD), hall, wanted)
        transport.places[902] = ChannelPlace(guild_id=OTHER, name="大厅", public=True)
        second = (902, transport.places[902])  # 那边有两个叫“大厅”的频道：和这里的一样，认第一个
        self.assertEqual(engine._find_source([lobby, hall, second], "#大厅（「猫猫俱乐部」服务器）", GUILD), hall)
        transport.places[903] = ChannelPlace(guild_id=OTHER, name="攻略", parent_id=HALL, public=True)
        t1 = (903, transport.places[903])
        thread_label = "帖子「攻略」（在 #大厅）（「猫猫俱乐部」服务器）"
        self.assertEqual(engine._source_name(*t1, GUILD), thread_label)
        self.assertEqual(engine._find_source([lobby, hall, t1], thread_label, GUILD), t1)  # 那边只有这一个帖子：照名单翻它
        transport.places[904] = ChannelPlace(guild_id=OTHER, name="攻略", parent_id=HALL, public=True)
        t2 = (904, transport.places[904])  # 群友又起了个同名的帖子：分不出是哪个，不猜
        self.assertEqual(engine._find_source([lobby, hall, t1, t2], thread_label, GUILD), [t1, t2])
        transport.guild_names[THIRD] = "猫猫俱乐部"
        transport.places[ARENA] = ChannelPlace(guild_id=THIRD, name="大厅", public=True)
        arena = (ARENA, transport.places[ARENA])  # 组里另外两个服务器同名、都有 #大厅：不猜
        self.assertEqual(engine._find_source([lobby, hall, arena], "#大厅（「猫猫俱乐部」服务器）", GUILD), [hall, arena])
        transport.guild_names[GUILD] = "测试服"
        transport.places[901] = ChannelPlace(guild_id=GUILD, name="大厅（「猫猫俱乐部」服务器）", public=True)
        odd = (901, transport.places[901])  # 这里有个频道就叫这个名字：写的是组里的服务器，就认那边的
        self.assertEqual(engine._find_source([odd, hall], "#大厅（「猫猫俱乐部」服务器）", GUILD), hall)
        long_name = "The Official XYZ Community Server For Everyone"
        transport.guild_names[GUILD], transport.guild_names[OTHER] = long_name + " A", long_name + " B"
        label = engine._source_name(HALL, transport.places[HALL], GUILD)
        self.assertTrue(label.endswith("…」服务器）"))  # 名单里截短了，两个服务器看起来一样
        self.assertEqual(engine._find_source([lobby, hall], label, GUILD), hall)
        cut = engine._server_label(OTHER).rstrip("…")
        for wanted in (f"#大厅（「{cut}」服务器）", f"「{cut}」服务器的 #大厅"):  # 抄的时候丢了“…”：也不会落到这里
            self.assertEqual(engine._find_source([lobby, hall], wanted, GUILD), hall, wanted)
        self.assertIsNone(engine._find_source([lobby], label, GUILD))  # 那边的 #大厅 没开共享：找不到，也不当成这里的
        self.assertEqual(engine._find_source([lobby, hall], f"#大厅（「{long_name} A」服务器）", GUILD), lobby)  # 写的是这里

    def test_this_servers_name_cut_its_own_way_still_finds_its_channels(self):
        # 服务器名字很长：写的这里的名字截得和提示词里不一样（截到 40 字、自己截短了），bot 不知道别的服务器也叫这个开头，
        # 还是这里
        engine, llm, transport, clock, memory = setup()
        transport.places[LOBBY] = ChannelPlace(guild_id=GUILD, name="大厅", public=True)
        lobby = (LOBBY, transport.places[LOBBY])
        long_name = "超长的服务器名字" * 9
        transport.guild_names[GUILD] = long_name
        label = engine._server_label(GUILD)
        for wanted in (f"#大厅（「{label}」服务器）", f"「{label.rstrip('…')}」服务器的 #大厅", f"「{long_name[:20]}…」服务器的 #大厅"):
            self.assertEqual(engine._find_source([lobby], wanted, GUILD), lobby, wanted)
        self.assertIsNone(engine._find_source([lobby], f"「{long_name[:20]}」服务器的 #大厅", GUILD))  # 没带“…”的半截名字：不认
        transport.guild_names[GUILD] = "【原神】提瓦特冒险团"  # 给它看的是“[原神]提瓦特冒险团”；它自己截短时可能照原样写【】
        for wanted in ("「【原神】提瓦…」服务器的 #大厅", "#大厅（「【原神】提瓦…」服务器）", "「[原神]提瓦…」服务器的 #大厅"):
            self.assertEqual(engine._find_source([lobby], wanted, GUILD), lobby, wanted)
        transport.guild_names[GUILD] = long_name
        transport.joined.append(OTHER)
        transport.guild_names[OTHER] = long_name[:30] + "二群"  # bot 在的别的服务器开头也一样，可这里看不到它：还是这里
        self.assertEqual(engine._find_source([lobby], f"「{long_name[:20]}…」服务器的 #大厅", GUILD), lobby)
        engine.settings.set("guild", GUILD, "cross_server", True)  # 开了跨服务器：那边的名字可能带到这里，分不出是哪个，不翻这里的
        self.assertIsNone(engine._find_source([lobby], f"「{long_name[:20]}…」服务器的 #大厅", GUILD))
        engine.settings.set("guild", GUILD, "cross_server", False)
        transport.guild_names[OTHER] = "别的服"
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())  # 连成组：整理记忆时写进组里的是截到 40 字的名字
        hall = (HALL, transport.places[HALL])
        for wanted in (f"「{label}」服务器的 #大厅", f"#大厅（「{label.rstrip('…')}」服务器）"):
            self.assertEqual(engine._find_source([lobby, hall], wanted, GUILD), lobby, wanted)

    async def test_cross_server_still_follows_inside_a_group(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "加油"))]))
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())
        await talk(engine, clock, 1, "@小D 我在准备期末考试", guild_id=OTHER, channel_id=HALL)
        await talk(engine, clock, 2)
        self.assertNotIn("期末考试", user_text(chats(llm)[-1]))  # 没开记忆共享、也没开跨服务器
        for guild in (GUILD, OTHER):
            engine.settings.set("guild", guild, "cross_server", True)
        await talk(engine, clock, 3)
        self.assertIn("在「别的服」服务器的 #大厅 跟你聊过：", user_text(chats(llm)[-1]))  # 连成组也照样跨服务器带

    async def test_secrets_stay_out_when_carried_by_cross_server(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "嗯"))]))
        link(engine.store, engine.cards, memory, GUILD, OTHER, now=clock())
        for guild in (GUILD, OTHER):
            engine.settings.set("guild", guild, "cross_server", True)
        await talk(engine, clock, 1, "@小D 我在准备期末考试", guild_id=OTHER, channel_id=HALL)
        await talk(engine, clock, 2, "@小D 这件事要保密：我其实挂科了", guild_id=OTHER, channel_id=HALL)
        await talk(engine, clock, 3, "@小D 别的服那边呢", author=12, name="小雨", guild_id=OTHER, channel_id=HALL)
        await talk(engine, clock, 4)
        text = user_text(chats(llm)[-1])
        self.assertIn("期末考试", text)
        self.assertNotIn("挂科", text)  # 说了要保密的那段不带（和没连成组一样）


class DmInGroups(unittest.IsolatedAsyncioTestCase):
    async def test_the_home_servers_group_counts_as_home(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        await talk(engine, clock, 1, guild_id=OTHER, channel_id=HALL)
        self.assertNotIn("学吉他", user_text(chats(llm)[-1]))  # 私信挂在 GUILD，OTHER 没开跨服务器
        link(engine.store, engine.cards, memory, OTHER, GUILD, now=clock())
        await talk(engine, clock, 2, guild_id=OTHER, channel_id=HALL)
        self.assertIn("学吉他", user_text(chats(llm)[-1]))  # 连成一组：和私信挂的服务器一样
        unlink(engine.store, engine.cards, memory, OTHER, now=clock())
        engine.groups_changed([GUILD, OTHER], left=True)  # 退出组：附过的撤回
        record = json.dumps([t.content for t in engine.states[HALL].conversation.turns], ensure_ascii=False)
        self.assertNotIn("学吉他", record)
        await talk(engine, clock, 3, guild_id=OTHER, channel_id=HALL)
        self.assertNotIn("学吉他", user_text(chats(llm)[-1]))

    async def test_leave_keeps_what_is_still_allowed(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        memory.dm_add(11, "最近在学吉他", sensitive=False, via="chat")
        engine.settings.set("guild", OTHER, "cross_server", True)  # 别的服开了跨服务器：本来就能带
        link(engine.store, engine.cards, memory, OTHER, GUILD, now=clock())
        await talk(engine, clock, 1, guild_id=OTHER, channel_id=HALL)
        await talk(engine, clock, 2)  # 测试服的频道也附上了
        unlink(engine.store, engine.cards, memory, OTHER, now=clock())
        engine.groups_changed([GUILD, OTHER], left=True)
        for channel in (HALL, CHANNEL):  # 退组以后照样能带的：不撤回（不用把记录改一遍）
            record = json.dumps([t.content for t in engine.states[channel].conversation.turns], ensure_ascii=False)
            self.assertIn("学吉他", record)

    async def test_dm_review_is_not_told_it_is_a_server_chat(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "好"))]))
        link(engine.store, engine.cards, memory, OTHER, GUILD, now=clock())
        for i in range(3):
            await engine.handle_message(dm(1 + i, f"第{i}句"))
            await engine.drain()
            clock.advance(30)
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        reviews = [c for c in llm.calls if is_review(c)]
        self.assertEqual(len(reviews), 1)
        self.assertIn("这段是私信里的聊天", system_text(reviews[0]))
        self.assertNotIn("这段聊天在服务器", system_text(reviews[0]))

    async def test_dm_lines_reach_the_group(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "加油"))]))
        await engine.handle_message(dm(1, "我在准备期末考试"))
        await engine.drain()
        clock.advance(60)
        await talk(engine, clock, 2, guild_id=OTHER, channel_id=HALL)
        self.assertNotIn("期末考试", user_text(chats(llm)[-1]))  # 私信的原话只带到私信挂的服务器
        link(engine.store, engine.cards, memory, OTHER, GUILD, now=clock())
        await talk(engine, clock, 3, guild_id=OTHER, channel_id=HALL)
        self.assertIn("在私信里跟你聊过", user_text(chats(llm)[-1]))  # 组里的服务器也算


class FakeResponse:
    def __init__(self):
        self.sent, self.edited = [], []

    def is_done(self):
        return False

    async def send_message(self, text, view=None, ephemeral=False):
        self.sent.append((text, view, ephemeral))

    async def edit_message(self, content=None, view=None):
        self.edited.append(content)


def owner_bot(guilds):
    store, cards, memory = world()
    changed = []
    by_id = {g.id: g for g in guilds}
    engine = NS(groups_changed=lambda members, left: changed.append((list(members), left)))
    return NS(
        store=store,
        cards=cards,
        memory=memory,
        engine=engine,
        guilds=list(guilds),
        get_guild=by_id.get,
        cfg=NS(engine=NS(memory_max_entries=50)),
        settings=NS(get=lambda *a, **kw: True),
        secrets=NS(owner_id=OWNER),
        changed=changed,
    )


class OwnerCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")
        self.bot = owner_bot([NS(id=GUILD, name="主服"), NS(id=OTHER, name="游戏服"), NS(id=THIRD, name="第三服")])

    def interaction(self, user=OWNER):
        return NS(user=NS(id=user), response=FakeResponse(), followup=NS(send=None))

    async def test_texts_and_choices(self):
        from dsbot.commands import (
            do_link,
            do_unlink,
            groups_text,
            link_preview_text,
            memory_list_text,
            owner_server_choices,
            unlink_preview_text,
        )

        bot = self.bot
        self.assertIn("还没有服务器组", groups_text(bot))
        self.assertEqual([c.value for c in owner_server_choices(bot, OWNER, "游戏")], [str(OTHER)])
        self.assertEqual(owner_server_choices(bot, 99, ""), [])  # 不是 bot 主人：服务器名单不给看
        bot.memory.add(OTHER, "游戏服的事", via="command")
        bot.cards.put(OTHER, 11, "阿明", "游戏服的卡", via="command")
        bot.cards.put(GUILD, 11, "阿明", "主服的卡", via="command")
        preview = link_preview_text(bot, plan_link(bot.store, bot.cards, bot.memory, GUILD, OTHER))
        self.assertIn("把「主服」和「游戏服」连成一组？", preview)
        self.assertIn("有 1 个人两边都有人设卡：用「游戏服」那张，「主服」那张存成旧版本", preview)
        self.assertIn("能换成「主服」那张，原来那张会存成旧版本，再改回上一版就换回去", preview)
        self.assertIn("服务器组还是 Beta 功能", preview)  # 还在测试：连之前提醒先备份
        self.assertIn("已把「主服」连进组 1，现在组里有：「游戏服」、「主服」。", do_link(bot, GUILD, OTHER))
        self.assertEqual(bot.changed, [([OTHER, GUILD], False)])
        self.assertIn("组 1：「游戏服」、「主服」 · 共用人设卡 1 张、长期记忆 1 条", groups_text(bot))
        self.assertIn("**服务器组**（Beta）", groups_text(bot))
        self.assertIn("游戏服（2） · 组 1", [c.name for c in owner_server_choices(bot, OWNER, "")])
        listing = memory_list_text(bot, GUILD)
        self.assertIn("这个服务器和「游戏服」连成了一组，长期记忆是组里共用的", listing)
        self.assertIn("(1) 游戏服的事", listing)
        preview = unlink_preview_text(bot, plan_unlink(bot.store, bot.cards, bot.memory, GUILD), GUILD)
        self.assertIn("组里只剩「游戏服」：组就散了", preview)
        self.assertIn("在「主服」出现过的人的人设卡（1 张）", preview)
        self.assertIn("「主服」已经退出组 1，带走了 1 张人设卡、1 条长期记忆。", do_unlink(bot, GUILD))
        self.assertEqual(bot.changed[-1], ([OTHER, GUILD], True))  # 退出：附过的私信记忆要照新的情况撤回
        self.assertIn("还没有服务器组", groups_text(bot))

    async def test_merged_memory_numbers_and_long_lists(self):
        from dsbot.commands import REPLY_PARTS, link_preview_text, memory_list_text, memory_number_choices, split_message

        bot = self.bot
        for i in range(75):
            bot.memory.add(OTHER, f"游戏服{i:02d}" + "很长的约定" * 38, via="command")
            bot.memory.add(GUILD, f"主服{i:02d}" + "很长的约定" * 38, via="command")
        preview = link_preview_text(bot, plan_link(bot.store, bot.cards, bot.memory, GUILD, OTHER))
        self.assertIn("「主服」原来的长期记忆会接在组里原来的后面、重新编号", preview)
        self.assertIn("超过上限 50 条：删到 49 条以内之前不会记新的", preview)
        self.assertIn("整理记忆时每个服务器只合并、删改自己服务器记的", preview)
        from dsbot.commands import do_link

        self.assertIn("编号变了", do_link(bot, GUILD, OTHER))
        g = bot.store.group_of(GUILD)
        text = memory_list_text(bot, GUILD)
        self.assertLessEqual(len(split_message(text)), REPLY_PARTS)  # 一次发得完
        for m in bot.memory.entries(g):
            self.assertIn(f"({m.no}) ", text)  # 每个编号都列出来了
        self.assertIn("每条只显示前", text)
        self.assertIn("（「游戏服」记的）", text)  # 组里别的服务器记的：标出来
        self.assertNotIn("（「主服」记的）", text)
        choices = memory_number_choices(bot, GUILD, "主服07")
        self.assertEqual([c.value for c in choices], [m.no for m in bot.memory.entries(g) if m.text.startswith("主服07")])
        self.assertTrue(all(len(c.name) <= 100 for c in memory_number_choices(bot, GUILD, "")))

    async def test_exactly_full_lists_and_previews_say_so(self):
        from dsbot.commands import do_link, link_preview_text, memory_list_text

        bot = self.bot
        bot.cfg.engine.memory_max_entries = 2
        bot.memory.add(OTHER, "游戏服的事", via="command")
        bot.memory.add(OTHER, "游戏服的事2", via="command")
        self.assertNotIn("⚠️", memory_list_text(bot, OTHER))  # 没连组、正好满了：它整理时自己会合并、删旧的
        bot.memory.delete(OTHER, 2)
        bot.memory.add(GUILD, "主服的事", via="command")
        self.assertIn("正好到上限 2 条", link_preview_text(bot, plan_link(bot.store, bot.cards, bot.memory, GUILD, OTHER)))
        do_link(bot, GUILD, OTHER)
        self.assertIn("⚠️ 记满了：整理记忆时它只合并、删掉这个服务器自己记的", memory_list_text(bot, GUILD))
        g = bot.store.group_of(GUILD)
        bot.memory.delete(g, next(m.no for m in bot.memory.entries(g) if m.text == "主服的事"))
        bot.memory.add(g, "游戏服的事3", via="command", origin=OTHER)
        self.assertNotIn("记满了", memory_list_text(bot, OTHER))  # 都是这里记的：它整理时自己腾得出地方
        self.assertIn("记满了", memory_list_text(bot, GUILD))

    def numbers_listed(self, text, entries):
        """列表里列出来的编号：前面带着内容的、只列编号的分开算；分条发时没有哪一行被从中间切开。"""
        import re

        from dsbot.commands import REPLY_PARTS, split_message

        parts = split_message(text)
        self.assertLessEqual(len(parts), REPLY_PARTS)
        self.assertEqual("\n".join(parts), text)  # 只在行和行之间分开：编号不会被切到两条消息里
        shown = [int(n) for n in re.findall(r"^\((\d+)\) ", text, re.M)]
        rest = text.partition("条放不下，只列编号：")[2]
        only = [int(n) for n in re.findall(r"\d+", rest.split("\n标着〔敏感〕")[0])]
        self.assertEqual(sorted(shown + only), sorted(e.no for e in entries))  # 编号全部列出来了，每个只列一次
        return shown, only

    async def test_every_number_is_listed_even_when_entries_do_not_fit(self):
        from dsbot.commands import dm_memory_text, memory_list_text

        bot = self.bot
        bot.cfg.engine.memory_max_entries = 2000  # 上限调得很大：截到 20 个字也放不下
        for i in range(900):
            bot.memory.add(GUILD, f"第{i}条" + "很长的约定" * 38, via="command")
        text = memory_list_text(bot, GUILD)
        shown, only = self.numbers_listed(text, bot.memory.entries(GUILD))
        self.assertIn("\n(1) 第0条", text)
        self.assertGreater(len(shown), 600)  # 放得下的照样列内容，放不下的才只列编号
        self.assertTrue(only)
        for i in range(900, 1300):  # 更多：只列编号的也超过一条消息
            bot.memory.add(GUILD, f"第{i}条" + "很长的约定" * 38, via="command")
        text = memory_list_text(bot, GUILD)
        shown, only = self.numbers_listed(text, bot.memory.entries(GUILD))
        self.assertGreater(len(", ".join(map(str, only))), 2000)
        # 私信记忆也一样：记得多、一次发不完时每条截短，编号照样全部列出来
        bot.cfg.engine.dm_memory_max_entries = 150
        for i in range(100):
            bot.memory.dm_add(11, f"第{i}条" + "很长的私信记忆" * 28, sensitive=i % 2 == 0, via="chat")
        text = dm_memory_text(bot, 11)
        self.assertIn("每条只显示前 120 个字", text)  # 截得越少越好：120 字放得下就不截到 80
        for i in range(100, 150):
            bot.memory.dm_add(11, f"第{i}条" + "很长的私信记忆" * 28, sensitive=i % 2 == 0, via="chat")
        text = dm_memory_text(bot, 11)
        self.numbers_listed(text, bot.memory.dm_entries(11))
        self.assertIn("每条只显示前", text)
        self.assertIn("查看时在「编号」一栏选它", text)  # 怎么看全一条
        self.assertIn("〔敏感〕", text)
        self.assertIn("判错了就改", text)

    async def test_lists_as_many_entries_with_text_as_fit(self):
        # 只列编号时：前面带着内容的条目，能放几条就放几条（多列一条、少一个编号，分条时末尾空出来的长短不一样，
        # 放不下以后再往后几条可能又放得下）
        import re

        from dsbot.commands import REPLY_PARTS, dm_memory_text, memory_list_text, numbers_lines, split_message

        def most(text, entries, line, tail=()):
            cap = int(re.search(r"每条只显示前 (\d+) 个字", text).group(1))
            body = [line(e, e.text if len(e.text) <= cap else e.text[: cap - 1] + "…") for e in entries]
            lines = text.split("\n")
            head = lines[: lines.index(body[0])]
            numbers = [e.no for e in entries]

            def fits(k):
                return len(split_message("\n".join(head + body[:k] + numbers_lines(numbers, k) + list(tail)))) <= REPLY_PARTS

            return max(k for k in range(len(body) + 1) if fits(k))

        bot = self.bot
        bot.cfg.engine.memory_max_entries = bot.cfg.engine.dm_memory_max_entries = 5000
        for i in range(1371):
            bot.memory.add(GUILD, f"第{i}条" + "很长的约定" * 38, via="command")
        text = memory_list_text(bot, GUILD)
        shown, _ = self.numbers_listed(text, bot.memory.entries(GUILD))
        self.assertEqual(len(shown), most(text, bot.memory.entries(GUILD), lambda e, t: f"({e.no}) {t}"))
        for i in range(1310):
            bot.memory.dm_add(11, f"第{i}条" + "很长的私信记忆" * 28, sensitive=i % 2 == 0, via="chat")
        text = dm_memory_text(bot, 11)
        shown, _ = self.numbers_listed(text, bot.memory.dm_entries(11))
        tail = text.split("\n")[-3:]  # 最后三行是说明（敏感、可共享、怎么改）
        self.assertTrue(tail[0].startswith("标着〔敏感〕"))
        self.assertEqual(len(shown), most(text, bot.memory.dm_entries(11), lambda e, t: f"({e.no}) {t}" + ("〔敏感〕" if e.sensitive else ""), tail))

    async def test_capped_lists_shorten_server_names_too(self):
        import re

        from dsbot.commands import do_link, memory_list_text

        long_name = "名字很长的游戏服" * 8
        bot = owner_bot([NS(id=GUILD, name="主服"), NS(id=OTHER, name=long_name)])
        bot.cfg.engine.memory_max_entries = 1000
        for i in range(300):
            bot.memory.add(OTHER, f"游戏服{i:03d}" + "很长的约定" * 38, via="command", origin=OTHER)
        bot.memory.add(GUILD, "主服的事", via="command", origin=GUILD)
        do_link(bot, GUILD, OTHER)
        text = memory_list_text(bot, GUILD)
        self.assertIn("每条只显示前", text)
        labels = re.findall(r"（(「[^」]*」)记的）", text)
        self.assertTrue(labels)
        self.assertTrue(all(len(label) <= 22 for label in labels), labels[:3])  # 截短时服务器名也截到 20 字
        from dsbot.commands import memory_entry_text

        self.assertTrue(memory_entry_text(bot, GUILD, 1).endswith(f"（「{long_name}」记的）"))  # 只看一条：服务器名写全

    async def test_one_entry_in_full(self):
        from dsbot.commands import do_link, memory_entry_text

        bot = self.bot
        bot.memory.add(OTHER, "游戏服的事" + "很长的约定" * 38, via="command", origin=OTHER)
        bot.memory.add(GUILD, "主服的事", via="command", origin=GUILD)
        self.assertEqual(memory_entry_text(bot, OTHER, 1), "(1) 游戏服的事" + "很长的约定" * 38)
        self.assertIn("没有第 9 条", memory_entry_text(bot, OTHER, 9))
        do_link(bot, GUILD, OTHER)
        self.assertEqual(memory_entry_text(bot, GUILD, 1), "(1) 游戏服的事" + "很长的约定" * 38 + "（「游戏服」记的）")
        self.assertEqual(memory_entry_text(bot, GUILD, 2), "(2) 主服的事")
        # /dsadmin 长期记忆 查看 选了编号：只看这一条
        from dsbot.commands import register_commands

        added = []
        bot.tree = NS(add_command=lambda c, **kw: added.append(c), command=lambda **kw: (lambda f: f), error=lambda f: f)
        bot.secrets = NS(owner_id=OWNER, control_guild_ids=())
        register_commands(bot)
        command = next(c for c in added if c.name == "dsadmin").get_command("长期记忆").get_command("查看").callback
        ask = NS(guild_id=GUILD, user=NS(id=OWNER), response=FakeResponse(), followup=NS(send=None))
        await command(ask, 2)
        self.assertEqual(ask.response.sent[-1][0], "(2) 主服的事")

    async def test_asks_before_doing_anything(self):
        from dsbot.commands import group_command

        bot, ask = self.bot, self.interaction()
        await group_command(bot, ask, "link", str(GUILD), str(OTHER))
        text, view, ephemeral = ask.response.sent[-1]
        self.assertIn("连成一组？", text)
        self.assertTrue(ephemeral)
        self.assertIsNone(bot.store.group_of(GUILD))  # 还没确认：什么都没改
        stranger = self.interaction(user=99)
        await view.confirm(stranger)
        self.assertIn("只有发起的人能确认", stranger.response.sent[-1][0])
        self.assertIsNone(bot.store.group_of(GUILD))
        await view.confirm(ask)
        self.assertIn("已把「主服」连进组 1", ask.response.edited[-1])
        self.assertIsNotNone(bot.store.group_of(GUILD))

    async def test_bad_input(self):
        from dsbot.commands import group_command

        bot = self.bot
        for args, expected in (
            (("link", "主服", None), "从弹出的列表里选一个服务器"),
            (("link", str(GUILD), None), "在「连到」一栏"),
            (("link", str(GUILD), "999"), "bot 不在 999"),
            (("link", str(GUILD), str(GUILD)), "同一个服务器"),
            (("leave", str(GUILD), None), "不在任何组里"),
        ):
            ask = self.interaction()
            await group_command(bot, ask, *args)
            self.assertIn(expected, ask.response.sent[-1][0])
        self.assertEqual(bot.store.groups(), {})

    async def test_stale_confirmation_is_checked_again(self):
        from dsbot.commands import group_command

        bot = self.bot
        ask = self.interaction()
        await group_command(bot, ask, "link", str(GUILD), str(OTHER))
        _, view, _ = ask.response.sent[-1]
        link(bot.store, bot.cards, bot.memory, GUILD, OTHER, now=1)  # 确认之前已经连上了
        await view.confirm(ask)
        self.assertIn("没办成：这两个服务器已经在同一组里了", ask.response.edited[-1])

    async def test_confirmation_only_runs_what_it_showed(self):
        from dsbot.commands import group_command

        bot = self.bot
        first, second = self.interaction(), self.interaction()
        await group_command(bot, first, "link", str(GUILD), str(OTHER))  # 预览：主服和游戏服连成新的一组
        await group_command(bot, second, "link", str(OTHER), str(THIRD))  # 预览：游戏服和第三服连成新的一组
        await second.response.sent[-1][1].confirm(second)
        await first.response.sent[-1][1].confirm(first)  # 这时候主服会加进游戏服和第三服的组：和预览的不一样
        self.assertIn("情况变了", first.response.edited[-1])
        self.assertIsNone(bot.store.group_of(GUILD))

    async def test_errors_are_reported_and_finished_next_time(self):
        from dsbot.commands import do_unlink, group_command, recover_groups

        bot = self.bot
        bot.cards.put(OTHER, 11, "阿明", "卡", via="command")
        link(bot.store, bot.cards, bot.memory, GUILD, OTHER, now=1)
        link(bot.store, bot.cards, bot.memory, THIRD, OTHER, now=2)
        g = bot.store.group_of(OTHER)
        real = bot.cards.copy_scope
        bot.cards.copy_scope = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("磁盘满了"))
        before = list(bot.changed)
        with self.assertRaises(RuntimeError):
            do_unlink(bot, THIRD)
        self.assertEqual(bot.store.leaving(), [(THIRD, g)])
        self.assertEqual(bot.store.memory_scope(THIRD), g)  # 复制没成：照常用组里那份（不会用一份不完整的）
        self.assertEqual(bot.changed, before)  # 什么都没变：不用撤回
        bot.memory.add(bot.store.memory_scope(THIRD), "这期间记下的", via="chat")
        bot.cards.copy_scope = real
        recover_groups(bot)  # 下次执行组命令时接着做完
        self.assertEqual((bot.store.leaving(), bot.store.memory_scope(THIRD)), ([], THIRD))
        self.assertIn((1, "这期间记下的"), texts(bot.memory, THIRD))  # 期间记下的也带过去了
        self.assertEqual(bot.changed[-1], ([THIRD, OTHER, GUILD], True))
        ask = self.interaction()
        await group_command(bot, ask, "leave", str(GUILD), None)
        view = ask.response.sent[-1][1]
        view.run = lambda: (_ for _ in ()).throw(RuntimeError("坏了"))
        await view.confirm(ask)
        self.assertIn("出错了：坏了", ask.response.edited[-1])

    async def test_clear_confirmation_checks_the_group(self):
        from dsbot.commands import ConfirmClear

        bot = self.bot
        bot.memory.add(GUILD, "主服的事", via="command")
        bot.memory.add(OTHER, "游戏服的事", via="command")
        view = ConfirmClear(bot, GUILD, OWNER)  # 问的是“清空这个服务器的 1 条”
        link(bot.store, bot.cards, bot.memory, GUILD, OTHER, now=1)  # 点之前连进了组
        ask = self.interaction()
        await view.children[0].callback(ask)
        self.assertIn("没清空", ask.response.edited[-1])
        self.assertEqual(bot.memory.count(bot.store.group_of(GUILD)), 2)

    async def test_clear_confirmation_notices_another_server_joining(self):
        from dsbot.commands import ConfirmClear

        bot = self.bot
        bot.memory.add(GUILD, "主服的事", via="command")
        bot.memory.add(THIRD, "第三服的事", via="command")
        link(bot.store, bot.cards, bot.memory, GUILD, OTHER, now=1)
        view = ConfirmClear(bot, GUILD, OWNER)  # 问的是“清空组里的 1 条”
        link(bot.store, bot.cards, bot.memory, THIRD, OTHER, now=2)  # 点之前第三服连进了同一组：它的记忆并了进来
        ask = self.interaction()
        await view.children[0].callback(ask)
        self.assertIn("没清空", ask.response.edited[-1])
        self.assertEqual(bot.memory.count(bot.store.group_of(GUILD)), 2)  # 第三服的那条还在


if __name__ == "__main__":
    unittest.main()
