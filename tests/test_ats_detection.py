"""Stage 2 ATS fingerprinting tests — deterministic, no network (spec §20.1)."""

import asyncio
from datetime import UTC, datetime

import pytest

from gtm_agent.discovery.ats_detection import (
    has_jsonld_job_posting,
    identify_ats,
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
