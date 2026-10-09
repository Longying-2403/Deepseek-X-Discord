"""启动入口：python -m dsbot [--config config.toml] [--check]"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import Secrets, load_config, load_secrets


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def check(config_path: str) -> int:
    """离线自检：配置能不能读、斜杠命令定义是否合法（不连 Discord、不花钱）。"""
    from .discord_app import DSBot
    from .ledger import Ledger
    from .llm import DeepSeekClient
    from .settings import Settings
    from .store import Store

    cfg = load_config(config_path)
    print(f"✓ 配置文件 {config_path} 读取成功，模型：{cfg.deepseek.model}")
    try:
        secrets = load_secrets()
        print(f"✓ .env 已填写（OWNER_ID={secrets.owner_id}，CONTROL_GUILD_ID={secrets.control_guild_id or '未设置'}）")
    except SystemExit as exc:
        print(f"✗ {exc}")
        secrets = Secrets("x", "x", 0, control_guild_id=1)

    store = Store(":memory:")
    bot = DSBot(
        cfg,
        secrets,
        store,
        Settings(cfg.defaults, store),
        Ledger(store, cfg.bot.timezone),
        DeepSeekClient(api_key="x", base_url=cfg.deepseek.base_url, model=cfg.deepseek.model),
    )
    groups = [(None, bot.tree.get_commands())]
    if secrets.control_guild_id:
        import discord

        groups.append((secrets.control_guild_id, bot.tree.get_commands(guild=discord.Object(id=secrets.control_guild_id))))
    for guild_id, commands in groups:
        for command in commands:
            payload = command.to_dict(bot.tree)
            json.dumps(payload, ensure_ascii=False)
            subs = [opt["name"] for opt in payload.get("options", [])]
            where = "全局" if guild_id is None else "私人服务器"
            print(f"✓ /{payload['name']}（{where}）：{' '.join(subs)}")
    print("自检完成。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="dsbot", description="DeepSeek 群友 Discord bot")
    parser.add_argument("--config", default=os.environ.get("DSBOT_CONFIG", "config.toml"))
    parser.add_argument("--check", action="store_true", help="只做离线自检，不启动")
    args = parser.parse_args()
    _load_dotenv()

    if args.check:
        return check(args.config)

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, cfg.bot.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    secrets = load_secrets()

    from .discord_app import DSBot
    from .ledger import Ledger
    from .llm import DeepSeekClient
    from .settings import Settings
    from .store import Store

    store = Store(cfg.bot.database)
    bot = DSBot(
        cfg,
        secrets,
        store,
        Settings(cfg.defaults, store),
        Ledger(store, cfg.bot.timezone),
        DeepSeekClient(
            api_key=secrets.deepseek_api_key,
            base_url=cfg.deepseek.base_url,
            model=cfg.deepseek.model,
            timeout=cfg.deepseek.timeout_seconds,
            max_retries=cfg.deepseek.max_retries,
        ),
    )
    bot.run(secrets.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
