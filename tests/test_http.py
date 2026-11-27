import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fin_edgar_mcp.cache import Cache
from fin_edgar_mcp.errors import NotFoundError, RateLimitedError, UpstreamError
from fin_edgar_mcp.http import CallRecord, Fetcher, current_call, public_url

URL = "https://data.sec.gov/test.json"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
async def fetcher(tmp_path: Path, clock: Clock, sleeps: list[float]):
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    cache = Cache(tmp_path / "c.duckdb", clock=clock)
    f = Fetcher(
        cache,
        limiters={},
        headers_for_host=lambda host: {"User-Agent": "test"},
        sleep=fake_sleep,
    )
    yield f
    await f.aclose()
    cache.close()


@respx.mock
async def test_second_call_is_a_cache_hit(fetcher: Fetcher):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    first = await fetcher.get_json(URL, ttl_seconds=60)
    second = await fetcher.get_json(URL, ttl_seconds=60)
    assert (first.cache_hit, second.cache_hit) == (False, True)
    assert second.data == {"a": 1}
    assert route.call_count == 1


@respx.mock
async def test_expired_entry_is_refetched(fetcher: Fetcher, clock: Clock):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    await fetcher.get_json(URL, ttl_seconds=60)
    clock.now += timedelta(seconds=61)
    again = await fetcher.get_json(URL, ttl_seconds=60)
    assert again.cache_hit is False
    assert route.call_count == 2


@respx.mock
async def test_stale_copy_served_when_upstream_fails(fetcher: Fetcher, clock: Clock):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    first = await fetcher.get_json(URL, ttl_seconds=60)
    clock.now += timedelta(hours=1)
    route.mock(return_value=httpx.Response(503))
    stale = await fetcher.get_json(URL, ttl_seconds=60)
    assert stale.stale is True
    assert stale.data == {"a": 1}
    assert stale.fetched_at == first.fetched_at
    assert route.call_count == 1 + 4  # one success, then 1 try + 3 retries


@respx.mock
async def test_retries_honor_retry_after(fetcher: Fetcher, sleeps: list[float]):
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    result = await fetcher.get_json(URL, ttl_seconds=60)
    assert result.data == {"ok": True}
    assert sleeps == [2.0]


@respx.mock
async def test_rate_limited_after_retries_exhausted(fetcher: Fetcher, sleeps: list[float]):
    respx.get(URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitedError):
        await fetcher.get_json(URL, ttl_seconds=60)
    assert len(sleeps) == 3
    assert sleeps[0] < sleeps[1] < sleeps[2]  # exponential backoff


@respx.mock
async def test_transport_errors_retry_then_fail(fetcher: Fetcher):
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(UpstreamError, match="Could not reach"):
        await fetcher.get_json(URL, ttl_seconds=60)


@respx.mock
async def test_404_is_not_found_without_retry(fetcher: Fetcher):
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFoundError, match="nothing here"):
        await fetcher.get_json(URL, ttl_seconds=60, not_found_message="nothing here")
    assert route.call_count == 1


@respx.mock
async def test_other_4xx_is_upstream_error(fetcher: Fetcher):
    respx.get(URL).mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
    with pytest.raises(UpstreamError, match="403: Forbidden"):
        await fetcher.get_json(URL, ttl_seconds=60)


@respx.mock
async def test_concurrent_identical_requests_share_one_call(fetcher: Fetcher):
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"a": 1})

    route = respx.get(URL).mock(side_effect=slow)
    results = await asyncio.gather(*(fetcher.get_json(URL, ttl_seconds=60) for _ in range(5)))
    assert route.call_count == 1
    assert all(r.data == {"a": 1} for r in results)


@respx.mock
async def test_api_key_never_in_cache_key_or_record(fetcher: Fetcher):
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    record = CallRecord()
    token = current_call.set(record)
    try:
        await fetcher.get_json(URL, ttl_seconds=60, params={"api_key": "SECRET", "x": "1"})
    finally:
        current_call.reset(token)
    assert record.urls == [f"{URL}?x=1"]
    assert "SECRET" not in str(fetcher.cache.get_response(f"{URL}?x=1"))
    assert record.cache_hit is False


def test_public_url():
    assert public_url(URL, None) == URL
    assert public_url(URL, {"api_key": "k"}) == URL
    assert public_url(URL, {"b": 2, "a": 1}) == f"{URL}?a=1&b=2"
