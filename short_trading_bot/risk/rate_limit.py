"""Token-bucket rate limiter (per KIS endpoint group).

KIS allows ~20 req/s live (stricter on paper); throttle to ~15/s. The clock is injectable
so the refill math is unit-testable without sleeping.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_sec: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = float(rate_per_sec)
        self._capacity = float(capacity) if capacity is not None else float(rate_per_sec)
        self._clock = clock
        self._tokens = self._capacity
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    async def acquire(self, tokens: float = 1.0) -> None:
        while not self.try_acquire(tokens):
            deficit = tokens - self._tokens
            await asyncio.sleep(max(deficit / self._rate, 0.001))

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens
