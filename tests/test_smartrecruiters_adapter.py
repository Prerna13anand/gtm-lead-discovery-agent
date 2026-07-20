"""Stage 3 SmartRecruiters adapter tests — fixture-based, no network (spec §20.1).

Mirrors tests/test_greenhouse_adapter.py, tests/test_lever_adapter.py,
tests/test_ashby_adapter.py, and tests/test_workable_adapter.py in structure;
see those files for the rationale behind the shared cases.
SmartRecruiters-specific cases (offset pagination across multiple pages, the
list endpoint's missing posting URL, and the live-verified "unknown company
returns 200+empty rather than 404" discrepancy) are new — see
discovery/extraction/smartrecruiters.py's module docstring for what was
verified live and why.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult
from gtm_agent.discovery.extraction.smartrecruiters import SmartRecruitersAdapter, _postings_url
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus

FIXTURES = Path(__file__).parent / "fixtures" / "smartrecruiters"
SAMPLE_PAGE1_JSON = (FIXTURES / "sample_page1.json").read_text(encoding="utf-8")
SAMPLE_PAGE2_JSON = (FIXTURES / "sample_page2.json").read_text(encoding="utf-8")
SAMPLE_DETAIL_JSON = (FIXTURES / "sample_detail.json").read_text(encoding="utf-8")


def _source(url: str) -> CareersSource:
    return CareersSource(
        company_id="acme",
        careers_url=url,
        resolution_strategy=ResolutionStrategy.HOMEPAGE_LINK,
        resolution_confidence=0.95,
        created_at=datetime.now(UTC),
    )


def _result(url: str, status_code: int, text: str) -> FetchResult:
    return FetchResult(url=url, status_code=status_code, text=text, headers=httpx.Headers({}))


class FakeFetcher:
    """Serves canned responses by exact URL; raises AssertionError on any
    unexpected request so a test fails loudly if the adapter's request
    pattern changes unexpectedly.
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
        self.requested_urls.append(url)
        if url in self.raise_for:
            raise FetchError(f"simulated failure for {url}")
        if url not in self.responses:
            raise AssertionError(f"unexpected URL requested: {url}")
        return self.responses[url]


@pytest.fixture
def adapter() -> SmartRecruitersAdapter:
    return SmartRecruitersAdapter()


# --- discover() ---


async def test_discover_success_maps_fields(adapter: SmartRecruitersAdapter) -> None:
    page1_url = _postings_url("acme", 0)
    page2_url = _postings_url("acme", 2)
    fetcher = FakeFetcher(
        {
            page1_url: _result(page1_url, 200, SAMPLE_PAGE1_JSON),
            page2_url: _result(page2_url, 200, SAMPLE_PAGE2_JSON),
        }
    )
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert len(result.value) == 3

    first = result.value[0]
    assert first.company_id == "acme"
    assert first.source_platform == "smartrecruiters"
    assert first.source_job_id == "1000000000001"
    # no canonical URL on the list endpoint — stays None until hydrate() (spec Appendix A)
    assert first.posting_url is None
    assert first.is_hydrated is False

    # raw_payload preserves the native SmartRecruiters shape untouched otherwise (spec §6.4)
    assert first.raw_payload["name"] == "Packaging Engineer"
    assert first.raw_payload["location"]["city"] == "New York"
    assert first.raw_payload["ref"] == "https://api.smartrecruiters.com/v1/companies/acme/postings/1000000000001"


async def test_discover_paginates_across_offset_pages(adapter: SmartRecruitersAdapter) -> None:
    # totalFound=3 with 2 items on page 1 forces a second request at offset=2
    # (spec Appendix A: "Offset" pagination) — verified live against a
    # 301-posting real board; this fixture reproduces the same shape in miniature.
    page1_url = _postings_url("acme", 0)
    page2_url = _postings_url("acme", 2)
    fetcher = FakeFetcher(
        {
            page1_url: _result(page1_url, 200, SAMPLE_PAGE1_JSON),
            page2_url: _result(page2_url, 200, SAMPLE_PAGE2_JSON),
        }
    )
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert [p.source_job_id for p in result.value] == ["1000000000001", "1000000000002", "1000000000003"]
    assert fetcher.requested_urls == [page1_url, page2_url]


