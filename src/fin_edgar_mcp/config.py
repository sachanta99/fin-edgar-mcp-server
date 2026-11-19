"""Configuration, read from environment variables only."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigMissingError

DEFAULT_CACHE_PATH = Path.home() / ".cache" / "fin-edgar-mcp" / "cache.duckdb"


@dataclass(frozen=True)
class TTLs:
    """Cache lifetimes in seconds, per source."""

    tickers: int = 7 * 24 * 3600
    company_facts: int = 24 * 3600
    submissions: int = 3600
    full_text_search: int = 3600
    fred_series: int = 7 * 24 * 3600
    fred_observations: int = 12 * 3600


@dataclass(frozen=True)
class Settings:
    edgar_user_agent: str | None = None
    fred_api_key: str | None = None
    cache_path: Path = DEFAULT_CACHE_PATH
    log_level: str = "INFO"
    sec_requests_per_second: float = 8.0
    fred_requests_per_second: float = 2.0
    max_retries: int = 3
    http_timeout_seconds: float = 30.0
    sql_timeout_seconds: float = 5.0
    ttls: TTLs = field(default_factory=TTLs)

    @classmethod
    def from_env(cls) -> Settings:
        cache = os.environ.get("FIN_MCP_CACHE_PATH")
        return cls(
            edgar_user_agent=(os.environ.get("EDGAR_USER_AGENT") or "").strip() or None,
            fred_api_key=(os.environ.get("FRED_API_KEY") or "").strip() or None,
            cache_path=Path(cache).expanduser() if cache else DEFAULT_CACHE_PATH,
            log_level=os.environ.get("FIN_MCP_LOG_LEVEL", "INFO").upper(),
        )

    def require_user_agent(self) -> str:
        if not self.edgar_user_agent:
            raise ConfigMissingError(
                "EDGAR_USER_AGENT is not set. The SEC requires a User-Agent that names you and "
                "gives a contact, e.g. 'fin-edgar-mcp-server/0.1 Jane Doe jane@example.com'. "
                "Set it in the server's env block."
            )
        return self.edgar_user_agent

    def require_fred_key(self) -> str:
        if not self.fred_api_key:
            raise ConfigMissingError(
                "FRED_API_KEY is not set. Get a free key at "
                "https://fred.stlouisfed.org/docs/api/api_key.html and add it to the server's env "
                "block. The SEC tools work without it."
            )
        return self.fred_api_key
