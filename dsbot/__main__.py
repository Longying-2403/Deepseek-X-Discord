"""Entry point: python -m dsbot [--config config.toml] [--check]"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import __version__
from .config import Secrets, load_config, load_secrets, unknown_note


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
    print(f"dsbot {__version__}")
    print(f"✓ Read config file {config_path}, model: {cfg.deepseek.model}")
    note = unknown_note(cfg)
    if note:
        print(f"! {note}")
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
    if secrets.tavily_api_key:
        print("✓ Reading links: posts on X via FxTwitter, web pages via Tavily; finding images online: Tavily. "
              'To try them: python -m dsbot --test-link URL / --test-image "keywords"')
    else:
        print("! TAVILY_API_KEY isn't set: only posts on X can be opened (other sites only get Discord's preview), "
              "and it can't find images online (the meme library still works)")
    from .memes import MemeLibrary

    memes = MemeLibrary(cfg.bot.meme_dir, cfg.bot.server_meme_dir, max_bytes=cfg.engine.image_max_bytes)
    shared = memes.names(0) if os.path.isdir(cfg.bot.meme_dir) else []
    print(
        f"✓ Shared meme library {cfg.bot.meme_dir}/: {len(shared)} meme{'s' if len(shared) != 1 else ''}"
        if shared
        else f"· Shared meme library {cfg.bot.meme_dir}/ is still empty (optional)"
    )

    from .memory import CardStore, MemoryStore

    for label, path in (("Chat history", cfg.bot.database), ("Profile cards", cfg.bot.cards_database), ("Long-term memory", cfg.bot.memory_database)):
        state = "already exists" if os.path.exists(path) else "created on first start"
        print(f"· {label}: {path} ({state})")
    store = Store(":memory:")
    bot = DSBot(
        cfg,
        secrets,
        store,
        Settings(cfg.defaults, store),
        Ledger(store, cfg.bot.timezone),
        DeepSeekClient(api_key="x", base_url=cfg.deepseek.base_url, model=cfg.deepseek.model),
        cards=CardStore(":memory:"),
        memory=MemoryStore(":memory:"),
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


def test_link(config_path: str, url: str) -> int:
    """Really open one link (posts on X go through FxTwitter; a web page uses up a little Tavily quota) to confirm reading links works."""
    import asyncio

    from .links import LinkReader, openable
    from .net import HttpClient
    from .tavily import Tavily

    cfg = load_config(config_path)
    secrets = load_secrets()
    if not openable(url):
        print("✗ This URL wouldn't be opened (images, videos, Discord's own links, private-network addresses and the like)")
        return 1

    async def run():  # type: ignore[no-untyped-def]
        http = HttpClient()
        try:
            tavily = Tavily(http, secrets.tavily_api_key) if secrets.tavily_api_key else None
            reader = LinkReader(http, tavily, tz=cfg.bot.timezone)
            if not reader.readable(url):
                return None
            return await reader.read(url, max_chars=cfg.engine.link_max_chars, image_side=cfg.engine.image_max_side)
        finally:
            await http.close()

    print(f"Opening {url} …")
    got = asyncio.run(run())
    if got is None:
        print("✗ TAVILY_API_KEY isn't set: only posts on X can be opened")
        return 1
    if got.result.error:
        print(f"✗ {got.result.error}")
        return 1
    print(f'✓ {got.result.kind} "{got.title}" ({got.result.detail})\n{got.result.text[:1500]}')
    if got.result.images:
        print(f"({len(got.result.images)} image{'s' if len(got.result.images) != 1 else ''} attached)")
    return 0


def test_image(config_path: str, query: str) -> int:
    """Really search for images once (uses 1 Tavily credit) and save the candidates to data/image-test/ to look at."""
    import asyncio
    from pathlib import Path

    from .net import HttpClient
    from .pictures import PictureFinder
    from .tavily import Tavily, TavilyError

    cfg = load_config(config_path)
    secrets = load_secrets()
    if not secrets.tavily_api_key:
        print("✗ Finding images online needs TAVILY_API_KEY in .env")
        return 1

    async def run():  # type: ignore[no-untyped-def]
        http = HttpClient()
        try:
            finder = PictureFinder(
                http, Tavily(http, secrets.tavily_api_key), keep=cfg.engine.image_candidates, max_bytes=cfg.engine.image_max_bytes
            )
            return await finder.find(query)
        finally:
            await http.close()

    print(f'Looking for images of "{query}"…')
    try:
        found = asyncio.run(run())
    except TavilyError as exc:
        print(f"✗ {exc}")
        return 1
    if not found:
        print("✗ No usable images found")
        return 1
    out = Path("data") / "image-test"
    out.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out, 0o777)  # test pictures: accounts on the NAS can delete them right after looking
    except OSError:
        pass
    for i, picture in enumerate(found, 1):
        path = out / f"candidate{i}-{picture.filename}"
        path.write_bytes(picture.data)
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
        print(f"  Image {i}: {picture.description or '(no description)'} ({picture.host}, {len(picture.data) // 1024} KB) → {path}")
    print("✓ Found some. In a real chat, it looks at these first and only sends one if it fits.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="dsbot", description="DeepSeek Buddy Discord bot")
    parser.add_argument("--version", action="version", version=f"dsbot {__version__}")
    parser.add_argument("--config", default=os.environ.get("DSBOT_CONFIG", "config.toml"))
    parser.add_argument("--check", action="store_true", help="only run the offline self-check; don't start the bot")
    parser.add_argument("--test-search", metavar="KEYWORDS", help="run one real search to confirm web search works (costs a tiny bit)")
    parser.add_argument("--test-link", metavar="URL", help="really open one link to confirm reading links works (a web page uses up a little Tavily quota)")
    parser.add_argument("--test-image", metavar="KEYWORDS", help="really search for images once and save the candidates to data/image-test/ (uses 1 Tavily credit)")
    args = parser.parse_args()
    _load_dotenv()

    if args.check:
        return check(args.config)
    if args.test_search:
        return test_search(args.config, args.test_search)
    if args.test_link:
        return test_link(args.config, args.test_link)
    if args.test_image:
        return test_image(args.config, args.test_image)

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, cfg.bot.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("dsbot").info("dsbot %s starting up", __version__)  # after swapping in new code, the log shows whether the new version took effect
    note = unknown_note(cfg)
    if note:
        logging.getLogger("dsbot").warning(note)
    secrets = load_secrets()

    from .discord_app import DSBot
    from .ledger import Ledger
    from .llm import DeepSeekClient
    from .memory import CardStore, MemoryStore
    from .settings import Settings
    from .store import Store

    os.umask(0o077)  # the database holds chat history: newly created files are readable and writable by us only
    store = Store(cfg.bot.database)
    cards = CardStore(cfg.bot.cards_database)  # profile cards and long-term memory get a file each, so they're easy to back up or move
    memory = MemoryStore(cfg.bot.memory_database)
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
        cards=cards,
        memory=memory,
    )
    bot.run(secrets.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
