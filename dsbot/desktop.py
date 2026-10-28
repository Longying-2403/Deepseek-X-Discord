"""操作 bot 主人的电脑：主人电脑上的桌面小程序（desktop_agent/agent.py）主动连过来，这里转达开关程序、打开网址。

小程序只做它自己白名单里的事（开关登记过的程序、用浏览器打开 http/https 网址），这边只能从里面选，
不能让它跑任意命令。连接用共享密钥（.env 的 DESKTOP_TOKEN）签名：连上时双方核对，之后每条指令、
每个结果都带签名，局域网里别人伪造不了指令。

协议（WebSocket 上的 JSON）：
  这边 → 小程序  {"type": "hello", "nonce": 随机串}
  小程序 → 这边  {"type": "auth", "mac": 签名("auth", nonce), "apps": [{"key", "name", "aliases"}], "host": 电脑名}
  这边 → 小程序  {"type": "ready"}
  这边 → 小程序  {"type": "run", "id": 1, "op": "open" | "close" | "url", "target": "...", "mac": 签名("run", nonce, id, op, target)}
  小程序 → 这边  {"type": "result", "id": 1, "ok": true, "text": "...", "mac": 签名("result", nonce, id, ok, text)}
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

OPS = ("open", "close", "url")
RUN_TIMEOUT = 20.0  # 一个动作最多等电脑这么久
AUTH_TIMEOUT = 10.0  # 连上以后这么久没证明自己就断开
MAX_APPS = 100
MAX_TEXT = 300


def sign(token: str, *parts: Any) -> str:
    """签名：HMAC-SHA256(密钥, 各部分用 \\n 连起来)。两边要完全一样（小程序那边有一份同样的）。"""
    msg = "\n".join(str(p) for p in parts).encode("utf-8")
    return hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _norm(name: str) -> str:
    return "".join(name.split()).casefold()


@dataclass
class App:
    key: str
    name: str
    aliases: tuple[str, ...] = ()

    def forms(self) -> set[str]:
        return {_norm(x) for x in (self.key, self.name, *self.aliases) if x}


class DesktopHub:
    """等主人电脑上的小程序连过来；同一时间只认一台（新的连上就换掉旧的）。"""

    def __init__(self, token: str, *, host: str = "0.0.0.0", port: int = 8765):
        self.token = token
        self.host = host
        self.port = port
        self.apps: list[App] = []  # 小程序上次报上来的白名单（断开了也留着，提示词里不用变来变去）
        self.computer = ""  # 电脑名
        self._ws: Any = None
        self._nonce = ""
        self._next_id = 0
        self._waiting: dict[int, asyncio.Future[tuple[bool, str]]] = {}
        self._lock = asyncio.Lock()  # 一次只办一件事
        self._runner: Any = None

    # ------------------------------------------------------------ 服务

    async def start(self) -> None:
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/desktop", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("桌面操作：等电脑上的小程序连过来（端口 %s）", self.port)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    def online(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def app_names(self) -> list[str]:
        return sorted({a.name for a in self.apps}, key=str.casefold)

    def match(self, wanted: str) -> App | None:
        """按名字找白名单里的程序：名字、别名一样（不分大小写、空格）；都不一样时，只有一个包含它的也算。"""
        key = _norm(wanted)
        if not key:
            return None
        exact = [a for a in self.apps if key in a.forms()]
        if exact:
            return exact[0]
        partial = [a for a in self.apps if any(key in f or f in key for f in a.forms() if len(f) >= 2)]
        return partial[0] if len(partial) == 1 else None

    async def _handle(self, request: Any) -> Any:
        from aiohttp import WSMsgType, web

        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=256 * 1024)
        await ws.prepare(request)
        nonce = secrets.token_hex(16)
        peer = request.remote
        try:
            await ws.send_json({"type": "hello", "nonce": nonce})
            msg = await asyncio.wait_for(ws.receive(), AUTH_TIMEOUT)
            data = json.loads(msg.data) if msg.type == WSMsgType.TEXT else {}
            if data.get("type") != "auth" or not hmac.compare_digest(str(data.get("mac", "")), sign(self.token, "auth", nonce)):
                log.warning("桌面操作：%s 连过来但密钥不对，断开", peer)
                await ws.close()
                return ws
        except (asyncio.TimeoutError, ValueError, TypeError):
            await ws.close()
            return ws
        apps = []
        for item in (data.get("apps") or [])[:MAX_APPS]:
            if isinstance(item, dict) and isinstance(item.get("key"), str):
                aliases = tuple(str(a)[:40] for a in (item.get("aliases") or []) if isinstance(a, str))[:10]
                apps.append(App(item["key"][:40], str(item.get("name") or item["key"])[:40], aliases))
        old = self._ws
        self._ws, self._nonce, self.apps = ws, nonce, apps
        self.computer = str(data.get("host") or "")[:60]
        if old is not None and not old.closed:
            await old.close()  # 换成新连上的这台
        self._fail_waiting("电脑那边重新连上了，刚才那件事没做完")
        await ws.send_json({"type": "ready"})
        log.info("桌面操作：电脑「%s」连上了（%s），能开关 %d 个程序", self.computer, peer, len(apps))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("type") == "result":
                    self._resolve(nonce, data)
        finally:
            if self._ws is ws:
                self._ws = None
                self._fail_waiting("电脑断开了，没收到结果")
                log.info("桌面操作：电脑「%s」断开了", self.computer)
        return ws

    def _resolve(self, nonce: str, data: dict[str, Any]) -> None:
        try:
            rid = int(data.get("id"))
        except (TypeError, ValueError):
            return
        ok = data.get("ok") is True
        text = str(data.get("text") or "")[:MAX_TEXT]
        if not hmac.compare_digest(str(data.get("mac", "")), sign(self.token, "result", nonce, rid, ok, text)):
            log.warning("桌面操作：结果的签名不对，不认")
            return
        fut = self._waiting.pop(rid, None)
        if fut is not None and not fut.done():
            fut.set_result((ok, text))

    def _fail_waiting(self, text: str) -> None:
        for fut in self._waiting.values():
            if not fut.done():
                fut.set_result((False, text))
        self._waiting.clear()

    # ------------------------------------------------------------ 办事

    async def run(self, op: str, target: str) -> tuple[bool, str]:
        """让电脑办一件事：(办成了没有, 结果)。电脑不在线、超时都算没办成。"""
        if op not in OPS:
            return False, "不认识的操作"
        async with self._lock:
            ws = self._ws
            if ws is None or ws.closed:
                return False, "电脑现在不在线（关机了，或者小程序没开）"
            self._next_id += 1
            rid = self._next_id
            fut: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
            self._waiting[rid] = fut
            try:
                await ws.send_json(
                    {"type": "run", "id": rid, "op": op, "target": target, "mac": sign(self.token, "run", self._nonce, rid, op, target)}
                )
                return await asyncio.wait_for(fut, RUN_TIMEOUT)
            except asyncio.TimeoutError:
                return False, "电脑那边一直没回，不知道办成了没有"
            except Exception as exc:  # 刚好断开之类
                return False, f"没发出去（{type(exc).__name__}）"
            finally:
                self._waiting.pop(rid, None)
