"""按人限速（每人每分钟能叫它几次）。2.10.0 以前这里还有主动发言的额度。"""

from __future__ import annotations

from collections import deque


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
