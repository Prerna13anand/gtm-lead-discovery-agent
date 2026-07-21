"""Stage 2 — ATS Fingerprinting (spec §5).

Goal: identify which ATS hosts the company's board, and extract the board
token needed to call its API.

Implements detection signals 1, 2, 3, 4, and 6 (URL host match, redirect
target, embedded script/iframe, DOM markers, DNS/CNAME) for Greenhouse,
Lever, and Ashby.

Signal 6 (`_resolve_cname_aliases`) uses `socket.gethostbyname_ex`'s alias
list as a stdlib-only, best-effort CNAME signal — it reflects the system
resolver's own alias-chain resolution (which typically surfaces CNAME
targets), not a raw authoritative DNS `CNAME` record query. That distinction
is worth flagging honestly: a resolver that doesn't populate aliases (some
platforms/`getaddrinfo`-backed resolvers don't) will simply find nothing
here, degrading silently to "signal absent" rather than lying about it.

Signal 5 (network-request inspection during Playwright rendering) is
*partially* implemented: `identify_from_captured_requests` below contains
the actual signal-matching logic (does a captured XHR/fetch URL match a
known ATS host?), reusable and independently tested. What is **not** done is
wiring it back into a live Stage 2 re-run after a rendered-DOM extraction —
that needs a two-pass orchestration (fingerprint → render → re-fingerprint
with the captured URLs) this codebase's CLI harness doesn't have, since it
has no orchestrator at all (spec §16, a pre-existing, already-documented
scope boundary). The rendered-DOM adapter's own "render once, learn the
endpoint, never render again" cache (`discovery.extraction.rendered_dom`)
already delivers Signal 5's main practical benefit — avoiding repeated
expensive renders — at the extraction layer, even without promoting the
company to a proper `AtsIdentification` at the fingerprinting layer.

What also lives here is the separate *routing* check that decides a company
needs rendering at all (`has_job_like_content` / `has_spa_root_or_ats_embed`,
used by `route_extraction`), not the rendering itself.

Board-token extraction (spec §5.2) lives in `discovery.ats_platforms` — shared
with Stage 3, since an ATS-API adapter needs to be able to resolve the same
token from a `CareersSource.careers_url` directly (see
`discovery.extraction.greenhouse` for why). Starting map only; verify against
current vendor URL formats before relying on a new platform's pattern.
"""

from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.discovery.ats_platforms import (
    ATS_DOM_MARKERS,
    extract_board_token,
    known_ats_platform_for_embed_src,
    known_ats_platform_for_host,
)
from gtm_agent.models.ats import AtsIdentification, AtsPlatform, DetectionSignal
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.results import AtsFingerprintStatus, StageResult

logger = get_logger(__name__)

# Shared with discovery.extraction.rendered_dom — both the routing check
# here and that adapter's own DOM-link fallback need the same notion of
# "this looks like a job posting link".
JOB_LIKE_LINK_RE = re.compile(r"/(?:jobs?|careers?|positions?|openings?)/[\w-]{3,}", re.I)

# Live-verified refinement: a real careers page's *own* static assets are
# frequently served from a path under the same prefix this pattern matches
# — e.g. `/careers/icons/caret-down.svg` — which would otherwise
# false-positive as a job link. Job posting URLs essentially never carry a
# file extension; asset paths essentially always do.
_ASSET_EXTENSION_RE = re.compile(
    r"\.(?:svg|png|jpe?g|gif|ico|css|js|mjs|woff2?|ttf|eot|webp|avif|json|map)(?:[?#]|$)", re.I
)


def is_job_like_href(href: str) -> bool:
    """Spec §6.2.3 / §6.2.4: does this href look like a link to a specific
    job posting? Shared by the routing check below and
    `discovery.extraction.rendered_dom`'s DOM-link fallback.
    """
    if not JOB_LIKE_LINK_RE.search(href):
        return False
    return not _ASSET_EXTENSION_RE.search(href)


# Common SPA mount-point markers (spec §6.2.3 detection: "a known SPA
# root"). Not exhaustive — a pragmatic, documented set covering the major
# frameworks (React's conventional #root, Next.js Pages Router's #__next,
# Gatsby's #___gatsby), not a claim of completeness.
_SPA_ROOT_SELECTORS = ("#root", "#app", "#__next", "#___gatsby")

# Live-verified gap in the selector list above, found while validating this
# adapter (see discovery/extraction/rendered_dom.py's module docstring): a
# real Next.js *App Router* site (React Server Components) carries none of
# those conventional single-mount-point ids — verified directly against its
# static HTML. What it does carry is React's own Suspense/streaming
# boundary marker, `<!--$-->`, which is part of the React/RSC wire protocol
# itself, not something specific to one site or a guess — present on any
# page using a Suspense boundary during server rendering, which any
# React-Server-Components page with client-hydrated sections will have.
_RSC_SUSPENSE_MARKER = "<!--$-->"

