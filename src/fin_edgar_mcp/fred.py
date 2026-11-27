"""FRED client: series metadata and observations."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .config import Settings
from .errors import InvalidInputError, NotFoundError
from .http import Fetcher, FetchResult

FRED_BASE = "https://api.stlouisfed.org/fred"
SERIES_URL = f"{FRED_BASE}/series"
OBSERVATIONS_URL = f"{FRED_BASE}/series/observations"
FRED_HOSTS = ("api.stlouisfed.org",)

# Plain-English names for common series. Any other valid FRED id passes straight through.
SERIES_ALIASES: dict[str, str] = {
    "cpi": "CPIAUCSL",
    "core_cpi": "CPILFESL",
    "unemployment": "UNRATE",
    "fed_funds": "FEDFUNDS",
    "10y_treasury": "DGS10",
    "2y_treasury": "DGS2",
    "gdp": "GDP",
    "real_gdp": "GDPC1",
    "pce_inflation": "PCEPI",
    "payrolls": "PAYEMS",
}

FREQUENCIES = {
    "daily": "d",
    "weekly": "w",
    "biweekly": "bw",
    "monthly": "m",
    "quarterly": "q",
    "semiannual": "sa",
    "annual": "a",
}

_SERIES_ID_RE = re.compile(r"^[A-Z0-9_]{1,40}$")


def resolve_series_id(series: str) -> str:
    text = series.strip()
    if not text:
        raise InvalidInputError("series is empty; pass a FRED id like 'UNRATE' or an alias.")
    alias = SERIES_ALIASES.get(text.lower())
    if alias:
        return alias
    sid = text.upper()
    if not _SERIES_ID_RE.match(sid):
        raise InvalidInputError(
            f"'{series}' is not a valid FRED series id. Aliases: {', '.join(SERIES_ALIASES)}."
        )
    return sid


class FredClient:
    def __init__(self, fetcher: Fetcher, settings: Settings) -> None:
        self.fetcher = fetcher
        self.settings = settings

    def _base_params(self, series_id: str) -> dict[str, Any]:
        return {
            "series_id": series_id,
            "api_key": self.settings.require_fred_key(),
            "file_type": "json",
        }

    async def series(self, series_id: str) -> tuple[dict[str, Any], FetchResult]:
        result = await self.fetcher.get_json(
            SERIES_URL,
            params=self._base_params(series_id),
            ttl_seconds=self.settings.ttls.fred_series,
            not_found_message=f"FRED has no series '{series_id}'.",
        )
        items = result.data.get("seriess") or []
        if not items:
            raise NotFoundError(f"FRED has no series '{series_id}'.")
        return items[0], result

    async def observations(
        self,
        series_id: str,
        *,
        start: date | None,
        end: date | None,
        frequency: str | None,
    ) -> tuple[list[dict[str, Any]], FetchResult]:
        params = self._base_params(series_id)
        if start:
            params["observation_start"] = start.isoformat()
        if end:
            params["observation_end"] = end.isoformat()
        if frequency:
            params["frequency"] = FREQUENCIES[frequency]
            params["aggregation_method"] = "avg"
        result = await self.fetcher.get_json(
            OBSERVATIONS_URL,
            params=params,
            ttl_seconds=self.settings.ttls.fred_observations,
            not_found_message=f"FRED has no series '{series_id}'.",
        )
        return list(result.data.get("observations") or []), result


def parse_value(raw: Any) -> float | None:
    if raw is None or raw == "." or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
