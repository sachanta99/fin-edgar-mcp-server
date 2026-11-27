"""End-to-end through the MCP protocol with an in-memory fastmcp client."""

from typing import Any

import pytest
import respx
from fastmcp import Client

from fin_edgar_mcp.config import Settings
from fin_edgar_mcp.errors import InvalidInputError
from fin_edgar_mcp.server import create_server
from fin_edgar_mcp.services import Services

from . import factories as f

TOOLS = {"get_company_financials", "search_sec_filings", "get_macro_indicator", "run_readonly_sql"}


async def call(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert result.structured_content is not None
    return result.structured_content


async def call_error(client: Client, tool: str, args: dict[str, Any]) -> str:
    result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.is_error
    return str(result.content[0].text)  # type: ignore[union-attr]


async def sql(client: Client, query: str) -> list[list[Any]]:
    return (await call(client, "run_readonly_sql", {"query": query}))["rows"]


# ---------- discovery ----------


async def test_lists_four_tools_with_schemas(client: Client):
    tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == TOOLS
    for t in tools.values():
        assert t.description
        assert t.output_schema is not None
        assert t.annotations is not None and t.annotations.read_only_hint is True
    fin = tools["get_company_financials"].input_schema
    assert fin["required"] == ["company"]
    assert fin["properties"]["years"]["maximum"] == 20
    assert "revenue" in fin["properties"]["metrics"]["description"]


async def test_schema_resource(client: Client):
    text = (await client.read_resource("schema://tables"))[0].text  # type: ignore[union-attr]
    for table in ("companies", "financial_facts", "filings", "macro_observations", "tool_calls"):
        assert table in text
    assert "http_cache" not in text


# ---------- get_company_financials ----------


async def test_financials_returns_sourced_facts(client: Client):
    out = await call(client, "get_company_financials", {"company": "aapl", "years": 3})
    assert out["cik"] == "0000320193"
    assert out["company_name"] == "Apple Inc."
    revenue = [x for x in out["facts"] if x["metric"] == "revenue"]
    assert [x["fiscal_year"] for x in revenue] == [2020, 2019, 2018]
    top = revenue[0]
    assert top["value"] == 274_515e6
    assert top["accession_number"] == f.K20
    assert top["source_url"] == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019320000096/"
    )
    assert revenue[1]["restated"] is True
    missing = {m["metric"] for m in out["missing_metrics"]}
    assert "operating_cash_flow" in missing
    assert out["provenance"]["cache_hit"] is False
    assert out["provenance"]["source_urls"]


async def test_financials_second_call_is_cached(client: Client, mock_api: respx.MockRouter):
    await call(client, "get_company_financials", {"company": "AAPL"})
    calls_before = mock_api.calls.call_count
    out = await call(client, "get_company_financials", {"company": "AAPL"})
    assert out["provenance"]["cache_hit"] is True
    assert mock_api.calls.call_count == calls_before


async def test_financials_by_cik_and_metric_filter(client: Client):
    out = await call(
        client,
        "get_company_financials",
        {"company": "320193", "metrics": ["net_income", "EPS_DILUTED"], "period": "annual"},
    )
    assert {x["metric"] for x in out["facts"]} == {"net_income", "eps_diluted"}
    assert out["missing_metrics"] == []


async def test_financials_quarterly(client: Client):
    out = await call(
        client, "get_company_financials", {"company": "AAPL", "period": "quarterly",
                                            "metrics": ["revenue"]}
    )  # fmt: skip
    assert [x["fiscal_period"] for x in out["facts"]] == ["Q2", "Q1"]


async def test_financials_fill_facts_table(client: Client):
    await call(client, "get_company_financials", {"company": "AAPL"})
    rows = await sql(
        client,
        "SELECT fiscal_year, value FROM financial_facts WHERE metric = 'revenue' "
        "AND period = 'annual' ORDER BY period_end DESC LIMIT 2",
    )
    assert rows == [[2020, 274_515e6], [2019, 260_100e6]]
    assert await sql(client, "SELECT ticker FROM companies") == [["AAPL"]]


async def test_unknown_ticker_suggests_matches(client: Client):
    msg = await call_error(client, "get_company_financials", {"company": "APPL"})
    assert msg.startswith("[not_found]")
    assert "AAPL" in msg


async def test_unknown_metric(client: Client):
    msg = await call_error(
        client, "get_company_financials", {"company": "AAPL", "metrics": ["ebitda"]}
    )
    assert msg.startswith("[invalid_input]")
    assert "revenue" in msg


