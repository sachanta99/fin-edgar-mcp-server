"""DuckDB-backed cache: raw HTTP responses, normalized tables and the audit log.

The database is opened with external access disabled and the configuration locked, so no
query (from this process or from ``run_readonly_sql``) can read files, attach databases,
install extensions or turn those protections back off.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from .errors import SQLRejectedError

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _naive_utc(dt: datetime) -> datetime:
    """DuckDB TIMESTAMP columns hold naive UTC (TIMESTAMPTZ would need pytz to read back)."""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    cache_key   VARCHAR PRIMARY KEY,
    url         VARCHAR NOT NULL,
    body        VARCHAR NOT NULL,
    fetched_at  TIMESTAMP NOT NULL  -- UTC
);
CREATE TABLE IF NOT EXISTS companies (
    cik         VARCHAR PRIMARY KEY,
    ticker      VARCHAR,
    name        VARCHAR NOT NULL,
    updated_at  TIMESTAMP NOT NULL  -- UTC
);
CREATE TABLE IF NOT EXISTS financial_facts (
    cik              VARCHAR NOT NULL,
    metric           VARCHAR NOT NULL,
    period           VARCHAR NOT NULL,
    value            DOUBLE NOT NULL,
    unit             VARCHAR NOT NULL,
    fiscal_year      INTEGER,
    fiscal_period    VARCHAR,
    period_start     DATE,
    period_end       DATE NOT NULL,
    form             VARCHAR NOT NULL,
    accession_number VARCHAR NOT NULL,
    filed            DATE NOT NULL,
    concept          VARCHAR NOT NULL,
    restated         BOOLEAN NOT NULL,
    PRIMARY KEY (cik, metric, period, period_end)
);
CREATE TABLE IF NOT EXISTS filings (
    accession_number VARCHAR PRIMARY KEY,
    cik              VARCHAR NOT NULL,
    form             VARCHAR NOT NULL,
    filing_date      DATE NOT NULL,
    report_date      DATE,
    primary_document VARCHAR,
    description      VARCHAR,
    document_url     VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS macro_series (
    series_id           VARCHAR PRIMARY KEY,
    title               VARCHAR NOT NULL,
    units               VARCHAR,
    frequency           VARCHAR,
    seasonal_adjustment VARCHAR,
    last_updated        VARCHAR
);
CREATE TABLE IF NOT EXISTS macro_observations (
    series_id VARCHAR NOT NULL,
    date      DATE NOT NULL,
    value     DOUBLE,
    PRIMARY KEY (series_id, date)
);
CREATE SEQUENCE IF NOT EXISTS tool_calls_seq;
CREATE TABLE IF NOT EXISTS tool_calls (
    id            BIGINT PRIMARY KEY DEFAULT nextval('tool_calls_seq'),
    called_at     TIMESTAMP NOT NULL,  -- UTC
    tool          VARCHAR NOT NULL,
    arguments     JSON,
    duration_ms   DOUBLE NOT NULL,
    cache_hit     BOOLEAN,
    upstream_urls VARCHAR[],
    row_count     INTEGER,
    error_code    VARCHAR
);
"""

# Tables that run_readonly_sql may read, with a one-line description for the schema resource.
QUERYABLE_TABLES: dict[str, str] = {
    "companies": "Companies this server has looked up: cik, ticker, name, updated_at.",
    "financial_facts": (
        "Normalized XBRL facts, one row per company, metric, period type and period end: "
        "cik, metric, period ('annual' or 'quarterly'), value, unit, fiscal_year, fiscal_period, "
        "period_start, period_end, form, accession_number, filed, concept, restated."
    ),
    "filings": (
        "Filings seen via search_sec_filings: accession_number, cik, form, filing_date, "
        "report_date, primary_document, description, document_url."
    ),
    "macro_series": (
        "FRED series metadata: series_id, title, units, frequency, seasonal_adjustment, "
        "last_updated."
    ),
    "macro_observations": "FRED observations: series_id, date, value (null where missing).",
    "tool_calls": (
        "Audit log of every tool call: id, called_at, tool, arguments, duration_ms, cache_hit, "
        "upstream_urls, row_count, error_code."
    ),
}


@dataclass(frozen=True)
class CachedResponse:
    body: str
    fetched_at: datetime
    url: str


@dataclass(frozen=True)
class QueryOutput:
    columns: list[str]
    column_types: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: float


