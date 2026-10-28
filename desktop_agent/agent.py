"""DeepSeek 群友 · 桌面小程序：放在 bot 主人的电脑上，让 bot 替主人开关程序、打开网址。

它主动连到 NAS 上的 bot（电脑不用开端口），只做 agent.toml 白名单里的事：
  - 开、关登记过的程序（关是正常关闭，没保存的东西程序会先问你，不会强制结束）
  - 用默认浏览器打开 http / https 网址
别的什么都不做（不跑命令、不碰文件）。

用法：
  pip install aiohttp
  复制 agent.example.toml 为 agent.toml，填好 token 和要开关的程序
  python agent.py            # 有窗口，看得到日志
  pythonw agent.py           # 没窗口，放进“启动”文件夹开机自动跑
想停：在任务管理器里结束 pythonw（或者关掉窗口）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tomllib
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("desktop-agent")

HERE = Path(__file__).resolve().parent
RETRY_MAX = 60.0  # 连不上时最多隔这么久再试
URL_OK = re.compile(r"^https?://[^\s\x00-\x1f\x7f]+$", re.IGNORECASE)
MAX_TEXT = 300


def sign(token: str, *parts: Any) -> str:
    """和 bot 那边（dsbot/desktop.py 的 sign）一模一样。"""
    msg = "\n".join(str(p) for p in parts).encode("utf-8")
    return hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _norm(name: str) -> str:
    return "".join(name.split()).casefold()


@dataclass
class AppEntry:
    key: str
    name: str
    open: str = ""  # 程序路径、快捷方式（.lnk），或者 steam:// 这类链接
    args: list[str] = field(default_factory=list)
    process: str = ""  # 关的时候按这个进程名关（比如 steam.exe）；不填就不能关
    aliases: list[str] = field(default_factory=list)

    def forms(self) -> set[str]:
        return {_norm(x) for x in (self.key, self.name, *self.aliases) if x}


@dataclass
class Config:
    servers: list[str]
    token: str
    apps: dict[str, AppEntry]


def load_config(path: Path) -> Config:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    servers = [str(s) for s in data.get("servers") or [] if str(s).startswith(("ws://", "wss://"))]
    token = str(data.get("token") or "").strip()
    if not servers:
        raise SystemExit("agent.toml 里要填 servers（例如 ws://192.168.31.13:8765/desktop）")
    if len(token) < 16:
        raise SystemExit("agent.toml 里的 token 要和 NAS 上 .env 的 DESKTOP_TOKEN 一样（至少 16 个字符）")
    apps: dict[str, AppEntry] = {}
    for key, raw in (data.get("apps") or {}).items():
        if not isinstance(raw, dict):
            continue
        apps[key] = AppEntry(
            key=key,
            name=str(raw.get("name") or key),
            open=str(raw.get("open") or ""),
            args=[str(a) for a in raw.get("args") or []],
            process=str(raw.get("process") or ""),
            aliases=[str(a) for a in raw.get("aliases") or []],
        )
    return Config(servers, token, apps)


def find_app(apps: dict[str, AppEntry], wanted: str) -> AppEntry | None:
    key = _norm(wanted)
    for app in apps.values():
        if key in app.forms():
            return app
    return None


class Actions:
    """真正动手的地方。runner / opener / browser 可以换掉（测试用）。"""

    def __init__(
        self,
        apps: dict[str, AppEntry],
        *,
        runner: Callable[..., Any] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
        opener: Callable[[str], Any] | None = None,
        browser: Callable[[str], Any] = webbrowser.open,
    ):
        self.apps = apps
        self.runner = runner
        self.popen = popen
        self.opener = opener or getattr(os, "startfile", None)
        self.browser = browser

    def do(self, op: str, target: str) -> tuple[bool, str]:
        try:
            if op == "open":
                return self.open_app(target)
            if op == "close":
                return self.close_app(target)
            if op == "url":
                return self.open_url(target)
        except Exception as exc:  # 程序路径写错、没权限之类
            log.exception("办 %s %s 出错", op, target)
            return False, f"出错了：{type(exc).__name__}: {exc}"[:MAX_TEXT]
        return False, "不认识的操作"

    def open_app(self, target: str) -> tuple[bool, str]:
        app = find_app(self.apps, target)
        if app is None or not app.open:
            return False, f"白名单里没有「{target}」"
        if app.args:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.popen([app.open, *app.args], creationflags=flags, close_fds=True)
        elif self.opener is not None:
            self.opener(app.open)  # 快捷方式、steam:// 链接、exe 都行
        else:
            self.popen([app.open], close_fds=True)
        log.info("打开了 %s", app.name)
        return True, f"打开了「{app.name}」"

    def close_app(self, target: str) -> tuple[bool, str]:
        app = find_app(self.apps, target)
        if app is None:
            return False, f"白名单里没有「{target}」"
        if not app.process:
            return False, f"「{app.name}」没登记进程名，不能关"
        # 不加 /F：正常关闭（像点了右上角的叉），没保存的东西程序会先问
        done = self.runner(["taskkill", "/IM", app.process], capture_output=True, text=True)
        if done.returncode == 0:
            log.info("关了 %s", app.name)
            return True, f"让「{app.name}」关掉了（有没保存的东西它会先问）"
        if done.returncode == 128:
            return True, f"「{app.name}」本来就没开"
        return False, f"「{app.name}」没关掉（{(done.stderr or done.stdout or '').strip()[:100]}）"

    def open_url(self, target: str) -> tuple[bool, str]:
        url = target.strip()
        if not URL_OK.match(url):
            return False, "只打开 http / https 网址"
        self.browser(url)
        log.info("打开了网址 %s", url)
        return True, "用浏览器打开了"


async def serve(cfg: Config, actions: Actions, *, once: bool = False) -> None:
    """连上 bot，等它派活。断了就隔一会儿再连（轮流试 servers 里的地址：家里的、ZeroTier 的）。"""
    import aiohttp

    delay = 2.0
    host = platform.node()
    apps = [{"key": a.key, "name": a.name, "aliases": a.aliases} for a in cfg.apps.values()]
    async with aiohttp.ClientSession() as session:
        while True:
            for url in cfg.servers:
                try:
                    async with session.ws_connect(url, heartbeat=30, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                        hello = await asyncio.wait_for(ws.receive_json(), 10)
                        nonce = str(hello.get("nonce", ""))
                        await ws.send_json({"type": "auth", "mac": sign(cfg.token, "auth", nonce), "apps": apps, "host": host})
                        ready = await asyncio.wait_for(ws.receive(), 10)
                        if ready.type != aiohttp.WSMsgType.TEXT or json.loads(ready.data).get("type") != "ready":
                            log.warning("%s 不认这个 token（和 NAS 上的 DESKTOP_TOKEN 不一样？）", url)
                            continue
                        log.info("连上了 %s", url)
                        delay = 2.0
                        await _work(ws, cfg.token, nonce, actions)
                        log.info("和 %s 断开了", url)
                except (OSError, asyncio.TimeoutError, aiohttp.ClientError, ValueError) as exc:
                    log.info("连不上 %s：%s", url, type(exc).__name__)
                if once:
                    return
            await asyncio.sleep(delay)
            delay = min(RETRY_MAX, delay * 2)


async def _work(ws: Any, token: str, nonce: str, actions: Actions) -> None:
    import aiohttp

    last = 0
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except ValueError:
            continue
        if data.get("type") != "run":
            continue
        try:
            rid = int(data.get("id"))
        except (TypeError, ValueError):
            continue
        op, target = str(data.get("op", "")), str(data.get("target", ""))
        if rid <= last or not hmac.compare_digest(str(data.get("mac", "")), sign(token, "run", nonce, rid, op, target)):
            log.warning("收到一条签名不对（或者重复）的指令，不办")
            continue
        last = rid
        ok, text = await asyncio.to_thread(actions.do, op, target)
        text = text[:MAX_TEXT]
        await ws.send_json({"type": "result", "id": rid, "ok": ok, "text": text, "mac": sign(token, "result", nonce, rid, ok, text)})


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "agent.toml"
    handlers: list[logging.Handler] = [logging.FileHandler(HERE / "agent.log", encoding="utf-8")]
    if sys.stdout is not None:  # pythonw 没有控制台
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)
    cfg = load_config(path)
    log.info("桌面小程序启动：%d 个程序（%s）", len(cfg.apps), "、".join(a.name for a in cfg.apps.values()) or "没有")
    try:
        asyncio.run(serve(cfg, Actions(cfg.apps)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
