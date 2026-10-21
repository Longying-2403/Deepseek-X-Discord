"""启动入口：python -m dsbot [--config config.toml] [--check]"""

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
    print(f"dsbot {__version__}")
    print(f"✓ 配置文件 {config_path} 读取成功，模型：{cfg.deepseek.model}")
    note = unknown_note(cfg)
    if note:
        print(f"! {note}")
    try:
        secrets = load_secrets()
        print(f"✓ .env 已填写（OWNER_ID={secrets.owner_id}，CONTROL_GUILD_ID={secrets.control_guild_id or '未设置'}）")
    except SystemExit as exc:
        print(f"✗ {exc}")
        secrets = Secrets("x", "x", 0, control_guild_id=1)
    check_files()
    print(f"✓ 联网搜索：DeepSeek 自带的搜索（{cfg.deepseek.anthropic_url}）"
          + ("，失败时改用 Tavily" if secrets.tavily_api_key else "")
          + "。想实际搜一次试试：python -m dsbot --test-search 关键词")
    if secrets.tavily_api_key:
        print("✓ 读链接：推文走 FxTwitter，网页走 Tavily；上网找图：Tavily。"
              "想试试：python -m dsbot --test-link 网址 / --test-image 关键词")
    else:
        print("! 没填 TAVILY_API_KEY：只能打开 X 的推文（别的网站只看 Discord 的预览），也不能上网找图（表情包库照常能用）")
    from .music import missing_parts

    if str(cfg.bot.music).strip().lower() in ("false", "0", "no", "off", ""):
        print("· 点歌：config.toml 里关掉了（[bot] music = false）")
    elif missing := missing_parts():
        print(f"! 点歌用不了：{missing}")
    else:
        how = "Tavily" if secrets.tavily_api_key else "B站自己的搜索（容易被风控，建议填 TAVILY_API_KEY）"
        print(f"✓ 点歌：yt-dlp、ffmpeg、语音库都在（只能放 B站的；按歌名找歌用 {how}）")
    from .memes import MemeLibrary

    memes = MemeLibrary(cfg.bot.meme_dir, cfg.bot.server_meme_dir, max_bytes=cfg.engine.image_max_bytes)
    shared = memes.names(0) if os.path.isdir(cfg.bot.meme_dir) else []
    print(f"✓ 通用表情包库 {cfg.bot.meme_dir}/：{len(shared)} 张" if shared else f"· 通用表情包库 {cfg.bot.meme_dir}/ 还是空的（可选）")

    from .memory import CardStore, MemoryStore

    for label, path in (("聊天记录", cfg.bot.database), ("人设卡", cfg.bot.cards_database), ("长期记忆", cfg.bot.memory_database)):
        state = "已有" if os.path.exists(path) else "第一次启动时新建"
        print(f"· {label}存在 {path}（{state}）")
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
            where = "全局" if guild_id is None else "私人服务器"
            print(f"✓ /{payload['name']}（{where}）：{' '.join(subs)}")
    print("自检完成。")
    return 0


def test_search(config_path: str, query: str) -> int:
    """用 .env 里的 key 真的搜一次（花一点点钱），确认联网搜索能用。"""
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

    print(f"搜索「{query}」……")
    try:
        result = asyncio.run(run())
    except SearchError as exc:
        print(f"✗ 搜索失败：{exc}")
        return 1
    u = result.usage
    print(f"✓ 用 {result.provider} 搜到了：\n{result.summary}")
    for title, url in result.sources:
        print(f"  - {title} {url}")
    print(f"用量：输入 {u.prompt_tokens} token（命中 {u.hit}），输出 {u.output} token，约 ${cfg.pricing.cost(u, time.time()):.5f}")
    return 0


def test_link(config_path: str, url: str) -> int:
    """真的打开一个链接（推文走 FxTwitter，网页用掉一点 Tavily 额度），确认读链接能用。"""
    import asyncio

    from .links import LinkReader, openable
    from .net import HttpClient
    from .tavily import Tavily

    cfg = load_config(config_path)
    secrets = load_secrets()
    if not openable(url):
        print("✗ 这个网址不会被打开（图片、视频、Discord 自己的链接、内网地址之类）")
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

    print(f"打开 {url} ……")
    got = asyncio.run(run())
    if got is None:
        print("✗ 没填 TAVILY_API_KEY：只能打开 X 的推文")
        return 1
    if got.result.error:
        print(f"✗ {got.result.error}")
        return 1
    print(f"✓ {got.result.kind}「{got.title}」（{got.result.detail}）\n{got.result.text[:1500]}")
    if got.result.images:
        print(f"（附了 {len(got.result.images)} 张图）")
    return 0


def test_image(config_path: str, query: str) -> int:
    """真的找一次图（用掉 1 次 Tavily 额度），把候选图存到 data/找图测试/ 看看。"""
    import asyncio
    from pathlib import Path

    from .net import HttpClient
    from .pictures import PictureFinder
    from .tavily import Tavily, TavilyError

    cfg = load_config(config_path)
    secrets = load_secrets()
    if not secrets.tavily_api_key:
        print("✗ 上网找图要在 .env 里填 TAVILY_API_KEY")
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

    print(f"找「{query}」的图……")
    try:
        found = asyncio.run(run())
    except TavilyError as exc:
        print(f"✗ {exc}")
        return 1
    if not found:
        print("✗ 没找到能用的图")
        return 1
    out = Path("data") / "找图测试"
    out.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out, 0o777)  # 测试用的图：NAS 上的账号看完能直接删
    except OSError:
        pass
    for i, picture in enumerate(found, 1):
        path = out / f"图{i}-{picture.filename}"
        path.write_bytes(picture.data)
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
        print(f"  图{i}：{picture.description or '（没有说明）'}（{picture.host}，{len(picture.data) // 1024} KB）→ {path}")
    print("✓ 找到了。真正聊天时，它会先看这几张、觉得合适才发。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="dsbot", description="DeepSeek 群友 Discord bot")
    parser.add_argument("--version", action="version", version=f"dsbot {__version__}")
    parser.add_argument("--config", default=os.environ.get("DSBOT_CONFIG", "config.toml"))
    parser.add_argument("--check", action="store_true", help="只做离线自检，不启动")
    parser.add_argument("--test-search", metavar="关键词", help="真的搜一次，确认联网搜索能用（花一点点钱）")
    parser.add_argument("--test-link", metavar="网址", help="真的打开一个链接，确认读链接能用（网页会用掉一点 Tavily 额度）")
    parser.add_argument("--test-image", metavar="关键词", help="真的找一次图，候选图存到 data/找图测试/（用掉 1 次 Tavily 额度）")
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
    logging.getLogger("dsbot").info("dsbot %s 启动中", __version__)  # 换了代码看日志就知道新版有没有生效
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

    os.umask(0o077)  # 数据库里有聊天记录：新建的文件只给自己读写
    store = Store(cfg.bot.database)
    cards = CardStore(cfg.bot.cards_database)  # 人设卡、长期记忆各一个文件，方便备份、搬家
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
