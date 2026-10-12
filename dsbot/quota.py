"""Proactive quota (a token bucket refilled by message volume), turn detection, per-person rate limiting."""

from __future__ import annotations

from collections import deque


class QuotaBucket:
    """Each time other people take refill_turns turns, 1 unit of quota is restored; at most capacity units are kept."""

    def __init__(self, capacity: float):
        self.tokens = float(capacity)
        self.turns_since_refill = 0

    def on_turn(self, refill_turns: int, capacity: int) -> None:
        self.turns_since_refill += 1
        if self.turns_since_refill >= max(1, refill_turns):
            self.turns_since_refill = 0
            self.tokens = min(float(capacity), self.tokens + 1.0)
        else:
            self.tokens = min(float(capacity), self.tokens)  # takes effect immediately when an admin lowers the cap

    def ready(self, capacity: int) -> bool:
        self.tokens = min(float(capacity), self.tokens)
        return self.tokens >= 1.0

    def spend(self, amount: float) -> None:
        self.tokens = max(0.0, self.tokens - amount)


class TurnTracker:
    """Several messages in a row from the same person within merge_seconds count as one turn, so typing in short lines can't farm quota."""

    def __init__(self) -> None:
        self.last_author: int | None = None
        self.last_at = 0.0

    def is_new_turn(self, author_id: int, now: float, merge_seconds: float) -> bool:
        new = author_id != self.last_author or (now - self.last_at) > merge_seconds
        self.last_author = author_id
        self.last_at = now
        return new


class SlidingWindow:
    """Sliding-window rate limiter, counted per key."""

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
        """Remove keys that expired long ago, so they don't pile up in a long-running process."""
        for key in [k for k, q in self._events.items() if not q or now - q[-1] >= window]:
            del self._events[key]
