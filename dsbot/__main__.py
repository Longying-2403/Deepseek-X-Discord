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


def check_files() -> None:
    """读文件需要的组件：真的开一个解析子进程读一个小 Word 文件。"""
    import asyncio
    import io
    import zipfile

    try:
        import pypdf

        print(f"✓ PDF 支持：pypdf {pypdf.__version__}")
    except ImportError:
        print("✗ 没装 pypdf，读不了 PDF（pip install -r requirements.txt）")

    from .files import read_file

    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>自检</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    result = asyncio.run(read_file(buf.getvalue(), "check.docx", None, 100))
    if result.text == "自检":
        print("✓ 文件解析子进程正常")
    else:
        print(f"✗ 文件解析子进程出问题：{result.error or result}")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        from .files import _child_user

        if _child_user():
            print("✓ 解析子进程以 nobody 用户运行，读不到 token 和 API key")
        else:
            print("! 解析子进程没法降成 nobody（bot 的代码或装的库别的用户读不到），会以 root 运行")


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
    check_files()

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

    os.umask(0o077)  # 数据库里有聊天记录：新建的文件只给自己读写
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
