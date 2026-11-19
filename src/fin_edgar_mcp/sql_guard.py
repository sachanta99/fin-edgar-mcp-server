"""Parser-level check for run_readonly_sql.

This is the second of three layers. The engine (READ ONLY transaction, external access off)
is the backstop; this layer gives the model a clear reason when a query is refused.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .errors import SQLRejectedError

# Functions that read files, URLs or engine internals. External access is already disabled,
# so these would fail anyway; rejecting them here gives a clearer message.
_BLOCKED_FUNCTION_PREFIXES = (
    "read_",
    "parquet_",
    "glob",
    "sniff_csv",
    "json_execute_serialized_sql",
    "query",
    "query_table",
)


def validate_readonly_sql(sql: str, allowed_tables: set[str]) -> str:
    """Return the query (stripped) if it is a single SELECT over allowed tables, else raise."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise SQLRejectedError("Query is empty.")
    try:
        statements = [s for s in sqlglot.parse(text, read="duckdb") if s is not None]
    except ParseError as exc:
        raise SQLRejectedError(f"Could not parse the query: {exc}") from exc
    if len(statements) != 1:
        raise SQLRejectedError("Send exactly one statement.")
    stmt = statements[0]

    if not isinstance(stmt, exp.Query):
        raise SQLRejectedError(
            f"Only SELECT or WITH ... SELECT queries are allowed, got {stmt.key.upper()}."
        )

    forbidden = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Create,
        exp.Drop,
        exp.Alter,
        exp.Command,
        exp.Copy,
        exp.Set,
        exp.Pragma,
        exp.Attach,
        exp.Detach,
        exp.Merge,
        exp.TruncateTable,
        exp.Into,
    )
    for node in stmt.walk():
        if isinstance(node, forbidden):
            raise SQLRejectedError(f"{node.key.upper()} is not allowed in a read-only query.")

    for func in stmt.find_all(exp.Func):
        name = (func.sql_name() if not isinstance(func, exp.Anonymous) else func.name).lower()
        if name.startswith(_BLOCKED_FUNCTION_PREFIXES):
            raise SQLRejectedError(f"Function {name}() is not allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    for table in stmt.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            raise SQLRejectedError("Table functions are not allowed; query the cache tables.")
        name = table.name.lower()
        if not name:
            raise SQLRejectedError("Could not identify a table in the FROM clause.")
        if table.args.get("db") or table.args.get("catalog"):
            raise SQLRejectedError(f"Qualified table names are not allowed: {table.sql()}.")
        if name not in allowed_tables and name not in cte_names:
            raise SQLRejectedError(
                f"Unknown table '{name}'. Queryable tables: {', '.join(sorted(allowed_tables))}."
            )
    return text
