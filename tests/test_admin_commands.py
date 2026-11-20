"""2.9.0 管理员命令改名、合并、改成子命令：/ds → /dsadmin。

- 名字：管理员的命令叫 /dsadmin（/ds 没有了）；/ds-rp、/ds-dm、/ds-music、/dsowner 照旧。
- 有几种做法的都是子命令，不用“操作”那一栏：区域（查看 / 允许 / 禁止 / 跟随上级）、表情包（查看 / 上传 / 删除）、
  长期记忆（查看 / 添加 / 删除 / 清空）。必填的栏都是必填，排在选填的前面。
- 合并：“区域列表”并进 区域 查看；“主动”“记忆共享”并进 设置（参数里多了主动发言、记忆共享；原来那两个命令会多说的
  提醒照样说）。设置 的「值」一栏会弹出能填的（开 / 关 / 默认）。
- bot 自己说的话、README 里写的命令，都是现在真的有的。"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, OWNER
from test_links_images import png

from dsbot.config import ADMIN_KEYS, SPECS, load_config
from dsbot.memory import CardStore, MemoryStore
from dsbot.store import Store

ROOT = Path(__file__).resolve().parents[1]
CATEGORY = 900
OTHER_CHANNEL = 101

WANTED = {  # 名字后面带 ? 的是选填的栏
    "区域": {"查看": [], "允许": ["范围?"], "禁止": ["范围?"], "跟随上级": ["范围?"]},
    "设置": ["参数", "值", "范围?"],
    "档位": ["档位", "范围?"],
    "人设": ["范围?"],
    "表情包": {"查看": [], "上传": ["图片", "名字?"], "删除": ["名字"]},
    "长期记忆": {"查看": ["编号?"], "添加": ["内容"], "删除": ["编号"], "清空": []},
    "兴趣": {"查看": [], "添加": ["话题"], "删除": ["话题"]},
    "提醒": {"查看": [], "取消": ["编号"]},
    "公告频道": {"设为": ["频道?"], "取消": []},
    "状态": [],
    "清空记忆": [],
}


def make_bot(case):
    """真的建一个 DSBot（不连 Discord，和 python -m dsbot --check 一样）；这个测试跑完就把它的数据库关掉。"""
    from dsbot.config import Secrets
    from dsbot.discord_app import DSBot
    from dsbot.ledger import Ledger
    from dsbot.llm import DeepSeekClient
    from dsbot.settings import Settings

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "config.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[bot]\n")
        cfg = load_config(path)
    store, cards, memory = Store(":memory:"), CardStore(":memory:"), MemoryStore(":memory:")
    for db in (store, cards, memory):
        case.addCleanup(db.close)
    llm = DeepSeekClient(api_key="x", base_url=cfg.deepseek.base_url, model=cfg.deepseek.model)
    return DSBot(
        cfg, Secrets("x", "x", OWNER, control_guild_ids=(GUILD,)), store, Settings(cfg.defaults, store),
        Ledger(store, cfg.bot.timezone), llm, cards=cards, memory=memory,
    )


def shape(option):
    """命令（或者组）在 Discord 那边的样子：组 → {子命令: …}；子命令 → [栏名，选填的带 ?]。"""
    options = option.get("options", [])
    if any(o["type"] in (1, 2) for o in options):
        return {o["name"]: shape(o) for o in options}
    return [o["name"] + ("" if o.get("required") else "?") for o in options]


class Reply:
    """假的 interaction.response：记下回的每一句、带的按钮、弹出的编辑框。"""

    def __init__(self):
        self.said, self.views, self.deferred, self.modal = [], [], False, None

    def is_done(self):
        return self.deferred

    async def send_message(self, text, view=None, ephemeral=False):
        assert ephemeral, "管理员命令的回复都只有自己看得到"
        self.said.append(text)
        self.views.append(view)

    async def defer(self, ephemeral=False, thinking=False):
        assert ephemeral
        self.deferred = True

    async def send_modal(self, modal):
        self.modal = modal

    async def edit_message(self, content=None, view=None):
        self.said.append(content)


def ask(*, channel=CHANNEL, category=CATEGORY, user=OWNER, **namespace):
    """有人在主服的一个频道里用命令（频道在类别「聊天区」里；category=None：不在任何类别里）。"""
    reply = Reply()

    async def followup(text, ephemeral=False):
        assert ephemeral
        reply.said.append(text)

    chat = NS(id=CATEGORY, name="聊天区", position=0)
    known = {CATEGORY: chat, CHANNEL: NS(id=CHANNEL, name="闲聊"), OTHER_CHANNEL: NS(id=OTHER_CHANNEL, name="大厅")}
    guild = NS(id=GUILD, name="主服", get_channel=known.get, get_channel_or_thread=known.get)
    place = NS(id=channel, category=chat if category else None, category_id=category)
    return NS(
        guild_id=GUILD, guild=guild, channel_id=channel, channel=place, user=NS(id=user, display_name="阿明", name="aming"),
        response=reply, followup=NS(send=followup), namespace=NS(**namespace),
    )


def scope(name):
    return NS(name=name, value={"本频道": "channel", "本类别": "category", "全服务器": "guild"}[name])


class WithBot(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")
        self.bot = make_bot(self)
        self.admin = next(c for c in self.bot.tree.get_commands() if c.name == "dsadmin")

    def command(self, *path):
        node = self.admin
        for name in path:
            node = node.get_command(name)
        return node

    async def run_command(self, *path, args=(), **where):
        """执行 /dsadmin <path>，返回它回的话（几条就用换行接起来）和那次的 interaction。"""
        interaction = ask(**where)
        await self.command(*path).callback(interaction, *args)
        return "\n".join(t for t in interaction.response.said if t), interaction

    def overrides(self, level, scope_id=None):
        scope_id = scope_id if scope_id is not None else {"channel": CHANNEL, "category": CATEGORY, "guild": GUILD}[level]
        return self.bot.settings.overrides(level, scope_id)


class Layout(WithBot):
    def test_names(self):
        import discord

        self.assertEqual({c.name for c in self.bot.tree.get_commands()}, {"dsadmin", "ds-rp", "ds-music", "ds-dm"})  # /ds 没有了
        self.assertEqual([c.name for c in self.bot.tree.get_commands(guild=discord.Object(id=GUILD))], ["dsowner"])
        self.assertTrue(self.admin.guild_only)  # 只在服务器里用
        self.assertEqual(self.admin.default_permissions, discord.Permissions(manage_guild=True))  # 默认只给管理员看

    def test_actions_are_subcommands(self):
        payload = self.admin.to_dict(self.bot.tree)
        self.assertEqual(shape(payload), WANTED)
        self.assertEqual(list(shape(payload)), list(WANTED))  # 在 Discord 里列出来的顺序
        for name, wanted in WANTED.items():
            if isinstance(wanted, dict):
                self.assertEqual(list(shape(payload)[name]), list(wanted), name)  # “查看”排在最前面

    def test_no_action_field_and_required_fields_first(self):
        def walk(option, path):
            options = option.get("options", [])
            if any(o["type"] in (1, 2) for o in options):
                for o in options:
                    walk(o, path + [o["name"]])
                return
            names = [o["name"] for o in options]
            self.assertNotIn("操作", names, path)  # 做法是子命令，不是一栏
            self.assertNotIn("开关", names, path)
            required = [bool(o.get("required")) for o in options]
            self.assertEqual(required, sorted(required, reverse=True), path)  # 必填的在前（Discord 也这么要求）

        walk(self.admin.to_dict(self.bot.tree), ["dsadmin"])

    def test_fits_discord_limits(self):
        def weight(option):
            self.assertLessEqual(len(option["name"]), 32, option["name"])
            self.assertLessEqual(len(option["description"]), 100, option["name"])
            self.assertLessEqual(len(option.get("options", [])), 25, option["name"])
            self.assertLessEqual(len(option.get("choices", [])), 25, option["name"])
            total = len(option["name"]) + len(option["description"])
            for choice in option.get("choices", []):
                self.assertLessEqual(len(choice["name"]), 100, choice)
                total += len(choice["name"]) + len(str(choice["value"]))
            return total + sum(weight(o) for o in option.get("options", []))

        self.assertLess(weight(self.admin.to_dict(self.bot.tree)), 4000)  # Discord 一个命令连子命令一共最多 8000 字

    def test_fields_that_pop_up_a_list(self):
        payload = self.admin.to_dict(self.bot.tree)

        def field(*path):
            node = payload
            for name in path:
                node = next(o for o in node["options"] if o["name"] == name)
            return node

        for path in (("设置", "值"), ("表情包", "删除", "名字"), ("长期记忆", "查看", "编号"), ("长期记忆", "删除", "编号"), ("兴趣", "删除", "话题"), ("提醒", "取消", "编号")):
            self.assertTrue(field(*path).get("autocomplete"), path)
        keys = [c["value"] for c in field("设置", "参数")["choices"]]
        self.assertEqual(keys, [k for k in ADMIN_KEYS if k not in ("enabled", "persona")])
        self.assertEqual(keys[0], "proactive")  # 主动发言排最前面
        self.assertIn("share_memory", keys)
        self.assertEqual([c["name"] for c in field("区域", "允许", "范围")["choices"]], ["本频道", "本类别", "全服务器"])


class Area(WithBot):
    async def test_allow_deny_and_follow(self):
        said, _ = await self.run_command("区域", "禁止", args=(scope("全服务器"),))
        self.assertIn("全服务器：已设为「禁止」。", said)
        self.assertIn("现在这个频道：不回应（来自", said)
        self.assertIn("白名单模式：在要用的频道里执行 `/dsadmin 区域 允许`", said)
        self.assertEqual(self.overrides("guild"), {"enabled": False})
        said, _ = await self.run_command("区域", "允许")  # 不选范围 = 本频道
        self.assertIn(f"<#{CHANNEL}>：已设为「允许」。", said)
        self.assertIn("现在这个频道：会回应", said)
        self.assertNotIn("白名单", said)
        self.assertEqual(self.overrides("channel"), {"enabled": True})
        said, _ = await self.run_command("区域", "禁止", args=(scope("本类别"),))
        self.assertIn("类别「聊天区」：已设为「禁止」。", said)
        self.assertIn("现在这个频道：会回应", said)  # 频道自己的设置优先
        self.assertEqual(self.overrides("category"), {"enabled": False})
        said, _ = await self.run_command("区域", "跟随上级")
        self.assertIn(f"<#{CHANNEL}>：已设为「跟随上级」。", said)
        self.assertIn("现在这个频道：不回应", said)  # 跟着类别走
        self.assertEqual(self.overrides("channel"), {})
        await self.run_command("区域", "跟随上级", args=(scope("本类别"),))
        await self.run_command("区域", "跟随上级", args=(scope("全服务器"),))
        self.assertEqual((self.overrides("category"), self.overrides("guild")), ({}, {}))

    async def test_channel_outside_any_category(self):
        for name in ("允许", "禁止", "跟随上级"):
            said, _ = await self.run_command("区域", name, args=(scope("本类别"),), category=None)
            self.assertIn("这个频道不在任何类别里", said)
        self.assertEqual(self.bot.settings.scoped("category", "enabled"), {})

    async def test_view_is_the_old_area_list(self):
        from dsbot.commands import area_list_text

        await self.run_command("区域", "禁止", args=(scope("全服务器"),))
        await self.run_command("区域", "允许")
        await self.run_command("设置", args=(NS(name="主动发言", value="proactive"), "开", None))
        said, interaction = await self.run_command("区域", "查看")
        self.assertEqual(said, area_list_text(self.bot, interaction.guild))
        self.assertIn("全服务器：🚫 禁止（单独设置）", said)
        self.assertIn(f"<#{CHANNEL}>：✅ 允许", said)
        self.assertIn(f"主动发言开着的地方：<#{CHANNEL}>", said)


class Settings(WithBot):
    def key(self, name):
        return NS(name=SPECS[name].label, value=name)

    async def test_proactive_moved_into_settings(self):
        said, _ = await self.run_command("设置", args=(self.key("proactive"), "开", None))
        self.assertIn(f"<#{CHANNEL}>：「主动发言」= 开", said)
        self.assertNotIn("⚠️", said)
        self.assertEqual(self.overrides("channel"), {"proactive": True})
        said, _ = await self.run_command("设置", args=(self.key("proactive"), "关", scope("全服务器")))
        self.assertIn("全服务器：「主动发言」= 关", said)
        self.assertEqual(self.overrides("guild"), {"proactive": False})
        said, _ = await self.run_command("设置", args=(self.key("proactive"), "默认", None))
        self.assertIn(f"<#{CHANNEL}>：「主动发言」已恢复默认。", said)
        self.assertEqual(self.overrides("channel"), {})

    async def test_proactive_warns_where_it_may_not_answer(self):
        await self.run_command("区域", "禁止")
        said, _ = await self.run_command("设置", args=(self.key("proactive"), "开", None))
        self.assertIn("「主动发言」= 开", said)
        self.assertIn("⚠️ 但这个频道不在允许的区域，它不会说话。先用 `/dsadmin 区域 允许`。", said)
        said, _ = await self.run_command("设置", args=(self.key("proactive"), "关", None))
        self.assertNotIn("⚠️", said)  # 关掉、恢复默认时不用提醒
        said, _ = await self.run_command("设置", args=(self.key("proactive"), "默认", None))
        self.assertNotIn("⚠️", said)

    async def test_share_memory_moved_into_settings(self):
        said, _ = await self.run_command("设置", args=(self.key("share_memory"), "开", scope("全服务器")))
        self.assertIn("全服务器：「记忆共享」= 开", said)
        self.assertIn("记忆共享：开（来自", said)  # 这个频道现在共享不共享、能翻哪些频道
        self.assertIn("能翻记录的频道：", said)
        self.assertIn("私密频道就算在这个范围里也不会把内容分享出去", said)
        self.assertEqual(self.overrides("guild"), {"share_memory": True})
        said, _ = await self.run_command("设置", args=(self.key("share_memory"), "关", None))
        self.assertIn(f"<#{CHANNEL}>：「记忆共享」= 关", said)
        self.assertIn("记忆共享：关（来自", said)
        self.assertNotIn("私密频道就算", said)
        self.assertEqual(self.overrides("channel"), {"share_memory": False})
        said, _ = await self.run_command("设置", args=(self.key("share_memory"), "默认", None))  # 以前的“跟随上级”
        self.assertIn("「记忆共享」已恢复默认。", said)
        self.assertIn("记忆共享：开（来自", said)  # 跟着全服务器走：又开了
        self.assertEqual(self.overrides("channel"), {})
        said, _ = await self.run_command("设置", args=(self.key("share_memory"), "开", None))
        self.assertNotIn("私密频道就算", said)  # 只在本频道开：用不着这句

    async def test_other_settings_as_before(self):
        said, _ = await self.run_command("设置", args=(self.key("aliases"), "DS,小D", scope("全服务器")))
        self.assertIn("全服务器：「别名」= DS,小D", said)
        self.assertIn(SPECS["aliases"].help, said)
        said, _ = await self.run_command("设置", args=(self.key("check_minutes"), "一百", None))
        self.assertIn("没改成：「几分钟看一眼」要填整数", said)
        said, _ = await self.run_command("设置", args=(self.key("dm"), "关", scope("本频道")))  # 私信只看全服务器这一级
        self.assertIn("全服务器：「私信」= 关", said)
        self.assertEqual(self.overrides("guild"), {"aliases": "DS,小D", "dm": False})
        said, _ = await self.run_command("设置", args=(self.key("images"), "关", scope("本类别")), category=None)
        self.assertIn("这个频道不在任何类别里", said)

    def test_value_choices(self):
        from dsbot.commands import RESET_CHOICE, value_choices

        def names(key, typed=""):
            return [(c.name, c.value) for c in value_choices(key, typed)]

        reset = (RESET_CHOICE, "默认")
        self.assertEqual(names("proactive"), [("开", "开"), ("关", "关"), reset])
        self.assertEqual(names("主动发言"), names("proactive"))  # 给的是中文名也认
        self.assertEqual(names("share_memory", "关"), [("关", "关")])
        self.assertEqual(names("images", "默"), [reset])
        self.assertEqual(names("check_minutes"), [reset])  # 数字：手打，只提醒能填“默认”
        self.assertEqual(names("check_minutes", "5"), [])  # 打的字和选项对不上：什么都不弹，发出去的是打的字
        self.assertEqual(names("mention_thinking"), [("off", "off"), ("low", "low"), ("high", "high"), reset])
        self.assertEqual(names(None), [])  # 还没选参数
        self.assertEqual(names("没有这个参数"), [])
        from dsbot.config import RESET_WORDS, parse_value

        self.assertIn("默认", RESET_WORDS)
        for key in ADMIN_KEYS:  # 弹出来的每一项，选了都填得进去
            for choice in value_choices(key, ""):
                if choice.value not in RESET_WORDS:
                    parse_value(SPECS[key], choice.value)

    async def test_value_field_reads_the_chosen_setting(self):
        pop = self.command("设置")._params["value"].autocomplete
        self.assertEqual([c.name for c in await pop(ask(参数="proactive"), "")][:2], ["开", "关"])  # Discord 给的栏名是“参数”
        self.assertEqual(await pop(ask(), ""), [])


class Memes(WithBot):
    def setUp(self):
        super().setUp()
        from dsbot.memes import MemeLibrary

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        shared = Path(self.tmp.name) / "表情包"
        shared.mkdir()
        (shared / "通用狗头.png").write_bytes(png())
        self.bot.memes = MemeLibrary(shared, Path(self.tmp.name) / "服务器表情包", max_bytes=100_000)
        self.bot.cfg.bot.meme_dir = "data/表情包"

    def picture(self, name="熊猫头.png", data=None, size=None):
        data = data if data is not None else png((0, 0, 255))

        async def read():
            return data

        return NS(filename=name, size=size if size is not None else len(data), read=read)

    async def test_upload_view_and_delete(self):
        from dsbot.commands import meme_name_choices

        said, _ = await self.run_command("表情包", "查看")
        self.assertIn("本服务器：还没有（用 `/dsadmin 表情包 上传` 加）", said)
        said, _ = await self.run_command("表情包", "上传", args=(self.picture(), "熊猫头-无语"))
        self.assertIn("已加入这个服务器的表情包库：「熊猫头-无语」", said)
        said, _ = await self.run_command("表情包", "上传", args=(self.picture("猫猫 震惊.png"),))  # 不填名字：用文件名
        self.assertIn("「猫猫 震惊」", said)
        said, _ = await self.run_command("表情包", "查看")
        self.assertIn("本服务器：熊猫头-无语、猫猫 震惊", said)
        self.assertIn("通用：通用狗头", said)
        # 删除时「名字」一栏弹出的：只有这个服务器自己的（通用库的要在 NAS 上删）
        self.assertEqual([c.value for c in meme_name_choices(self.bot, GUILD, "")], ["熊猫头-无语", "猫猫 震惊"])
        self.assertEqual([c.value for c in meme_name_choices(self.bot, GUILD, "震惊")], ["猫猫 震惊"])
        pop = self.command("表情包", "删除")._params["name"].autocomplete
        self.assertEqual([c.name for c in await pop(ask(), "熊猫")], ["熊猫头-无语"])
        said, _ = await self.run_command("表情包", "删除", args=("熊猫头-无语",))
        self.assertIn("已删掉表情包「熊猫头-无语」。", said)
        said, _ = await self.run_command("表情包", "删除", args=("熊猫头-无语",))
        self.assertIn("这个服务器没有叫「熊猫头-无语」的表情包。", said)
        said, _ = await self.run_command("表情包", "删除", args=("通用狗头",))
        self.assertIn("「通用狗头」是通用库里的，要在 NAS 上的 `data/表情包/` 文件夹里删。", said)
        said, _ = await self.run_command("表情包", "删除", args=("  ",))
        self.assertIn("要删哪一张？", said)
        self.assertEqual(self.bot.memes.names(GUILD), ["猫猫 震惊", "通用狗头"])

    async def test_upload_problems(self):
        said, _ = await self.run_command("表情包", "上传", args=(self.picture(size=200_000),))
        self.assertIn("图片太大了", said)
        said, _ = await self.run_command("表情包", "上传", args=(self.picture("说明.txt", b"not a picture"),))
        self.assertIn("没存成：只支持 PNG、JPG、GIF、WebP 图片", said)
        await self.run_command("设置", args=(NS(name="发图", value="images"), "关", None))
        said, _ = await self.run_command("表情包", "上传", args=(self.picture(),))
        self.assertIn("⚠️ 但这里关掉了发图，它不会发：`/dsadmin 设置 参数:发图 值:开`", said)
        self.assertEqual(self.bot.memes.counts(GUILD), (1, 1))


class Memory(WithBot):
    async def test_add_view_delete(self):
        said, _ = await self.run_command("长期记忆", "查看")
        self.assertIn("**长期记忆**（0 / ", said)
        said, _ = await self.run_command("长期记忆", "添加", args=("  周五晚上  开黑 ",))
        self.assertIn("已记下第 1 条：周五晚上 开黑", said)
        await self.run_command("长期记忆", "添加", args=("阿明是群主",))
        said, _ = await self.run_command("长期记忆", "查看")
        self.assertIn("(1) 周五晚上 开黑", said)
        self.assertIn("(2) 阿明是群主", said)
        said, _ = await self.run_command("长期记忆", "查看", args=(2,))  # 选了编号：只看这一条
        self.assertEqual(said, "(2) 阿明是群主")
        said, _ = await self.run_command("长期记忆", "查看", args=(9,))
        self.assertIn("没有第 9 条（`/dsadmin 长期记忆 查看` 能看到编号）。", said)
        pop = self.command("长期记忆", "删除")._params["number"].autocomplete
        self.assertEqual([(c.name, c.value) for c in await pop(ask(), "群主")], [("(2) 阿明是群主", 2)])
        said, _ = await self.run_command("长期记忆", "删除", args=(1,))
        self.assertIn("已删掉第 1 条：周五晚上 开黑", said)
        said, _ = await self.run_command("长期记忆", "删除", args=(1,))
        self.assertIn("没有第 1 条（`/dsadmin 长期记忆 查看` 能看到编号）。", said)
        self.assertEqual([m.text for m in self.bot.memory.entries(GUILD)], ["阿明是群主"])

    async def test_add_problems(self):
        ec = self.bot.cfg.engine
        said, _ = await self.run_command("长期记忆", "添加", args=("   ",))
        self.assertIn("要记什么？", said)
        said, _ = await self.run_command("长期记忆", "添加", args=("长" * (ec.memory_entry_chars + 1),))
        self.assertIn(f"太长了（{ec.memory_entry_chars + 1} 字）", said)
        for i in range(ec.memory_max_entries):
            self.bot.memory.add(GUILD, f"第 {i} 件事", via="command", origin=GUILD)
        said, _ = await self.run_command("长期记忆", "添加", args=("再记一件",))
        self.assertIn("已经记满了", said)
        self.assertEqual(self.bot.memory.count(GUILD), ec.memory_max_entries)

    async def test_clear_asks_first(self):
        said, _ = await self.run_command("长期记忆", "清空")
        self.assertIn("长期记忆本来就是空的。", said)
        self.bot.memory.add(GUILD, "一件事", via="command", origin=GUILD)
        said, interaction = await self.run_command("长期记忆", "清空")
        self.assertIn("确定要清空", said)
        self.assertEqual(self.bot.memory.count(GUILD), 1)  # 还没确认：没清
        view = interaction.response.views[-1]
        stranger = ask(user=99)
        await view.children[0].callback(stranger)
        self.assertIn("只有发起的人能确认。", stranger.response.said[-1])
        click = ask()
        await view.children[0].callback(click)
        self.assertIn("的长期记忆（1 条）。", click.response.said[-1])
        self.assertEqual(self.bot.memory.count(GUILD), 0)

    async def test_without_long_memory(self):
        self.bot.memory = None
        for path, args in ((("查看",), ()), (("添加",), ("事",)), (("删除",), (1,)), (("清空",), ())):
            said, _ = await self.run_command("长期记忆", *path, args=args)
            self.assertEqual(said, "长期记忆没有启用。", path)


class Bubbling(WithBot):
    """2.11.0 冒泡：开关只在本频道这一级；/dsadmin 兴趣 管它拿去搜的话题。"""

    def key(self, name):
        return NS(name=SPECS[name].label, value=name)

    async def test_switch_is_for_this_channel_only(self):
        from dsbot.commands import OWNER_SET_KEYS

        said, _ = await self.run_command("设置", args=(self.key("bubble"), "开", scope("全服务器")))
        self.assertIn(f"<#{CHANNEL}>：「冒泡」= 中", said)  # 选了全服务器也是本频道；以前的“开”就是中档
        self.assertIn("冒泡：中 · 安静 4～6 小时以后冒一次，只在 8～23 点，每天最多 3 次", said)
        self.assertIn("`/dsadmin 兴趣 查看` 能看到、能加、能删", said)
        self.assertEqual((self.overrides("channel"), self.overrides("guild")), ({"bubble": "mid"}, {}))
        said, _ = await self.run_command("设置", args=(self.key("bubble"), "高", scope("本类别")))
        self.assertIn(f"<#{CHANNEL}>：「冒泡」= 高", said)
        self.assertIn("冒泡：高 · 安静 2～3 小时以后冒一次，只在 8～23 点，每天最多 5 次", said)
        self.assertEqual(self.overrides("category"), {})
        said, _ = await self.run_command("设置", args=(self.key("bubble"), "低", None))
        self.assertIn("冒泡：低 · 安静 8～12 小时以后冒一次，只在 8～23 点，每天最多 1 次", said)
        said, _ = await self.run_command("设置", args=(self.key("bubble"), "很高", None))
        self.assertIn("没改成：「冒泡」只能是：关 / 低 / 中 / 高", said)
        said, _ = await self.run_command("设置", args=(self.key("bubble"), "关", None))
        self.assertIn("冒泡：关", said)
        self.assertNotIn("兴趣 查看", said)
        self.assertNotIn("bubble", OWNER_SET_KEYS)  # 主人的 设置 是全局、全服务器的：不能拿来开冒泡
        self.assertNotIn("bubble_hours", SPECS)  # 2.12.0 起由档位决定

    def test_value_choices_and_old_switch(self):
        from dsbot.commands import bubble_level, value_choices

        self.assertEqual([c.name for c in value_choices("bubble", "")][:4], ["关", "低", "中", "高"])
        self.bot.settings._overrides[("channel", CHANNEL)] = {"bubble": True}  # 2.11.0 存下的“开”
        self.assertEqual(bubble_level(self.bot, CHANNEL), "mid")
        self.bot.settings._overrides[("channel", CHANNEL)] = {"bubble": False}
        self.assertEqual(bubble_level(self.bot, CHANNEL), "off")

    async def test_status_has_a_bubble_line(self):
        said, _ = await self.run_command("状态")
        self.assertIn("冒泡：关（`/dsadmin 设置 参数:冒泡 值:低/中/高` 在这个频道打开）", said)
        await self.run_command("设置", args=(self.key("bubble"), "中", None))
        said, _ = await self.run_command("状态")
        self.assertIn("冒泡：中 · 安静 4～6 小时以后冒一次", said)

    async def test_interests(self):
        from dsbot.commands import interest_choices

        said, _ = await self.run_command("兴趣", "查看")
        self.assertIn("还没有。", said)
        self.assertIn("⚠️ 这个频道没开冒泡", said)
        said, _ = await self.run_command("兴趣", "添加", args=("  显卡   价格 ",))
        self.assertIn("已加上：「显卡 价格」。", said)
        self.assertIn("⚠️ 这个频道没开冒泡，现在用不上", said)
        said, _ = await self.run_command("兴趣", "添加", args=("   ",))
        self.assertIn("要加什么话题？", said)
        self.bot.store.use_interest(CHANNEL, "原神 新版本", 100.0, 20)  # 它冒泡时自己搜过的
        self.bot.store.use_interest(CHANNEL, "原神 新版本", 200.0, 20)
        self.bot.store.use_interest(OTHER_CHANNEL, "别的频道的", 100.0, 20)
        await self.run_command("设置", args=(self.key("bubble"), "开", None))
        said, _ = await self.run_command("兴趣", "查看")
        self.assertIn("管理员加的：显卡 价格", said)
        self.assertIn("它从聊天里看出来的：原神 新版本（搜过 2 次）", said)
        self.assertNotIn("别的频道的", said)  # 每个频道各记各的
        self.assertNotIn("⚠️", said)
        pop = self.command("兴趣", "删除")._params["topic"].autocomplete
        self.assertEqual([(c.name, c.value) for c in await pop(ask(), "原神")], [("原神 新版本（它从聊天里看出来的）", "原神 新版本")])
        self.assertEqual(len(interest_choices(self.bot, CHANNEL, "")), 2)
        said, _ = await self.run_command("兴趣", "删除", args=("原神 新版本",))
        self.assertIn("已删掉「原神 新版本」，以后它也不会再搜这个", said)  # 它自己看出来的：记成不让搜
        said, _ = await self.run_command("兴趣", "查看")
        self.assertIn("不让搜的：原神 新版本", said)
        said, _ = await self.run_command("兴趣", "删除", args=("原神 新版本",))
        self.assertIn("已经可以搜了：从“不让搜”里拿掉了「原神 新版本」。", said)
        said, _ = await self.run_command("兴趣", "删除", args=("显卡 价格",))
        self.assertIn("已删掉「显卡 价格」。", said)
        said, _ = await self.run_command("兴趣", "删除", args=("显卡 价格",))
        self.assertIn("这个频道没记着「显卡 价格」", said)
        self.assertEqual(self.bot.store.interests(CHANNEL), [])
        await self.run_command("兴趣", "添加", args=("原神 新版本",))  # 不让搜的再“添加”一次：又可以搜了
        self.bot.store.set_interest(CHANNEL, "原神 新版本", "blocked", 1.0)
        await self.run_command("兴趣", "添加", args=("原神 新版本",))
        self.assertEqual([(t, k) for t, k, _, _ in self.bot.store.interests(CHANNEL)], [("原神 新版本", "manual")])


class TheRest(WithBot):
    async def test_status_preset_persona_and_reset(self):
        said, _ = await self.run_command("状态")
        self.assertIn(f"**<#{CHANNEL}> 的状态**", said)
        self.assertIn("（/dsadmin 长期记忆 查看）", said)
        preset = next(iter(__import__("dsbot.config", fromlist=["PRESETS"]).PRESETS))
        said, _ = await self.run_command("档位", args=(NS(name=preset, value=preset), None))
        self.assertIn(f"已切到「{preset}」档", said)
        _, interaction = await self.run_command("人设", args=(None,))
        self.assertIsNotNone(interaction.response.modal)  # 弹出编辑框
        said, _ = await self.run_command("清空记忆")
        self.assertIn("已清空本频道的对话记录和摘要。", said)


class WhatItSays(unittest.TestCase):
    """bot 自己说的话、README 里写的命令：都是现在真的有的写法。"""

    def setUp(self):
        try:
            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")
        bot = make_bot(self)
        self.tree = shape(next(c for c in bot.tree.get_commands() if c.name == "dsadmin").to_dict(bot.tree))
        self.labels = {SPECS[k].label for k in ADMIN_KEYS if k not in ("enabled", "persona")}

    def sources(self):
        files = sorted((ROOT / "dsbot").glob("*.py")) + [ROOT / "config.toml", ROOT / "README.md", ROOT / ".env.example"]
        return [(path, path.read_text(encoding="utf-8")) for path in files if path.exists()]

    def test_the_old_name_is_gone(self):
        names = "区域列表|区域|主动|记忆共享|档位|设置|人设|表情包|长期记忆|状态|清空记忆"
        for path, text in self.sources():
            for line in text.splitlines():
                with self.subTest(path.name):
                    self.assertIsNone(re.search(rf"/ds\s*`?\s*(?:{names})", line), line.strip()[:120])
                    self.assertIsNone(re.search(r"/dsadmin (?:区域列表|主动|记忆共享)", line), line.strip()[:120])  # 并掉的三个

    def check(self, mention):
        words = mention.split()[1:]
        node = self.tree
        while words and isinstance(node, dict):
            self.assertIn(words[0], node, mention)  # 有这个命令（或者子命令）
            node = node[words.pop(0)]
        if isinstance(node, dict):
            return  # 只写到组的名字（“/dsadmin 长期记忆”）
        fields = {name.rstrip("?") for name in node}
        for word in words:
            name, colon, value = word.partition(":")
            self.assertTrue(colon, mention)  # 后面跟的都是 “栏:值”
            self.assertIn(name, fields, mention)
            if name == "参数":
                self.assertIn(value, self.labels, mention)
            if name == "范围":
                self.assertIn(value, ("本频道", "本类别", "全服务器"), mention)

    def test_every_mention_is_a_real_command(self):
        found = 0
        for path, text in self.sources():
            for mention in re.findall(r"`(/dsadmin(?: [^`\n]*)?)`", text):
                with self.subTest(path.name, mention=mention):
                    self.check(mention)
                    found += 1
        self.assertGreater(found, 15)  # 真的查到了（commands.py 里就有十几处）


if __name__ == "__main__":
    unittest.main()
