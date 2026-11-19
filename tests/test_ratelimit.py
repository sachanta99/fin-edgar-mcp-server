import asyncio

import pytest

from fin_edgar_mcp.ratelimit import RateLimiter


async def test_burst_of_50_never_exceeds_8_per_second():
    """50 concurrent callers at t=0: every 1-second window holds at most 8 request starts."""
    slots: list[float] = []

    async def spy_sleep(delay: float) -> None:
        slots.append(delay)  # the clock is frozen at 0, so the delay is the start time

    limiter = RateLimiter(8, clock=lambda: 0.0, sleep=spy_sleep)
    await asyncio.gather(*(limiter.acquire() for _ in range(50)))
    starts = sorted([0.0, *slots])  # the first caller does not sleep
    assert len(starts) == 50
    for i, t in enumerate(starts):
        assert len([s for s in starts[i:] if s < t + 1.0]) <= 8
    assert starts[-1] == pytest.approx(49 / 8)


async def test_concurrent_callers_get_distinct_slots():
    limiter = RateLimiter(100)
    loop = asyncio.get_running_loop()
    times: list[float] = []

    async def call() -> None:
        await limiter.acquire()
        times.append(loop.time())

    await asyncio.gather(*(call() for _ in range(20)))
    times.sort()
    # 20 calls at 100/s need at least ~0.19 s in total.
    assert times[-1] - times[0] >= 0.17


def test_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        RateLimiter(0)
