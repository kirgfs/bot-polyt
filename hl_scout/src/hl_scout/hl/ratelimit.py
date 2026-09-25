"""Weight-based token bucket for the Info API budget (1200 weight/min per IP) [api_notes §4]."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class WeightLimiter:
    """Refills `budget_per_min / 60` weight per second up to `budget_per_min`.

    `acquire(w)` waits until the bucket holds `w` (a request heavier than the whole bucket waits for a full
    bucket). `charge(w)` books extra weight after the fact (per-item surcharges): the balance may go negative,
    which delays the next callers.
    """

    def __init__(
        self,
        budget_per_min: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if budget_per_min <= 0:
            raise ValueError("budget_per_min must be positive")
        self.capacity = float(budget_per_min)
        self.rate = self.capacity / 60.0
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()
        self.spent = 0.0

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self, weight: float) -> None:
        async with self._lock:
            need = min(weight, self.capacity)
            while True:
                self._refill()
                if self._tokens >= need:
                    self._tokens -= weight
                    self.spent += weight
                    return
                await self._sleep((need - self._tokens) / self.rate)

    def charge(self, weight: float) -> None:
        if weight <= 0:
            return
        self._refill()
        self._tokens -= weight
        self.spent += weight
