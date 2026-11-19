from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx
from fastmcp import Client

from fin_edgar_mcp.config import Settings
from fin_edgar_mcp.server import create_server
from fin_edgar_mcp.services import Services

from . import factories as f

SEC_UA = "fin-edgar-mcp-server/0.1 Test Runner test@example.com"


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        edgar_user_agent=SEC_UA,
        fred_api_key="test-fred-key",
        cache_path=tmp_path / "cache.duckdb",
        sec_requests_per_second=1000,
        fred_requests_per_second=1000,
    )


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(200, json=f.TICKERS)
        )
        router.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json").mock(
            return_value=httpx.Response(200, json=f.AAPL_FACTS)
        )
        router.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json").mock(
            return_value=httpx.Response(200, json=f.MSFT_FACTS)
        )
        router.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0001318605.json").mock(
            return_value=httpx.Response(200, json=f.TSLA_FACTS)
        )
        router.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000019617.json").mock(
            return_value=httpx.Response(404, text="Not Found")
        )
        router.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
            return_value=httpx.Response(200, json=f.AAPL_SUBMISSIONS)
        )
        router.get("https://data.sec.gov/submissions/CIK0000320193-submissions-001.json").mock(
            return_value=httpx.Response(200, json=f.AAPL_SUBMISSIONS_PAGE)
        )
        router.get("https://efts.sec.gov/LATEST/search-index").mock(
            return_value=httpx.Response(200, json=f.FULL_TEXT)
        )

        def fred_series(request: httpx.Request) -> httpx.Response:
            sid = request.url.params.get("series_id")
            if sid == "UNRATE":
                return httpx.Response(200, json=f.FRED_SERIES_UNRATE)
            return httpx.Response(
                400,
                json={
                    "error_code": 400,
                    "error_message": "Bad Request. The series does not exist.",
                },
            )

        router.get("https://api.stlouisfed.org/fred/series").mock(side_effect=fred_series)
        router.get("https://api.stlouisfed.org/fred/series/observations").mock(
            return_value=httpx.Response(200, json=f.FRED_OBS_UNRATE)
        )
        yield router


@pytest.fixture
async def services(settings: Settings, mock_api: respx.MockRouter) -> AsyncIterator[Services]:
    svc = Services.create(settings, sleep=_no_sleep)
    yield svc
    await svc.aclose()


@pytest.fixture
async def client(services: Services) -> AsyncIterator[Client]:
    async with Client(create_server(lambda: services)) as c:
        yield c


@pytest.fixture
def make_services(tmp_path: Path, mock_api: respx.MockRouter):
    created: list[Services] = []

    def _make(**overrides: object) -> Services:
        base = Settings(
            edgar_user_agent=SEC_UA,
            fred_api_key="test-fred-key",
            cache_path=tmp_path / f"cache{len(created)}.duckdb",
            sec_requests_per_second=1000,
            fred_requests_per_second=1000,
        )
        svc = Services.create(replace(base, **overrides), sleep=_no_sleep)  # type: ignore[arg-type]
        created.append(svc)
        return svc

    yield _make
    for svc in created:
        svc.cache.close()
