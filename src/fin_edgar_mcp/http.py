"""Cached, rate-limited, retrying HTTP client shared by the EDGAR and FRED clients.

Flow for each request: fresh cache entry -> return it. Otherwise one in-flight upstream call
per cache key (concurrent callers share it), paced by the host's rate limiter, retried on
429/5xx/transport errors. If every attempt fails and an expired copy exists, that copy is
returned with ``stale=True``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from .cache import Cache
from .errors import FinMCPError, NotFoundError, RateLimitedError, UpstreamError
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]

# Query parameters that must never be stored in cache keys, logs or responses.
SECRET_PARAMS = frozenset({"api_key"})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 30.0


@dataclass
class FetchResult:
    data: Any
    url: str
    cache_hit: bool
    fetched_at: datetime
    stale: bool = False


@dataclass
class CallRecord:
    """Collects what one tool call touched upstream, for the audit log and provenance."""

    fetches: list[FetchResult] = field(default_factory=list)

    @property
    def urls(self) -> list[str]:
        return list(dict.fromkeys(f.url for f in self.fetches))

    @property
    def cache_hit(self) -> bool | None:
        if not self.fetches:
            return None
        return all(f.cache_hit for f in self.fetches)


current_call: ContextVar[CallRecord | None] = ContextVar("current_call", default=None)


def public_url(url: str, params: Mapping[str, Any] | None) -> str:
    """URL with query string, secrets removed. Used for cache keys, logs and provenance."""
    if not params:
        return url
    safe = sorted((k, str(v)) for k, v in params.items() if k not in SECRET_PARAMS)
    return f"{url}?{urlencode(safe)}" if safe else url


class Fetcher:
    def __init__(
        self,
        cache: Cache,
        *,
        limiters: Mapping[str, RateLimiter],
        headers_for_host: Callable[[str], dict[str, str]],
        max_retries: int = 3,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleeper = asyncio.sleep,
        backoff_base: float = 0.5,
    ) -> None:
        self.cache = cache
        self._limiters = dict(limiters)
        self._headers_for_host = headers_for_host
        self._max_retries = max_retries
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds, transport=transport, follow_redirects=True
        )
        self._inflight: dict[str, asyncio.Future[FetchResult]] = {}
        self.upstream_requests = 0  # total HTTP attempts, for tests and diagnostics

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(
        self,
        url: str,
        *,
        ttl_seconds: int,
        params: Mapping[str, Any] | None = None,
        not_found_message: str | None = None,
    ) -> FetchResult:
        key = public_url(url, params)
        cached = self.cache.get_response(key)
        now = self.cache.now()
        if cached and now - cached.fetched_at < timedelta(seconds=ttl_seconds):
            result = FetchResult(json.loads(cached.body), key, True, cached.fetched_at)
            self._record(result)
            return result

        existing = self._inflight.get(key)
        if existing is not None:
            result = await asyncio.shield(existing)
            self._record(result)
            return result

        loop = asyncio.get_running_loop()
        future: asyncio.Future[FetchResult] = loop.create_future()
        self._inflight[key] = future
        try:
            try:
                body = await self._fetch_with_retries(url, params, not_found_message)
                fetched_at = self.cache.put_response(key, key, body)
                result = FetchResult(json.loads(body), key, False, fetched_at)
            except (UpstreamError, RateLimitedError) as exc:
                if cached is None:
                    raise
                log.warning("serving stale cache for %s: %s", key, exc.message)
                result = FetchResult(
                    json.loads(cached.body), key, True, cached.fetched_at, stale=True
                )
            future.set_result(result)
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved so asyncio does not warn when nobody waits
            raise
        finally:
            self._inflight.pop(key, None)
        self._record(result)
        return result

    async def _fetch_with_retries(
        self, url: str, params: Mapping[str, Any] | None, not_found_message: str | None
    ) -> str:
        host = urlsplit(url).hostname or ""
        limiter = self._limiters.get(host)
        safe_url = public_url(url, params)
        last_error: FinMCPError | None = None
        for attempt in range(self._max_retries + 1):
            if limiter is not None:
                await limiter.acquire()
            self.upstream_requests += 1
            started = time.perf_counter()
            try:
                resp = await self._client.get(
                    url, params=dict(params or {}), headers=self._headers_for_host(host)
                )
            except httpx.TransportError as exc:
                last_error = UpstreamError(
                    f"Could not reach {host}: {type(exc).__name__}", details={"url": safe_url}
                )
                await self._backoff(attempt, None)
                continue
            log.debug(
                "GET %s -> %s in %.0f ms",
                safe_url,
                resp.status_code,
                (time.perf_counter() - started) * 1000,
            )
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 404:
                raise NotFoundError(
                    not_found_message or f"{host} has no data at this address.",
                    details={"url": safe_url},
                )
            if resp.status_code in RETRYABLE_STATUS:
                cls = RateLimitedError if resp.status_code == 429 else UpstreamError
                last_error = cls(
                    f"{host} answered HTTP {resp.status_code} after {attempt + 1} attempt(s).",
                    details={"url": safe_url},
                )
                if attempt < self._max_retries:
                    await self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            raise self._client_error(host, resp, safe_url, not_found_message)
        assert last_error is not None
        raise last_error

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self._max_retries:
            return
        delay: float | None = None
        if retry_after:
            try:
                delay = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                delay = None
        if delay is None:
            delay = self._backoff_base * (2**attempt) + random.uniform(0, self._backoff_base)
        await self._sleep(delay)

    @staticmethod
    def _client_error(
        host: str, resp: httpx.Response, safe_url: str, not_found_message: str | None
    ) -> FinMCPError:
        detail = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                detail = str(payload.get("error_message") or payload.get("message") or "")
        except ValueError:
            pass
        # FRED answers 400 "series does not exist" for unknown ids.
        if "does not exist" in detail.lower():
            return NotFoundError(not_found_message or detail, details={"url": safe_url})
        return UpstreamError(
            f"{host} rejected the request with HTTP {resp.status_code}"
            + (f": {detail}" if detail else "."),
            details={"url": safe_url},
        )

    @staticmethod
    def _record(result: FetchResult) -> None:
        record = current_call.get()
        if record is not None:
            record.fetches.append(result)
