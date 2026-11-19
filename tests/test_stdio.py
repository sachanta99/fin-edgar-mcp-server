"""Start the real console script over stdio, the way Claude Desktop does."""

import os
import shutil
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def test_server_starts_over_stdio_and_lists_tools(tmp_path: Path):
    script = shutil.which("fin-edgar-mcp")
    command, args = (script, []) if script else (sys.executable, ["-m", "fin_edgar_mcp.server"])
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "FIN_MCP_CACHE_PATH": str(tmp_path / "cache.duckdb"),
        "EDGAR_USER_AGENT": "fin-edgar-mcp-server/0.1 Test test@example.com",
    }
    transport = StdioTransport(command=command, args=args, env=env)
    async with Client(transport) as client:
        names = {t.name for t in await client.list_tools()}
        assert names == {
            "get_company_financials",
            "search_sec_filings",
            "get_macro_indicator",
            "run_readonly_sql",
        }
        result = await client.call_tool("run_readonly_sql", {"query": "SELECT 1 AS one"})
        assert result.structured_content is not None
        assert result.structured_content["rows"] == [[1]]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
def test_logs_never_go_to_stdout():
    from fin_edgar_mcp import server

    assert "stderr" in server.configure_logging.__code__.co_names
