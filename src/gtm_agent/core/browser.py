"""Shared Playwright rendering layer — spec §6.2.3.

Mirrors `core.fetch.Fetcher`'s shape: a single shared, lazily-started
resource, used via an async context manager, that every caller routes
through rather than launching its own browser.

Rendering discipline, per spec §6.2.3:
    - Block images, fonts, media, and analytics at the request level — a
      large speed and memory win.
    - Wait on a content-presence condition (job-like elements appear), not a
      fixed sleep, with a hard timeout ceiling.
    - Capture the XHR/fetch requests the page makes, so a caller can look
      for a clean JSON endpoint to call directly on future runs (the
      "render once, learn the endpoint" pattern — implemented one layer up,
      in `discovery.extraction.rendered_dom`, since deciding whether a
      captured response *is* job data is that module's concern, not this
      one's).
    - Reuse a browser instance across companies with a fresh context each;
      never a fresh browser per company — `BrowserRenderer` launches
      Chromium once (on first use) and hands out a fresh `BrowserContext`
      per `render()` call, closed after use.

The analytics blocklist below is a pragmatic, non-exhaustive set of hostname
fragments — not a full ad-blocker. It was built from real third-party
traffic observed live during this adapter's development (see
`discovery.extraction.rendered_dom`'s module docstring), not guessed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from playwright.async_api import Response, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from gtm_agent.core.logging import get_logger

logger = get_logger(__name__)

_BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})

# Verified live (spec §5.3 build-note spirit: document what was actually
# observed, not assumed) against a real rendered careers page — see
# discovery/extraction/rendered_dom.py's module docstring for the specific
# site. Substring match against the request host.
_ANALYTICS_HOST_FRAGMENTS: tuple[str, ...] = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "googleadservices.com",
    "googlesyndication.com",
    "analytics.google.com",
    "google.com/ccm",
    "google.com/rmkt",
    "google.com/pagead",
    "facebook.net",
    "facebook.com/tr",
    "connect.facebook.net",
    "segment.io",
    "segment.com",
    "sentry.io",
    "hotjar.com",
    "clarity.ms",
    "adroll.com",
    "px.ads.linkedin.com",
    "linkedin.com/px",
    "bat.bing.com",
    "track.hubspot.com",
    "alb.reddit.com",
    "pixel-config.reddit.com",
    "mixpanel.com",
    "amplitude.com",
    "fullstory.com",
    "intercom.io",
)

_NAVIGATION_TIMEOUT_MS = 15_000  # matches settings.http_timeout_seconds default, for consistency
_CONTENT_WAIT_TIMEOUT_MS = 8_000  # a softer ceiling — see module docstring on how a timeout here is handled


class RenderTimeoutError(Exception):
    """Raised when navigation itself fails or exceeds the hard timeout ceiling."""


@dataclass(frozen=True)
class CapturedResponse:
    url: str
    status: int
    content_type: str
    body: str


@dataclass(frozen=True)
class RenderResult:
    html: str
    final_url: str
    xhr_responses: list[CapturedResponse] = field(default_factory=list)
    content_wait_satisfied: bool = True
    """False when the content-presence wait timed out — navigation still
    succeeded and `html` is real, just possibly rendered before the page
    finished hydrating. Not a hard failure (see module docstring)."""


class BrowserRenderer:
    """Thin async wrapper around a shared Playwright Chromium instance.

    Usage:
        async with BrowserRenderer() as renderer:
            result = await renderer.render(url, wait_js=my_predicate)
    """

    def __init__(
        self,
        *,
        navigation_timeout_ms: int = _NAVIGATION_TIMEOUT_MS,
        content_wait_timeout_ms: int = _CONTENT_WAIT_TIMEOUT_MS,
    ) -> None:
        self._navigation_timeout_ms = navigation_timeout_ms
        self._content_wait_timeout_ms = content_wait_timeout_ms
        self._playwright: Any | None = None
        self._browser: Any | None = None

    async def __aenter__(self) -> BrowserRenderer:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _ensure_browser(self) -> Any:
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        return self._browser

    async def render(self, url: str, *, wait_js: str | None = None) -> RenderResult:
        """Render one page in a fresh browser context (spec §6.2.3: fresh
        context per company, shared browser instance).
        """
        browser = await self._ensure_browser()
        context = await browser.new_context()
        try:
            await context.route("**/*", _block_unwanted_requests)

            page = await context.new_page()
            captured: list[CapturedResponse] = []
            capture_tasks: list[asyncio.Task[None]] = []

            def on_response(response: Response) -> None:
                capture_tasks.append(asyncio.ensure_future(_capture_json_response(response, captured)))

            page.on("response", on_response)

            try:
                # `domcontentloaded`, not `networkidle` — verified live that
                # `networkidle` is unreliable on real pages with recurring
                # background chatter (analytics beacons, polling), taking
                # 20s+ even with that traffic blocked (see module
                # docstring's analytics list). The explicit content-presence
                # wait below is the spec's actual "is the data here yet?"
                # signal — that's what §6.2.3 asks for, not page-quiescence.
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=self._navigation_timeout_ms
                )
            except PlaywrightTimeoutError as exc:
                raise RenderTimeoutError(f"navigation to {url} timed out: {exc}") from exc
            except PlaywrightError as exc:
                raise RenderTimeoutError(f"navigation to {url} failed: {exc}") from exc

            content_wait_satisfied = True
            if wait_js:
                try:
                    await page.wait_for_function(wait_js, timeout=self._content_wait_timeout_ms)
                except PlaywrightTimeoutError:
                    # A soft failure (spec §6.2.3 discipline note, module
                    # docstring): the page loaded, it just never showed
                    # content matching our predicate within the ceiling.
                    # Proceed with whatever rendered — not a hard RENDER_TIMEOUT.
                    content_wait_satisfied = False
                    logger.info("content_wait_timed_out", url=url)

            html = await page.content()
            final_url = page.url

            # Response bodies are read by tasks scheduled from the
            # 'response' event as it fires (Playwright buffers each body, so
            # reading it slightly after the event is safe) — wait for
            # whichever of those are still in flight before returning.
            if capture_tasks:
                await asyncio.gather(*capture_tasks, return_exceptions=True)

            return RenderResult(
                html=html,
                final_url=final_url,
                xhr_responses=captured,
                content_wait_satisfied=content_wait_satisfied,
            )
        finally:
            await context.close()


async def _block_unwanted_requests(route: Route) -> None:
    # Must be a real `async def` awaiting `abort()`/`continue_()` — a sync
    # handler that merely *returns* the (un-awaited) coroutine silently does
    # nothing; verified live that requests sail straight through in that case.
    request = route.request
    if request.resource_type in _BLOCKED_RESOURCE_TYPES:
        await route.abort()
        return
    if any(fragment in request.url for fragment in _ANALYTICS_HOST_FRAGMENTS):
        await route.abort()
        return
    await route.continue_()


async def _capture_json_response(response: Response, sink: list[CapturedResponse]) -> None:
    """Record XHR/fetch responses whose content-type is JSON — spec §6.2.3's
    "capture the XHR/fetch requests the page makes", narrowed to the ones
    that could plausibly be the "clean JSON endpoint" the spec describes.
    Deliberately tolerant of failure: a response can be aborted, redirected,
    or have its body already consumed by the time we read it, none of which
    should fail the whole render.
    """
    request = response.request
    if request.resource_type not in ("xhr", "fetch"):
        return
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        return
    try:
        body = await response.text()
    except PlaywrightError:
        return
    sink.append(CapturedResponse(url=response.url, status=response.status, content_type=content_type, body=body))
