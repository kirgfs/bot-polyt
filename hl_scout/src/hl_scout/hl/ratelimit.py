"""Weight budget for the Info API (1200 weight per minute per IP) [api_notes §4].

The docs do not say how the minute is counted, so the limiter keeps a log of the last 60 seconds: no 60-second
window ever holds more than the budget, even right after start (a token bucket allows 2× in its first minute,
and the smoke run got HTTP 429 from exactly that).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

WINDOW_S = 60.0


class WeightLimiter:
    """`acquire(w, reserve=r)` waits until the window has room for `w + r` and books it. `r` reserves the largest
    per-item surcharge the response can bring; `settle(ticket, actual)` swaps the reservation for the real
    surcharge once the response is in. `charge(w)` books weight after the fact (may overshoot the budget, which
    delays the next callers). A request heavier than the whole budget waits for an empty window.
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
        self._clock = clock
        self._sleep = sleep
        self._log: deque[list[float]] = deque()  # [time, weight]; weight is mutable until settled
        self._lock = asyncio.Lock()
        self.spent = 0.0

    def _used(self) -> float:
        cutoff = self._clock() - WINDOW_S
        while self._log and self._log[0][0] <= cutoff:
            self._log.popleft()
        return sum(w for _, w in self._log)

    @property
    def available(self) -> float:
        return self.capacity - self._used()

    def _wait_for(self, amount: float) -> float:
        """Seconds until `amount` of booked weight leaves the window."""
        now, freed = self._clock(), 0.0
        for t, w in self._log:
            freed += w
            if freed >= amount - 1e-9:
                return max(t + WINDOW_S - now, 1e-3)
        return WINDOW_S

    async def acquire(self, weight: float, reserve: float = 0.0) -> list[float]:
        async with self._lock:
            need = min(weight + reserve, self.capacity)
            while True:
                used = self._used()
                if used + need <= self.capacity + 1e-9:
                    ticket = [self._clock(), weight + reserve]
                    self._log.append(ticket)
                    self.spent += weight
                    return ticket
                await self._sleep(self._wait_for(used + need - self.capacity))

    def settle(self, ticket: list[float], reserved: float, actual: float) -> None:
        ticket[1] += actual - reserved
        self.spent += actual

    def charge(self, weight: float) -> None:
        if weight <= 0:
            return
        self._log.append([self._clock(), weight])
        self.spent += weight
