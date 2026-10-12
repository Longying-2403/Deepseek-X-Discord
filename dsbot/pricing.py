"""Estimate cost from usage. Prices are in USD per million tokens at peak rates; off-peak discounts are applied automatically."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Usage


@dataclass(frozen=True)
class Pricing:
    # deepseek-flash peak prices (official pricing page, 2026-09)
    cache_hit: float = 0.006
    cache_miss: float = 0.30
    output: float = 1.20
    offpeak_factor: float = 0.5
    # peak hours: Monday to Friday, UTC 01:00-04:00 and 06:00-10:00 (Chinese public holidays aren't recognized and count as peak, to stay on the safe side)
    peak_hours_utc: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))

    def is_peak(self, ts: float) -> bool:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if dt.weekday() >= 5:
            return False
        return any(start <= dt.hour < end for start, end in self.peak_hours_utc)

    def cost(self, usage: Usage, ts: float) -> float:
        factor = 1.0 if self.is_peak(ts) else self.offpeak_factor
        raw = usage.hit * self.cache_hit + usage.miss * self.cache_miss + usage.output * self.output
        return raw / 1_000_000 * factor