async def test_company_without_xbrl(client: Client):
    msg = await call_error(client, "get_company_financials", {"company": "JPM"})
    assert msg.startswith("[not_found]")
    assert "XBRL" in msg


async def test_years_out_of_range_rejected_by_schema(client: Client):
    msg = await call_error(client, "get_company_financials", {"company": "AAPL", "years": 50})
    assert "20" in msg


async def test_large_request_is_truncated(services: Services, monkeypatch):
    monkeypatch.setattr("fin_edgar_mcp.services.MAX_FACTS_PER_RESPONSE", 9)
    out = await services.get_company_financials("AAPL", years=10)
    revenue = [x for x in out.facts if x.metric == "revenue"]
    assert len(revenue) == 1  # 9 facts / 9 metrics
    assert out.truncated is True
    assert out.note and "run_readonly_sql" in out.note
    small = await services.get_company_financials("AAPL", metrics=["revenue"], years=2)
    assert small.truncated is False


# ---------- search_sec_filings ----------


async def test_search_by_company_and_form(client: Client):
    out = await call(client, "search_sec_filings", {"company": "AAPL", "form_types": ["10-k"]})
    assert out["mode"] == "company"
    assert [x["filing_date"] for x in out["filings"]] == ["2020-10-30", "2019-10-31"]
    first = out["filings"][0]
    assert first["document_url"].endswith("/000032019320000096/aapl-20200926.htm")
    assert first["report_date"] == "2020-09-26"
    stored = await sql(client, "SELECT count(*) FROM filings")
    assert stored == [[2]]


async def test_search_date_range_reads_older_pages(client: Client):
    out = await call(
        client,
        "search_sec_filings",
        {"company": "AAPL", "form_types": ["10-K"], "date_from": "2017-01-01",
         "date_to": "2018-12-31"},
    )  # fmt: skip
    assert [x["accession_number"] for x in out["filings"]] == [f.K18, f.K17]


async def test_search_limit_truncates(client: Client):
    out = await call(client, "search_sec_filings", {"company": "AAPL", "limit": 2})
    assert len(out["filings"]) == 2
    assert out["total_matched"] == 4
    assert out["truncated"] is True


async def test_full_text_search(client: Client, mock_api: respx.MockRouter):
    out = await call(
        client, "search_sec_filings", {"query": '"supply chain"', "form_types": ["10-K"]}
    )
    assert out["mode"] == "full_text"
    assert [x["cik"] for x in out["filings"]] == ["0000320193", "0000789019"]
    assert out["filings"][0]["document_url"].endswith("aapl-20200926.htm")
    request = mock_api.calls.last.request
    assert request.url.params["forms"] == "10-K"
    assert request.headers["User-Agent"].startswith("fin-edgar-mcp-server")


async def test_search_needs_company_or_query(client: Client):
    msg = await call_error(client, "search_sec_filings", {})
    assert msg.startswith("[invalid_input]")


async def test_search_bad_date_order(client: Client):
    msg = await call_error(
        client,
        "search_sec_filings",
        {"company": "AAPL", "date_from": "2020-01-01", "date_to": "2019-01-01"},
    )
    assert msg.startswith("[invalid_input]")


# ---------- get_macro_indicator ----------


async def test_macro_alias_and_missing_values(client: Client, mock_api: respx.MockRouter):
    out = await call(client, "get_macro_indicator", {"series": "unemployment",
                                                     "start": "2020-01-01"})  # fmt: skip
    assert out["series_id"] == "UNRATE"
    assert out["units"] == "Percent"
    assert [o["value"] for o in out["observations"]] == [3.5, 3.5, None, 14.7]
    for url in out["provenance"]["source_urls"]:
        assert "api_key" not in url
    obs_request = mock_api.calls.last.request
    assert obs_request.url.params["observation_start"] == "2020-01-01"
    assert obs_request.url.params["api_key"] == "test-fred-key"
    rows = await sql(client, "SELECT count(*), count(value) FROM macro_observations")
    assert rows == [[4, 3]]


async def test_macro_frequency_passed_through(client: Client, mock_api: respx.MockRouter):
    out = await call(client, "get_macro_indicator", {"series": "UNRATE", "frequency": "annual"})
    assert mock_api.calls.last.request.url.params["frequency"] == "a"
    assert "Annual" in out["frequency"]


async def test_macro_unknown_series(client: Client):
    msg = await call_error(client, "get_macro_indicator", {"series": "NOPE123"})
    assert msg.startswith("[not_found]")


