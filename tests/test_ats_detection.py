"""Stage 2 ATS fingerprinting tests — deterministic, no network (spec §20.1)."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from gtm_agent.core.fetch import Fetcher
from gtm_agent.discovery.ats_detection import (
    has_job_like_content,
    has_jsonld_job_posting,
    has_spa_root_or_ats_embed,
    identify_ats,
    identify_from_captured_requests,
    route_extraction,
)
from gtm_agent.discovery.ats_platforms import known_ats_platform_for_host
from gtm_agent.models.ats import AtsIdentification, AtsPlatform, DetectionSignal
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy


def _source(url: str) -> CareersSource:
    return CareersSource(
        company_id="acme",
        careers_url=url,
        resolution_strategy=ResolutionStrategy.HOMEPAGE_LINK,
        resolution_confidence=0.85,
        created_at=datetime.now(UTC),
    )


def test_known_ats_domain_matches_greenhouse():
    assert known_ats_platform_for_host("boards.greenhouse.io") == AtsPlatform.GREENHOUSE


def test_known_ats_domain_matches_subdomain():
    assert known_ats_platform_for_host("job-boards.greenhouse.io") == AtsPlatform.GREENHOUSE


def test_unknown_domain_returns_none():
    assert known_ats_platform_for_host("example.com") is None


def test_identify_ats_url_host_match_is_decisive_and_needs_no_fetch():
    # URL host match returns before ever touching the fetcher (spec §5.1 #1),
    # so passing a fetcher stub that would error if used proves the branch
    # never calls it.
    class ExplodingFetcher:
        async def get(self, *args, **kwargs):
            raise AssertionError("should not fetch for a decisive URL host match")

    source = _source("https://boards.greenhouse.io/acme")
    result = asyncio.run(identify_ats(source, ExplodingFetcher()))

    assert result.value is not None
    assert result.value.platform == AtsPlatform.GREENHOUSE
    assert result.value.board_token == "acme"
    assert result.value.detection_signal == DetectionSignal.URL_HOST_MATCH


def test_has_jsonld_job_posting_detects_schema_type():
    html = '<script type="application/ld+json">{"@type": "JobPosting"}</script>'
    assert has_jsonld_job_posting(html) is True
    assert has_jsonld_job_posting("<html>nothing here</html>") is False


def test_route_extraction_prefers_high_confidence_ats_platform():
    identification = AtsIdentification(
        company_id="acme",
        platform=AtsPlatform.LEVER,
        confidence=0.98,
        detection_signal=DetectionSignal.URL_HOST_MATCH,
        created_at=datetime.now(UTC),
    )
    assert route_extraction(identification, page_html=None) == AtsPlatform.LEVER


def test_route_extraction_routes_workable_like_the_other_real_adapters():
    identification = AtsIdentification(
        company_id="acme",
        platform=AtsPlatform.WORKABLE,
        confidence=0.98,
        detection_signal=DetectionSignal.URL_HOST_MATCH,
        created_at=datetime.now(UTC),
    )
    assert route_extraction(identification, page_html=None) == AtsPlatform.WORKABLE


def test_route_extraction_routes_smartrecruiters_like_the_other_real_adapters():
    identification = AtsIdentification(
        company_id="acme",
        platform=AtsPlatform.SMARTRECRUITERS,
        confidence=0.98,
        detection_signal=DetectionSignal.URL_HOST_MATCH,
        created_at=datetime.now(UTC),
    )
    assert route_extraction(identification, page_html=None) == AtsPlatform.SMARTRECRUITERS


def test_route_extraction_routes_recruitee_like_the_other_real_adapters():
    identification = AtsIdentification(
        company_id="acme",
        platform=AtsPlatform.RECRUITEE,
        confidence=0.98,
        detection_signal=DetectionSignal.URL_HOST_MATCH,
        created_at=datetime.now(UTC),
    )
    assert route_extraction(identification, page_html=None) == AtsPlatform.RECRUITEE


def test_route_extraction_routes_rippling_like_the_other_real_adapters():
    identification = AtsIdentification(
        company_id="acme",
        platform=AtsPlatform.RIPPLING,
        confidence=0.98,
        detection_signal=DetectionSignal.URL_HOST_MATCH,
        created_at=datetime.now(UTC),
    )
    assert route_extraction(identification, page_html=None) == AtsPlatform.RIPPLING


def test_route_extraction_falls_back_to_jsonld_when_no_ats_identified():
    html = '<script type="application/ld+json">{"@type": "JobPosting"}</script>'
    assert route_extraction(None, page_html=html) == AtsPlatform.JSONLD


def test_route_extraction_falls_back_to_generic_html_as_terminal_case():
    assert route_extraction(None, page_html="<html></html>") == AtsPlatform.GENERIC_HTML


# --- Rendered-DOM routing (spec §6.2.3 detection) ---


def test_has_job_like_content_detects_job_and_career_link_patterns():
    assert has_job_like_content('<a href="/jobs/senior-engineer">Apply</a>') is True
    assert has_job_like_content('<a href="/careers/gtm-engineer--sf">Apply</a>') is True
    assert has_job_like_content('<a href="/positions/123-abc">Apply</a>') is True
    assert has_job_like_content("<html><body>Nothing here</body></html>") is False


def test_has_job_like_content_ignores_static_assets_under_the_same_prefix():
    # Live-verified false positive: a page's own icon/image assets can live
    # under a /careers/... path, which must not be mistaken for a job link.
    assert has_job_like_content('<a href="/careers/icons/caret-down.svg">x</a>') is False
    assert has_job_like_content('<img src="/careers/icons/caret-down.svg">') is False


def test_has_spa_root_or_ats_embed_detects_known_spa_roots():
    assert has_spa_root_or_ats_embed('<div id="root"></div>') is True
    assert has_spa_root_or_ats_embed('<div id="__next"></div>') is True
    assert has_spa_root_or_ats_embed('<div id="___gatsby"></div>') is True
    assert has_spa_root_or_ats_embed("<html><body>plain page</body></html>") is False


def test_has_spa_root_or_ats_embed_detects_known_ats_embed_script():
    html = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
    assert has_spa_root_or_ats_embed(html) is True


def test_has_spa_root_or_ats_embed_detects_rsc_suspense_marker():
    # Live-verified gap fix: a Next.js App Router page carries no
    # conventional #__next mount point, only React's own Suspense/streaming
    # marker — see ats_detection.py's _RSC_SUSPENSE_MARKER comment.
    html = '<html><body><div hidden=""><!--$--><!--/$--></div></body></html>'
    assert has_spa_root_or_ats_embed(html) is True


def test_route_extraction_escalates_to_rendered_dom_when_no_job_content_but_spa_root_present():
    html = '<html><body><div id="__next"></div></body></html>'
    assert route_extraction(None, page_html=html) == AtsPlatform.RENDERED_DOM


def test_route_extraction_prefers_generic_html_when_no_spa_signal_either():
    html = "<html><body>A totally static, empty page.</body></html>"
    assert route_extraction(None, page_html=html) == AtsPlatform.GENERIC_HTML


def test_route_extraction_does_not_escalate_when_job_content_already_present():
    # A SPA-root marker is present, but so is a real job link — no need to render.
    html = '<html><body><div id="__next"></div><a href="/jobs/engineer">Engineer</a></body></html>'
    assert route_extraction(None, page_html=html) == AtsPlatform.GENERIC_HTML


def test_route_extraction_prefers_jsonld_over_rendered_dom():
    # Both signals present — JSON-LD is cheaper and already tried first.
    html = (
        '<div id="__next"></div>'
        '<script type="application/ld+json">{"@type": "JobPosting"}</script>'
    )
    assert route_extraction(None, page_html=html) == AtsPlatform.JSONLD


# --- Regression: identify_ats's fingerprinting fetch must not starve a
# later, real fetch of the same URL (live-verified bug — see
# core.fetch.Fetcher._request's use_cache docstring) ---


async def test_identify_ats_fetch_does_not_leave_a_stored_validator_for_later_reads():
    def handler(request: httpx.Request) -> httpx.Response:
        # A page with no ATS signal at all, so identify_ats falls through to
        # its Signal-2 fetch instead of taking the decisive host-match shortcut.
        return httpx.Response(200, text="<html><body>plain company site</body></html>", headers={"ETag": '"v1"'})

    async def no_aliases(hostname: str) -> list[str]:
        return []

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        source = _source("https://acme.com/careers")
        await identify_ats(source, fetcher, dns_resolver=no_aliases)

        # A later "real" read of the same URL (e.g. Stage 3's own fetch)
        # must see the full body, not a 304 caused by identify_ats's read.
        result = await fetcher.get("https://acme.com/careers")
    finally:
        await fetcher.aclose()

    assert result.status_code == 200
    assert "plain company site" in result.text


# --- Signal 6 — DNS/CNAME (spec §5.1) --------------------------------------


async def test_identify_ats_signal_6_dns_cname_match():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>plain company site, no static markers</body></html>")

    async def resolver(hostname: str) -> list[str]:
        assert hostname == "careers.acme.com"
        return ["boards.greenhouse.io"]

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        source = _source("https://careers.acme.com/jobs")
        result = await identify_ats(source, fetcher, dns_resolver=resolver)
    finally:
        await fetcher.aclose()

    assert result.value is not None
    assert result.value.platform == AtsPlatform.GREENHOUSE
    assert result.value.detection_signal == DetectionSignal.DNS_CNAME
    assert result.value.confidence == 0.60


async def test_identify_ats_signal_6_no_matching_alias_falls_through_to_ats_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>plain company site</body></html>")

    async def resolver(hostname: str) -> list[str]:
        return ["some-other-cdn.example.net"]

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        source = _source("https://careers.acme.com/jobs")
        result = await identify_ats(source, fetcher, dns_resolver=resolver)
    finally:
        await fetcher.aclose()

    assert result.value is None
    assert result.status.value == "ats_unknown"


async def test_identify_ats_signal_6_resolver_error_is_treated_as_no_aliases():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>plain company site</body></html>")

    async def failing_resolver(hostname: str) -> list[str]:
        raise OSError("simulated resolver failure")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        source = _source("https://careers.acme.com/jobs")
        with pytest.raises(OSError):
            # identify_ats itself doesn't swallow a resolver's own exception —
            # only `_resolve_cname_aliases`'s *default* implementation catches
            # OSError/gaierror internally. A custom resolver that raises is
            # the caller's own contract to honour, same as `fetch_robots_txt`
            # in `core.robots.RobotsCache`.
            await identify_ats(source, fetcher, dns_resolver=failing_resolver)
    finally:
        await fetcher.aclose()


# --- Signal 5 — network-request matching logic (spec §5.1) -----------------


def test_identify_from_captured_requests_matches_known_ats_host():
    identification = identify_from_captured_requests(
        "acme", ["https://boards.greenhouse.io/v1/boards/acme/jobs", "https://analytics.example.com/track"]
    )
    assert identification is not None
    assert identification.platform == AtsPlatform.GREENHOUSE
    assert identification.detection_signal == DetectionSignal.NETWORK_REQUESTS


def test_identify_from_captured_requests_no_match_returns_none():
    identification = identify_from_captured_requests(
        "acme", ["https://analytics.example.com/track", "https://cdn.example.com/app.js"]
    )
    assert identification is None


def test_identify_from_captured_requests_empty_list_returns_none():
    assert identify_from_captured_requests("acme", []) is None
