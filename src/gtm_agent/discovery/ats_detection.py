"""Stage 2 — ATS Fingerprinting (spec §5).

Goal: identify which ATS hosts the company's board, and extract the board
token needed to call its API.

Phase 1 implements detection signals 1-4 (URL host match, redirect target,
embedded script/iframe, DOM markers) for Greenhouse, Lever, and Ashby.

NOT implemented (left as a TODO):
    - Signal 6, DNS/CNAME lookups for white-labelled boards.

Signal 5 (network-request inspection during Playwright rendering) is
implemented, but not here — it happens as part of the rendered-DOM adapter's
own render step (`discovery.extraction.rendered_dom`), since that's the only
place a render actually occurs. What lives here is the *routing* check that
decides a company needs rendering at all (`has_job_like_content` /
`has_spa_root_or_ats_embed`, used by `route_extraction`), not the rendering
itself.

Board-token extraction (spec §5.2) lives in `discovery.ats_platforms` — shared
with Stage 3, since an ATS-API adapter needs to be able to resolve the same
token from a `CareersSource.careers_url` directly (see
`discovery.extraction.greenhouse` for why). Starting map only; verify against
current vendor URL formats before relying on a new platform's pattern.
"""

from __future__ import annotations

import re
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

_ADAPTER_ROUTING_CONFIDENCE_FLOOR = 0.8  # spec §5.3


async def identify_ats(
    source: CareersSource,
    fetcher: Fetcher,
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
        result = await fetcher.get(source.careers_url)
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

    # TODO(later phase): Signal 5 — feed a rendered-DOM adapter's captured
    # XHR targets back into Stage 2 to catch JS-injected boards that turn
    # out to be a *known* ATS with no static markers. The rendered-DOM
    # adapter (discovery.extraction.rendered_dom) already captures and
    # inspects XHR/fetch responses for its own purpose (spec §6.2.3's
    # endpoint-learning), but feeding that back into this function would
    # restructure Stage 2 into a two-pass flow — out of scope here.
    # TODO(later phase): Signal 6 — DNS/CNAME lookup for white-labelled boards.

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
