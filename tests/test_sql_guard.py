from pathlib import Path

import pytest

from fin_edgar_mcp.cache import QUERYABLE_TABLES, Cache
from fin_edgar_mcp.errors import SQLRejectedError
from fin_edgar_mcp.sql_guard import validate_readonly_sql

ALLOWED = set(QUERYABLE_TABLES)

REJECTED = [
    "INSERT INTO companies VALUES ('1', 'X', 'X', now())",
    "UPDATE companies SET name = 'x'",
    "DELETE FROM companies",
    "CREATE TABLE t AS SELECT 1",
    "DROP TABLE companies",
    "ALTER TABLE companies ADD COLUMN x INT",
    "ATTACH '/tmp/other.db' AS other",
    "DETACH other",
    "COPY companies TO '/tmp/out.csv'",
    "PRAGMA database_list",
    "SET enable_external_access = true",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SELECT 1; SELECT 2",
    "SELECT * FROM companies; DROP TABLE companies",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "SELECT * FROM read_parquet('s3://bucket/x.parquet')",
    "SELECT * FROM glob('/*')",
    "SELECT * FROM '/etc/passwd'",
    "SELECT * FROM http_cache",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM main.companies",
    "SELECT * INTO new_table FROM companies",
    "SELECT * FROM query('select 1')",
    "",
    "   ;  ",
    "SELEC * FROM companies",
    "DESCRIBE companies",
    "SUMMARIZE companies",
    "SHOW TABLES",
]

ACCEPTED = [
    "SELECT * FROM companies",
    "select cik, name from companies where ticker = 'AAPL' limit 5;",
    "WITH r AS (SELECT * FROM financial_facts WHERE metric = 'revenue') SELECT * FROM r",
    "SELECT f.value, m.value FROM financial_facts f JOIN macro_observations m "
    "ON f.period_end = m.date",
    "SELECT tool, count(*) FROM tool_calls GROUP BY tool",
    "SELECT series_id, avg(value) FROM macro_observations GROUP BY 1 UNION ALL SELECT 'x', 1",
]


@pytest.mark.parametrize("sql", REJECTED)
def test_rejected(sql: str):
    with pytest.raises(SQLRejectedError):
        validate_readonly_sql(sql, ALLOWED)


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepted(sql: str):
    assert validate_readonly_sql(sql, ALLOWED)


# ---- engine layer: holds even if the parser check were bypassed ----


@pytest.fixture
def cache(tmp_path: Path):
    c = Cache(tmp_path / "c.duckdb")
    yield c
    c.close()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO companies VALUES ('1', 'X', 'X', now())",
        "DELETE FROM tool_calls",
        "CREATE TABLE t (x INT)",
        "SELECT * FROM read_csv('/etc/passwd')",
        "ATTACH '/tmp/x.db'",
        "COPY companies TO '/tmp/out.csv'",
        "SET enable_external_access = true",
    ],
)
def test_engine_blocks_writes_and_file_access(cache: Cache, sql: str):
    with pytest.raises(SQLRejectedError):
        cache.run_readonly(sql, max_rows=10, timeout_seconds=5)


def test_engine_timeout_cancels_long_query(cache: Cache):
    with pytest.raises(SQLRejectedError, match="cancelled"):
        cache.run_readonly(
            "SELECT count(*) FROM range(100000000000) a", max_rows=10, timeout_seconds=0.2
        )


def test_row_cap_sets_truncated(cache: Cache):
    out = cache.run_readonly("SELECT * FROM range(50)", max_rows=10, timeout_seconds=5)
    assert len(out.rows) == 10
    assert out.truncated is True


def test_cache_still_writable_after_readonly_query(cache: Cache):
    cache.run_readonly("SELECT 1", max_rows=1, timeout_seconds=5)
    cache.upsert_company("0000000001", "X", "X Corp")
    out = cache.run_readonly("SELECT name FROM companies", max_rows=5, timeout_seconds=5)
    assert out.rows == [["X Corp"]]
