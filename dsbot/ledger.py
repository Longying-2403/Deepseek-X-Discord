"""花费账本：按 bot.timezone 的自然日累计，每个服务器一笔。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .store import Store


class Ledger:
    def __init__(self, store: Store, tz: str):
        self.store = store
        self.tz = ZoneInfo(tz)
        self._day: str | None = None
        self._spend: dict[int, float] = {}

    def day(self, now: float) -> str:
        return datetime.fromtimestamp(now, self.tz).date().isoformat()

    def _sync(self, now: float) -> None:
        day = self.day(now)
        if day != self._day:
            self._day = day
            self._spend = self.store.spend_by_guild(day)

    def add(self, guild_id: int, cost: float, now: float) -> None:
        self._sync(now)
        self._spend[guild_id] = self._spend.get(guild_id, 0.0) + cost

    def guild_spent(self, guild_id: int, now: float) -> float:
        self._sync(now)
        return self._spend.get(guild_id, 0.0)

    def total_spent(self, now: float) -> float:
        self._sync(now)
        return sum(self._spend.values())

    def all_guilds(self, now: float) -> dict[int, float]:
        self._sync(now)
        return dict(self._spend)
