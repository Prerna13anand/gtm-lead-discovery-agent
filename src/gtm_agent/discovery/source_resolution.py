"""Stage 1 — Source Resolution (spec §4).

Goal: from `(company_name, domain)` to a canonical careers URL with a
confidence score.

Phase 1 implements the resolution ladder's strategies A, B, and E:
    A — homepage link extraction (`HomepageLinkStrategy`)
    B — conventional path probing (`PathProbeStrategy`)
    E — manual override (handled directly in `resolve_source`, since it
        short-circuits the entire ladder rather than being one more rung)

Phase 2 adds:
    C — sitemap.xml (`SitemapStrategy`)
    D — Tavily search fallback (`TavilySearchStrategy`)
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.discovery.ats_platforms import known_ats_platform_for_host
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.company import Company
from gtm_agent.models.results import SourceResolutionStatus, StageResult
from gtm_agent.services.tavily import TavilyClient, TavilyNotConfiguredError, TavilySearchError

logger = get_logger(__name__)

# --- Strategy A scoring (spec §4.1) ---
_LINK_TEXT_RE = re.compile(r"careers?|jobs|join us|we'?re hiring|work with us|open roles", re.I)
_HREF_PATH_RE = re.compile(r"careers?|jobs|join|hiring|opportunities", re.I)
_BLOG_PATH_RE = re.compile(r"/blog/|/news/|/press/", re.I)
_OTHER_COMPANIES_TEXT_RE = re.compile(r"other companies|browse (all )?jobs|job board for", re.I)

_ANCHOR_ACCEPT_THRESHOLD = 5  # must clear at least one strong positive signal

# --- Strategy B conventional paths (spec §4.1) ---
_CONVENTIONAL_PATHS = (
    "/careers",
    "/career",
    "/jobs",
    "/join",
    "/join-us",
    "/hiring",
    "/work-with-us",
    "/about/careers",
    "/company/careers",
    "/en/careers",
)
_PATH_PROBE_DELAY_SECONDS = 0.5  # placeholder politeness gap; real rate limiting is a fetch-layer TODO (§16.3)

# --- Strategy C sitemap.xml (spec §4.1) ---
_SITEMAP_PATH = "/sitemap.xml"
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
_SITEMAP_INDEX_MARKER_RE = re.compile(r"<sitemapindex[\s>]", re.I)
_MAX_SITEMAP_INDEX_ENTRIES = 10  # bound on child-sitemap fetches; not spec-mandated, kept cheap per §2.5

# --- Validation markers (spec §4.2) ---
_CAREERS_COPY_RE = re.compile(r"open positions|join our team|current openings", re.I)
_JOB_DETAIL_HREF_RE = re.compile(r"/jobs?/[\w-]+", re.I)
_MIN_JOB_LIKE_LINKS = 3

# --- Confidence table (spec §4.3) ---
_CONFIDENCE_HOMEPAGE_ATS = 0.95
_CONFIDENCE_HOMEPAGE_OWN_DOMAIN = 0.85
_CONFIDENCE_PATH_PROBE = 0.75
_CONFIDENCE_SITEMAP = 0.75
_CONFIDENCE_TAVILY_DOMAIN_RESTRICTED = 0.60
_CONFIDENCE_TAVILY_UNRESTRICTED = 0.40  # below _CONFIDENCE_FLOOR — always NEEDS_REVIEW
_CONFIDENCE_FLOOR = 0.50


class DomainUnreachableError(Exception):
    """Raised internally when the company's domain itself cannot be reached (spec §4.4)."""


@dataclass(frozen=True)
class ResolutionCandidate:
    url: str
    confidence: float
    strategy: ResolutionStrategy
    validated: bool = True


class ResolutionStrategyHandler(Protocol):
    strategy: ResolutionStrategy

    async def attempt(self, company: Company, fetcher: Fetcher) -> ResolutionCandidate | None:
        """Return a candidate if this strategy found one, else None to fall through."""
        ...


def _looks_like_careers_page(html: str) -> bool:
    """Spec §4.2 validation: markers, or enough job-detail-shaped links."""
    tree = HTMLParser(html)
    body_text = tree.body.text(separator=" ", strip=True) if tree.body else html
    if _CAREERS_COPY_RE.search(body_text):
        return True

    job_like_links = 0
    for anchor in tree.css("a[href]"):
        href = anchor.attributes.get("href") or ""
        if _JOB_DETAIL_HREF_RE.search(href):
            job_like_links += 1
            if job_like_links >= _MIN_JOB_LIKE_LINKS:
                return True

    if 'application/ld+json' in html and "JobPosting" in html:
        return True

    return False