class Cache:
    def __init__(self, path: Path | str, *, clock: Clock = utcnow) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = duckdb.connect(
            self.path,
            config={"enable_external_access": False, "lock_configuration": True},
        )
        with self._lock:
            self._conn.execute(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def now(self) -> datetime:
        return self._clock()

    # ---------- raw HTTP cache ----------

    def get_response(self, cache_key: str) -> CachedResponse | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body, fetched_at, url FROM http_cache WHERE cache_key = ?", [cache_key]
            ).fetchone()
        if row is None:
            return None
        fetched_at: datetime = row[1]
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        return CachedResponse(body=row[0], fetched_at=fetched_at.astimezone(UTC), url=row[2])

    def put_response(self, cache_key: str, url: str, body: str) -> datetime:
        now = self.now()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?, ?)",
                [cache_key, url, body, _naive_utc(now)],
            )
        return now

    # ---------- normalized tables ----------

    def upsert_company(self, cik: str, ticker: str | None, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO companies VALUES (?, ?, ?, ?)",
                [cik, ticker, name, _naive_utc(self.now())],
            )

    def replace_facts(self, cik: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM financial_facts WHERE cik = ?", [cik])
                if rows:
                    self._conn.executemany(
                        "INSERT INTO financial_facts VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def upsert_filings(self, rows: Iterable[Sequence[Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO filings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
            )

    def upsert_macro(self, meta: Sequence[Any], observations: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO macro_series VALUES (?, ?, ?, ?, ?, ?)", list(meta)
                )
                if observations:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO macro_observations VALUES (?, ?, ?)",
                        observations,
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ---------- audit log ----------

    def log_call(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        duration_ms: float,
        cache_hit: bool | None,
        upstream_urls: list[str],
        row_count: int | None,
        error_code: str | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tool_calls (called_at, tool, arguments, duration_ms, cache_hit, "
                "upstream_urls, row_count, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    _naive_utc(self.now()),
                    tool,
                    json.dumps(arguments, default=str),
                    duration_ms,
                    cache_hit,
                    upstream_urls,
                    row_count,
                    error_code,
                ],
            )

    def describe_tables(self) -> str:
        """Plain-text schema of the queryable tables, for the MCP schema resource."""
        names = list(QUERYABLE_TABLES)
        placeholders = ", ".join("?" for _ in names)
        with self._lock:
            cols = self._conn.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                f"WHERE table_name IN ({placeholders}) ORDER BY table_name, ordinal_position",
                names,
            ).fetchall()
        lines = [
            "Tables available to run_readonly_sql (DuckDB SQL dialect, SELECT only).",
            "They fill as the other tools fetch data; query them after calling those tools.",
            "",
        ]
        for name in names:
            lines.append(f"{name}: {QUERYABLE_TABLES[name]}")
            for table, column, dtype in cols:
                if table == name:
                    lines.append(f"  - {column} {dtype}")
            lines.append("")
        return "\n".join(lines)

    # ---------- read-only query execution ----------

    def run_readonly(self, sql: str, *, max_rows: int, timeout_seconds: float) -> QueryOutput:
        """Run an already-validated query inside a READ ONLY transaction.

        The transaction makes the engine itself refuse writes, independent of the parser check
        in ``sql_guard``. A timer interrupts the query after ``timeout_seconds``.
        """
        with self._lock:
            cur = self._conn.cursor()
        timer = threading.Timer(timeout_seconds, cur.interrupt)
        started = time.perf_counter()
        try:
            cur.execute("BEGIN TRANSACTION READ ONLY")
            timer.start()
            try:
                rel = cur.execute(sql)
                columns = [d[0] for d in rel.description or []]
                column_types = [str(d[1]) for d in rel.description or []]
                fetched = rel.fetchmany(max_rows + 1)
            except duckdb.InterruptException as exc:
                raise SQLRejectedError(
                    f"Query cancelled after {timeout_seconds:g} seconds. Add filters or a LIMIT."
                ) from exc
            except duckdb.Error as exc:
                raise SQLRejectedError(f"Query failed: {exc}") from exc
            finally:
                timer.cancel()
                with contextlib.suppress(duckdb.Error):
                    cur.execute("ROLLBACK")
        finally:
            cur.close()
        elapsed_ms = (time.perf_counter() - started) * 1000
        truncated = len(fetched) > max_rows
        rows = [[_jsonable(v) for v in row] for row in fetched[:max_rows]]
        return QueryOutput(columns, column_types, rows, truncated, round(elapsed_ms, 2))


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)
