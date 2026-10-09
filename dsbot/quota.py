"""主动发言额度（按消息量恢复的令牌桶）、轮次识别、按人限速。"""

from __future__ import annotations

from collections import deque


class QuotaBucket:
    """别人每说 refill_turns 轮，恢复 1 次额度；最多攒 capacity 次。"""

    def __init__(self, capacity: float):
        self.tokens = float(capacity)
        self.turns_since_refill = 0

    def on_turn(self, refill_turns: int, capacity: int) -> None:
        self.turns_since_refill += 1
        if self.turns_since_refill >= max(1, refill_turns):
            self.turns_since_refill = 0
            self.tokens = min(float(capacity), self.tokens + 1.0)
        else:
            self.tokens = min(float(capacity), self.tokens)  # 管理员调小上限后立即生效

    def ready(self, capacity: int) -> bool:
        self.tokens = min(float(capacity), self.tokens)
        return self.tokens >= 1.0

    def spend(self, amount: float) -> None:
        self.tokens = max(0.0, self.tokens - amount)


class TurnTracker:
    """同一个人在 merge_seconds 内连发的几条算一轮，免得分行打字刷额度。"""

    def __init__(self) -> None:
        self.last_author: int | None = None
        self.last_at = 0.0

    def is_new_turn(self, author_id: int, now: float, merge_seconds: float) -> bool:
        new = author_id != self.last_author or (now - self.last_at) > merge_seconds
        self.last_author = author_id
        self.last_at = now
        return new


class SlidingWindow:
    """按 key 计数的滑动窗口限速。"""

    def __init__(self) -> None:
        self._events: dict[object, deque[float]] = {}

    def allow(self, key: object, limit: int, window: float, now: float) -> bool:
        q = self._events.setdefault(key, deque())
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def prune(self, window: float, now: float) -> None:
        """清掉早就过期的 key，免得常驻进程里越攒越多。"""
        for key in [k for k, q in self._events.items() if not q or now - q[-1] >= window]:
            del self._events[key]