async def test_macro_invalid_series_id(client: Client):
    msg = await call_error(client, "get_macro_indicator", {"series": "drop table;"})
    assert msg.startswith("[invalid_input]")


async def test_macro_without_key(make_services):
    svc = make_services(fred_api_key=None)
    async with Client(create_server(lambda: svc)) as c:
        msg = await call_error(c, "get_macro_indicator", {"series": "UNRATE"})
        assert msg.startswith("[config_missing]")
        # SEC tools still work without a FRED key.
        assert await call(c, "search_sec_filings", {"company": "AAPL", "limit": 1})


async def test_direct_calls_accept_iso_date_strings(services: Services, mock_api: respx.MockRouter):
    out = await services.get_macro_indicator("unemployment", start="2020-01-01", end="2020-12-31")
    assert out.series_id == "UNRATE"
    obs_request = mock_api.calls.last.request
    assert obs_request.url.params["observation_start"] == "2020-01-01"
    assert obs_request.url.params["observation_end"] == "2020-12-31"
    filings = await services.search_sec_filings("AAPL", date_from="2000-01-01", limit=1)
    assert filings.filings


@pytest.mark.parametrize("bad", ["2020/01/01", "yesterday", 20200101])
async def test_direct_calls_reject_bad_dates(services: Services, bad: Any):
    with pytest.raises(InvalidInputError, match="YYYY-MM-DD"):
        await services.get_macro_indicator("UNRATE", start=bad)


async def test_direct_calls_report_sources_on_cache_hits(services: Services):
    first = await services.get_macro_indicator("UNRATE")
    second = await services.get_macro_indicator("UNRATE")
    assert first.provenance.cache_hit is False
    assert second.provenance.cache_hit is True
    for prov in (first.provenance, second.provenance):
        assert len(prov.source_urls) == 2  # series metadata + observations
        assert all("api_key" not in u for u in prov.source_urls)
    assert second.provenance.source_urls == first.provenance.source_urls
    facts = await services.get_company_financials("AAPL", metrics=["revenue"])
    assert facts.provenance.source_urls


async def test_sec_without_user_agent(make_services):
    svc = make_services(edgar_user_agent=None)
    async with Client(create_server(lambda: svc)) as c:
        msg = await call_error(c, "get_company_financials", {"company": "AAPL"})
        assert msg.startswith("[config_missing]")
        assert "EDGAR_USER_AGENT" in msg


# ---------- run_readonly_sql and the audit log ----------


async def test_sql_rejects_writes(client: Client):
    msg = await call_error(client, "run_readonly_sql", {"query": "DELETE FROM tool_calls"})
    assert msg.startswith("[sql_rejected]")


async def test_every_call_is_audited(client: Client):
    await call(client, "get_company_financials", {"company": "AAPL"})
    await call_error(client, "get_macro_indicator", {"series": "NOPE123"})
    await call(client, "get_company_financials", {"company": "AAPL"})
    rows = await sql(
        client,
        "SELECT tool, cache_hit, error_code, row_count, len(upstream_urls) "
        "FROM tool_calls ORDER BY id",
    )
    assert rows[0][0] == "get_company_financials" and rows[0][1] is False
    assert rows[0][3] > 0 and rows[0][4] == 2  # ticker file + company facts
    assert rows[1][0] == "get_macro_indicator" and rows[1][2] == "not_found"
    assert rows[2][1] is True
    # The SQL call that read the log is itself logged afterwards.
    assert (await sql(client, "SELECT count(*) FROM tool_calls"))[0][0] == 4


async def test_internal_errors_are_masked(client: Client, services: Services, monkeypatch):
    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("secret stack detail")

    monkeypatch.setattr(services, "get_macro_indicator", boom)
    msg = await call_error(client, "get_macro_indicator", {"series": "UNRATE"})
    assert msg.startswith("[internal_error]")
    assert "secret stack detail" not in msg


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("EDGAR_USER_AGENT", "  ua@example.com ")
    monkeypatch.setenv("FRED_API_KEY", "")
    monkeypatch.setenv("FIN_MCP_CACHE_PATH", str(tmp_path / "x.duckdb"))
    s = Settings.from_env()
    assert s.edgar_user_agent == "ua@example.com"
    assert s.fred_api_key is None
    assert s.cache_path == tmp_path / "x.duckdb"


@pytest.mark.parametrize("bad", ["", "   ", "not a ticker!"])
async def test_bad_company_input(client: Client, bad: str):
    msg = await call_error(client, "get_company_financials", {"company": bad})
    assert msg.startswith("[invalid_input]")
