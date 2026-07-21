"""Structured-HTML (JSON-LD) adapter tests — spec §6.2.2.

Previously untested directly (only exercised indirectly via ATS-detection
routing and normalisation tests) — added as part of the final audit's test
coverage remediation.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult, RobotsDisallowedError
from gtm_agent.discovery.extraction.jsonld import JsonLdAdapter
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.results import ExtractionStatus


def _source(url: str) -> CareersSource:
    return CareersSource(
        company_id="acme",
        careers_url=url,
        resolution_strategy=ResolutionStrategy.HOMEPAGE_LINK,
        resolution_confidence=0.9,
        created_at=datetime.now(UTC),
    )


def _result(url: str, status_code: int, text: str) -> FetchResult:
    return FetchResult(url=url, status_code=status_code, text=text, headers=httpx.Headers({}))


class FakeFetcher:
    def __init__(
        self,
        responses: dict[str, FetchResult] | None = None,
        raise_error_for: dict[str, Exception] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.raise_error_for = raise_error_for or {}

    async def get(self, url: str, **kwargs: object) -> FetchResult:
        if url in self.raise_error_for:
            raise self.raise_error_for[url]
        if url not in self.responses:
            raise AssertionError(f"unexpected URL requested: {url}")
        return self.responses[url]


@pytest.fixture
def adapter() -> JsonLdAdapter:
    return JsonLdAdapter()


_JOB_POSTING_HTML = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "JobPosting", "title": "Backend Engineer",
 "identifier": {"value": "job-42"}, "url": "https://acme.com/jobs/42"}
</script>
</head></html>
"""


async def test_discover_extracts_jobposting_objects(adapter: JsonLdAdapter) -> None:
    url = "https://acme.com/careers"
    fetcher = FakeFetcher({url: _result(url, 200, _JOB_POSTING_HTML)})

    result = await adapter.discover(_source(url), fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert len(result.value) == 1
    assert result.value[0].source_job_id == "job-42"
    assert result.value[0].posting_url == "https://acme.com/jobs/42"
    assert result.value[0].is_hydrated is True


async def test_discover_ignores_non_jobposting_jsonld(adapter: JsonLdAdapter) -> None:
    html = """
    <script type="application/ld+json">
    {"@type": "Organization", "name": "Acme"}
    </script>
    """
    url = "https://acme.com/careers"
    fetcher = FakeFetcher({url: _result(url, 200, html)})

    result = await adapter.discover(_source(url), fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_handles_graph_wrapped_jsonld(adapter: JsonLdAdapter) -> None:
    html = json.dumps(
        {
            "@graph": [
                {"@type": "JobPosting", "title": "A", "url": "https://acme.com/a"},
                {"@type": "JobPosting", "title": "B", "url": "https://acme.com/b"},
            ]
        }
    )
    url = "https://acme.com/careers"
    fetcher = FakeFetcher({url: _result(url, 200, f'<script type="application/ld+json">{html}</script>')})

    result = await adapter.discover(_source(url), fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert len(result.value) == 2


async def test_discover_malformed_json_is_skipped_not_fatal(adapter: JsonLdAdapter) -> None:
    html = '<script type="application/ld+json">{not valid json</script>'
    url = "https://acme.com/careers"
    fetcher = FakeFetcher({url: _result(url, 200, html)})

    result = await adapter.discover(_source(url), fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_blocked_403(adapter: JsonLdAdapter) -> None:
    url = "https://acme.com/careers"
    fetcher = FakeFetcher({url: _result(url, 403, "")})

    result = await adapter.discover(_source(url), fetcher)

    assert result.status == ExtractionStatus.BLOCKED_403


async def test_discover_robots_disallowed(adapter: JsonLdAdapter) -> None:
    url = "https://acme.com/careers"
    fetcher = FakeFetcher(raise_error_for={url: RobotsDisallowedError(f"{url} disallowed")})

    result = await adapter.discover(_source(url), fetcher)

    assert result.status == ExtractionStatus.ROBOTS_DISALLOWED


async def test_hydrate_is_a_noop(adapter: JsonLdAdapter) -> None:
    from gtm_agent.models.job import RawPosting

    posting = RawPosting(
        company_id="acme", source_platform="jsonld", raw_payload={"title": "X"}, fetched_at=datetime.now(UTC)
    )
    result = await adapter.hydrate(posting, FakeFetcher())
    assert result is posting
