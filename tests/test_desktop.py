"""桌面操作：只替 bot 主人本人、在他自己的私信里，开关电脑上白名单里的程序、打开他自己发的网址。"""

import asyncio
import importlib.util
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from helpers import CHANNEL, GUILD, OWNER, make_engine, msg
from test_memory import ACTIONS, REPLY, system_text, user_text

from dsbot.actions import parse_actions
from dsbot.desktop import App, DesktopHub, sign
from dsbot.models import ChannelPlace
from dsbot.prompts import DESKTOP_NOT_OWNER, DESKTOP_OFF, DESKTOP_URL_NOT_HIS

ROOT = Path(__file__).resolve().parents[1]
DM = 7000  # 主人和 bot 的私信
OTHER_DM = 7001  # 别人和 bot 的私信
TOKEN = "t" * 32


def load_agent():
    spec = importlib.util.spec_from_file_location("desktop_agent_under_test", ROOT / "desktop_agent" / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass 要按模块名找到它
    spec.loader.exec_module(mod)
    return mod


agent = load_agent()


class Parse(unittest.TestCase):
    def test_variants(self):
        actions, ok = parse_actions(
            '{"actions": [{"type": "desktop", "op": "open", "target": "Steam"},'
            ' {"type": "电脑", "op": "关掉", "app": "记事本"},'
            ' {"type": "desktop", "url": "https://example.com/a"},'
            ' {"type": "desktop", "op": "open", "target": "Steam"},'
            ' {"type": "desktop", "op": "format", "target": "C:"},'
            ' {"type": "desktop", "op": "open"}]}'
        )
        self.assertTrue(ok)
        self.assertEqual(
            [(a.op, a.query) for a in actions],
            [("open", "Steam"), ("close", "记事本"), ("url", "https://example.com/a")],  # 重复的、不认识的操作、没写目标的都不要
        )


class FakeDesktop:
    def __init__(self, apps=("Steam", "记事本"), online=True):
        self.apps = [App(n.lower(), n) for n in apps]
        self.calls = []
        self.up = online

    def app_names(self):
        return sorted(a.name for a in self.apps)

    def match(self, wanted):
        return next((a for a in self.apps if a.name.casefold() == wanted.strip().casefold()), None)

    async def run(self, op, target):
        self.calls.append((op, target))
        if not self.up:
            return False, "电脑现在不在线（关机了，或者小程序没开）"
        return True, f"办好了 {op} {target}"


def owner_dm(text, mid=1, **kw):
    kw.setdefault("author", OWNER)
    kw.setdefault("name", "主人")
    kw.setdefault("channel_id", DM)
    return msg(mid, text, mentions_bot=True, private=True, **kw)


def desktop_engine(first, *, defaults=None):
    """first：模型第一次的输出；办完电脑的事以后再问它时回“好了”。"""
    state = {"n": 0}

    def responder(call):
        state["n"] += 1
        return first if state["n"] == 1 else ACTIONS(REPLY(1, "好了"))

    engine, llm, transport, clock = make_engine(responder, defaults=defaults)
    for cid, uid, name in ((DM, OWNER, "主人"), (OTHER_DM, 11, "阿明")):
        transport.places[cid] = ChannelPlace(guild_id=GUILD, name="私信", dm=True)
        transport.dm_with[cid] = name
        transport.dm_users[cid] = uid
    engine.desktop = FakeDesktop()

    async def sleep(seconds):
        clock.advance(seconds)

    engine.sleep = sleep
    return engine, llm, transport, clock


def tool_result(llm):
    """办完以后再问模型的那一次：最后一条消息里的结果。"""
    return user_text(llm.calls[1]) if len(llm.calls) > 1 else ""


OPEN_STEAM = ACTIONS({"type": "desktop", "op": "open", "target": "steam"})


class Engine(unittest.IsolatedAsyncioTestCase):
    async def test_owner_opens_an_app_in_his_dm(self):
        engine, llm, transport, clock = desktop_engine(OPEN_STEAM)
        await engine.handle_message(owner_dm("帮我开一下 steam"))
        await engine.drain()
        self.assertEqual(engine.desktop.calls, [("open", "steam")])
        self.assertIn("（电脑：办好了 open steam）", tool_result(llm))
        self.assertEqual(transport.sent[-1][1], "好了")
        system = system_text(llm.calls[0])
        self.assertIn("## dsowner 的电脑", system)
        self.assertIn("能开关的程序：Steam、记事本", system)

    async def test_not_for_anyone_else(self):
        engine, llm, transport, clock = desktop_engine(OPEN_STEAM)
        await engine.handle_message(owner_dm("帮我开 steam", author=11, name="阿明", channel_id=OTHER_DM))
        await engine.drain()
        self.assertEqual(engine.desktop.calls, [])
        self.assertIn(DESKTOP_OFF, tool_result(llm))
        self.assertNotIn("## dsowner 的电脑", system_text(llm.calls[0]))

    async def test_not_in_server_channels(self):
        engine, llm, transport, clock = desktop_engine(OPEN_STEAM)
        await engine.handle_message(msg(1, "@小D 开 steam", author=OWNER, name="主人", mentions_bot=True))
        await engine.drain()
        self.assertEqual(engine.desktop.calls, [])
        self.assertIn(DESKTOP_OFF, tool_result(llm))
        self.assertNotIn("## dsowner 的电脑", system_text(llm.calls[0]))

    async def test_only_this_rounds_own_request(self):
        # 主人私信里这一轮叫它的消息被删了：不办
        engine, llm, transport, clock = desktop_engine(OPEN_STEAM)

        async def hook(call):
            if len(llm.calls) == 1:
                engine.handle_delete(GUILD, DM, [1])

        llm.hook = hook
        await engine.handle_message(owner_dm("帮我开 steam"))
        await engine.drain()
        self.assertEqual(engine.desktop.calls, [])

    async def test_url_must_be_his_own(self):
        bad = ACTIONS({"type": "desktop", "op": "url", "target": "https://evil.example/x"})
        engine, llm, transport, clock = desktop_engine(bad)
        await engine.handle_message(owner_dm("帮我打开刚才那个网页看看"))
        await engine.drain()
        self.assertEqual(engine.desktop.calls, [])
        self.assertIn(DESKTOP_URL_NOT_HIS, tool_result(llm))

        good = ACTIONS({"type": "desktop", "op": "url", "target": "https://example.com/a?b=1"})
        engine, llm, transport, clock = desktop_engine(good)
        await engine.handle_message(owner_dm("帮我打开 https://example.com/a?b=1"))
        await engine.drain()
        self.assertEqual(engine.desktop.calls, [("url", "https://example.com/a?b=1")])

    async def test_unknown_app_and_offline(self):
        engine, llm, transport, clock = desktop_engine(ACTIONS({"type": "desktop", "op": "open", "target": "cmd"}))
        await engine.handle_message(owner_dm("开个 cmd"))
        await engine.drain()
        self.assertEqual(engine.desktop.calls, [])
        self.assertIn("电脑上没登记「cmd」这个程序，能开关的有：Steam、记事本", tool_result(llm))

        engine, llm, transport, clock = desktop_engine(OPEN_STEAM)
        engine.desktop.up = False
        await engine.handle_message(owner_dm("开 steam"))
        await engine.drain()
        self.assertIn("没办成：电脑现在不在线", tool_result(llm))

    async def test_switched_off_by_setting(self):
        engine, llm, transport, clock = desktop_engine(OPEN_STEAM, defaults={"desktop": False})
        await engine.handle_message(owner_dm("开 steam"))
        await engine.drain()
        self.assertEqual(engine.desktop.calls, [])
        self.assertNotIn("## dsowner 的电脑", system_text(llm.calls[0]))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recorder:
    def __init__(self):
        self.done = []

    def do(self, op, target):
        self.done.append((op, target))
        return True, f"做了 {op} {target}"


class Link(unittest.IsolatedAsyncioTestCase):
    """真的连一次：bot 这边的 DesktopHub 和电脑上的小程序（都在本机）。"""

    async def asyncSetUp(self):
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            self.skipTest("没装 aiohttp")
        self.port = free_port()
        self.hub = DesktopHub(TOKEN, host="127.0.0.1", port=self.port)
        await self.hub.start()
        self.addAsyncCleanup(self.hub.close)

    def start_agent(self, token=TOKEN, actions=None):
        cfg = agent.Config([f"ws://127.0.0.1:{self.port}/desktop"], token, {"steam": agent.AppEntry("steam", "Steam", aliases=["蒸汽"])})
        actions = actions or Recorder()
        task = asyncio.create_task(agent.serve(cfg, actions))
        self.addAsyncCleanup(self.stop, task)
        return actions

    async def stop(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def wait_online(self, want=True):
        for _ in range(200):
            if self.hub.online() == want:
                return
            await asyncio.sleep(0.02)
        self.fail("没连上" if want else "还连着")

    async def test_run_and_result(self):
        rec = self.start_agent()
        await self.wait_online()
        self.assertEqual(self.hub.app_names(), ["Steam"])
        self.assertEqual(self.hub.match("蒸汽").key, "steam")
        self.assertEqual(await self.hub.run("open", "steam"), (True, "做了 open steam"))
        self.assertEqual(rec.done, [("open", "steam")])

    async def test_wrong_token_is_refused(self):
        rec = self.start_agent(token="x" * 32)
        await asyncio.sleep(0.5)
        self.assertFalse(self.hub.online())
        self.assertEqual(await self.hub.run("open", "steam"), (False, "电脑现在不在线（关机了，或者小程序没开）"))
        self.assertEqual(rec.done, [])

    async def test_forged_and_replayed_commands_are_ignored(self):
        rec = self.start_agent()
        await self.wait_online()
        ws = self.hub._ws
        await ws.send_json({"type": "run", "id": 50, "op": "url", "target": "https://evil.example", "mac": "0" * 64})
        await self.hub.run("open", "steam")  # id 1
        good = {"type": "run", "id": 1, "op": "open", "target": "steam", "mac": sign(TOKEN, "run", self.hub._nonce, 1, "open", "steam")}
        await ws.send_json(good)  # 原样再发一次（重放）
        await self.hub.run("close", "steam")
        self.assertEqual(rec.done, [("open", "steam"), ("close", "steam")])

    async def test_offline_after_disconnect(self):
        self.start_agent()
        await self.wait_online()
        await self.hub._ws.close()
        await self.wait_online(False)
        ok, text = await self.hub.run("open", "steam")
        self.assertFalse(ok)


class AgentActions(unittest.TestCase):
    def make(self, returncode=0):
        self.opened, self.ran, self.browsed, self.spawned = [], [], [], []
        apps = {
            "steam": agent.AppEntry("steam", "Steam", open="steam://open/main", process="steam.exe", aliases=["蒸汽"]),
            "tool": agent.AppEntry("tool", "工具", open="C:/tools/tool.exe", args=["--quiet"]),
        }
        return agent.Actions(
            apps,
            runner=lambda cmd, **kw: (self.ran.append(cmd), NS(returncode=returncode, stdout="", stderr="拒绝访问"))[1],
            popen=lambda cmd, **kw: self.spawned.append(cmd),
            opener=self.opened.append,
            browser=self.browsed.append,
        )

    def test_open_and_close(self):
        actions = self.make()
        self.assertEqual(actions.do("open", "蒸汽"), (True, "打开了「Steam」"))
        self.assertEqual(self.opened, ["steam://open/main"])
        self.assertTrue(actions.do("open", "工具")[0])
        self.assertEqual(self.spawned, [["C:/tools/tool.exe", "--quiet"]])  # 不经过命令行解释，参数原样传
        self.assertTrue(actions.do("close", "steam")[0])
        self.assertEqual(self.ran, [["taskkill", "/IM", "steam.exe"]])  # 不加 /F：正常关闭
        self.assertEqual(actions.do("close", "工具"), (False, "「工具」没登记进程名，不能关"))
        self.assertFalse(actions.do("open", "cmd")[0])
        self.assertEqual(actions.do("open", "C:/Windows/System32/cmd.exe"), (False, "白名单里没有「C:/Windows/System32/cmd.exe」"))

    def test_close_results(self):
        self.assertEqual(self.make(128).do("close", "steam"), (True, "「Steam」本来就没开"))
        ok, text = self.make(1).do("close", "steam")
        self.assertFalse(ok)
        self.assertIn("拒绝访问", text)

    def test_urls(self):
        actions = self.make()
        self.assertTrue(actions.do("url", "https://example.com/a b")[0] is False)
        for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "ms-settings:", "http://x\nshutdown"):
            self.assertFalse(actions.do("url", bad)[0], bad)
        self.assertTrue(actions.do("url", "https://example.com/a?b=1")[0])
        self.assertEqual(self.browsed, ["https://example.com/a?b=1"])
        self.assertEqual(actions.do("format", "C:"), (False, "不认识的操作"))

    def test_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.toml"
            path.write_text((ROOT / "desktop_agent" / "agent.example.toml").read_text(encoding="utf-8").replace("在这里填 DESKTOP_TOKEN", TOKEN), encoding="utf-8")
            cfg = agent.load_config(path)
            self.assertEqual(cfg.servers, ["ws://192.168.31.13:8765/desktop", "ws://10.147.17.58:8765/desktop"])
            self.assertEqual(sorted(cfg.apps), ["notepad", "steam"])
            path.write_text('servers = ["ws://x:1/desktop"]\ntoken = "short"\n', encoding="utf-8")
            with self.assertRaises(SystemExit):
                agent.load_config(path)

    def test_same_signature_both_sides(self):
        self.assertEqual(agent.sign(TOKEN, "run", "n", 1, "open", "steam"), sign(TOKEN, "run", "n", 1, "open", "steam"))
        self.assertEqual(agent.sign(TOKEN, "result", "n", 1, True, "x"), sign(TOKEN, "result", "n", 1, True, "x"))


if __name__ == "__main__":
    unittest.main()