async def test_discover_single_page_when_totalFound_fits(adapter: SmartRecruitersAdapter) -> None:
    page1_url = _postings_url("acme", 0)
    body = json.dumps({"offset": 0, "limit": 100, "totalFound": 2, "content": json.loads(SAMPLE_PAGE1_JSON)["content"]})
    fetcher = FakeFetcher({page1_url: _result(page1_url, 200, body)})
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert len(result.value) == 2
    # no second page fetched — totalFound was fully covered by page 1
    assert fetcher.requested_urls == [page1_url]


async def test_token_resolved_directly_from_careers_url_without_extra_fetch(
    adapter: SmartRecruitersAdapter,
) -> None:
    page1_url = _postings_url("acme", 0)
    body = json.dumps({"offset": 0, "limit": 100, "totalFound": 0, "content": []})
    fetcher = FakeFetcher({page1_url: _result(page1_url, 200, body)})
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    # exactly one request — the postings API — since the token was already in the URL
    assert fetcher.requested_urls == [page1_url]


async def test_token_resolution_falls_back_to_redirect_target(adapter: SmartRecruitersAdapter) -> None:
    careers_url = "https://acme.com/careers"
    redirect_target = "https://careers.smartrecruiters.com/acme"
    page1_url = _postings_url("acme", 0)
    body = json.dumps({"offset": 0, "limit": 100, "totalFound": 0, "content": []})

    fetcher = FakeFetcher(
        {
            careers_url: _result(redirect_target, 200, "<html>redirected</html>"),
            page1_url: _result(page1_url, 200, body),
        }
    )
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert fetcher.requested_urls == [careers_url, page1_url]


async def test_token_unresolvable_returns_board_not_found_without_hitting_postings_api(
    adapter: SmartRecruitersAdapter,
) -> None:
    careers_url = "https://acme.com/careers"
    fetcher = FakeFetcher({careers_url: _result(careers_url, 200, "<html>no ats here</html>")})
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND
    assert result.value is None
    assert all("api.smartrecruiters.com" not in url for url in fetcher.requested_urls)


async def test_discover_zero_postings_is_success_not_board_not_found(adapter: SmartRecruitersAdapter) -> None:
    # Live-verified discrepancy (see module docstring): an unrestricted
    # company that genuinely has no open roles, AND an unknown company,
    # both return this exact shape. Indistinguishable — treated as a real,
    # validated empty board per spec §2.3, not guessed at as "not found".
    page1_url = _postings_url("acme", 0)
    body = json.dumps({"offset": 0, "limit": 100, "totalFound": 0, "content": []})
    fetcher = FakeFetcher({page1_url: _result(page1_url, 200, body)})
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_404_returns_board_not_found_defensively(adapter: SmartRecruitersAdapter) -> None:
    # Not the live-observed path (see module docstring) but handled
    # defensively in case some other malformed input reaches this endpoint.
    page1_url = _postings_url("acme", 0)
    fetcher = FakeFetcher({page1_url: _result(page1_url, 404, "")})
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND


async def test_discover_blocked_403(adapter: SmartRecruitersAdapter) -> None:
    page1_url = _postings_url("acme", 0)
    fetcher = FakeFetcher({page1_url: _result(page1_url, 403, "")})
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BLOCKED_403


