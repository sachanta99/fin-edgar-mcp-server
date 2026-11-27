FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FIN_MCP_CACHE_PATH=/data/cache.duckdb

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home --uid 1000 mcp && mkdir /data && chown mcp /data

USER mcp
VOLUME ["/data"]
# MCP over stdio: run with `docker run -i`.
ENTRYPOINT ["fin-edgar-mcp"]
