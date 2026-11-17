"""2.8.0 连通的频道，部署到正式 bot 以后修掉的问题（命令）：
1. /dsowner chatsync 的「频道」「连到」两栏只认 bot 弹出的列表里选的那一项（送来的是频道 ID）。在手机、平板上用 # 选频道（Discord 自己的
   频道列表，送来的是 <#ID>），或者只打了频道名没选，都被当成“没选”。
2. /dsowner chatsync（还有一样的 /dsowner 服务器组）原来是一个命令：必填的“操作”加上选填的几栏。在 iPad 上先填了选填的栏，它又要你选
   “操作”，选完以后选填的栏就没送过来（正式 bot 的日志里每次都是 None）。

- 用 # 选的频道（<#ID>）照样认。
- 打了名字的：「服务器名 · #频道名」（和列表里写的一样）、「服务器名 #频道名」，或者只打频道名（带不带 # 都行）——bot 看得到的文字频道里
  只有一个叫这个名字的就是它；有几个同名的，回复里列出是哪几个，请他从列表里选。
- 从列表里选的（频道 ID）照旧；认不出的照旧请他从列表里选。
- 查看、连通（连起来）、断开（退出）是三个子命令：`/dsowner chatsync 连通 频道:… 连到:…`，要填的栏都是必填、按顺序出来，各自办该办的事。"""

import os
import tempfile
import unittest
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, OWNER
from test_chatsync import HALL, LOBBY, SECRET, FakeResponse, text_channel, world
from test_dm import OTHER

from dsbot.config import load_config
from dsbot.memory import CardStore, MemoryStore
from dsbot.store import Store

PREVIEW = "把「主服」#闲聊和「别的服」#大厅连通？"


class ChannelFieldsOfTheChatsyncCommand(unittest.IsolatedAsyncioTestCase):
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

    async def ask(self, channel, other):
        """bot 主人执行 /dsowner chatsync 操作:连通，两栏填的是 channel、other。返回 bot 的回复。"""
        from dsbot.commands import sync_command

        interaction = NS(user=NS(id=OWNER), response=FakeResponse(), followup=NS(send=None))
        await sync_command(self.bot, interaction, "link", channel, other)
        return interaction.response.sent[0][0]

    async def test_a_channel_picked_with_discords_hash_picker_is_accepted(self):
        reply = await self.ask(f"<#{CHANNEL}>", f"<#{HALL}>")  # 手机、平板上用 # 选的
        self.assertIn(PREVIEW, reply)

    async def test_a_typed_channel_name_is_accepted_when_only_one_channel_has_it(self):
        for channel, other in (
            ("主服 · #闲聊", "别的服 · #大厅"),  # 和列表里写的一样
            ("主服 #闲聊", "别的服 #大厅"),
            ("#闲聊", "别的服 大厅"),
            ("闲聊", "别的服 · #大厅"),
        ):
            with self.subTest(channel=channel, other=other):
                self.assertIn(PREVIEW, await self.ask(channel, other))

    async def test_several_channels_with_the_typed_name_are_listed(self):
        reply = await self.ask("大厅", str(CHANNEL))  # 主服、别的服都有 #大厅
        self.assertIn("有 2 个频道都叫这个名字", reply)
        self.assertIn("「主服」#大厅", reply)
        self.assertIn("「别的服」#大厅", reply)
        self.assertIn("从弹出的列表里选一个频道", reply)
        reply = await self.ask(str(CHANNEL), "#大厅")
        self.assertIn("有 2 个频道都叫这个名字", reply)
        self.assertIn("在「连到」一栏从弹出的列表里选", reply)

    async def test_control_a_picked_id_works_and_an_unknown_name_is_still_refused(self):
        self.assertIn(PREVIEW, await self.ask(str(CHANNEL), str(HALL)))  # 从 bot 的列表里选的：和以前一样
        reply = await self.ask("没有这个频道", str(HALL))
        self.assertIn("在「频道」一栏从弹出的列表里选一个频道", reply)
        self.assertNotIn("都叫这个名字", reply)


class OwnerCommandLayout(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import discord  # noqa: F401

            import dsbot.commands  # noqa: F401
        except ImportError:
            self.skipTest("没装 discord.py")

    def owner_command(self):
        """真的建一个 DSBot（不连 Discord，和 python -m dsbot --check 一样）：返回 bot 和它注册的 /dsowner。"""
        import discord

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
        store = Store(":memory:")
        llm = DeepSeekClient(api_key="x", base_url=cfg.deepseek.base_url, model=cfg.deepseek.model)
        secrets = Secrets("x", "x", 0, control_guild_ids=(1,))
        bot = DSBot(
            cfg, secrets, store, Settings(cfg.defaults, store), Ledger(store, cfg.bot.timezone), llm,
            cards=CardStore(":memory:"), memory=MemoryStore(":memory:"),
        )
        owner = next(c for c in bot.tree.get_commands(guild=discord.Object(id=1)) if c.name == "dsowner")
        return bot, owner

    def test_chatsync_and_server_groups_are_subcommands_whose_fields_are_required(self):
        bot, owner = self.owner_command()
        payload = {opt["name"]: opt for opt in owner.to_dict(bot.tree)["options"]}
        wanted = {
            "chatsync": {"查看": [], "连通": ["频道", "连到"], "断开": ["频道"]},
            "服务器组": {"查看": [], "连起来": ["服务器", "连到"], "退出": ["服务器"]},
        }
        for group, subs in wanted.items():
            with self.subTest(group):
                self.assertEqual(payload[group]["type"], 2)  # 子命令组（不再是一个带“操作”的命令）
                got = {sub["name"]: sub for sub in payload[group]["options"]}
                self.assertEqual(set(got), set(subs))
                for name, fields in subs.items():
                    options = got[name].get("options", [])
                    self.assertEqual([o["name"] for o in options], fields)  # 按顺序出来
                    for option in options:
                        self.assertTrue(option.get("required"), (group, name, option["name"]))  # 都是必填
                        self.assertTrue(option.get("autocomplete"), (group, name, option["name"]))  # 弹出列表

    async def test_each_subcommand_does_its_own_job(self):
        bot, owner = self.owner_command()

        async def run(group, name, *args):
            interaction = NS(user=NS(id=0), response=FakeResponse(), followup=NS(send=None))  # bot 主人（OWNER_ID 填的 0）
            await owner.get_command(group).get_command(name).callback(interaction, *args)
            return interaction.response.sent[0][0]

        self.assertIn("还没有连通的频道", await run("chatsync", "查看"))
        self.assertIn("bot 看不到频道 1", await run("chatsync", "连通", "1", "2"))  # 连通：先查这两个频道能不能连
        self.assertIn("这个频道没有连通", await run("chatsync", "断开", "1"))
        self.assertIn("还没有服务器组", await run("服务器组", "查看"))
        self.assertIn("bot 不在 1、2 这个服务器里", await run("服务器组", "连起来", "1", "2"))
        self.assertNotIn("从弹出的列表里选", await run("服务器组", "退出", "1"))  # 认出了服务器，去办退出


if __name__ == "__main__":
    unittest.main()