async def test_discover_malformed_json_returns_schema_violation(adapter: SmartRecruitersAdapter) -> None:
    page1_url = _postings_url("acme", 0)
    fetcher = FakeFetcher({page1_url: _result(page1_url, 200, "{not valid json")})
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_missing_content_key_returns_schema_violation(adapter: SmartRecruitersAdapter) -> None:
    page1_url = _postings_url("acme", 0)
    fetcher = FakeFetcher({page1_url: _result(page1_url, 200, json.dumps({"offset": 0, "limit": 100}))})
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    # spec §17: an ATS API shape change must fail loudly, never be mistaken for zero jobs.
    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_304_is_treated_as_unchanged_not_an_error(adapter: SmartRecruitersAdapter) -> None:
    # A conditional request (spec §6.3) returns 304 with an empty body — this
    # must not be parsed as JSON and must not be mistaken for a schema violation.
    page1_url = _postings_url("acme", 0)
    fetcher = FakeFetcher({page1_url: _result(page1_url, 304, "")})
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_304_on_second_page_stops_pagination_without_error(
    adapter: SmartRecruitersAdapter,
) -> None:
    page1_url = _postings_url("acme", 0)
    page2_url = _postings_url("acme", 2)
    fetcher = FakeFetcher(
        {
            page1_url: _result(page1_url, 200, SAMPLE_PAGE1_JSON),
            page2_url: _result(page2_url, 304, ""),
        }
    )
    source = _source("https://careers.smartrecruiters.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    # page 1's two postings are kept; page 2 contributed nothing but didn't error
    assert len(result.value) == 2


# --- hydrate() ---


def _posting(
    ref: str | None = "https://api.smartrecruiters.com/v1/companies/acme/postings/1000000000001",
) -> RawPosting:
    payload: dict[str, object] = {"id": "1000000000001", "name": "Packaging Engineer"}
    if ref is not None:
        payload["ref"] = ref
    return RawPosting(
        company_id="acme",
        source_platform="smartrecruiters",
        source_job_id="1000000000001",
        posting_url=None,
        raw_payload=payload,
        fetched_at=datetime.now(UTC),
        is_hydrated=False,
    )


async def test_hydrate_extracts_description_and_posting_url_and_marks_hydrated(
    adapter: SmartRecruitersAdapter,
) -> None:
    ref = "https://api.smartrecruiters.com/v1/companies/acme/postings/1000000000001"
    fetcher = FakeFetcher({ref: _result(ref, 200, SAMPLE_DETAIL_JSON)})

    result = await adapter.hydrate(_posting(ref), fetcher)

    assert result.is_hydrated is True
    assert result.posting_url == "https://jobs.smartrecruiters.com/Acme/1000000000001-packaging-engineer"
    assert "<p>Build packaging systems.</p>" in result.raw_payload["description"]
    assert "5+ years experience" in result.raw_payload["description"]
    # boilerplate sections are deliberately excluded (spec §7.6)
    assert "EEO statement" not in result.raw_payload["description"]
    assert "Acme builds things" not in result.raw_payload["description"]
    # original fields are preserved, not replaced
    assert result.raw_payload["name"] == "Packaging Engineer"


async def test_hydrate_missing_jobAd_returns_posting_unchanged(adapter: SmartRecruitersAdapter) -> None:
    ref = "https://api.smartrecruiters.com/v1/companies/acme/postings/1000000000001"
    fetcher = FakeFetcher({ref: _result(ref, 200, json.dumps({"id": "1000000000001", "name": "..."}))})
    original = _posting(ref)

    result = await adapter.hydrate(original, fetcher)

    assert result == original
    assert result.is_hydrated is False


async def test_hydrate_fetch_failure_returns_posting_unchanged(adapter: SmartRecruitersAdapter) -> None:
    ref = "https://api.smartrecruiters.com/v1/companies/acme/postings/1000000000001"
    fetcher = FakeFetcher(raise_for={ref})
    original = _posting(ref)

    result = await adapter.hydrate(original, fetcher)

    assert result == original


async def test_hydrate_http_error_returns_posting_unchanged(adapter: SmartRecruitersAdapter) -> None:
    ref = "https://api.smartrecruiters.com/v1/companies/acme/postings/1000000000001"
    fetcher = FakeFetcher({ref: _result(ref, 404, "")})
    original = _posting(ref)

    result = await adapter.hydrate(original, fetcher)

    assert result == original


async def test_hydrate_without_ref_is_a_noop(adapter: SmartRecruitersAdapter) -> None:
    original = _posting(ref=None)
    fetcher = FakeFetcher()  # no responses registered — a real call would fail loudly

    result = await adapter.hydrate(original, fetcher)

    assert result == original
    assert fetcher.requested_urls == []
