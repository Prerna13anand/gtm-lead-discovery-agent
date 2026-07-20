"""Stage 1 Source Resolution tests — Strategies C (sitemap.xml) and D (Tavily
search fallback), spec §4.1.

Scoped to the two strategies added in Phase 2. Strategies A, B, and E
(Phase 1) are exercised only incidentally, via integration tests that confirm
C and D are correctly wired into the resolution ladder.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult
from gtm_agent.discovery.source_resolution import (
    SitemapStrategy,
    TavilySearchStrategy,
    resolve_source,
)
from gtm_agent.models.careers_source import ResolutionStrategy
from gtm_agent.models.company import Company
from gtm_agent.models.results import SourceResolutionStatus
from gtm_agent.services.tavily import TavilySearchError


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


# --- Strategy D — Tavily search fallback (spec §4.1) ---

_RESTRICTED_QUERY = '"Acme" careers open positions site:acme.com'
_UNRESTRICTED_QUERY = '"Acme" jobs'


class FakeTavilyClient:
    """A test double for `TavilyClient` — `TavilySearchStrategy` is injected
    with one directly, so these tests exercise the strategy's fallback
    ordering and host-validation logic without going through real HTTP.
    """

    def __init__(
        self,
        configured: bool = True,
        responses: dict[str, list[dict[str, object]]] | None = None,
        raise_for: dict[str, Exception] | None = None,
    ) -> None:
        self._configured = configured
        self.responses = responses or {}
        self.raise_for = raise_for or {}
        self.queries: list[tuple[str, str | None]] = []

    @property
    def is_configured(self) -> bool:
        return self._configured

    async def search(
        self, query: str, *, fetcher: object, restrict_domain: str | None = None
    ) -> list[dict[str, object]]:
        self.queries.append((query, restrict_domain))
        if query in self.raise_for:
            raise self.raise_for[query]
        return self.responses.get(query, [])


async def test_tavily_not_configured_declines_without_searching() -> None:
    tavily = FakeTavilyClient(configured=False)
    strategy = TavilySearchStrategy(tavily_client=tavily)

    candidate = await strategy.attempt(_company(), FakeFetcher())

    assert candidate is None
    assert tavily.queries == []


async def test_domain_restricted_result_resolves_at_0_60_confidence() -> None:
    tavily = FakeTavilyClient(responses={_RESTRICTED_QUERY: [{"url": "https://acme.com/careers"}]})
    strategy = TavilySearchStrategy(tavily_client=tavily)
    fetcher = FakeFetcher({"https://acme.com/careers": _result("https://acme.com/careers", 200, _CAREERS_PAGE_HTML)})

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is not None
    assert candidate.url == "https://acme.com/careers"
    assert candidate.strategy == ResolutionStrategy.TAVILY_SEARCH
    assert candidate.confidence == 0.60
    assert candidate.validated is True
    # domain-restricted query is tried first, with the site restriction passed through
    assert tavily.queries == [(_RESTRICTED_QUERY, "acme.com")]


async def test_falls_back_to_unrestricted_when_restricted_finds_nothing() -> None:
    tavily = FakeTavilyClient(
        responses={
            _RESTRICTED_QUERY: [],
            _UNRESTRICTED_QUERY: [{"url": "https://jobs.lever.co/acme"}],
        }
    )
    strategy = TavilySearchStrategy(tavily_client=tavily)
    fetcher = FakeFetcher(
        {"https://jobs.lever.co/acme": _result("https://jobs.lever.co/acme", 200, _CAREERS_PAGE_HTML)}
    )

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is not None
    assert candidate.url == "https://jobs.lever.co/acme"
    assert candidate.confidence == 0.40
    assert tavily.queries == [(_RESTRICTED_QUERY, "acme.com"), (_UNRESTRICTED_QUERY, None)]


async def test_unrestricted_result_on_unknown_host_is_rejected() -> None:
    # spec §4.1: an unrestricted result must be validated against the known
    # company domain or ATS domain list — an aggregator page must not be accepted.
    tavily = FakeTavilyClient(
        responses={
            _RESTRICTED_QUERY: [],
            _UNRESTRICTED_QUERY: [{"url": "https://some-job-aggregator.example/acme-inc-jobs"}],
        }
    )
    strategy = TavilySearchStrategy(tavily_client=tavily)

    candidate = await strategy.attempt(_company(), FakeFetcher())

    assert candidate is None


async def test_unrestricted_result_on_company_subdomain_is_accepted() -> None:
    tavily = FakeTavilyClient(
        responses={
            _RESTRICTED_QUERY: [],
            _UNRESTRICTED_QUERY: [{"url": "https://careers.acme.com/openings"}],
        }
    )
    strategy = TavilySearchStrategy(tavily_client=tavily)
    fetcher = FakeFetcher(
        {"https://careers.acme.com/openings": _result("https://careers.acme.com/openings", 200, _CAREERS_PAGE_HTML)}
    )

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is not None
    assert candidate.url == "https://careers.acme.com/openings"


async def test_unrestricted_result_on_known_ats_domain_is_accepted() -> None:
    tavily = FakeTavilyClient(
        responses={
            _RESTRICTED_QUERY: [],
            _UNRESTRICTED_QUERY: [
                {"url": "https://some-job-aggregator.example/acme-inc-jobs"},  # skipped: unknown host
                {"url": "https://boards.greenhouse.io/acme"},  # accepted: known ATS domain
            ],
        }
    )
    strategy = TavilySearchStrategy(tavily_client=tavily)
    fetcher = FakeFetcher(
        {"https://boards.greenhouse.io/acme": _result("https://boards.greenhouse.io/acme", 200, _CAREERS_PAGE_HTML)}
    )

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is not None
    assert candidate.url == "https://boards.greenhouse.io/acme"


async def test_match_failing_page_validation_is_returned_unvalidated() -> None:
    tavily = FakeTavilyClient(responses={_RESTRICTED_QUERY: [{"url": "https://acme.com/careers"}]})
    strategy = TavilySearchStrategy(tavily_client=tavily)
    fetcher = FakeFetcher({"https://acme.com/careers": _result("https://acme.com/careers", 200, _PLAIN_PAGE_HTML)})

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is not None
    assert candidate.validated is False


async def test_search_error_on_restricted_query_falls_back_to_unrestricted() -> None:
    # Only TavilyNotConfiguredError/TavilySearchError are caught by attempt() —
    # simulate the kind of failure search() actually raises.
    tavily = FakeTavilyClient(
        raise_for={_RESTRICTED_QUERY: TavilySearchError("simulated Tavily outage")},
        responses={_UNRESTRICTED_QUERY: [{"url": "https://jobs.lever.co/acme"}]},
    )
    strategy = TavilySearchStrategy(tavily_client=tavily)
    fetcher = FakeFetcher(
        {"https://jobs.lever.co/acme": _result("https://jobs.lever.co/acme", 200, _CAREERS_PAGE_HTML)}
    )

    candidate = await strategy.attempt(_company(), fetcher)

    assert candidate is not None
    assert candidate.url == "https://jobs.lever.co/acme"
    assert candidate.confidence == 0.40


async def test_search_error_on_both_queries_declines() -> None:
    tavily = FakeTavilyClient(
        raise_for={
            _RESTRICTED_QUERY: TavilySearchError("simulated outage"),
            _UNRESTRICTED_QUERY: TavilySearchError("simulated outage"),
        }
    )
    strategy = TavilySearchStrategy(tavily_client=tavily)

    candidate = await strategy.attempt(_company(), FakeFetcher())

    assert candidate is None


async def test_low_confidence_unrestricted_match_needs_review_through_resolve_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Full-ladder integration: A/B/C all decline, D resolves via the
    # unrestricted path (0.40 confidence) — resolve_source's existing
    # confidence-floor check must route this to NEEDS_REVIEW without any
    # Strategy-D-specific handling.
    monkeypatch.setattr(
        "gtm_agent.discovery.source_resolution.asyncio.sleep", AsyncMock(return_value=None)
    )

    import gtm_agent.discovery.source_resolution as source_resolution

    fake_tavily_strategy = TavilySearchStrategy(
        tavily_client=FakeTavilyClient(
            responses={
                _RESTRICTED_QUERY: [],
                _UNRESTRICTED_QUERY: [{"url": "https://jobs.lever.co/acme"}],
            }
        )
    )
    # _STRATEGY_LADDER holds already-constructed strategy instances (built at
    # import time), so patching the TavilySearchStrategy class wouldn't reach
    # the one resolve_source() actually iterates over — replace the ladder entry directly.
    monkeypatch.setattr(
        source_resolution,
        "_STRATEGY_LADDER",
        (
            source_resolution.HomepageLinkStrategy(),
            source_resolution.PathProbeStrategy(),
            source_resolution.SitemapStrategy(),
            fake_tavily_strategy,
        ),
    )

    fetcher = FakeFetcher(
        {
            "https://acme.com": _result("https://acme.com", 200, "<html><body>Hello</body></html>"),
            "https://acme.com/sitemap.xml": _result("https://acme.com/sitemap.xml", 404, ""),
            "https://jobs.lever.co/acme": _result("https://jobs.lever.co/acme", 200, _CAREERS_PAGE_HTML),
        }
    )

    result = await resolve_source(_company(), fetcher)

    assert result.status == SourceResolutionStatus.NEEDS_REVIEW
    assert result.value is not None
    assert result.value.resolution_strategy == ResolutionStrategy.TAVILY_SEARCH
    assert result.value.resolution_confidence == 0.40
