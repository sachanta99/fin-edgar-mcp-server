"""Tool logic, independent of MCP wiring so it can be tested and reused directly."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .cache import QUERYABLE_TABLES, Cache
from .config import Settings
from .edgar import SEC_HOSTS, EdgarClient, document_url, filing_index_url, iter_submission_rows
from .errors import InvalidInputError
from .financials import STANDARD_METRICS, normalize_all, normalize_metric
from .fred import FRED_HOSTS, FREQUENCIES, FredClient, parse_value, resolve_series_id
from .http import CallRecord, Fetcher, current_call
from .models import (
    CompanyFinancials,
    Filing,
    FilingSearchResult,
    FinancialFact,
    MacroObservation,
    MacroSeries,
    MissingMetric,
    Period,
    Provenance,
    SQLResult,
)
from .ratelimit import RateLimiter
from .sql_guard import validate_readonly_sql

MAX_FACTS_PER_RESPONSE = 200
MAX_OBSERVATIONS = 5000
MAX_FILING_LIMIT = 100
MAX_SUBMISSION_PAGES = 5


def build_provenance(record: CallRecord | None) -> Provenance:
    fetches = record.fetches if record else []
    if not fetches:
        return Provenance(source_urls=[], cache_hit=True, fetched_at=datetime.now(UTC))
    return Provenance(
        source_urls=record.urls if record else [],
        cache_hit=all(f.cache_hit for f in fetches),
        fetched_at=min(f.fetched_at for f in fetches),
        stale=any(f.stale for f in fetches),
    )


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass
class Services:
    settings: Settings
    cache: Cache
    fetcher: Fetcher
    edgar: EdgarClient
    fred: FredClient

    @classmethod
    def create(cls, settings: Settings, **fetcher_kwargs: Any) -> Services:
        cache = Cache(settings.cache_path)
        sec = RateLimiter(settings.sec_requests_per_second)
        fred = RateLimiter(settings.fred_requests_per_second)
        limiters = {h: sec for h in SEC_HOSTS} | {h: fred for h in FRED_HOSTS}

        def headers_for_host(host: str) -> dict[str, str]:
            if host in SEC_HOSTS:
                return {
                    "User-Agent": settings.require_user_agent(),
                    "Accept-Encoding": "gzip, deflate",
                }
            return {"User-Agent": "fin-edgar-mcp-server/0.1"}

        fetcher = Fetcher(
            cache,
            limiters=limiters,
            headers_for_host=headers_for_host,
            max_retries=settings.max_retries,
            timeout_seconds=settings.http_timeout_seconds,
            **fetcher_kwargs,
        )
        return cls(
            settings, cache, fetcher, EdgarClient(fetcher, settings), FredClient(fetcher, settings)
        )

    async def aclose(self) -> None:
        await self.fetcher.aclose()
        self.cache.close()

    # ---------- get_company_financials ----------

    async def get_company_financials(
        self,
        company: str,
        metrics: list[str] | None = None,
        period: Period = "annual",
        years: int = 5,
    ) -> CompanyFinancials:
        if not 1 <= years <= 20:
            raise InvalidInputError("years must be between 1 and 20.")
        if period not in ("annual", "quarterly"):
            raise InvalidInputError("period must be 'annual' or 'quarterly'.")
        wanted = [m.strip().lower() for m in (metrics or list(STANDARD_METRICS))]
        unknown = [m for m in wanted if m not in STANDARD_METRICS]
        if unknown:
            raise InvalidInputError(
                f"Unknown metric(s): {', '.join(unknown)}.",
                details={"available": ", ".join(STANDARD_METRICS)},
            )
        wanted = list(dict.fromkeys(wanted))

        co = await self.edgar.resolve_company(company)
        facts_result = await self.edgar.company_facts(co.cik)
        payload = facts_result.data
        if not facts_result.cache_hit:
            self._store_facts(co.cik, payload)

        per_metric = years if period == "annual" else years * 4
        budget = max(1, MAX_FACTS_PER_RESPONSE // len(wanted))
        take = min(per_metric, budget)
        truncated = False

        facts: list[FinancialFact] = []
        missing: list[MissingMetric] = []
        for name in wanted:
            spec = STANDARD_METRICS[name]
            series = normalize_metric(payload, spec, period)[:per_metric]
            if len(series) > take:
                series, truncated = series[:take], True
            if not series:
                missing.append(MissingMetric(metric=name, concepts_tried=list(spec.concepts)))
                continue
            facts.extend(
                FinancialFact(
                    metric=f.metric,
                    value=f.value,
                    unit=f.unit,
                    fiscal_year=f.fiscal_year,
                    fiscal_period=f.fiscal_period,
                    period_start=f.period_start,
                    period_end=f.period_end,
                    form=f.form,
                    accession_number=f.accession_number,
                    filed=f.filed,
                    concept=f.concept,
                    restated=f.restated,
                    source_url=filing_index_url(co.cik, f.accession_number),
                )
                for f in series
            )
        name = str(payload.get("entityName") or co.name)
        return CompanyFinancials(
            cik=co.cik,
            company_name=name,
            period=period,
            facts=facts,
            missing_metrics=missing,
            truncated=truncated,
            note=(
                f"Showing the latest {take} periods per metric to keep the response small. "
                "Ask for fewer metrics or years, or query financial_facts with run_readonly_sql."
                if truncated
                else None
            ),
            provenance=build_provenance(current_call.get()),
        )

    def _store_facts(self, cik: str, payload: dict[str, Any]) -> None:
        rows = []
        for (_metric, period), series in normalize_all(payload).items():
            for f in series:
                rows.append(
                    (
                        cik,
                        f.metric,
                        period,
                        f.value,
                        f.unit,
                        f.fiscal_year,
                        f.fiscal_period,
                        f.period_start,
                        f.period_end,
                        f.form,
                        f.accession_number,
                        f.filed,
                        f.concept,
                        f.restated,
                    )
                )
        self.cache.replace_facts(cik, rows)

    # ---------- search_sec_filings ----------

    async def search_sec_filings(
        self,
        company: str | None = None,
        form_types: list[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> FilingSearchResult:
        if not 1 <= limit <= MAX_FILING_LIMIT:
            raise InvalidInputError(f"limit must be between 1 and {MAX_FILING_LIMIT}.")
        if date_from and date_to and date_from > date_to:
            raise InvalidInputError("date_from is after date_to.")
        query = (query or "").strip() or None
        company = (company or "").strip() or None
        if not company and not query:
            raise InvalidInputError("Pass a company, a full-text query, or both.")
        forms = [f.strip().upper() for f in form_types or [] if f.strip()] or None

        co = await self.edgar.resolve_company(company) if company else None
        if query:
            filings, total = await self._full_text(
                query, co.cik if co else None, forms, date_from, date_to
            )
            mode = "full_text"
        else:
            assert co is not None
            filings, total = await self._company_filings(co.cik, co.name, forms, date_from, date_to)
            mode = "company"

        filings.sort(key=lambda f: (f.filing_date, f.accession_number), reverse=True)
        return FilingSearchResult(
            filings=filings[:limit],
            total_matched=total,
            truncated=total > limit,
            mode=mode,  # type: ignore[arg-type]
            provenance=build_provenance(current_call.get()),
        )

    async def _company_filings(
        self,
        cik: str,
        name: str,
        forms: list[str] | None,
        date_from: date | None,
        date_to: date | None,
    ) -> tuple[list[Filing], int]:
        sub = (await self.edgar.submissions(cik)).data
        filings_block = sub.get("filings", {})
        rows = iter_submission_rows(filings_block.get("recent", {}))

        oldest = min((r["filingDate"] for r in rows if r.get("filingDate")), default=None)
        if date_from and oldest and date_from.isoformat() < oldest:
            pages = [
                f
                for f in filings_block.get("files", [])
                if not date_to or str(f.get("filingFrom", "")) <= date_to.isoformat()
                if str(f.get("filingTo", "9999")) >= date_from.isoformat()
            ][:MAX_SUBMISSION_PAGES]
            for page in pages:
                rows.extend(
                    iter_submission_rows((await self.edgar.submissions_page(page["name"])).data)
                )

        out: list[Filing] = []
        store = []
        for r in rows:
            filed = _parse_date(r.get("filingDate"))
            accn = r.get("accessionNumber")
            form = str(r.get("form") or "")
            if not filed or not accn:
                continue
            if forms and form.upper() not in forms:
                continue
            if (date_from and filed < date_from) or (date_to and filed > date_to):
                continue
            doc = document_url(cik, accn, r.get("primaryDocument"))
            report = _parse_date(r.get("reportDate"))
            desc = r.get("primaryDocDescription") or None
            out.append(
                Filing(
                    form=form,
                    filing_date=filed,
                    report_date=report,
                    accession_number=accn,
                    company_name=name,
                    cik=cik,
                    description=desc,
                    document_url=doc,
                    index_url=filing_index_url(cik, accn),
                )
            )
            store.append((accn, cik, form, filed, report, r.get("primaryDocument"), desc, doc))
        self.cache.upsert_filings(store)
        return out, len(out)

    async def _full_text(
        self,
        query: str,
        cik: str | None,
        forms: list[str] | None,
        date_from: date | None,
        date_to: date | None,
    ) -> tuple[list[Filing], int]:
        data = (
            await self.edgar.full_text_search(
                query, cik=cik, forms=forms, date_from=date_from, date_to=date_to
            )
        ).data
        hits_block = data.get("hits", {}) if isinstance(data, dict) else {}
        total_raw = hits_block.get("total", 0)
        total = int(total_raw.get("value", 0) if isinstance(total_raw, dict) else total_raw or 0)
        out: list[Filing] = []
        seen: set[str] = set()
        for hit in hits_block.get("hits", []):
            src = hit.get("_source", {})
            hit_id = str(hit.get("_id", ""))
            accn = src.get("adsh") or hit_id.split(":")[0]
            filed = _parse_date(src.get("file_date"))
            ciks = src.get("ciks") or []
            if not accn or not filed or not ciks or accn in seen:
                continue
            seen.add(accn)
            hit_cik = str(ciks[0]).zfill(10)
            filename = hit_id.split(":", 1)[1] if ":" in hit_id else None
            names = src.get("display_names") or []
            out.append(
                Filing(
                    form=str(src.get("form") or src.get("file_type") or ""),
                    filing_date=filed,
                    report_date=_parse_date(src.get("period_ending")),
                    accession_number=accn,
                    company_name=str(names[0]) if names else None,
                    cik=hit_cik,
                    description=src.get("file_description") or None,
                    document_url=document_url(hit_cik, accn, filename),
                    index_url=filing_index_url(hit_cik, accn),
                )
            )
        return out, max(total, len(out))

    # ---------- get_macro_indicator ----------

    async def get_macro_indicator(
        self,
        series: str,
        start: date | None = None,
        end: date | None = None,
        frequency: str | None = None,
    ) -> MacroSeries:
        if start and end and start > end:
            raise InvalidInputError("start is after end.")
        if frequency is not None and frequency not in FREQUENCIES:
            raise InvalidInputError(f"frequency must be one of: {', '.join(FREQUENCIES)}.")
        sid = resolve_series_id(series)
        meta, _ = await self.fred.series(sid)
        raw_obs, _ = await self.fred.observations(sid, start=start, end=end, frequency=frequency)

        observations = []
        for o in raw_obs:
            d = _parse_date(o.get("date"))
            if d is not None:
                observations.append(MacroObservation(date=d, value=parse_value(o.get("value"))))
        truncated = len(observations) > MAX_OBSERVATIONS
        if truncated:
            observations = observations[-MAX_OBSERVATIONS:]

        title = str(meta.get("title") or sid)
        units = str(meta.get("units") or "")
        freq = str(meta.get("frequency") or "")
        if frequency is None:
            self.cache.upsert_macro(
                (
                    sid,
                    title,
                    units,
                    freq,
                    meta.get("seasonal_adjustment"),
                    meta.get("last_updated"),
                ),
                [(sid, o.date, o.value) for o in observations],
            )
        return MacroSeries(
            series_id=sid,
            title=title,
            units=units,
            frequency=FREQUENCIES_LABEL.get(frequency or "", freq),
            seasonal_adjustment=meta.get("seasonal_adjustment"),
            last_updated=meta.get("last_updated"),
            observations=observations,
            truncated=truncated,
            provenance=build_provenance(current_call.get()),
        )

    # ---------- run_readonly_sql ----------

    async def run_readonly_sql(self, query: str, max_rows: int = 200) -> SQLResult:
        if not 1 <= max_rows <= 1000:
            raise InvalidInputError("max_rows must be between 1 and 1000.")
        sql = validate_readonly_sql(query, set(QUERYABLE_TABLES))
        out = await asyncio.to_thread(
            self.cache.run_readonly,
            sql,
            max_rows=max_rows,
            timeout_seconds=self.settings.sql_timeout_seconds,
        )
        return SQLResult(
            columns=out.columns,
            column_types=out.column_types,
            rows=out.rows,
            row_count=len(out.rows),
            truncated=out.truncated,
            elapsed_ms=out.elapsed_ms,
        )

    def schema_text(self) -> str:
        return self.cache.describe_tables()


FREQUENCIES_LABEL = {
    "daily": "Daily (aggregated)",
    "weekly": "Weekly (aggregated, average)",
    "biweekly": "Biweekly (aggregated, average)",
    "monthly": "Monthly (aggregated, average)",
    "quarterly": "Quarterly (aggregated, average)",
    "semiannual": "Semiannual (aggregated, average)",
    "annual": "Annual (aggregated, average)",
}