def _score_anchor(anchor_text: str, href: str, in_footer_or_nav: bool) -> tuple[float, bool]:
    """Score one anchor per spec §4.1 Strategy A. Returns (score, is_known_ats_domain)."""
    score = 0.0
    is_ats = False

    if _LINK_TEXT_RE.search(anchor_text):
        score += 5
    if _HREF_PATH_RE.search(href):
        score += 4

    parsed = urlparse(href)
    if parsed.netloc and known_ats_platform_for_host(parsed.netloc) is not None:
        score += 5
        is_ats = True

    if in_footer_or_nav:
        score += 2
    if _BLOG_PATH_RE.search(href):
        score -= 3
    if _OTHER_COMPANIES_TEXT_RE.search(anchor_text):
        score -= 5

    return score, is_ats


class HomepageLinkStrategy:
    """Strategy A (spec §4.1). Preferred: reflects what the company actually links to."""

    strategy = ResolutionStrategy.HOMEPAGE_LINK

    async def attempt(self, company: Company, fetcher: Fetcher) -> ResolutionCandidate | None:
        homepage_url = f"https://{company.domain}"
        try:
            result = await fetcher.get(homepage_url)
        except FetchError as exc:
            if isinstance(exc.original, (httpx.ConnectError, httpx.ConnectTimeout)):
                raise DomainUnreachableError(str(exc)) from exc
            logger.info("homepage_fetch_failed", domain=company.domain, error=str(exc))
            return None

        if result.status_code >= 400:
            return None

        tree = HTMLParser(result.text)
        best_score = 0.0
        best_href: str | None = None
        best_is_ats = False

        for anchor in tree.css("a[href]"):
            href = anchor.attributes.get("href") or ""
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            text = anchor.text(strip=True) or ""
            in_footer_or_nav = _in_footer_or_nav(anchor)
            score, is_ats = _score_anchor(text, href, in_footer_or_nav)
            if score > best_score:
                best_score = score
                best_href = href
                best_is_ats = is_ats

        if best_href is None or best_score < _ANCHOR_ACCEPT_THRESHOLD:
            return None

        resolved_url = urljoin(str(result.url), best_href)

        if best_is_ats:
            # spec §4.2: being on a known ATS domain satisfies validation on its own.
            return ResolutionCandidate(
                url=resolved_url,
                confidence=_CONFIDENCE_HOMEPAGE_ATS,
                strategy=self.strategy,
                validated=True,
            )

        validated = await _fetch_and_validate(resolved_url, fetcher)
        return ResolutionCandidate(
            url=resolved_url,
            confidence=_CONFIDENCE_HOMEPAGE_OWN_DOMAIN,
            strategy=self.strategy,
            validated=validated,
        )


def _in_footer_or_nav(anchor) -> bool:  # noqa: ANN001 — selectolax Node has no public type export
    node = anchor.parent
    while node is not None:
        if node.tag in ("footer", "nav"):
            return True
        node = node.parent
    return False


async def _fetch_and_validate(url: str, fetcher: Fetcher) -> bool:
    try:
        # use_cache=False: `url` becomes `source.careers_url` on success, and
        # Stage 2/3 read that exact URL again moments later. Storing a
        # validator from this exploratory validation read would make that
        # later, real read see a spurious 304 instead of real content — a
        # live-verified bug (see `core.fetch.Fetcher._request`'s docstring).
        result = await fetcher.get(url, use_cache=False)
    except FetchError:
        return False
    if result.status_code >= 400:
        return False
    return _looks_like_careers_page(result.text)


