"""测试用的假模型、假 Discord、假时钟。"""

from __future__ import annotations

import copy
import random
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsbot.config import AppConfig  # noqa: E402
from dsbot.engine import Engine  # noqa: E402
from dsbot.ledger import Ledger  # noqa: E402
from dsbot.models import ChannelInfo, ChannelPlace, Completion, IncomingMessage, SearchResult, Usage  # noqa: E402
from dsbot.settings import Settings  # noqa: E402
from dsbot.store import Store  # noqa: E402

# 2026-10-07 是周三。UTC 12:00 不在高峰时段。
T0 = 1_791_374_400.0
GUILD = 1
CHANNEL = 100
OWNER = 7  # 测试里的 OWNER_ID（dsowner）


class FakeClock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeLLM:
    def __init__(self, responder: Callable[[dict[str, Any]], Any] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.responder = responder or (lambda call: '{"actions": []}')
        self.prompt_tokens = 1000
        self.hook = None  # 可选：async def hook(call)，模拟“调用进行中”发生的事

    async def complete(self, messages, *, thinking="off", json_mode=False, max_tokens=None, user_id=None, timeout_scale=1.0):
        call = {
            "messages": copy.deepcopy(messages),
            "thinking": thinking,
            "json_mode": json_mode,
            "max_tokens": max_tokens,
            "user_id": user_id,
            "timeout_scale": timeout_scale,
        }
        self.calls.append(call)
        if self.hook is not None:
            await self.hook(call)
        out = self.responder(call)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, Completion):
            return out
        return Completion(
            content=out,
            reasoning="想了一下",
            usage=Usage(prompt_tokens=self.prompt_tokens, hit=self.prompt_tokens - 100, miss=100, output=30),
            finish_reason="stop",
        )


class _NullTyping:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class FakeTransport:
    def __init__(self):
        self.sent: list[tuple[int, str, int | None, bool]] = []
        self.reactions: list[tuple[int, int, str]] = []
        self.stickers_sent: list[tuple[int, str, int | None, bool]] = []
        self.files_sent: list[tuple[int, bytes, str, int | None, bool]] = []  # 发出去的图（表情包、找到的图）
        self.emojis: list[str] = []  # 服务器里能用的表情名
        self.stickers: list[str] = []  # 服务器里能发的贴图名
        self.typing_calls = 0
        self.can_send = True
        self._next_id = 90_000
        # 频道 id -> 位置；没登记的频道当成 GUILD 里的公开频道
        self.places: dict[int, ChannelPlace] = {}
        self.names = {CHANNEL: "闲聊"}
        self.channel_map: dict[str, set[int]] = {}  # 服务器里的频道名 -> 频道（#频道名 换成链接用）
        self.members: dict[str, set[int]] = {}  # 按名字找人能找到的（名字 -> 用户）
        self.member_searches: list[list[str]] = []
        self.mentions: list[tuple[int, ...]] = []  # 每条发出去的文字真的提醒了谁（和 sent 一一对应）

    def channel_info(self, channel_id: int) -> ChannelInfo:
        return ChannelInfo(
            guild_name="测试服",
            channel_name=self.names.get(channel_id, f"频道{channel_id}"),
            bot_name="小D",
            emojis=list(self.emojis),
            stickers=list(self.stickers),
        )

    def locate(self, channel_id: int) -> ChannelPlace | None:
        if channel_id in self.places:
            return self.places[channel_id]
        return ChannelPlace(guild_id=GUILD, name=self.names.get(channel_id, f"频道{channel_id}"), public=True)

    async def send(self, channel_id, text, *, reply_to, ping, mentions=()):
        if not self.can_send:
            return None
        self._next_id += 1
        self.sent.append((channel_id, text, reply_to, ping))
        self.mentions.append(tuple(mentions))
        return self._next_id

    def channel_names(self, guild_id):
        return {name: set(ids) for name, ids in self.channel_map.items()}

    async def find_members(self, guild_id, starts):
        self.member_searches.append(list(starts))
        return {name: set(ids) for name, ids in self.members.items() if any(s.startswith(name[:2]) for s in starts)}

    async def send_sticker(self, channel_id, name, *, reply_to, ping):
        if not self.can_send:
            return None
        self._next_id += 1
        self.stickers_sent.append((channel_id, name, reply_to, ping))
        return self._next_id

    async def send_file(self, channel_id, data, filename, *, reply_to, ping):
        if not self.can_send:
            return None
        self._next_id += 1
        self.files_sent.append((channel_id, data, filename, reply_to, ping))
        return self._next_id

    async def react(self, channel_id, message_id, emoji):
        self.reactions.append((channel_id, message_id, emoji))
        return True

    def typing(self, channel_id):
        self.typing_calls += 1
        return _NullTyping()


class FakeSearcher:
    """假的联网搜索：记录调用，按关键词返回结果；结果是 Exception 时抛出。"""

    def __init__(self, results: dict[str, Any] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.results = results or {}

    async def search(self, query: str, *, today: str, max_chars: int, user_id: str | None = None) -> SearchResult:
        self.calls.append((query, today))
        out = self.results.get(query)
        if isinstance(out, Exception):
            raise out
        if out is None:
            out = SearchResult(
                summary=f"关于「{query}」搜到的内容",
                sources=[("示例网站", "https://example.com/a")],
                usage=Usage(prompt_tokens=3000, hit=0, miss=3000, output=200),
                provider="DeepSeek",
            )
        return out


def make_engine(
    responder=None,
    *,
    image_loader=None,
    defaults: dict[str, Any] | None = None,
    searcher=None,
    owner_id=OWNER,
    link_reader=None,
    pictures=None,
    memes=None,
    cards=None,
    memory=None,
    engine_cfg: dict[str, Any] | None = None,
):
    cfg = AppConfig()
    for key, value in (engine_cfg or {}).items():
        setattr(cfg.engine, key, value)
    if defaults:
        cfg.defaults.update(defaults)
    store = Store(":memory:")
    settings = Settings(cfg.defaults, store)
    ledger = Ledger(store, cfg.bot.timezone)
    llm = FakeLLM(responder)
    transport = FakeTransport()
    clock = FakeClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def no_images(att, max_side):
        return None

    engine = Engine(
        cfg,
        settings,
        store,
        llm,
        ledger,
        transport,
        clock=clock,
        sleep=fake_sleep,
        rng=random.Random(0),
        image_loader=image_loader or no_images,
        searcher=searcher,
        owner_id=owner_id,
        link_reader=link_reader,
        pictures=pictures,
        memes=memes,
        cards=cards,
        memory=memory,
    )
    engine.test_sleeps = sleeps  # type: ignore[attr-defined]
    return engine, llm, transport, clock


def msg(message_id: int, text: str, *, author: int = 11, name: str = "阿明", **kw) -> IncomingMessage:
    kw.setdefault("channel_id", CHANNEL)
    kw.setdefault("guild_id", GUILD)
    return IncomingMessage(message_id=message_id, author_id=author, author_name=name, text=text, **kw)
