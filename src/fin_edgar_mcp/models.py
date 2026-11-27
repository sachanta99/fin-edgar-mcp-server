"""Pydantic models for every tool output. Clients see these as JSON schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Period = Literal["annual", "quarterly"]


class Provenance(BaseModel):
    """Where a response came from and how fresh it is."""

    source_urls: list[str] = Field(description="Upstream URLs the data was read from.")
    cache_hit: bool = Field(description="True if every upstream response came from the cache.")
    fetched_at: datetime = Field(description="When the oldest upstream response was fetched (UTC).")
    stale: bool = Field(
        default=False,
        description="True if the upstream call failed and an expired cached copy was served.",
    )


class FinancialFact(BaseModel):
    metric: str = Field(description="Standard metric name, e.g. 'revenue'.", examples=["revenue"])
    value: float = Field(description="Value exactly as reported in the XBRL filing.")
    unit: str = Field(description="XBRL unit, e.g. 'USD' or 'USD/shares'.", examples=["USD"])
    fiscal_year: int | None = Field(description="Fiscal year the period belongs to.")
    fiscal_period: str | None = Field(description="FY, Q1, Q2 or Q3.", examples=["FY"])
    period_start: date | None = Field(description="Start of the period (null for balance sheet).")
    period_end: date = Field(description="End of the period, or the balance sheet date.")
    form: str = Field(description="Form of the filing the value was taken from.", examples=["10-K"])
    accession_number: str = Field(
        description="EDGAR accession number of that filing.", examples=["0000320193-24-000123"]
    )
    filed: date = Field(description="Date that filing was filed.")
    concept: str = Field(
        description="XBRL concept the value was tagged with.",
        examples=["us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"],
    )
    restated: bool = Field(
        description="True if filings reported different values for this period; the latest is used."
    )
    source_url: str = Field(description="EDGAR filing index for the accession number.")


class MissingMetric(BaseModel):
    metric: str
    concepts_tried: list[str]


class CompanyFinancials(BaseModel):
    cik: str = Field(description="10-digit SEC Central Index Key.", examples=["0000320193"])
    company_name: str
    period: Period
    facts: list[FinancialFact] = Field(description="Newest period first, grouped by metric.")
    missing_metrics: list[MissingMetric] = Field(
        description="Requested metrics with no reported data, and the XBRL concepts tried."
    )
    truncated: bool = Field(default=False, description="True if periods were cut to fit.")
    note: str | None = Field(default=None, description="How to get the rest when truncated.")
    provenance: Provenance


class Filing(BaseModel):
    form: str = Field(examples=["10-K"])
    filing_date: date
    report_date: date | None = Field(description="Period the filing reports on, if any.")
    accession_number: str
    company_name: str | None = None
    cik: str | None = None
    description: str | None = None
    document_url: str = Field(description="Primary document URL on sec.gov.")
    index_url: str = Field(description="Filing index URL on sec.gov.")


class FilingSearchResult(BaseModel):
    filings: list[Filing] = Field(description="Newest first.")
    total_matched: int = Field(description="Matches before the limit was applied.")
    truncated: bool
    mode: Literal["company", "full_text"]
    provenance: Provenance


class MacroObservation(BaseModel):
    date: date
    value: float | None = Field(description="Null where FRED reports no value ('.').")


class MacroSeries(BaseModel):
    series_id: str = Field(examples=["CPIAUCSL"])
    title: str
    units: str
    frequency: str
    seasonal_adjustment: str | None = None
    last_updated: str | None = None
    observations: list[MacroObservation] = Field(description="Oldest first.")
    truncated: bool
    provenance: Provenance


class SQLResult(BaseModel):
    columns: list[str]
    column_types: list[str]
    rows: list[list[object]]
    row_count: int
    truncated: bool = Field(description="True if more rows matched than max_rows.")
    elapsed_ms: float
