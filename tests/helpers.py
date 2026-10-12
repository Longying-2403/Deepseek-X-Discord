"""Fake model, fake Discord and fake clock for the tests."""

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

# 2026-10-07 is a Wednesday. 12:00 UTC is outside peak hours.
T0 = 1_791_374_400.0
GUILD = 1
CHANNEL = 100


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
        self.hook = None  # optional: async def hook(call), to simulate things that happen "while the call is in progress"

    async def complete(self, messages, *, thinking="off", json_mode=False, max_tokens=None, user_id=None):
        call = {
            "messages": copy.deepcopy(messages),
            "thinking": thinking,
            "json_mode": json_mode,
            "max_tokens": max_tokens,
            "user_id": user_id,
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
            reasoning="thought about it",
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
        self.typing_calls = 0
        self.can_send = True
        self._next_id = 90_000
        # channel id -> place; channels not registered here count as public channels in GUILD
        self.places: dict[int, ChannelPlace] = {}
        self.names = {CHANNEL: "chat"}

    def channel_info(self, channel_id: int) -> ChannelInfo:
        return ChannelInfo(guild_name="Test Server", channel_name=self.names.get(channel_id, f"channel{channel_id}"), bot_name="Dee")

    def locate(self, channel_id: int) -> ChannelPlace | None:
        if channel_id in self.places:
            return self.places[channel_id]
        return ChannelPlace(guild_id=GUILD, name=self.names.get(channel_id, f"channel{channel_id}"), public=True)

    async def send(self, channel_id, text, *, reply_to, ping):
        if not self.can_send:
            return None
        self._next_id += 1
        self.sent.append((channel_id, text, reply_to, ping))
        return self._next_id

    async def react(self, channel_id, message_id, emoji):
        self.reactions.append((channel_id, message_id, emoji))
        return True

    def typing(self, channel_id):
        self.typing_calls += 1
        return _NullTyping()


class FakeSearcher:
    """Fake web search: records calls and returns results by query; raises when the result is an Exception."""

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
                summary=f'search results about "{query}"',
                sources=[("Example Site", "https://example.com/a")],
                usage=Usage(prompt_tokens=3000, hit=0, miss=3000, output=200),
                provider="DeepSeek",
            )
        return out


def make_engine(responder=None, *, image_loader=None, defaults: dict[str, Any] | None = None, searcher=None):
    cfg = AppConfig()
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
    )
    engine.test_sleeps = sleeps  # type: ignore[attr-defined]
    return engine, llm, transport, clock


def msg(message_id: int, text: str, *, author: int = 11, name: str = "Alex", **kw) -> IncomingMessage:
    kw.setdefault("channel_id", CHANNEL)
    kw.setdefault("guild_id", GUILD)
    return IncomingMessage(message_id=message_id, author_id=author, author_name=name, text=text, **kw)
