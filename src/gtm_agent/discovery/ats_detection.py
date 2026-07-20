"""Stage 2 — ATS Fingerprinting (spec §5).

Goal: identify which ATS hosts the company's board, and extract the board
token needed to call its API.

Phase 1 implements detection signals 1-4 (URL host match, redirect target,
embedded script/iframe, DOM markers) for Greenhouse, Lever, and Ashby.

NOT implemented in Phase 1 (left as TODOs):
    - Signal 5, network-request inspection during Playwright rendering —
      depends on the rendered-DOM adapter, which doesn't exist until Phase 2.
    - Signal 6, DNS/CNAME lookups for white-labelled boards.

Board-token extraction (spec §5.2) lives in `discovery.ats_platforms` — shared
with Stage 3, since an ATS-API adapter needs to be able to resolve the same
token from a `CareersSource.careers_url` directly (see
`discovery.extraction.greenhouse` for why). Starting map only; verify against
current vendor URL formats before relying on a new platform's pattern.
"""

from __future__ import annotations

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

    # TODO(phase 2): Signal 5 — inspect XHR/fetch targets during Playwright
    # rendering. Catches JS-injected boards with no static markers.
    # TODO(phase 2): Signal 6 — DNS/CNAME lookup for white-labelled boards.

    logger.info("ats_unknown", company_id=source.company_id, url=source.careers_url)
    return StageResult(status=AtsFingerprintStatus.ATS_UNKNOWN, detail="no detection signal matched")


def has_jsonld_job_posting(html: str) -> bool:
    """Cheap check used by routing (spec §5.3): does the page carry schema.org/JobPosting JSON-LD?"""
    return 'application/ld+json' in html and "JobPosting" in html


def route_extraction(identification: AtsIdentification | None, page_html: str | None = None) -> AtsPlatform:
    """Decide which adapter family should handle extraction — spec §5.3.

    `identification` is None when Stage 2 didn't identify a platform at all
    (`ats_unknown`) — routing then falls through to the JSON-LD check and
    finally the generic-HTML terminal fallback.

    `page_html` is the already-fetched careers page body, if available, used
    for the JSON-LD check. Rendered-DOM routing is not reachable in Phase 1
    (no Playwright adapter exists yet) — the routing decision is still made
    here so Phase 2 only has to add the adapter, not the branch.
    """
    if identification is not None and identification.platform in (
        AtsPlatform.GREENHOUSE,
        AtsPlatform.LEVER,
        AtsPlatform.ASHBY,
    ):
        if identification.confidence >= _ADAPTER_ROUTING_CONFIDENCE_FLOOR:
            return identification.platform

    if page_html is not None and has_jsonld_job_posting(page_html):
        return AtsPlatform.JSONLD

    # TODO(phase 2): route to AtsPlatform.RENDERED_DOM when static fetch shows
    # no job-like content but a known SPA root or ATS embed script is present.

    return AtsPlatform.GENERIC_HTML