class PathProbeStrategy:
    """Strategy B (spec §4.1). Probed sequentially, never in parallel, per §2.5."""

    strategy = ResolutionStrategy.PATH_PROBE

    async def attempt(self, company: Company, fetcher: Fetcher) -> ResolutionCandidate | None:
        for i, path in enumerate(_CONVENTIONAL_PATHS):
            if i > 0:
                await asyncio.sleep(_PATH_PROBE_DELAY_SECONDS)

            url = f"https://{company.domain}{path}"
            try:
                # use_cache=False: same reasoning as _fetch_and_validate —
                # `url` becomes `source.careers_url` on success.
                head_result = await fetcher.head(url, use_cache=False)
            except FetchError:
                continue

            status = head_result.status_code
            if status in (405, 501) or status == 200 and not head_result.text:
                # HEAD unsupported or inconclusive — fall back to GET (spec §4.1)
                try:
                    get_result = await fetcher.get(url, use_cache=False)
                except FetchError:
                    continue
                status = get_result.status_code
                body = get_result.text
            elif 200 <= status < 300:
                try:
                    get_result = await fetcher.get(url, use_cache=False)
                except FetchError:
                    continue
                body = get_result.text
            else:
                continue

            if not (200 <= status < 300):
                continue

            if _looks_like_careers_page(body):
                return ResolutionCandidate(
                    url=url,
                    confidence=_CONFIDENCE_PATH_PROBE,
                    strategy=self.strategy,
                    validated=True,
                )

        return None


class SitemapStrategy:
    """Strategy C (spec §4.1). Fetch /sitemap.xml (and any sitemap index it
    points to), filter for careers-like paths. Effective on marketing sites
    built with static generators; cheap and high-precision when present.
    """

    strategy = ResolutionStrategy.SITEMAP

    async def attempt(self, company: Company, fetcher: Fetcher) -> ResolutionCandidate | None:
        sitemap_url = f"https://{company.domain}{_SITEMAP_PATH}"
        try:
            result = await fetcher.get(sitemap_url)
        except FetchError:
            return None

        if result.status_code >= 400:
            return None

        locs = _LOC_RE.findall(result.text)
        if not locs:
            return None

        if _SITEMAP_INDEX_MARKER_RE.search(result.text):
            locs = await _urls_from_sitemap_index(locs, fetcher)

        careers_url = next((loc for loc in locs if _HREF_PATH_RE.search(urlparse(loc).path)), None)
        if careers_url is None:
            return None

        validated = await _fetch_and_validate(careers_url, fetcher)
        return ResolutionCandidate(
            url=careers_url,
            confidence=_CONFIDENCE_SITEMAP,
            strategy=self.strategy,
            validated=validated,
        )


async def _urls_from_sitemap_index(sitemap_locs: list[str], fetcher: Fetcher) -> list[str]:
    """A sitemap index points to child sitemaps rather than pages directly
    (spec §4.1 Strategy C: "and any sitemap index it points to"). Fetch a
    bounded number of them, sequentially, and pool their <loc> entries.
    """
    urls: list[str] = []
    for sitemap_url in sitemap_locs[:_MAX_SITEMAP_INDEX_ENTRIES]:
        try:
            result = await fetcher.get(sitemap_url)
        except FetchError:
            continue
        if result.status_code >= 400:
            continue
        urls.extend(_LOC_RE.findall(result.text))
    return urls


class TavilySearchStrategy:
    """Strategy D (spec §4.1). Falls back to a Tavily search: a domain-restricted
    query first, then an unrestricted one.

    Domain-restrict first — the unrestricted query is prone to returning an
    aggregator's page *about* the company rather than the company's own
    board, which is a silent correctness failure. Any unrestricted result's
    host is therefore validated against the known company domain and the ATS
    domain list before being accepted.
    """

    strategy = ResolutionStrategy.TAVILY_SEARCH

    def __init__(self, tavily_client: TavilyClient | None = None) -> None:
        self._tavily_client = tavily_client or TavilyClient()

    async def attempt(self, company: Company, fetcher: Fetcher) -> ResolutionCandidate | None:
        if not self._tavily_client.is_configured:
            logger.debug("tavily_not_configured", domain=company.domain)
            return None

        restricted_query = f'"{company.name}" careers open positions site:{company.domain}'
        try:
            restricted_results = await self._tavily_client.search(
                restricted_query, fetcher=fetcher, restrict_domain=company.domain
            )
        except (TavilyNotConfiguredError, TavilySearchError) as exc:
            logger.info("tavily_search_failed", domain=company.domain, error=str(exc))
            restricted_results = []

        candidate_url = _first_result_url(restricted_results)
        if candidate_url is not None:
            validated = await _fetch_and_validate(candidate_url, fetcher)
            return ResolutionCandidate(
                url=candidate_url,
                confidence=_CONFIDENCE_TAVILY_DOMAIN_RESTRICTED,
                strategy=self.strategy,
                validated=validated,
            )

        unrestricted_query = f'"{company.name}" jobs'
        try:
            unrestricted_results = await self._tavily_client.search(unrestricted_query, fetcher=fetcher)
        except (TavilyNotConfiguredError, TavilySearchError) as exc:
            logger.info("tavily_search_failed", domain=company.domain, error=str(exc))
            return None

        candidate_url = _first_result_url_on_known_host(unrestricted_results, company.domain)
        if candidate_url is None:
            return None

        validated = await _fetch_and_validate(candidate_url, fetcher)
        return ResolutionCandidate(
            url=candidate_url,
            confidence=_CONFIDENCE_TAVILY_UNRESTRICTED,
            strategy=self.strategy,
            validated=validated,
        )


