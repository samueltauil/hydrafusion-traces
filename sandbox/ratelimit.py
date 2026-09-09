"""Sliding-window rate limiter used by the ingest gateway."""

import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_seconds
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        if len(self._events) > self.max_events:
            return False
        self._events.append(now)
        return True


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float, now: float | None = None):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = float(capacity)
        self.updated = time.monotonic() if now is None else now

    def consume(self, amount: int = 1, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        elapsed = now - self.updated
        self.updated = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        if self.tokens < amount:
            return False
        self.tokens -= amount
        return True
