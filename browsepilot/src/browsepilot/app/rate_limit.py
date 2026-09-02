"""In-memory sliding-window rate limiter (single instance; swap for Redis at scale)."""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """Per-key sliding-window limiter over a fixed wall-clock window.

    Each ``allow(key)`` records a hit timestamp; hits older than
    ``window_seconds`` are pruned lazily on the next call for that key.
    Thread-safe via a single lock (append + prune are atomic together).
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > self._window:
                dq.popleft()
            if len(dq) >= self._max:
                return False
            dq.append(now)
            return True
