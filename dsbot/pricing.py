"""按 usage 估算花费。价格单位：美元 / 百万 token，按高峰价填，非高峰自动打折。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Usage


@dataclass(frozen=True)
class Pricing:
    # deepseek-flash 的高峰价（2026-09 官方价格页）
    cache_hit: float = 0.006
    cache_miss: float = 0.30
    output: float = 1.20
    offpeak_factor: float = 0.5
    # 高峰时段：周一到周五 UTC 01:00-04:00、06:00-10:00（不识别中国法定节假日，按高峰算，偏保守）
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
