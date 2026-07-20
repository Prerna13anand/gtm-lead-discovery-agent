"""Shared async HTTP fetch layer.

Phase 1 scope: a single shared `httpx.AsyncClient`, sane timeouts, an honest
static User-Agent, and bounded retry with backoff on transient failures. This
is intentionally the simple version.

Deliberately NOT implemented yet (spec §6.3) — left as TODOs for later phases:
    - robots.txt fetching/caching and pre-request consultation (spec §21.1)
    - conditional requests (ETag / If-None-Match, Last-Modified / 304 handling)
    - per-domain semaphore / politeness rate limiting (spec §16.3)
    - honouring `Retry-After` headers
    - content-addressed raw-payload caching (spec §6.4)

No adapter or stage should construct its own `httpx.Client` — everything
routes through `Fetcher` so that when the above land, every caller gets them
for free.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import TracebackType

import httpx

from gtm_agent.config import get_settings
from gtm_agent.core.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_NON_RETRYABLE_STATUS_CODES = {401, 403, 404}  # answers, not failures — spec §6.3


@dataclass(frozen=True)
class FetchResult:
    url: str
    status_code: int
    text: str
    headers: httpx.Headers


class FetchError(Exception):
    """Raised when a request exhausts its retry budget or hits a non-retryable failure."""

    def __init__(self, message: str, *, original: Exception | None = None) -> None:
        super().__init__(message)
        self.original = original


class Fetcher:
    """Thin async HTTP client wrapper with bounded retry.

    Usage:
        async with Fetcher() as fetcher:
            result = await fetcher.get("https://example.com")
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={"User-Agent": settings.http_user_agent},
            follow_redirects=True,
            transport=transport,
        )

        # Per-run request/byte counters — spec §15.1's `scrape_run.http_requests_made`
        # and `.bytes_fetched`. A caller building a scrape_run ledger entry
        # snapshots these before and after a company's attempt and records the
        # delta; see core/run_ledger.py. Counts every attempt made over the
        # wire, including retries, since each is a real request sent.
        self.request_count = 0
        self.bytes_fetched = 0

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, url: str, **kwargs: object) -> FetchResult:
        return await self._request("GET", url, **kwargs)

    async def head(self, url: str, **kwargs: object) -> FetchResult:
        return await self._request("HEAD", url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs: object) -> FetchResult:
        # TODO(phase 2): consult robots.txt cache before issuing the request (spec §21.1)
        # TODO(phase 2): attach If-None-Match / If-Modified-Since from cache (spec §6.3)
        # TODO(phase 2): acquire a per-domain semaphore before issuing the request (spec §16.3)

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                self.request_count += 1  # a request was sent, even though it failed at the transport level
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                await self._sleep_backoff(attempt)
                continue

            self.request_count += 1
            self.bytes_fetched += len(response.content)

            if response.status_code in _NON_RETRYABLE_STATUS_CODES:
                return FetchResult(
                    url=str(response.url),
                    status_code=response.status_code,
                    text=response.text,
                    headers=response.headers,
                )

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                # TODO(phase 2): honour Retry-After header instead of fixed backoff (spec §6.3)
                await self._sleep_backoff(attempt)
                continue

            return FetchResult(
                url=str(response.url),
                status_code=response.status_code,
                text=response.text,
                headers=response.headers,
            )

        logger.warning("fetch_exhausted_retries", url=url, method=method, error=str(last_exc))
        raise FetchError(
            f"{method} {url} failed after {self._max_retries} retries: {last_exc}",
            original=last_exc,
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = self._backoff_base_seconds * (2**attempt)
        await asyncio.sleep(delay)
