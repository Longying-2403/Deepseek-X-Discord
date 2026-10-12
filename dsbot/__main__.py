"""Entry point: python -m dsbot [--config config.toml] [--check]"""

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
    """Components needed for reading files: actually start a parsing subprocess and read a small Word file."""
    import asyncio
    import io
    import zipfile

    try:
        import pypdf

        print(f"✓ PDF support: pypdf {pypdf.__version__}")
    except ImportError:
        print("✗ pypdf isn't installed, so PDFs can't be read (pip install -r requirements.txt)")

    from .files import read_file

    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>self-check</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    result = asyncio.run(read_file(buf.getvalue(), "check.docx", None, 100))
    if result.text == "self-check":
        print("✓ The file-parsing subprocess works")
    else:
        print(f"✗ Problem with the file-parsing subprocess: {result.error or result}")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        from .files import _child_user

        if _child_user():
            print("✓ The parsing subprocess runs as the nobody user and can't read the token or API key")
        else:
            print("! The parsing subprocess can't drop to nobody (other users can't read the bot's code or installed libraries), so it will run as root")


def check(config_path: str) -> int:
    """Offline self-check: can the config be read, and are the slash command definitions valid (no Discord connection, costs nothing)."""
    from .discord_app import DSBot
    from .ledger import Ledger
    from .llm import DeepSeekClient
    from .settings import Settings
    from .store import Store

    cfg = load_config(config_path)
    print(f"✓ Read config file {config_path}, model: {cfg.deepseek.model}")
    try:
        secrets = load_secrets()
        print(f"✓ .env is filled in (OWNER_ID={secrets.owner_id}, CONTROL_GUILD_ID={secrets.control_guild_id or 'not set'})")
    except SystemExit as exc:
        print(f"✗ {exc}")
        secrets = Secrets("x", "x", 0, control_guild_id=1)
    check_files()
    print(f"✓ Web search: DeepSeek's built-in search ({cfg.deepseek.anthropic_url})"
          + (", falling back to Tavily if it fails" if secrets.tavily_api_key else "")
          + '. To try a real search: python -m dsbot --test-search "keywords"')

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
            where = "global" if guild_id is None else "control server"
            print(f"✓ /{payload['name']} ({where}): {' '.join(subs)}")
    print("Self-check complete.")
    return 0


def test_search(config_path: str, query: str) -> int:
    """Run one real search with the key from .env (costs a tiny bit) to confirm web search works."""
    import asyncio
    import time

    from .search import SearchError, WebSearch, today_text

    cfg = load_config(config_path)
    secrets = load_secrets()
    searcher = WebSearch(
        api_key=secrets.deepseek_api_key,
        url=cfg.deepseek.anthropic_url,
        model=cfg.deepseek.model,
        timeout=cfg.deepseek.search_timeout_seconds,
        max_uses=cfg.deepseek.search_max_uses,
        tavily_key=secrets.tavily_api_key,
    )

    async def run():  # type: ignore[no-untyped-def]
        try:
            return await searcher.search(
                query, today=today_text(time.time(), cfg.bot.timezone), max_chars=cfg.engine.search_max_chars
            )
        finally:
            await searcher.close()

    print(f'Searching for "{query}"...')
    try:
        result = asyncio.run(run())
    except SearchError as exc:
        print(f"✗ Search failed: {exc}")
        return 1
    u = result.usage
    print(f"✓ Found it with {result.provider}:\n{result.summary}")
    for title, url in result.sources:
        print(f"  - {title} {url}")
    print(f"Usage: {u.prompt_tokens} input tokens ({u.hit} cache hits), {u.output} output tokens, about ${cfg.pricing.cost(u, time.time()):.5f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="dsbot", description="DeepSeek Buddy Discord bot")
    parser.add_argument("--config", default=os.environ.get("DSBOT_CONFIG", "config.toml"))
    parser.add_argument("--check", action="store_true", help="only run the offline self-check; don't start the bot")
    parser.add_argument("--test-search", metavar="KEYWORDS", help="run one real search to confirm web search works (costs a tiny bit)")
    args = parser.parse_args()
    _load_dotenv()

    if args.check:
        return check(args.config)
    if args.test_search:
        return test_search(args.config, args.test_search)

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

    os.umask(0o077)  # the database holds chat history: newly created files are readable and writable by us only
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
