"""MCP wiring: the four tools, the schema resource, audit logging and error mapping."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from . import __version__
from .config import Settings
from .errors import ErrorCode, FinMCPError
from .financials import STANDARD_METRICS
from .fred import SERIES_ALIASES
from .http import CallRecord, current_call
from .models import CompanyFinancials, FilingSearchResult, MacroSeries, SQLResult
from .services import Services

log = logging.getLogger("fin_edgar_mcp")

INSTRUCTIONS = """\
Financial data from SEC EDGAR and FRED. Every number is returned exactly as reported, with
its filing accession number, XBRL concept and source URL: cite those, and never estimate a
value a tool did not return. Missing data is listed under missing_metrics.
Compute ratios yourself from the returned facts, or with run_readonly_sql over the cache.
Read the resource schema://tables before writing SQL.
"""

Frequency = Literal["daily", "weekly", "biweekly", "monthly", "quarterly", "semiannual", "annual"]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str) -> None:
    # stdout carries the MCP protocol, so logs must go to stderr.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _row_count(result: Any) -> int | None:
    for attr in ("facts", "filings", "observations", "rows"):
        if hasattr(result, attr):
            return len(getattr(result, attr))
    return None


def create_server(services_factory: Callable[[], Services] | None = None) -> FastMCP:
    """Build the server. Services are created on first use so startup never blocks."""
    holder: dict[str, Services] = {}

    def services() -> Services:
        if "s" not in holder:
            factory = services_factory or (lambda: Services.create(Settings.from_env()))
            holder["s"] = factory()
        return holder["s"]

    @asynccontextmanager
    async def audited(tool: str, arguments: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        svc = services()
        record = CallRecord()
        token = current_call.set(record)
        started = time.perf_counter()
        outcome: dict[str, Any] = {"result": None}
        error_code: str | None = None
        try:
            yield outcome
        except FinMCPError as exc:
            error_code = exc.code.value
            raise ToolError(exc.to_text()) from exc
        except Exception as exc:
            error_code = ErrorCode.INTERNAL_ERROR.value
            log.exception("tool %s failed", tool)
            raise ToolError(
                f"[{error_code}] Unexpected error: {type(exc).__name__}. See server logs."
            ) from exc
        finally:
            current_call.reset(token)
            duration_ms = (time.perf_counter() - started) * 1000
            try:
                svc.cache.log_call(
                    tool=tool,
                    arguments=arguments,
                    duration_ms=round(duration_ms, 2),
                    cache_hit=record.cache_hit,
                    upstream_urls=record.urls,
                    row_count=_row_count(outcome["result"]),
                    error_code=error_code,
                )
            except Exception:  # the audit log must never break a tool call
                log.exception("could not write audit log row")
            log.info(
                "tool=%s duration_ms=%.1f cache_hit=%s error=%s",
                tool,
                duration_ms,
                record.cache_hit,
                error_code,
            )

    mcp = FastMCP(name="fin-edgar", instructions=INSTRUCTIONS, version=__version__)
    fetch_hints = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}

    @mcp.tool(annotations=fetch_hints)
    async def get_company_financials(
        company: Annotated[
            str, Field(description="Ticker (e.g. 'AAPL', 'BRK.B') or SEC CIK (e.g. '320193').")
        ],
        metrics: Annotated[
            list[str] | None,
            Field(
                description="Metrics to return. Default: all of "
                + ", ".join(STANDARD_METRICS)
                + "."
            ),
        ] = None,
        period: Annotated[
            Literal["annual", "quarterly"],
            Field(description="annual = 10-K fiscal years; quarterly = 10-Q quarters (no Q4)."),
        ] = "annual",
        years: Annotated[int, Field(ge=1, le=20, description="How many years back.")] = 5,
    ) -> CompanyFinancials:
        """Reported financials from a company's XBRL filings, each value with its source filing.

        Values are exactly as filed; restated periods use the latest filing and are flagged.
        """
        args = {"company": company, "metrics": metrics, "period": period, "years": years}
        async with audited("get_company_financials", args) as out:
            out["result"] = await services().get_company_financials(company, metrics, period, years)
        result: CompanyFinancials = out["result"]
        return result

    @mcp.tool(annotations=fetch_hints)
    async def search_sec_filings(
        company: Annotated[
            str | None, Field(description="Ticker or CIK. Optional when query is given.")
        ] = None,
        form_types: Annotated[
            list[str] | None, Field(description="Form types, e.g. ['10-K', '10-Q', '8-K'].")
        ] = None,
        date_from: Annotated[date | None, Field(description="Earliest filing date.")] = None,
        date_to: Annotated[date | None, Field(description="Latest filing date.")] = None,
        query: Annotated[
            str | None,
            Field(
                description="Optional EDGAR full-text search phrase, e.g. '\"going concern\"'. "
                "Searches filings since 2001 across all companies unless company is set."
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="Max filings to return.")] = 20,
    ) -> FilingSearchResult:
        """Find SEC filings by company, form type and date, or by full-text search."""
        args = {
            "company": company,
            "form_types": form_types,
            "date_from": date_from,
            "date_to": date_to,
            "query": query,
            "limit": limit,
        }
        async with audited("search_sec_filings", args) as out:
            out["result"] = await services().search_sec_filings(
                company, form_types, date_from, date_to, query, limit
            )
        result: FilingSearchResult = out["result"]
        return result

    @mcp.tool(annotations=fetch_hints)
    async def get_macro_indicator(
        series: Annotated[
            str,
            Field(
                description="FRED series id (e.g. 'UNRATE') or alias: "
                + ", ".join(f"{k}={v}" for k, v in SERIES_ALIASES.items())
                + "."
            ),
        ],
        start: Annotated[date | None, Field(description="First observation date.")] = None,
        end: Annotated[date | None, Field(description="Last observation date.")] = None,
        frequency: Annotated[
            Frequency | None,
            Field(description="Downsample to a lower frequency (averaged). Default: native."),
        ] = None,
    ) -> MacroSeries:
        """A FRED economic time series with its metadata. Missing values are null."""
        args = {"series": series, "start": start, "end": end, "frequency": frequency}
        async with audited("get_macro_indicator", args) as out:
            out["result"] = await services().get_macro_indicator(series, start, end, frequency)
        result: MacroSeries = out["result"]
        return result

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
    async def run_readonly_sql(
        query: Annotated[
            str,
            Field(
                description="One DuckDB SELECT (or WITH ... SELECT) over the cache tables. "
                "Read schema://tables first."
            ),
        ],
        max_rows: Annotated[int, Field(ge=1, le=1000, description="Row cap.")] = 200,
    ) -> SQLResult:
        """Read-only SQL over the local cache of filings, financial facts and FRED data.

        Writes, file access and multiple statements are rejected; queries stop after 5 seconds.
        """
        async with audited("run_readonly_sql", {"query": query, "max_rows": max_rows}) as out:
            out["result"] = await services().run_readonly_sql(query, max_rows)
        result: SQLResult = out["result"]
        return result

    @mcp.resource(
        "schema://tables",
        name="cache_schema",
        description="Tables and columns that run_readonly_sql can query.",
        mime_type="text/plain",
    )
    def cache_schema() -> str:
        return services().schema_text()

    return mcp


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    if not settings.edgar_user_agent:
        log.warning("EDGAR_USER_AGENT is not set; SEC tools will return config_missing.")
    log.info("starting fin-edgar-mcp-server %s, cache at %s", __version__, settings.cache_path)
    create_server(lambda: Services.create(settings)).run()


if __name__ == "__main__":
    main()
