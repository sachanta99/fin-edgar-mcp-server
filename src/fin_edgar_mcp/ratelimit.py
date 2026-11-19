"""Per-host request pacing.

A strict-spacing limiter: requests to one host start at least ``1 / rate`` seconds apart.
That guarantees no more than ``rate`` requests in any one-second window, which a bursty
token bucket does not.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class RateLimiter:
    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self.interval = 1.0 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until this caller may send a request."""
        async with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.interval
        delay = slot - now
        if delay > 0:
            await self._sleep(delay)