def _first_result_url(results: list[dict[str, object]]) -> str | None:
    for result in results:
        url = result.get("url")
        if isinstance(url, str) and url:
            return url
    return None


def _first_result_url_on_known_host(results: list[dict[str, object]], company_domain: str) -> str | None:
    """spec §4.1 Strategy D: validate an unrestricted result's host against
    the known company domain or the known ATS domain list before accepting.
    """
    for result in results:
        url = result.get("url")
        if not isinstance(url, str) or not url:
            continue
        host = urlparse(url).netloc
        if _host_matches_domain(host, company_domain) or known_ats_platform_for_host(host) is not None:
            return url
    return None


def _host_matches_domain(host: str, domain: str) -> bool:
    host = host.lower().lstrip(".")
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith(f".{domain}")


_STRATEGY_LADDER: tuple[ResolutionStrategyHandler, ...] = (
    HomepageLinkStrategy(),
    PathProbeStrategy(),
    SitemapStrategy(),
    TavilySearchStrategy(),
)


async def resolve_source(
    company: Company,
    fetcher: Fetcher,
    *,
    manual_override_url: str | None = None,
) -> StageResult[CareersSource, SourceResolutionStatus]:
    """Run the resolution ladder for one company. Spec §4.

    A manual override, if given, short-circuits the ladder entirely (spec §4.1
    Strategy E) and is never overwritten by automated resolution.
    """
    now = datetime.now(UTC)

    if manual_override_url:
        source = CareersSource(
            company_id=company.id,
            careers_url=manual_override_url,
            resolution_strategy=ResolutionStrategy.MANUAL_OVERRIDE,
            resolution_confidence=1.0,
            is_manual_override=True,
            needs_review=False,
            created_at=now,
            last_verified_at=now,
        )
        return StageResult(status=SourceResolutionStatus.RESOLVED, value=source)

    try:
        for handler in _STRATEGY_LADDER:
            candidate = await handler.attempt(company, fetcher)
            if candidate is None:
                continue

            if candidate.confidence < _CONFIDENCE_FLOOR:
                source = CareersSource(
                    company_id=company.id,
                    careers_url=candidate.url,
                    resolution_strategy=candidate.strategy,
                    resolution_confidence=candidate.confidence,
                    needs_review=True,
                    created_at=now,
                )
                return StageResult(status=SourceResolutionStatus.NEEDS_REVIEW, value=source)

            if not candidate.validated:
                source = CareersSource(
                    company_id=company.id,
                    careers_url=candidate.url,
                    resolution_strategy=candidate.strategy,
                    resolution_confidence=candidate.confidence,
                    needs_review=True,
                    created_at=now,
                )
                return StageResult(status=SourceResolutionStatus.RESOLUTION_UNVALIDATED, value=source)

            source = CareersSource(
                company_id=company.id,
                careers_url=candidate.url,
                resolution_strategy=candidate.strategy,
                resolution_confidence=candidate.confidence,
                needs_review=False,
                created_at=now,
                last_verified_at=now,
            )
            return StageResult(status=SourceResolutionStatus.RESOLVED, value=source)
    except DomainUnreachableError as exc:
        logger.warning("domain_unreachable", domain=company.domain, error=str(exc))
        return StageResult(status=SourceResolutionStatus.DOMAIN_UNREACHABLE, detail=str(exc))

    logger.info("no_careers_page_found", company_id=company.id, domain=company.domain)
    return StageResult(status=SourceResolutionStatus.NO_CAREERS_PAGE, detail="resolution ladder exhausted")
