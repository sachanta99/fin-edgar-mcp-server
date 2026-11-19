"""Turn an EDGAR company-facts payload into clean, sourced metric series.

Pure functions, no I/O, so every rule here is unit tested against fixtures.

Rules (from the requirements doc):
- Each standard metric maps to an ordered list of XBRL concepts. Per period, the
  highest-priority concept that reports it wins, so a company that switched tags
  (e.g. SalesRevenueNet -> RevenueFromContractWithCustomer...) still has a full history.
- Annual = 10-K facts covering a full fiscal year (or balance-sheet instants on an FY filing).
  Quarterly = 10-Q facts covering about 3 months (or instants on a Q1-Q3 filing).
  Year-to-date 10-Q facts are skipped; Q4 is never derived.
- When several filings report the same period, the latest filing wins. ``restated`` is true
  only when they reported different values (comparatives repeating a number are not restated).
- The fiscal year/period label comes from the earliest filing that reported the period,
  because that filing reported it as its current period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .models import Period


@dataclass(frozen=True)
class MetricSpec:
    name: str
    description: str
    concepts: tuple[str, ...]  # "taxonomy:Concept", highest priority first
    unit: str


STANDARD_METRICS: dict[str, MetricSpec] = {
    m.name: m
    for m in (
        MetricSpec(
            "revenue",
            "Total revenue",
            (
                "us-gaap:Revenues",
                "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
                "us-gaap:SalesRevenueNet",
                "us-gaap:SalesRevenueGoodsNet",
            ),
            "USD",
        ),
        MetricSpec(
            "net_income",
            "Net income attributable to the parent",
            ("us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss"),
            "USD",
        ),
        MetricSpec("operating_income", "Operating income", ("us-gaap:OperatingIncomeLoss",), "USD"),
        MetricSpec(
            "eps_diluted",
            "Diluted earnings per share",
            ("us-gaap:EarningsPerShareDiluted", "us-gaap:EarningsPerShareBasicAndDiluted"),
            "USD/shares",
        ),
        MetricSpec("total_assets", "Total assets", ("us-gaap:Assets",), "USD"),
        MetricSpec("total_liabilities", "Total liabilities", ("us-gaap:Liabilities",), "USD"),
        MetricSpec(
            "stockholders_equity",
            "Stockholders' equity",
            (
                "us-gaap:StockholdersEquity",
                "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            ),
            "USD",
        ),
        MetricSpec(
            "cash_and_equivalents",
            "Cash and cash equivalents",
            (
                "us-gaap:CashAndCashEquivalentsAtCarryingValue",
                "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                "us-gaap:Cash",
            ),
            "USD",
        ),
        MetricSpec(
            "operating_cash_flow",
            "Net cash from operating activities",
            (
                "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            ),
            "USD",
        ),
    )
}

ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"})
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})
ANNUAL_DAYS = (340, 390)
QUARTER_DAYS = (80, 100)


@dataclass(frozen=True)
class NormalizedFact:
    metric: str
    value: float
    unit: str
    fiscal_year: int | None
    fiscal_period: str | None
    period_start: date | None
    period_end: date
    form: str
    accession_number: str
    filed: date
    concept: str
    restated: bool


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _matches_period(raw: dict[str, Any], period: Period) -> bool:
    form = str(raw.get("form", "")).upper()
    start = _parse_date(raw.get("start"))
    end = _parse_date(raw.get("end"))
    if end is None:
        return False
    fp = str(raw.get("fp") or "").upper()
    if period == "annual":
        if form not in ANNUAL_FORMS:
            return False
        if start is None:
            return fp in ("FY", "")
        return ANNUAL_DAYS[0] <= (end - start).days <= ANNUAL_DAYS[1]
    if form not in QUARTERLY_FORMS:
        return False
    if start is None:
        return fp in ("Q1", "Q2", "Q3")
    return QUARTER_DAYS[0] <= (end - start).days <= QUARTER_DAYS[1]


def _concept_facts(company_facts: dict[str, Any], concept: str, unit: str) -> list[dict[str, Any]]:
    taxonomy, _, name = concept.partition(":")
    node = company_facts.get("facts", {}).get(taxonomy, {}).get(name)
    if not node:
        return []
    units: dict[str, list[dict[str, Any]]] = node.get("units", {})
    if unit in units:
        return units[unit]
    # Some filers report in another currency; use the only unit if there is exactly one.
    return next(iter(units.values())) if len(units) == 1 else []


def _unit_of(company_facts: dict[str, Any], concept: str, unit: str) -> str:
    taxonomy, _, name = concept.partition(":")
    units = company_facts.get("facts", {}).get(taxonomy, {}).get(name, {}).get("units", {})
    if unit in units or len(units) != 1:
        return unit
    return str(next(iter(units)))


def _series_for_concept(
    company_facts: dict[str, Any], metric: str, concept: str, unit: str, period: Period
) -> dict[tuple[date | None, date], NormalizedFact]:
    groups: dict[tuple[date | None, date], list[dict[str, Any]]] = {}
    for raw in _concept_facts(company_facts, concept, unit):
        if raw.get("val") is None or not raw.get("accn") or not _matches_period(raw, period):
            continue
        end = _parse_date(raw.get("end"))
        filed = _parse_date(raw.get("filed"))
        if end is None or filed is None:
            continue
        key = (_parse_date(raw.get("start")), end)
        groups.setdefault(key, []).append(raw)

    real_unit = _unit_of(company_facts, concept, unit)
    out: dict[tuple[date | None, date], NormalizedFact] = {}
    for (start, end), raws in groups.items():
        by_filed = sorted(raws, key=lambda r: (str(r["filed"]), str(r["accn"])))
        earliest, latest = by_filed[0], by_filed[-1]
        values = {float(r["val"]) for r in raws}
        fy = earliest.get("fy")
        out[(start, end)] = NormalizedFact(
            metric=metric,
            value=float(latest["val"]),
            unit=real_unit,
            fiscal_year=int(fy) if fy is not None else None,
            fiscal_period=(str(earliest.get("fp")) if earliest.get("fp") else None),
            period_start=start,
            period_end=end,
            form=str(latest.get("form")),
            accession_number=str(latest["accn"]),
            filed=_parse_date(latest["filed"]) or end,
            concept=concept,
            restated=len(values) > 1,
        )
    return out


def normalize_metric(
    company_facts: dict[str, Any], spec: MetricSpec, period: Period
) -> list[NormalizedFact]:
    """All periods for one metric, newest first, merged across concepts by priority."""
    merged: dict[date, NormalizedFact] = {}
    for concept in spec.concepts:
        series = _series_for_concept(company_facts, spec.name, concept, spec.unit, period)
        for (_start, end), fact in series.items():
            # Periods are keyed on end date; the first (highest-priority) concept keeps it.
            if end not in merged:
                merged[end] = fact
    return sorted(merged.values(), key=lambda f: f.period_end, reverse=True)


def normalize_all(
    company_facts: dict[str, Any],
) -> dict[tuple[str, Period], list[NormalizedFact]]:
    """Every standard metric for both period types. Used to fill the financial_facts table."""
    result: dict[tuple[str, Period], list[NormalizedFact]] = {}
    for spec in STANDARD_METRICS.values():
        for period in ("annual", "quarterly"):
            result[(spec.name, period)] = normalize_metric(company_facts, spec, period)
    return result