_CONFIDENCE_URL_HOST_MATCH = 0.98
_CONFIDENCE_REDIRECT_TARGET = 0.95
_CONFIDENCE_EMBED_SIGNAL = 0.90
_CONFIDENCE_DOM_MARKER = 0.75
_CONFIDENCE_DNS_CNAME = 0.60  # spec §5.1: "Moderate; useful for white-labelled boards"
_CONFIDENCE_NETWORK_REQUEST = 0.85  # spec §5.1: "Strong"

_ADAPTER_ROUTING_CONFIDENCE_FLOOR = 0.8  # spec §5.3

DnsResolver = Callable[[str], Awaitable[list[str]]]


async def _resolve_cname_aliases(hostname: str) -> list[str]:
    """Best-effort CNAME alias resolution via the system resolver — see
    module docstring's caveat on what this can and can't guarantee. Run in
    a thread since `socket.gethostbyname_ex` is blocking.
    """
    try:
        _, aliases, _ = await asyncio.to_thread(socket.gethostbyname_ex, hostname)
    except (OSError, socket.gaierror):
        return []
    return aliases


def identify_from_captured_requests(
    company_id: str, xhr_urls: list[str], *, now: datetime | None = None
) -> AtsIdentification | None:
    """Spec §5.1 Signal 5: "if the page had to be rendered, inspect XHR
    targets" for a *known* ATS host that carries no static markers. Pure,
    synchronous, and independently callable — see module docstring for what
    wiring this into a live two-pass Stage 2 re-run would still require.
    """
    now = now or datetime.now(UTC)
    for url in xhr_urls:
        platform = known_ats_platform_for_host(urlparse(url).netloc)
        if platform is not None:
            return AtsIdentification(
                company_id=company_id,
                platform=platform,
                board_token=extract_board_token(platform, url),
                confidence=_CONFIDENCE_NETWORK_REQUEST,
                detection_signal=DetectionSignal.NETWORK_REQUESTS,
                created_at=now,
                last_verified_at=now,
            )
    return None


async def identify_ats(
    source: CareersSource,
    fetcher: Fetcher,
    *,
    dns_resolver: DnsResolver = _resolve_cname_aliases,
) -> StageResult[AtsIdentification, AtsFingerprintStatus]:
    """Run detection signals in confidence order against a resolved careers source."""
    now = datetime.now(UTC)

    # Signal 1 — URL host match. Decisive.
    parsed = urlparse(source.careers_url)
    platform = known_ats_platform_for_host(parsed.netloc)
    if platform is not None:
        token = extract_board_token(platform, source.careers_url)
        identification = AtsIdentification(
            company_id=source.company_id,
            platform=platform,
            board_token=token,
            confidence=_CONFIDENCE_URL_HOST_MATCH,
            detection_signal=DetectionSignal.URL_HOST_MATCH,
            created_at=now,
            last_verified_at=now,
        )
        return StageResult(status=AtsFingerprintStatus.IDENTIFIED, value=identification)

    try:
        # `use_cache=False`: this is an exploratory fingerprinting read, not
        # the "real" content fetch spec §6.3's conditional caching exists to
        # protect. Stage 3 (or this same routing step's own page_html peek —
        # see `route_extraction` callers) reads this exact URL again
        # moments later for extraction; if this read stored a validator,
        # that next read would get a spurious 304 and see an empty body
        # instead of the real page — a real, live-verified bug this
        # parameter exists to prevent (see `core.fetch.Fetcher._request`).
        result = await fetcher.get(source.careers_url, use_cache=False)
    except FetchError as exc:
        logger.info("ats_detection_fetch_failed", url=source.careers_url, error=str(exc))
        return StageResult(status=AtsFingerprintStatus.ATS_UNKNOWN, detail=str(exc))

    # Signal 2 — redirect target. `Fetcher` follows redirects, so `result.url`
    # already reflects the final host if a redirect occurred.
    final_host = urlparse(result.url).netloc
    if final_host != parsed.netloc:
        platform = known_ats_platform_for_host(final_host)
        if platform is not None:
            token = extract_board_token(platform, result.url)
            identification = AtsIdentification(
                company_id=source.company_id,
                platform=platform,
                board_token=token,
                confidence=_CONFIDENCE_REDIRECT_TARGET,
                detection_signal=DetectionSignal.REDIRECT_TARGET,
                created_at=now,
                last_verified_at=now,
            )
            return StageResult(status=AtsFingerprintStatus.IDENTIFIED, value=identification)

    tree = HTMLParser(result.text)

    # Signal 3 — embedded script / iframe src. Decisive.
    for tag in ("script", "iframe"):
        for node in tree.css(f"{tag}[src]"):
            src = node.attributes.get("src") or ""
            platform = known_ats_platform_for_embed_src(src)
            if platform is not None:
                token = extract_board_token(platform, src)
                identification = AtsIdentification(
                    company_id=source.company_id,
                    platform=platform,
                    board_token=token,
                    confidence=_CONFIDENCE_EMBED_SIGNAL,
                    detection_signal=DetectionSignal.EMBEDDED_SCRIPT_OR_IFRAME,
                    created_at=now,
                    last_verified_at=now,
                )
                return StageResult(status=AtsFingerprintStatus.IDENTIFIED, value=identification)

    # Signal 4 — DOM markers. Strong, but not decisive; no board token available
    # from a bare marker, so extraction will need to resolve the token later.
    for candidate_platform, selectors in ATS_DOM_MARKERS.items():
        for selector in selectors:
            if tree.css_first(selector) is not None:
                identification = AtsIdentification(
                    company_id=source.company_id,
                    platform=candidate_platform,
                    board_token=None,
                    confidence=_CONFIDENCE_DOM_MARKER,
                    detection_signal=DetectionSignal.DOM_MARKERS,
                    created_at=now,
                    last_verified_at=now,
                )
                return StageResult(status=AtsFingerprintStatus.IDENTIFIED, value=identification)

    # Signal 6 — DNS/CNAME lookup for white-labelled boards. Moderate; the
    # careers hostname itself isn't a known ATS host, but its CNAME target
    # may be (e.g. `careers.company.com` CNAME'd to a Greenhouse-operated
    # host). See module docstring for what this signal can and can't
    # guarantee, and the `dns_cname` open question that motivates a
    # moderate rather than high confidence.
    hostname = parsed.netloc.split(":")[0]  # strip a port, if any, before resolving
    aliases = await dns_resolver(hostname)
    for alias in aliases:
        platform = known_ats_platform_for_host(alias)
        if platform is not None:
            identification = AtsIdentification(
                company_id=source.company_id,
                platform=platform,
                board_token=extract_board_token(platform, source.careers_url),
                confidence=_CONFIDENCE_DNS_CNAME,
                detection_signal=DetectionSignal.DNS_CNAME,
                created_at=now,
                last_verified_at=now,
            )
            return StageResult(status=AtsFingerprintStatus.IDENTIFIED, value=identification)

    # Signal 5's live wiring (feeding a rendered-DOM render's captured XHR
    # targets back into this function) is not done here — see module
    # docstring. `identify_from_captured_requests` implements the signal's
    # actual matching logic and is independently tested; it is simply not
    # yet called from a live two-pass Stage 2 re-run, since no orchestrator
    # exists to drive one (spec §16, pre-existing scope boundary).

    logger.info("ats_unknown", company_id=source.company_id, url=source.careers_url)
    return StageResult(status=AtsFingerprintStatus.ATS_UNKNOWN, detail="no detection signal matched")


