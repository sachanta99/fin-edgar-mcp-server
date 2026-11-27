"""Opt-in checks against the real APIs: ``pytest -m live``.

Needs network access to sec.gov and api.stlouisfed.org, EDGAR_USER_AGENT, and FRED_API_KEY
for the FRED test. These also confirm the endpoint shapes the offline fixtures imitate.
"""

import os
from pathlib import Path

import pytest

from fin_edgar_mcp.config import Settings
from fin_edgar_mcp.services import Services

pytestmark = pytest.mark.live


@pytest.fixture
async def live(tmp_path: Path):
    if not os.environ.get("EDGAR_USER_AGENT"):
        pytest.skip("EDGAR_USER_AGENT not set")
    base = Settings.from_env()
    svc = Services.create(
        Settings(
            edgar_user_agent=base.edgar_user_agent,
            fred_api_key=base.fred_api_key,
            cache_path=tmp_path / "live.duckdb",
        )
    )
    yield svc
    await svc.aclose()


async def test_apple_revenue_has_sources(live: Services):
    out = await live.get_company_financials("AAPL", metrics=["revenue"], years=5)
    assert len(out.facts) == 5
    for fact in out.facts:
        assert fact.accession_number and fact.form.startswith("10-K")
        assert fact.value > 1e11


async def test_apple_10k_filings(live: Services):
    out = await live.search_sec_filings("AAPL", form_types=["10-K"], limit=3)
    assert len(out.filings) == 3
    assert all(f.document_url.startswith("https://www.sec.gov/Archives/") for f in out.filings)


async def test_full_text_search(live: Services):
    out = await live.search_sec_filings(query='"going concern"', form_types=["10-K"], limit=5)
    assert out.filings


async def test_fred_unemployment(live: Services):
    if not live.settings.fred_api_key:
        pytest.skip("FRED_API_KEY not set")
    out = await live.get_macro_indicator("unemployment")
    assert out.series_id == "UNRATE"
    assert out.observations
