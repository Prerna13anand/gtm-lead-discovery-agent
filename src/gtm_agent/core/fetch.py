"""Shared async HTTP fetch layer.

Phase 1 scope: a single shared `httpx.AsyncClient`, sane timeouts, an honest
static User-Agent, and bounded retry with backoff on transient failures.

Phase 2 addition: conditional requests (spec §6.3). The fetcher remembers the
`ETag` / `Last-Modified` validators returned for a URL and sends them back as
`If-None-Match` / `If-Modified-Since` on the next request to that URL. A `304`
response means "unchanged since last time" — callers see it via
`FetchResult.status_code == 304` and can skip re-processing.

Phase 5 additions — the four politeness/compliance mechanisms flagged as
TODOs since Phase 1, now implemented:
    - robots.txt fetching, per-host caching, and pre-request consultation
      (spec §21.1), via `core.robots.RobotsCache`. A disallowed request
      raises `RobotsDisallowedError` before any request is sent — no retry,
      no exceptions, no override flag reachable from production code.
    - a per-domain semaphore (spec §16.3) bounding concurrent in-flight
      requests to any one host, independent of `httpx`'s own connection-pool
      limits.
    - a minimum delay (with jitter) between requests to the same host (spec
      §6.3's "Rate limit" row) — distinct from the concurrency semaphore
      above: this bounds *request rate* even at concurrency 1. Spec §16.3:
      "ATS API hosts get a higher allowance than startup origins" — this
      codebase does not implement that per-host-class differentiation
      (it would need `core.fetch` to know about ATS host lists, which live
      in `discovery.ats_platforms` — the wrong dependency direction for a
      cross-cutting fetch layer); every host gets the same configurable
      interval today, tunable per `Fetcher` instance via
      `min_request_interval_seconds`.
    - honouring a numeric or HTTP-date `Retry-After` header (spec §6.3) in
      place of fixed exponential backoff, when the server sends one.

Content-addressed raw-payload caching (spec §6.4) remains a separate,
not-yet-built store — `core.run_ledger.archive_raw_payloads` already covers
the per-`(company_id, run_id)` archival spec §6.4 actually specifies; the
distinct "content-addressed... keyed by URL" cache spec §6.3's own table
mentions is additional infrastructure this codebase does not build.

No adapter or stage should construct its own `httpx.Client` — everything
routes through `Fetcher` so every caller gets all of the above for free.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from urllib.parse import urlparse

import httpx

from gtm_agent.config import get_settings
from gtm_agent.core.logging import get_logger
from gtm_agent.core.robots import RobotsCache

logger = get_logger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_NON_RETRYABLE_STATUS_CODES = {401, 403, 404}  # answers, not failures — spec §6.3

# Spec §16.3: "a per-domain semaphore of 1-2 — the per-domain limit is the
# one that matters for politeness." 2 is the upper end of that range, so a
# two-phase adapter's hydrate() pagination isn't overly serialised while
# still bounding concurrent load on any one origin.
DEFAULT_PER_DOMAIN_CONCURRENCY = 2

# Spec §6.3's "Rate limit" row: "Minimum delay between requests to the same
# host, with jitter." No specific figure is given by the spec; 0.5s is a
# conservative default sized for a small startup's own origin (spec §1.5:
# "Simple, low-traffic sites"), not for ATS-API hosts, which this codebase
# does not yet differentiate (see module docstring).
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 0.5
_JITTER_RANGE = (0.8, 1.2)  # +/-20%


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


class RobotsDisallowedError(FetchError):
    """Spec §21.1 / §17: "No exceptions, no override flag." A subclass of
    `FetchError` (not a sibling exception) deliberately: every existing
    caller in this codebase already has an `except FetchError` handler, so
    a robots-disallowed request is guaranteed to be treated as a failure
    everywhere, with zero call sites needing to change to stay correct.
    Callers that want to distinguish it precisely — to report
    `ExtractionStatus.ROBOTS_DISALLOWED` / `ScrapeRunStatus.ROBOTS_DISALLOWED`,
    spec §17's own dedicated status, rather than a generic failure — catch
    this subclass ahead of the general case; several adapters do.
    """


class Fetcher:
    """Thin async HTTP client wrapper with bounded retry, per-domain
    concurrency limiting, and robots.txt enforcement.

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
        respect_robots: bool = True,
        per_domain_concurrency: int = DEFAULT_PER_DOMAIN_CONCURRENCY,
        min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        settings = get_settings()
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._user_agent = settings.http_user_agent
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
        # `robots.txt` fetches are deliberately NOT counted here — they're an
        # internal compliance mechanism, amortised across many requests via
        # the 24h cache, not part of the substantive scrape being measured.
        self.request_count = 0
        self.bytes_fetched = 0

        # Conditional-request validators (spec §6.3), keyed by the URL passed
        # to get()/head(). In-memory and per-Fetcher-instance — deliberately
        # not the persistent, cross-sweep cache of §6.4; that's a separate,
        # not-yet-built store this can later sit behind.
        self._validators: dict[str, dict[str, str]] = {}

        # Spec §21.1. `respect_robots=False` exists purely so tests mocking
        # the transport for an unrelated purpose (Apollo/PDL/Tavily API
        # shape, ATS fingerprinting, source resolution, ...) don't also have
        # to stub a `/robots.txt` response. It is not reachable from any
        # production call site in this codebase — `main.py` and every stage
        # construct `Fetcher()` with defaults — so it is not the "override
        # flag" spec §21.1 rules out; that flag would be an operator-facing
        # config knob to bypass a real site's policy, which does not exist
        # anywhere in `config.Settings`.
        self._respect_robots = respect_robots
        self._robots_cache = RobotsCache() if respect_robots else None

        # Spec §16.3: bounds concurrent in-flight requests to any one host,
        # independent of httpx's own global connection-pool limits. Built
        # lazily per host so a Fetcher used for one company doesn't
        # pre-allocate semaphores for hosts it never talks to.
        self._per_domain_concurrency = per_domain_concurrency
        self._domain_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(per_domain_concurrency)
        )

        # Spec §6.3's "Rate limit" row — see module docstring. `0` disables
        # it entirely (used by tests exercising unrelated behaviour, same
        # convention as `respect_robots=False`).
        self._min_request_interval = min_request_interval_seconds
        self._last_request_at: dict[str, float] = {}
        self._rate_limit_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

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

    async def get(self, url: str, *, use_cache: bool = True, **kwargs: object) -> FetchResult:
        return await self._request("GET", url, use_cache=use_cache, **kwargs)

    async def head(self, url: str, *, use_cache: bool = True, **kwargs: object) -> FetchResult:
        return await self._request("HEAD", url, use_cache=use_cache, **kwargs)

    async def post(self, url: str, *, use_cache: bool = True, **kwargs: object) -> FetchResult:
        return await self._request("POST", url, use_cache=use_cache, **kwargs)

    async def is_allowed(self, url: str) -> bool:
        """Public robots.txt check (spec §21.1) for callers that need to
        navigate a URL through something other than this `Fetcher` itself —
        specifically `discovery.extraction.rendered_dom.RenderedDomAdapter`,
        whose Playwright browser context makes its own network requests
        outside `Fetcher._request`'s normal path. Always `True` when
        `respect_robots=False`.
        """
        if not self._respect_robots:
            return True
        return await self._check_robots(url)

    async def _request(self, method: str, url: str, *, use_cache: bool = True, **kwargs: object) -> FetchResult:
        if self._respect_robots and not await self._check_robots(url):
            logger.warning("robots_disallowed", url=url, method=method)
            raise RobotsDisallowedError(f"{method} {url} disallowed by robots.txt (spec §21.1)")

        # `use_cache=False` opts a single call out of conditional requests
        # entirely — neither sending stored validators nor storing new ones
        # from the response. This exists for exploratory reads of the same
        # URL a caller makes for a *different* purpose than the "real" fetch
        # spec §6.3's conditional caching is meant to protect (e.g. Stage 2
        # fingerprinting peeking at a page before Stage 3 extracts from it,
        # spec §16.1's own `process_company` pseudocode: distinct pipeline
        # steps, same URL). Without this, two same-process reads of one URL
        # for two different purposes make the *second* one see a 304 the
        # server only meant for a genuine later sweep — a real, live-verified
        # bug (see `discovery.ats_detection.identify_ats` and `main.py`'s
        # Stage 2/3 handoff) where a company's own fingerprinting read
        # silently starved its extraction read of real content.
        conditional_headers = self._conditional_headers(url) if use_cache else {}
        if conditional_headers:
            merged_headers = {**conditional_headers, **(kwargs.pop("headers", None) or {})}
            kwargs["headers"] = merged_headers

        host_key = self._host_key(url)
        domain_semaphore = self._domain_semaphores[host_key]
        async with domain_semaphore:
            await self._respect_rate_limit(host_key)

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

                if response.status_code < 400 and use_cache:
                    # Errors don't get to overwrite a validator that pointed at good content.
                    self._store_validators(url, response.headers)

                if response.status_code in _NON_RETRYABLE_STATUS_CODES:
                    return FetchResult(
                        url=str(response.url),
                        status_code=response.status_code,
                        text=response.text,
                        headers=response.headers,
                    )

                if response.status_code in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                    retry_after = self._retry_after_seconds(response.headers)
                    if retry_after is not None:
                        await asyncio.sleep(retry_after)
                    else:
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

    async def _respect_rate_limit(self, host_key: str) -> None:
        """Spec §6.3: "Minimum delay between requests to the same host, with
        jitter." Locked per host so two concurrent requests to the same
        host (up to `per_domain_concurrency` of them, per the semaphore
        already held around this call) don't both read a stale
        `_last_request_at` and both proceed without waiting on each other.
        """
        if self._min_request_interval <= 0:
            return
        async with self._rate_limit_locks[host_key]:
            last = self._last_request_at.get(host_key)
            if last is not None:
                jittered_interval = self._min_request_interval * random.uniform(*_JITTER_RANGE)
                remaining = jittered_interval - (time.monotonic() - last)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_request_at[host_key] = time.monotonic()

    async def _check_robots(self, url: str) -> bool:
        assert self._robots_cache is not None  # only called when self._respect_robots
        return await self._robots_cache.is_allowed(
            url, user_agent=self._user_agent, fetch_robots_txt=self._fetch_robots_txt
        )

    async def _fetch_robots_txt(self, robots_url: str) -> str | None:
        """Bypasses `_request` entirely: fetching `robots.txt` itself must
        never be subject to its own robots-check (that would be circular)
        or the per-domain semaphore meant to bound the *substantive* scrape
        traffic (spec §16.3) — one fetch per host per TTL window is
        negligible load, not what that limit exists to constrain.
        """
        try:
            response = await self._client.get(robots_url)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        return response.text

    @staticmethod
    def _host_key(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _retry_after_seconds(headers: httpx.Headers) -> float | None:
        """Spec §6.3: "Honour `Retry-After` when present." Supports both of
        the header's two legal forms — an integer delay in seconds, or an
        HTTP-date — falling back to fixed exponential backoff (returning
        `None`) if the header is absent or unparseable as either, never
        raising.
        """
        value = headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0.0, (target - datetime.now(UTC)).total_seconds())

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = self._backoff_base_seconds * (2**attempt)
        await asyncio.sleep(delay)

    def _conditional_headers(self, url: str) -> dict[str, str]:
        """Build If-None-Match / If-Modified-Since headers from stored validators (spec §6.3)."""
        validators = self._validators.get(url)
        if not validators:
            return {}
        headers: dict[str, str] = {}
        if etag := validators.get("etag"):
            headers["If-None-Match"] = etag
        if last_modified := validators.get("last_modified"):
            headers["If-Modified-Since"] = last_modified
        return headers

    def _store_validators(self, url: str, headers: httpx.Headers) -> None:
        """Remember ETag / Last-Modified for this URL so the next request can be conditional."""
        etag = headers.get("ETag")
        last_modified = headers.get("Last-Modified")
        if not etag and not last_modified:
            return
        validators: dict[str, str] = {}
        if etag:
            validators["etag"] = etag
        if last_modified:
            validators["last_modified"] = last_modified
        self._validators[url] = validators
