"""SEC EDGAR client: ticker lookup, submissions, XBRL company facts, full-text search."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .config import Settings
from .errors import InvalidInputError, NotFoundError
from .http import Fetcher, FetchResult

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FULL_TEXT_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}"

SEC_HOSTS = ("www.sec.gov", "data.sec.gov", "efts.sec.gov")

_CIK_RE = re.compile(r"^(?:CIK)?0*(\d{1,10})$", re.IGNORECASE)
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


@dataclass(frozen=True)
class Company:
    cik: str  # 10-digit, zero padded
    ticker: str | None
    name: str

    @property
    def cik_int(self) -> int:
        return int(self.cik)


def pad_cik(value: int | str) -> str:
    return str(int(value)).zfill(10)


def filing_index_url(cik: str, accession_number: str) -> str:
    return (
        ARCHIVES_URL.format(cik_int=int(cik), accn_nodash=accession_number.replace("-", "")) + "/"
    )


def document_url(cik: str, accession_number: str, primary_document: str | None) -> str:
    base = filing_index_url(cik, accession_number)
    return base + primary_document if primary_document else base


class EdgarClient:
    def __init__(self, fetcher: Fetcher, settings: Settings) -> None:
        self.fetcher = fetcher
        self.settings = settings

    # ---------- company lookup ----------

    async def _ticker_table(self) -> list[dict[str, Any]]:
        self.settings.require_user_agent()
        result = await self.fetcher.get_json(TICKERS_URL, ttl_seconds=self.settings.ttls.tickers)
        data = result.data
        rows = data.values() if isinstance(data, dict) else data
        return [r for r in rows if isinstance(r, dict) and "cik_str" in r]

    async def resolve_company(self, company: str) -> Company:
        """Resolve a ticker or CIK to a Company. Raises NotFoundError with suggestions."""
        text = company.strip()
        if not text:
            raise InvalidInputError("company is empty; pass a ticker like 'AAPL' or a CIK.")
        rows = await self._ticker_table()

        cik_match = _CIK_RE.match(text)
        if cik_match:
            cik = pad_cik(cik_match.group(1))
            for r in rows:
                if pad_cik(r["cik_str"]) == cik:
                    return self._remember(
                        Company(cik, str(r.get("ticker") or "") or None, r["title"])
                    )
            # Valid CIKs exist outside the ticker file (funds, delisted companies).
            name = await self._name_from_submissions(cik)
            return self._remember(Company(cik, None, name))

        ticker = text.upper().replace(".", "-")
        if not _TICKER_RE.match(ticker):
            raise InvalidInputError(f"'{company}' is not a ticker or CIK.")
        for r in rows:
            if str(r.get("ticker", "")).upper() == ticker:
                return self._remember(Company(pad_cik(r["cik_str"]), ticker, r["title"]))

        raise NotFoundError(
            f"No SEC registrant has the ticker '{ticker}'.",
            details={"close_matches": self._suggest(text, rows) or "none"},
        )

    def _remember(self, company: Company) -> Company:
        self.fetcher.cache.upsert_company(company.cik, company.ticker, company.name)
        return company

    async def _name_from_submissions(self, cik: str) -> str:
        sub = await self.submissions(cik)
        return str(sub.data.get("name") or f"CIK {cik}")

    @staticmethod
    def _suggest(text: str, rows: list[dict[str, Any]], limit: int = 5) -> str:
        tickers = {str(r.get("ticker", "")).upper(): r for r in rows}
        titles = {str(r.get("title", "")).lower(): r for r in rows}
        picks: list[str] = []
        for t in difflib.get_close_matches(text.upper(), list(tickers), n=limit, cutoff=0.6):
            picks.append(f"{t} ({tickers[t]['title']})")
        low = text.lower()
        for title, r in titles.items():
            if len(picks) >= limit:
                break
            if low in title:
                label = f"{str(r.get('ticker', '')).upper()} ({r['title']})"
                if label not in picks:
                    picks.append(label)
        return ", ".join(picks[:limit])

    # ---------- raw endpoints ----------

    async def submissions(self, cik: str) -> FetchResult:
        self.settings.require_user_agent()
        return await self.fetcher.get_json(
            SUBMISSIONS_URL.format(cik=pad_cik(cik)),
            ttl_seconds=self.settings.ttls.submissions,
            not_found_message=f"EDGAR has no filings for CIK {pad_cik(cik)}.",
        )

    async def submissions_page(self, name: str) -> FetchResult:
        self.settings.require_user_agent()
        return await self.fetcher.get_json(
            SUBMISSIONS_PAGE_URL.format(name=name), ttl_seconds=self.settings.ttls.submissions
        )

    async def company_facts(self, cik: str) -> FetchResult:
        self.settings.require_user_agent()
        return await self.fetcher.get_json(
            COMPANY_FACTS_URL.format(cik=pad_cik(cik)),
            ttl_seconds=self.settings.ttls.company_facts,
            not_found_message=(
                f"EDGAR has no XBRL financial data for CIK {pad_cik(cik)}. "
                "Funds, very small or foreign filers often do not file XBRL."
            ),
        )

    async def full_text_search(
        self,
        query: str,
        *,
        cik: str | None,
        forms: list[str] | None,
        date_from: date | None,
        date_to: date | None,
    ) -> FetchResult:
        self.settings.require_user_agent()
        params: dict[str, Any] = {"q": query}
        if cik:
            params["ciks"] = pad_cik(cik)
        if forms:
            params["forms"] = ",".join(forms)
        if date_from or date_to:
            params["dateRange"] = "custom"
            params["startdt"] = (date_from or date(2001, 1, 1)).isoformat()
            params["enddt"] = (date_to or date.today()).isoformat()
        return await self.fetcher.get_json(
            FULL_TEXT_SEARCH_URL, params=params, ttl_seconds=self.settings.ttls.full_text_search
        )


def iter_submission_rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    """EDGAR stores filings column-wise; turn one block into a list of row dicts."""
    accns = block.get("accessionNumber") or []
    cols = (
        "accessionNumber",
        "filingDate",
        "reportDate",
        "form",
        "primaryDocument",
        "primaryDocDescription",
    )
    rows = []
    for i in range(len(accns)):
        row = {}
        for c in cols:
            values = block.get(c) or []
            row[c] = values[i] if i < len(values) else None
        rows.append(row)
    return rows