def has_jsonld_job_posting(html: str) -> bool:
    """Cheap check used by routing (spec §5.3): does the page carry schema.org/JobPosting JSON-LD?"""
    return 'application/ld+json' in html and "JobPosting" in html


def has_job_like_content(html: str) -> bool:
    """Cheap check used by routing (spec §6.2.3 detection): does the
    *static* page already show job-detail-shaped links? If so, there's
    nothing to render — a rendered-DOM escalation is for pages that show
    none of this without JS. Checks actual `<a href>` values rather than
    regex-scanning the raw HTML, so it isn't fooled by the pattern
    appearing somewhere that isn't a link (script text, a comment, ...).
    """
    tree = HTMLParser(html)
    for anchor in tree.css("a[href]"):
        href = anchor.attributes.get("href") or ""
        if is_job_like_href(href):
            return True
    return False


def has_spa_root_or_ats_embed(html: str) -> bool:
    """Cheap check used by routing (spec §6.2.3 detection): a known SPA
    mount-point marker, or a known ATS embed script/iframe (the same signal
    Signal 3, §5.1, already looks for) — both suggest real content exists
    but needs JS execution to appear, as opposed to a page with genuinely
    nothing to show.
    """
    tree = HTMLParser(html)
    for selector in _SPA_ROOT_SELECTORS:
        if tree.css_first(selector) is not None:
            return True
    if _RSC_SUSPENSE_MARKER in html:
        return True
    for tag in ("script", "iframe"):
        for node in tree.css(f"{tag}[src]"):
            src = node.attributes.get("src") or ""
            if known_ats_platform_for_embed_src(src) is not None:
                return True
    return False


def route_extraction(identification: AtsIdentification | None, page_html: str | None = None) -> AtsPlatform:
    """Decide which adapter family should handle extraction — spec §5.3.

    `identification` is None when Stage 2 didn't identify a platform at all
    (`ats_unknown`) — routing then falls through to the JSON-LD check, then
    the rendered-DOM check, and finally the generic-HTML terminal fallback.

    `page_html` is the already-fetched careers page body, if available, used
    for both the JSON-LD check and the rendered-DOM escalation check.
    """
    if identification is not None and identification.platform in (
        AtsPlatform.GREENHOUSE,
        AtsPlatform.LEVER,
        AtsPlatform.ASHBY,
        AtsPlatform.WORKABLE,
        AtsPlatform.SMARTRECRUITERS,
        AtsPlatform.RECRUITEE,
        AtsPlatform.RIPPLING,
    ):
        if identification.confidence >= _ADAPTER_ROUTING_CONFIDENCE_FLOOR:
            return identification.platform

    if page_html is not None and has_jsonld_job_posting(page_html):
        return AtsPlatform.JSONLD

    if page_html is not None and not has_job_like_content(page_html) and has_spa_root_or_ats_embed(page_html):
        return AtsPlatform.RENDERED_DOM

    return AtsPlatform.GENERIC_HTML
