"""Stage 1 Source Resolution tests — Strategy C (sitemap.xml), spec §4.1.

Scoped to the sitemap strategy added in Phase 2. Strategies A, B, and E
(Phase 1) are exercised only incidentally, via one integration test that
confirms Strategy C is correctly wired into the resolution ladder.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult
from gtm_agent.discovery.source_resolution import (
    SitemapStrategy,
    resolve_source,
)
from gtm_agent.models.careers_source import ResolutionStrategy
from gtm_agent.models.company import Company
from gtm_agent.models.results import SourceResolutionStatus


def _company(domain: str = "acme.com") -> Company:
    return Company(id="acme", name="Acme", domain=domain, added_at=datetime.now(UTC))


def _result(url: str, status_code: int, text: str) -> FetchResult:
    return FetchResult(url=url, status_code=status_code, text=text, headers=httpx.Headers({}))


def _urlset(*urls: str) -> str:
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'


def _sitemap_index(*sitemap_urls: str) -> str:
    entries = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in sitemap_urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</sitemapindex>'


_CAREERS_PAGE_HTML = "<html><body><h1>Careers</h1><p>Open positions at Acme.</p></body></html>"
_PLAIN_PAGE_HTML = "<html><body><p>Nothing to see here.</p></body></html>"


class FakeFetcher:
    """Serves canned responses by exact URL for both get() and head().

    Unlike the extraction-adapter FakeFetcher, unregistered URLs 404 rather
    than raising — convenient here since a declining strategy legitimately
    probes URLs that don't exist.
    """

    def __init__(
        self,
        responses: dict[str, FetchResult] | None = None,
        raise_for: set[str] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.raise_for = raise_for or set()
        self.requested_urls: list[str] = []

    async def get(self, url: str, **kwargs: object) -> FetchResult:
        return await self._respond(url)

    async def head(self, url: str, **kwargs: object) -> FetchResult:
        return await self._respond(url)

    async def _respond(self, url: str) -> FetchResult:
        self.requested_urls.append(url)
        if url in self.raise_for:
            raise FetchError(f"simulated failure for {url}")
        return self.responses.get(url, _result(url, 404, ""))


@pytest.fixture
def strategy() -> SitemapStrategy:
    return SitemapStrategy()


async def test_urlset_with_careers_url_resolves_and_validates(strategy: SitemapStrategy) -> None:
    fetcher = FakeFetcher(
        {
            "https://acme.com/sitemap.xml": _result(
                "https://acme.com/sitemap.xml",
                200,
                _urlset("https://acme.com/about", "https://acme.com/careers", "https://acme.com/blog/post-1"),
            ),
            "https://acme.com/careers": _result("https://acme.com/careers", 200, _CAREERS_PAGE_HTML),
        }
    )

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is not None
    assert candidate.url == "https://acme.com/careers"
    assert candidate.strategy == ResolutionStrategy.SITEMAP
    assert candidate.confidence == 0.75
    assert candidate.validated is True


async def test_sitemap_index_is_traversed_for_child_sitemaps(strategy: SitemapStrategy) -> None:
    fetcher = FakeFetcher(
        {
            "https://acme.com/sitemap.xml": _result(
                "https://acme.com/sitemap.xml", 200, _sitemap_index("https://acme.com/sitemap-pages.xml")
            ),
            "https://acme.com/sitemap-pages.xml": _result(
                "https://acme.com/sitemap-pages.xml", 200, _urlset("https://acme.com/careers")
            ),
            "https://acme.com/careers": _result("https://acme.com/careers", 200, _CAREERS_PAGE_HTML),
        }
    )

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is not None
    assert candidate.url == "https://acme.com/careers"
    # confirms the multi-hop traversal actually happened, not just the root sitemap
    assert fetcher.requested_urls == [
        "https://acme.com/sitemap.xml",
        "https://acme.com/sitemap-pages.xml",
        "https://acme.com/careers",
    ]


async def test_missing_sitemap_declines(strategy: SitemapStrategy) -> None:
    fetcher = FakeFetcher({"https://acme.com/sitemap.xml": _result("https://acme.com/sitemap.xml", 404, "")})

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is None


async def test_sitemap_with_no_careers_like_urls_declines(strategy: SitemapStrategy) -> None:
    fetcher = FakeFetcher(
        {
            "https://acme.com/sitemap.xml": _result(
                "https://acme.com/sitemap.xml",
                200,
                _urlset("https://acme.com/about", "https://acme.com/blog/post-1", "https://acme.com/pricing"),
            )
        }
    )

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is None


async def test_sitemap_match_failing_validation_is_returned_unvalidated(strategy: SitemapStrategy) -> None:
    fetcher = FakeFetcher(
        {
            "https://acme.com/sitemap.xml": _result(
                "https://acme.com/sitemap.xml", 200, _urlset("https://acme.com/careers")
            ),
            "https://acme.com/careers": _result("https://acme.com/careers", 200, _PLAIN_PAGE_HTML),
        }
    )

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is not None
    assert candidate.url == "https://acme.com/careers"
    assert candidate.validated is False


async def test_sitemap_fetch_failure_declines(strategy: SitemapStrategy) -> None:
    fetcher = FakeFetcher(raise_for={"https://acme.com/sitemap.xml"})

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is None


async def test_empty_urlset_declines(strategy: SitemapStrategy) -> None:
    fetcher = FakeFetcher(
        {"https://acme.com/sitemap.xml": _result("https://acme.com/sitemap.xml", 200, _urlset())}
    )

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is None


async def test_resolve_source_falls_through_to_sitemap_when_earlier_strategies_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strategy B probes 10 conventional paths with a real politeness sleep
    # between each (spec §2.5) — patch it out so this integration test stays fast.
    monkeypatch.setattr(
        "gtm_agent.discovery.source_resolution.asyncio.sleep", AsyncMock(return_value=None)
    )

    # The sitemap-discovered URL deliberately avoids every path in
    # _CONVENTIONAL_PATHS, so Strategy B's probe genuinely can't find it —
    # otherwise Strategy B (tried first) would resolve it before Strategy C
    # ever runs, and this wouldn't be testing the fall-through at all.
    fetcher = FakeFetcher(
        {
            # Strategy A: homepage has no qualifying anchors.
            "https://acme.com": _result("https://acme.com", 200, "<html><body>Hello</body></html>"),
            # Strategy B: every conventional path 404s (FakeFetcher default).
            # Strategy C: sitemap resolves.
            "https://acme.com/sitemap.xml": _result(
                "https://acme.com/sitemap.xml", 200, _urlset("https://acme.com/opportunities")
            ),
            "https://acme.com/opportunities": _result("https://acme.com/opportunities", 200, _CAREERS_PAGE_HTML),
        }
    )

    result = await resolve_source(_company(), fetcher)

    assert result.status == SourceResolutionStatus.RESOLVED
    assert result.value is not None
    assert result.value.resolution_strategy == ResolutionStrategy.SITEMAP
    assert result.value.careers_url == "https://acme.com/opportunities"
    assert result.value.resolution_confidence == 0.75
