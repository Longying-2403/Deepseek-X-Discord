"""每个人在所有服务器共用一张人设卡（config.toml 的 [bot] shared_cards）；CONTROL_GUILD_ID 可以填几个服务器（见 test_units）。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from helpers import GUILD, msg
from test_dm import OTHER, chats, dm
from test_dm_memory import HALL, setup
from test_memory import ACTIONS, REPLY, chat, is_review, system_text, three_rounds, user_text
from test_server_groups import THIRD, owner_bot, talk

from dsbot.config import load_config
from dsbot.groups import group_name, leftovers, link, plan_link, plan_unlink, unlink
from dsbot.memory import SHARED, CardStore, MemoryStore, SharedCardMarks
from dsbot.prompts import build_review_prompt, build_system_prompt
from dsbot.store import Store

FOURTH = 4
ROOT = Path(__file__).resolve().parents[1]


def versions(cards, scope, uid):
    """旧版本（从早到晚）：(内容, 是不是从别处并进来的)。"""
    rows = cards.db.execute(
        "SELECT text, merged FROM card_versions WHERE guild_id = ? AND user_id = ? ORDER BY id", (scope, uid)
    ).fetchall()
    return [(r["text"], r["merged"]) for r in rows]


def names(cards, scope):
    rows = cards.db.execute("SELECT user_id, seen_in, name FROM card_seen WHERE guild_id = ?", (scope,)).fetchall()
    return {(r["user_id"], r["seen_in"]): r["name"] for r in rows}


def share(cards, store=None, on=True):
    store = store if store is not None else Store(":memory:")
    alive = set(store.groups()) | {g for _, g in store.leaving()}
    return cards.set_shared(on, scope_of=store.memory_scope, alive=alive)


def shared_setup(responder=None):
    """setup() 的两个服务器（GUILD 测试服、OTHER 别的服），改成所有服务器共用一张人设卡。"""
    engine, llm, transport, clock, memory = setup(responder)
    share(engine.cards, engine.store)
    return engine, llm, transport, clock, memory


class Config(unittest.TestCase):
    def test_switch_in_config_toml(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "config.toml")

            def load(text):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                return load_config(path)

            self.assertFalse(load("").bot.shared_cards)  # 默认：每个服务器一张
            self.assertFalse(load('[bot]\nlog_level = "INFO"\n').bot.shared_cards)
            self.assertTrue(load("[bot]\nshared_cards = true\n").bot.shared_cards)
            self.assertTrue(load('[bot]\nshared_cards = "开"\n').bot.shared_cards)
            self.assertFalse(load('[bot]\nshared_cards = "false"\n').bot.shared_cards)
            with self.assertRaises(ValueError):  # 填错了不猜：猜错了会把卡并起来、分开
                load('[bot]\nshared_cards = "maybe"\n')
        shipped = load_config(ROOT / "config.toml")
        self.assertFalse(shipped.bot.shared_cards)
        self.assertEqual(shipped.unknown, [])


class Switching(unittest.TestCase):
    def test_each_person_ends_up_with_one_card(self):
        store = Store(":memory:")
        g = store.new_group_id()
        store.join_group(THIRD, g, now=1)
        store.join_group(FOURTH, g, now=1)
        cards = CardStore(":memory:")
        cards.put(GUILD, 11, "阿明", "测试服的卡", via="command", now=10)
        cards.put(OTHER, 11, "Ming", "别的服旧卡", via="command", now=15)
        cards.put(OTHER, 11, "Ming", "别的服的卡", via="command", now=20)  # 最近改过的：当共用的那张
        cards.put(g, 12, "小雨", "组里的卡", via="command", now=30)
        cards.note_seen(g, 12, THIRD, "小雨")
        cards.note_seen(g, 12, FOURTH, "Rain")
        change = share(cards, store)
        self.assertEqual((change.shared, change.people, change.merged), (True, 2, 1))
        self.assertTrue(cards.shared)
        card = cards.get(SHARED, 11)
        self.assertEqual((card.text, card.name, card.updated_at), ("别的服的卡", "Ming", 20))  # 版本号不变：附过的不用重新附
        # 测试服那张存成旧版本，记成从别处并进来的：改回上一版先换成它，再改回上一版就换回来
        self.assertEqual(versions(cards, SHARED, 11), [("别的服旧卡", 0), ("测试服的卡", 1)])
        self.assertTrue(cards.swaps_on_restore(SHARED, 11))
        self.assertEqual(cards.restore_previous(SHARED, 11), "测试服的卡")
        self.assertEqual(cards.restore_previous(SHARED, 11), "别的服的卡")
        self.assertEqual(cards.get(SHARED, 12).text, "组里的卡")
        # 记下他在哪些服务器出现过、在那里叫什么（@ 人、按名字查卡、改回每个服务器一张时用）
        self.assertEqual(names(cards, SHARED), {(11, GUILD): "阿明", (11, OTHER): "Ming", (12, THIRD): "小雨", (12, FOURTH): "Rain"})
        for scope in (GUILD, OTHER, g):  # 别处都空了
            self.assertEqual((cards.count(scope), cards.users(scope), cards.seen_in(scope)), (0, set(), {}))
        self.assertIsNone(share(cards, store))  # 已经是共用的、也没东西要扫：什么也不做

    def test_versions_keep_other_servers_cards_first(self):
        cards = CardStore(":memory:")
        for i in range(6):
            cards.put(OTHER, 11, "阿明", f"别的服v{i}", via="command", now=10 + i)  # 现在是 v5，旧版本 v0～v4
        cards.put(GUILD, 11, "阿明", "测试服的卡", via="command", now=5)
        cards.put(GUILD, 11, "阿明", "测试服的新卡", via="command", now=6)
        cards.put(THIRD, 11, "阿明", "第三服的卡", via="command", now=8)
        cards.put(THIRD, 12, "小雨", "一样的卡", via="command", now=1)
        cards.put(GUILD, 12, "小雨", "一样的卡", via="command", now=2)
        share(cards)
        self.assertEqual(cards.get(SHARED, 11).text, "别的服v5")
        # 改回上一版的顺序：先是别的服务器现在的卡（新的先），再是这张自己的旧版本，最后是别处的旧版本；一共最多 5 个
        self.assertEqual(
            versions(cards, SHARED, 11),
            [("别的服v2", 0), ("别的服v3", 0), ("别的服v4", 0), ("测试服的新卡", 1), ("第三服的卡", 1)],
        )
        self.assertEqual((cards.get(SHARED, 12).updated_at, versions(cards, SHARED, 12)), (2, []))  # 一样的不重复留

    def test_deleted_cards_do_not_come_back(self):
        cards = CardStore(":memory:")
        share(cards)
        cards.put(SHARED, 11, "阿明", "共用的卡", via="command", now=10)
        cards.delete(SHARED, 11, now=100)  # 本人清空：所有服务器都没了
        cards.put(GUILD, 11, "阿明", "降级到旧版本时记下的", via="chat", now=50)  # 记在服务器名下、又比删卡早
        cards.put(OTHER, 12, "小雨", "降级期间新写的", via="chat", now=200)
        change = share(cards)  # 启动时照样扫一遍
        self.assertEqual(change.people, 2)
        self.assertIsNone(cards.get(SHARED, 11))  # 比删卡早的不顶上来
        self.assertEqual(versions(cards, SHARED, 11), [("降级到旧版本时记下的", 0)])  # 只存成旧版本：本人想要能改回来
        self.assertEqual(cards.get(SHARED, 12).text, "降级期间新写的")
        self.assertEqual(cards.deleted_at(SHARED, 11), 100)

    def test_review_skips_chat_from_before_any_deletion(self):
        cards = CardStore(":memory:")
        cards.put(GUILD, 11, "阿明", "卡", via="command", now=1)
        cards.delete(GUILD, 11, now=100)  # 共用之前在测试服删过
        share(cards)
        self.assertIsNone(cards.deleted_at(SHARED, 11))  # 共用的那张没被删过（不挡着以后分回去）
        self.assertEqual(cards.forgotten_at(SHARED, 11), 100)  # 但整理记忆时，测试服删卡之前的聊天也不拿来写
        self.assertEqual(cards.forgotten_at(GUILD, 11), 100)

    def test_dead_groups_are_left_alone(self):
        store = Store(":memory:")
        cards = CardStore(":memory:")
        cards.put(-5, 13, "小绿", "散掉的组剩下的", via="command", now=1)
        share(cards, store)
        self.assertEqual(cards.get(-5, 13).text, "散掉的组剩下的")  # 文件之间对不上留下的：不动
        self.assertIsNone(cards.get(SHARED, 13))
        self.assertEqual(leftovers(store, cards, None), {-5: (1, 0)})  # 照样报出来

    def test_back_to_one_card_per_server(self):
        store = Store(":memory:")
        g = store.new_group_id()
        store.join_group(THIRD, g, now=1)
        store.join_group(FOURTH, g, now=1)
        cards = CardStore(":memory:")
        share(cards, store)
        cards.put(SHARED, 11, "阿明", "旧的", via="command", now=1)
        cards.put(SHARED, 11, "阿明", "共用的卡", via="command", now=2)
        for guild, name in ((GUILD, "阿明"), (OTHER, "Ming"), (THIRD, ""), (FOURTH, "明")):
            cards.note_seen(SHARED, 11, guild, name)
        cards.put(SHARED, 12, "小雨", "没在哪出现过", via="command", now=3)
        change = share(cards, store, on=False)
        self.assertEqual((change.shared, change.people, change.stayed), (False, 1, 1))
        self.assertFalse(cards.shared)
        self.assertEqual((cards.get(GUILD, 11).text, cards.get(GUILD, 11).name, cards.get(GUILD, 11).updated_at), ("共用的卡", "阿明", 2))
        self.assertEqual(cards.get(OTHER, 11).name, "Ming")  # 卡上的名字用他在那里的名字
        self.assertEqual(cards.get(g, 11).text, "共用的卡")  # 连成一组的：放在组里
        self.assertEqual(names(cards, g), {(11, THIRD): "", (11, FOURTH): "明"})  # 组里记着他在哪几个服务器出现过
        self.assertEqual(versions(cards, OTHER, 11), [("旧的", 0)])  # 旧版本也跟着
        self.assertIsNone(cards.get(SHARED, 11))
        self.assertEqual(cards.get(SHARED, 12).text, "没在哪出现过")  # 不知道放哪：留着，再改成共用时还在
        self.assertIsNone(share(cards, store, on=False))  # 已经是每个服务器一张
        share(cards, store)
        self.assertEqual(cards.get(SHARED, 12).text, "没在哪出现过")
        self.assertEqual(cards.get(SHARED, 11).text, "共用的卡")  # 分回去的几份一样：并回来还是一张

    def test_a_card_in_use_is_kept_whatever_marks_came_with_it(self):
        store = Store(":memory:")
        cards = CardStore(":memory:")
        cards.put(OTHER, 11, "Ming", "别的服的卡", via="command", now=50)
        cards.put(GUILD, 11, "阿明", "测试服的旧卡", via="command", now=10)
        cards.delete(GUILD, 11, now=100)  # 在测试服清空了卡（比别的服那张新）
        g = link(store, cards, None, GUILD, OTHER, now=200).group_id  # 连成一组：用别的服那张，测试服删卡的记号也并进组里
        self.assertEqual((cards.get(g, 11).text, cards.deleted_at(g, 11)), ("别的服的卡", 100))
        share(cards, store)
        self.assertEqual(cards.get(SHARED, 11).text, "别的服的卡")  # 组里在用的那张照样是现在的卡
        # 分回去时复制到的服务器自己有更早的删卡记号；那边留下的那份照样算在用的
        cards.put(SHARED, 12, "小雨", "小雨的卡", via="command", now=60)
        for guild in (THIRD, FOURTH):
            cards.note_seen(SHARED, 12, guild, "小雨")
        cards.db.execute("INSERT INTO card_tombstones (guild_id, user_id, deleted_at) VALUES (?, 12, 90)", (THIRD,))
        cards.db.commit()
        share(cards, store, on=False)
        self.assertEqual(cards.get(THIRD, 12).text, "小雨的卡")
        cards.delete(FOURTH, 12, now=300)  # 第四服那份删了，第三服那份还在用
        share(cards, store)
        self.assertEqual(cards.get(SHARED, 12).text, "小雨的卡")

    def test_no_duplicate_old_versions(self):
        cards = CardStore(":memory:")
        for i, text in enumerate(("v1", "v2", "v3", "v4", "X", "Y")):
            cards.put(GUILD, 11, "阿明", text, via="command", now=10 + i)  # 测试服：现在是 Y，旧版本 v1～v4、X
        cards.put(OTHER, 11, "阿明", "X", via="command", now=1)  # 别的服那张也是 X
        share(cards)
        self.assertEqual(cards.get(SHARED, 11).text, "Y")
        self.assertEqual(versions(cards, SHARED, 11), [("v1", 0), ("v2", 0), ("v3", 0), ("v4", 0), ("X", 1)])  # X 只留一份

    def test_servers_with_only_a_cleared_card_do_not_count_as_seen(self):
        cards = CardStore(":memory:")
        cards.put(GUILD, 11, "阿明", "测试服的卡", via="command", now=1)
        cards.put(GUILD, 11, "阿明", "", via="chat", now=2)  # 叫它忘掉：只剩旧版本
        cards.put(OTHER, 11, "Ming", "别的服的卡", via="command", now=3)
        share(cards)
        self.assertEqual(names(cards, SHARED), {(11, OTHER): "Ming"})  # 和改之前一样：测试服没有他的卡
        self.assertIn(("测试服的卡", 1), versions(cards, SHARED, 11))  # 旧版本还在

    def test_people_only_seen_in_dms_stay_that_way_after_switching_back_and_forth(self):
        store = Store(":memory:")
        cards = CardStore(":memory:")
        share(cards, store)
        cards.put(SHARED, 11, "SecretFox", "卡", via="command", now=1)  # 卡是在别的服写的：卡上是那边的昵称
        cards.note_seen(SHARED, 11, OTHER, "SecretFox")
        cards.note_seen(SHARED, 11, GUILD, "")  # 测试服：只在私信里出现过
        share(cards, store, on=False)
        # 每个服务器一张：测试服那份不知道他叫什么，名字空着（不拿别的服的昵称顶上：不然测试服就列出他、能按那个名字 @ 他）
        self.assertEqual((cards.get(GUILD, 11).name, cards.get(OTHER, 11).name), ("", "SecretFox"))
        self.assertEqual((cards.names(GUILD), cards.owners(GUILD), cards.find(GUILD, "SecretFox")), ([], [], []))
        share(cards, store)
        self.assertEqual(names(cards, SHARED), {(11, OTHER): "SecretFox", (11, GUILD): ""})
        self.assertEqual(cards.named_here(SHARED, GUILD), [])
        # 分回去以后他在测试服的频道里说过话（名字补上了）：再改成共用时就按这个名字算他在测试服出现过
        share(cards, store, on=False)
        cards.touch_name(GUILD, 11, "明明")
        share(cards, store)
        self.assertEqual(names(cards, SHARED), {(11, OTHER): "SecretFox", (11, GUILD): "明明"})
        # 分回去以后测试服连进了组（每个服务器一张时）：组里照样记着他在测试服只在私信里出现过
        cards.note_seen(SHARED, 11, THIRD, "")
        share(cards, store, on=False)
        g = link(store, cards, None, THIRD, FOURTH, now=10).group_id
        self.assertEqual(names(cards, g)[(11, THIRD)], "")
        share(cards, store)
        self.assertEqual(names(cards, SHARED)[(11, THIRD)], "")
        # 改回去时那个服务器已经在组里：组里那份卡上的名字也不拿别处的昵称顶上（组里都只在私信里出现过就空着）
        cards.note_seen(SHARED, 11, FOURTH, "")
        share(cards, store, on=False)
        self.assertEqual(cards.get(g, 11).name, "")
        self.assertEqual((cards.owners_seen(g, THIRD), cards.find_here(g, "SecretFox", THIRD)), ([], []))
        share(cards, store)
        cards.note_seen(SHARED, 11, FOURTH, "阿四")  # 在组里的第四服说过话：分回去时用这个名字
        share(cards, store, on=False)
        self.assertEqual(cards.get(g, 11).name, "阿四")

    def test_deletion_marks_follow_servers_through_groups_while_shared(self):
        store = Store(":memory:")
        cards = CardStore(":memory:")
        cards.put(GUILD, 11, "阿明", "卡", via="command", now=1)
        cards.delete(GUILD, 11, now=100)  # 在测试服删了卡
        share(cards, store)
        g = link(store, cards.for_groups(), None, GUILD, OTHER, now=200).group_id  # 共用期间连组：卡不动，删卡的记号并进组里
        self.assertEqual((cards.deleted_at(g, 11), cards.count(g)), (100, 0))
        share(cards, store, on=False)  # 改回每个服务器一张：测试服用组里那份，整理记忆照样不拿删之前的聊天写卡
        self.assertEqual(cards.deleted_at(store.memory_scope(GUILD), 11), 100)
        share(cards, store)
        unlink(store, cards.for_groups(), None, GUILD, now=300)  # 共用期间退组（组散了）：记号复制给两边
        share(cards, store, on=False)
        self.assertEqual((cards.deleted_at(GUILD, 11), cards.deleted_at(OTHER, 11)), (100, 100))
        self.assertIsNone(cards.db.execute("SELECT 1 FROM card_tombstones WHERE guild_id = ?", (g,)).fetchone())

    def test_group_operations_wait_for_the_disk_while_shared(self):
        store, cards, memory = Store(":memory:"), CardStore(":memory:"), MemoryStore(":memory:")
        share(cards, store)
        seen = []
        real = memory.merge_scope

        def spy(*args, **kw):  # 连组做到一半时：卡文件也是每次提交都写进磁盘（FULL = 2）
            seen.append([db.execute("PRAGMA synchronous").fetchone()[0] for db in (store.db, cards.db, memory.db)])
            return real(*args, **kw)

        memory.merge_scope = spy
        link(store, cards.for_groups(), memory, GUILD, OTHER, now=1)
        self.assertTrue(seen and all(levels == [2, 2, 2] for levels in seen), seen)

    def test_group_mates_seen_only_in_dms_are_not_counted(self):
        cards = CardStore(":memory:")
        share(cards)
        cards.put(SHARED, 31, "SecretFox", "小红的卡", via="command")  # 卡是在不相干的服务器写的：卡上是那边的昵称
        cards.note_seen(SHARED, 31, OTHER, "")  # 只在别的服的私信里出现过（私信挂在别的服）
        # 测试服和别的服连成了组：她在别的服只在私信里出现过，不知道叫什么，卡上的名字又可能是别处的昵称：在测试服查不到，也不算
        self.assertEqual(cards.find_here(SHARED, "SecretFox", GUILD, near={OTHER}), [])
        self.assertEqual(cards.owners_near(SHARED, GUILD, {OTHER}), set())
        self.assertEqual([c.user_id for c, _, _, _ in cards.find_here(SHARED, "SecretFox", OTHER, near={GUILD})], [31])  # 在别的服能查到
        self.assertEqual(cards.owners_near(SHARED, OTHER, {GUILD}), {31})
        cards.note_seen(SHARED, 31, OTHER, "小红")  # 她在别的服的频道里说过话了：测试服就查得到、也算上
        self.assertEqual([shown for _, shown, _, _ in cards.find_here(SHARED, "小红", GUILD, near={OTHER})], ["小红"])
        self.assertEqual(cards.owners_near(SHARED, GUILD, {OTHER}), {31})

    def test_names_from_unrelated_servers_do_not_count(self):
        cards = CardStore(":memory:")
        share(cards)
        cards.put(SHARED, 24, "SecretFox", "狐狸的卡", via="command")  # 卡是在别的服写的：卡上的名字是那边的昵称
        cards.note_seen(SHARED, 24, OTHER, "SecretFox")
        cards.note_seen(SHARED, 24, GUILD, "阿狸")

        def find(name, near):
            return [(c.user_id, shown, local) for c, shown, _, local in cards.find_here(SHARED, name, GUILD, near=near)]

        self.assertEqual(find("SecretFox", set()), [])  # 在测试服按他在别的服的昵称查不到（连不上是同一个人）
        self.assertEqual(find("Fox", set()), [])
        self.assertEqual(find("阿狸", set()), [(24, "阿狸", True)])
        self.assertEqual(find("SecretFox", {OTHER}), [(24, "阿狸", True)])  # 别的服和测试服连成了组：认
        cards.put(SHARED, 25, "Rain", "小雨的卡", via="command")
        cards.note_seen(SHARED, 25, GUILD, "")  # 只在私信里出现过：不知道他在这里叫什么，只能按卡上的名字认
        self.assertEqual(find("Rain", set()), [(25, "Rain", False)])

    def test_mode_is_kept_in_the_file_and_a_failure_changes_nothing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "cards.sqlite3")
            cards = CardStore(path)
            cards.put(GUILD, 11, "阿明", "测试服的卡", via="command", now=1)
            cards.put(OTHER, 11, "阿明", "别的服的卡", via="command", now=2)
            cards.put(GUILD, 12, "小雨", "小雨的卡", via="command", now=3)
            real, calls = cards._write, []

            def broken(*args):
                calls.append(args)
                if len(calls) == 2:  # 并到第二个人时出错
                    raise RuntimeError("磁盘满了")
                return real(*args)

            cards._write = broken
            with self.assertRaises(RuntimeError):
                share(cards)
            cards._write = real
            self.assertFalse(cards.shared)  # 像没改过：还按每个服务器一张用
            self.assertEqual(
                (cards.get(GUILD, 11).text, cards.get(OTHER, 11).text, cards.get(SHARED, 11)), ("测试服的卡", "别的服的卡", None)
            )
            share(cards)
            cards.close()
            again = CardStore(path)
            self.assertTrue(again.shared)  # 记在卡文件里：重启后还是共用
            self.assertEqual(again.get(SHARED, 11).text, "别的服的卡")
            again.close()


class InChat(unittest.IsolatedAsyncioTestCase):
    async def test_card_follows_the_person_to_every_server(self):
        engine, llm, transport, clock, memory = shared_setup(
            chat([ACTIONS({"type": "card", "to": 1, "text": "自称猫娘"}, REPLY(1, "记住了")), ACTIONS(REPLY(1, "在"))])
        )
        await talk(engine, clock, 1, "@小D 记住我是猫娘")  # 在测试服说的
        self.assertEqual(engine.cards.get(SHARED, 11).text, "自称猫娘")
        self.assertIsNone(engine.cards.get(GUILD, 11))
        await talk(engine, clock, 2, guild_id=OTHER, channel_id=HALL)  # 到别的服
        call = chats(llm)[-1]
        self.assertIn("自称猫娘", user_text(call))  # 卡跟着他走
        system = system_text(call)
        self.assertIn("人设卡：能用（所有服务器共用，每人一张，每张最多 1600 字）", system)
        self.assertIn("## 人设卡是所有服务器共用的", system)
        self.assertNotIn("## 服务器组", system)
        self.assertEqual(engine.cards.seen_in(SHARED), {11: {GUILD, OTHER}})
        await talk(engine, clock, 3, guild_id=OTHER, channel_id=HALL)
        self.assertIn("有卡的人：阿明", system_text(chats(llm)[-1]))  # 在这里出现过：列出来
        self.assertEqual(memory.count(SHARED), 0)  # 长期记忆不受影响

    async def test_people_from_unrelated_servers_cannot_be_looked_up(self):
        who = lambda name: ACTIONS({"type": "who", "name": name})  # noqa: E731
        outputs = [who("小明"), ACTIONS(REPLY(1, "好")), who("小明"), ACTIONS(REPLY(1, "好"))]
        outputs += [who("小绿"), ACTIONS(REPLY(1, "好")), who("小绿"), ACTIONS(REPLY(1, "好"))]
        engine, llm, transport, clock, memory = shared_setup(chat(outputs))
        engine.cards.put(SHARED, 21, "小明", "测试服小明：自称猫娘", via="command")
        engine.cards.note_seen(SHARED, 21, GUILD, "小明")  # 只在测试服出现过
        engine.cards.put(SHARED, 22, "阿花", "阿花的卡", via="command")
        engine.cards.note_seen(SHARED, 22, GUILD, "阿花")
        engine.cards.note_seen(SHARED, 22, OTHER, "小明")  # 阿花在别的服叫“小明”
        engine.cards.put(SHARED, 23, "小绿", "小绿的卡", via="command")
        engine.cards.note_seen(SHARED, 23, GUILD, "小绿")  # 只在测试服出现过
        await talk(engine, clock, 1, "@小D 小明是谁", guild_id=OTHER, channel_id=HALL)  # 别的服问：这里叫小明的是阿花
        call = chats(llm)[-1]
        self.assertIn("阿花的卡", user_text(call))
        self.assertNotIn("测试服小明", user_text(call))
        self.assertNotIn("另外还有人有卡", system_text(call))  # 没连组：别的服务器的人不提
        await talk(engine, clock, 3, "@小D 小明是谁")  # 测试服问：是测试服的小明，阿花在别的服的名字不算
        self.assertIn("测试服小明：自称猫娘", user_text(chats(llm)[-1]))
        self.assertNotIn("阿花的卡", user_text(chats(llm)[-1]))
        await talk(engine, clock, 5, "@小D 小绿是谁", guild_id=OTHER, channel_id=HALL)  # 只在测试服出现过的人：别的服查不到
        self.assertIn("（没有「小绿」的人设卡。）", user_text(chats(llm)[-1]))
        self.assertNotIn("小绿的卡", user_text(chats(llm)[-1]))
        link(engine.store, engine.cards.for_groups(), memory, OTHER, GUILD, now=clock())  # 连成一组（不动卡）：组里别的服务器的人查得到
        await talk(engine, clock, 7, "@小D 小绿是谁", guild_id=OTHER, channel_id=HALL)
        call = chats(llm)[-1]
        self.assertIn("【小绿】（还没在这个服务器出现过：是别的服务器记下的人，别当成这里同名的人）", user_text(call))
        self.assertIn("另外还有人有卡，没在这里的频道出现过", system_text(call))

    async def test_review_writes_the_shared_card(self):
        review = json.dumps({"cards": [{"to": 1, "text": "阿明说他是猫娘"}], "memory": [{"add": "周五打副本"}]}, ensure_ascii=False)
        engine, llm, transport, clock, memory = shared_setup(chat([ACTIONS()], review=review))
        await three_rounds(engine, clock)
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertEqual(engine.cards.get(SHARED, 11).text, "阿明说他是猫娘")
        self.assertIsNone(engine.cards.get(GUILD, 11))
        self.assertEqual([m.text for m in memory.entries(GUILD)], ["周五打副本"])  # 长期记忆还是这个服务器自己的
        reviews = [c for c in llm.calls if is_review(c)]
        self.assertIn("人设卡是所有服务器共用的（每人一张，别的服务器也会用上）", system_text(reviews[-1]))
        self.assertIn(GUILD, engine.cards.seen_in(SHARED)[11])

    async def test_review_skips_chat_from_before_a_deletion_elsewhere(self):
        review = json.dumps({"cards": [{"to": 1, "text": "阿明说他是猫娘"}], "memory": []}, ensure_ascii=False)
        engine, llm, transport, clock, memory = setup(chat([ACTIONS()], review=review))
        await three_rounds(engine, clock)
        engine.cards.put(GUILD, 11, "阿明", "要删的", via="command")
        engine.cards.delete(GUILD, 11, now=clock())  # 在测试服删了卡（还没改成共用）
        share(engine.cards, engine.store)  # 改成共用（重启）
        clock.advance(600)
        await engine.tick()
        await engine.drain()
        self.assertIsNone(engine.cards.get(SHARED, 11))  # 删之前的聊天不拿来写共用的那张

    async def test_dm_uses_the_shared_card(self):
        engine, llm, transport, clock, memory = shared_setup()
        engine.cards.put(SHARED, 11, "阿明", "自称猫娘", via="command")
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        call = chats(llm)[-1]
        self.assertIn("下面的人设、长期记忆是那个服务器的，人设卡是所有服务器共用的（每人一张）", system_text(call))
        self.assertIn("和他的人设卡（所有服务器都会用上这张卡）；", system_text(call))
        self.assertIn("自称猫娘", user_text(call))

    async def test_people_only_seen_in_dms_are_counted(self):
        engine, llm, transport, clock, memory = shared_setup(chat([ACTIONS(REPLY(1, "在"))]))
        engine.cards.put(SHARED, 11, "阿明", "自称猫娘", via="command")
        engine.cards.note_seen(SHARED, 11, GUILD, "")  # 测试服：只在私信里出现过
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertIn("有卡的人：阿明", system_text(chats(llm)[-1]))  # 私信里：对面这个人的卡附着呢，列上他
        await talk(engine, clock, 2, author=12, name="小雨")  # 测试服的频道：不列他，但说还有人有卡
        system = system_text(chats(llm)[-1])
        self.assertNotIn("有卡的人：阿明", system)
        self.assertIn("另外还有人有卡，没在这里的频道出现过", system)

    async def test_a_card_without_a_name_is_still_listed_in_his_dm(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        engine.cards.put(GUILD, 11, "", "自称猫娘", via="command")  # 改回每个服务器一张时，他在这里只在私信里出现过：名字空着
        await engine.handle_message(dm(1, "在吗"))
        await engine.drain()
        self.assertIn("有卡的人：阿明", system_text(chats(llm)[-1]))
        self.assertIn("自称猫娘", user_text(chats(llm)[-1]))

    async def test_not_shared_the_prompts_are_unchanged(self):
        engine, llm, transport, clock, memory = setup(chat([ACTIONS(REPLY(1, "在"))]))
        engine.cards.put(GUILD, 11, "阿明", "自称猫娘", via="command")
        await talk(engine, clock, 1)
        system = system_text(chats(llm)[-1])
        self.assertIn("人设卡：能用（每张最多 1600 字），有卡的人：阿明", system)
        self.assertNotIn("所有服务器共用", system)


class Prompts(unittest.TestCase):
    def test_group_and_shared_wording(self):
        grouped = build_system_prompt(guild="G", channel="c", name="小D", persona="", summary=None, cards=[], group=["B"])
        self.assertIn("- 人设卡、长期记忆是组里共用的", grouped)
        both = build_system_prompt(guild="G", channel="c", name="小D", persona="", summary=None, cards=[], group=["B"], cards_shared=True)
        self.assertIn("- 长期记忆是组里共用的", both)
        self.assertIn("## 人设卡是所有服务器共用的", both)
        off = build_system_prompt(guild="G", channel="c", name="小D", persona="", summary=None, cards=None, cards_shared=True)
        self.assertNotIn("人设卡是所有服务器共用的", off)  # 这里没开人设卡：不提
        review = build_review_prompt(
            name="小D", cards=True, memory=True, card_max=1, entry_max=1, count=0, max_entries=1, group="G", shared="G"
        )
        self.assertIn("长期记忆是组里共用的；人设卡是所有服务器共用的（每人一张）", review)
        alone = build_review_prompt(name="小D", cards=True, memory=True, card_max=1, entry_max=1, count=0, max_entries=1, shared="G")
        self.assertIn("这段聊天在服务器「G」里。人设卡是所有服务器共用的", alone)
        no_cards = build_review_prompt(name="小D", cards=False, memory=True, card_max=1, entry_max=1, count=0, max_entries=1, shared="G")
        self.assertNotIn("人设卡是所有服务器共用的", no_cards)


class Owner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")
        self.bot = owner_bot([NS(id=GUILD, name="主服"), NS(id=OTHER, name="游戏服"), NS(id=THIRD, name="第三服")])

    async def test_groups_leave_cards_alone(self):
        from dsbot.commands import card_where, do_link, do_unlink, group_cards, groups_text, link_preview_text, unlink_preview_text

        bot = self.bot
        share(bot.cards, bot.store)
        bot.cards.put(SHARED, 11, "阿明", "共用的卡", via="command")
        bot.cards.note_seen(SHARED, 11, GUILD, "阿明")
        bot.memory.add(OTHER, "游戏服的事", via="command")
        self.assertEqual(card_where(bot, GUILD), "所有服务器（每人一张）")
        self.assertIsInstance(group_cards(bot), SharedCardMarks)  # 卡不跟着搬，只搬删卡的记号
        self.assertIn("之后它们共用长期记忆", groups_text(bot))
        self.assertIn("人设卡是所有服务器共用的", groups_text(bot))
        preview = link_preview_text(bot, plan_link(bot.store, group_cards(bot), bot.memory, GUILD, OTHER))
        self.assertIn("组里先用「游戏服」现在的长期记忆。", preview)
        self.assertIn("人设卡本来就是所有服务器共用的", preview)
        self.assertNotIn("两边都有人设卡", preview)
        self.assertIn("人设卡是所有服务器共用的，没动", do_link(bot, GUILD, OTHER))
        g = bot.store.group_of(GUILD)
        self.assertEqual((bot.cards.get(SHARED, 11).text, bot.cards.count(g), bot.cards.seen_in(g)), ("共用的卡", 0, {}))
        self.assertIn(f"{group_name(g)}：「游戏服」、「主服」 · 共用长期记忆 1 条", groups_text(bot))
        preview = unlink_preview_text(bot, plan_unlink(bot.store, group_cards(bot), bot.memory, GUILD), GUILD)
        self.assertIn("人设卡是所有服务器共用的，不动。", preview)
        self.assertIn("带走了 1 条长期记忆（人设卡是所有服务器共用的，没动）", do_unlink(bot, GUILD))
        self.assertEqual((bot.cards.get(SHARED, 11).text, bot.cards.count(GUILD)), ("共用的卡", 0))

    async def test_dm_notices_say_the_card_is_used_everywhere(self):
        from dsbot.commands import DM_WARNING, dm_warning
        from dsbot.discord_app import DSBot

        bot = self.bot
        self.assertEqual(dm_warning(bot), DM_WARNING)  # 每个服务器一张：和原来一样
        self.assertIn("和你的人设卡来聊", DSBot._dm_first_note(bot, NS(id=GUILD, name="主服")))
        share(bot.cards, bot.store)
        self.assertIn("你的人设卡是所有服务器共用的：私信里写进人设卡的，在别的服务器也会用上。\n📒", dm_warning(bot))
        note = DSBot._dm_first_note(bot, NS(id=GUILD, name="主服"))
        self.assertIn("和你在所有服务器共用的那张人设卡来聊", note)
        self.assertIn("人设卡在所有服务器都会用上", note)

    async def test_dm_notices_mention_the_group(self):
        from dsbot.commands import dm_group_note
        from dsbot.discord_app import DSBot

        bot = self.bot
        self.assertEqual(dm_group_note(bot, GUILD), "")  # 没连组：不多说
        self.assertNotIn("连成了一组", DSBot._dm_first_note(bot, NS(id=GUILD, name="主服")))
        link(bot.store, bot.cards, bot.memory, GUILD, OTHER, now=1)
        self.assertIn("「主服」和「游戏服」连成了一组：长期记忆、人设卡是组里共用的", dm_group_note(bot, GUILD))
        note = DSBot._dm_first_note(bot, NS(id=GUILD, name="主服"))
        self.assertIn("你在那边叫我时我也可能提起私信里聊的", note)
        self.assertTrue(note.endswith(DSBot._dm_first_note(bot, NS(id=THIRD, name="第三服")).split("\n")[-1]))  # 还是以私信记忆的说明结尾
        share(bot.cards, bot.store)
        self.assertIn("连成了一组：长期记忆是组里共用的", dm_group_note(bot, GUILD))  # 人设卡本来就是所有服务器共用的
        lonely = bot.store.new_group_id()
        bot.store.join_group(THIRD, lonely, now=2)  # 组里只剩它自己（连组做到一半）：不多说，也不多一个空行
        self.assertEqual(dm_group_note(bot, THIRD), "")
        self.assertNotIn("\n\n", DSBot._dm_first_note(bot, NS(id=THIRD, name="第三服")))

    async def test_switching_waits_for_the_disk(self):
        from dsbot.commands import apply_card_mode

        bot = self.bot
        bot.cfg = NS(engine=NS(memory_max_entries=50), bot=NS(shared_cards=True))
        levels, real = [], bot.cards.set_shared

        def spy(*args, **kw):
            levels.append(bot.cards.db.execute("PRAGMA synchronous").fetchone()[0])
            return real(*args, **kw)

        bot.cards.set_shared = spy
        apply_card_mode(bot)
        self.assertEqual(levels, [2])  # 换的时候每次提交都写进磁盘
        self.assertEqual(bot.cards.db.execute("PRAGMA synchronous").fetchone()[0], 1)  # 换完改回平时的 NORMAL

    async def test_new_groups_skip_ids_left_in_the_cards_file(self):
        from dsbot.commands import do_link

        bot = self.bot
        share(bot.cards, bot.store)
        bot.cards.db.execute("INSERT INTO card_tombstones (guild_id, user_id, deleted_at) VALUES (-3, 11, 1)")  # 以前的组留下的
        bot.cards.db.commit()
        do_link(bot, GUILD, OTHER)
        self.assertEqual(bot.store.group_of(GUILD), -4)  # 改回每个服务器一张时卡会分到组里：别撞上旧组留下的

    async def test_startup_follows_config_and_keeps_the_old_way_on_errors(self):
        from dsbot.commands import apply_card_mode

        bot = self.bot
        bot.cfg = NS(engine=NS(memory_max_entries=50), bot=NS(shared_cards=True))
        bot.cards.put(GUILD, 11, "阿明", "主服的卡", via="command", now=1)
        with self.assertLogs("dsbot.commands", level="INFO") as logs:
            apply_card_mode(bot)
        self.assertTrue(bot.cards.shared)
        self.assertIn("并好了 1 个人的卡", "\n".join(logs.output))
        apply_card_mode(bot)  # 已经是共用的：什么也不做
        bot.cfg.bot.shared_cards = False
        bot.cards.set_shared = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("磁盘满了"))
        with self.assertLogs("dsbot.commands", level="ERROR"):
            apply_card_mode(bot)
        self.assertTrue(bot.cards.shared)  # 没改成：还按原来的方式用
        self.assertEqual(bot.cards.get(SHARED, 11).text, "主服的卡")


if __name__ == "__main__":
    unittest.main()
