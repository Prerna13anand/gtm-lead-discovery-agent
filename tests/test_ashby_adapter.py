"""Stage 3 Ashby adapter tests — fixture-based, no network (spec §20.1).

Mirrors tests/test_greenhouse_adapter.py and tests/test_lever_adapter.py in
structure; see those files for the rationale behind each case. Differences
follow from Ashby's actual shape (verified live during Phase 2C): a
`{"jobs": [...], "apiVersion": N}` wrapper (like Greenhouse, unlike Lever's
bare array), and a plain-text 404 body rather than a JSON error object.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult, RobotsDisallowedError
from gtm_agent.discovery.extraction.ashby import AshbyAdapter, _job_board_url
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.results import ExtractionStatus

FIXTURES = Path(__file__).parent / "fixtures" / "ashby"
SAMPLE_BOARD_JSON = (FIXTURES / "sample_board.json").read_text(encoding="utf-8")


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
        raise_error_for: dict[str, Exception] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.raise_for = raise_for or set()
        self.raise_error_for = raise_error_for or {}
        self.requested_urls: list[str] = []

    async def get(self, url: str, **kwargs: object) -> FetchResult:
        self.requested_urls.append(url)
        if url in self.raise_error_for:
            raise self.raise_error_for[url]
        if url in self.raise_for:
            raise FetchError(f"simulated failure for {url}")
        if url not in self.responses:
            raise AssertionError(f"unexpected URL requested: {url}")
        return self.responses[url]


@pytest.fixture
def adapter() -> AshbyAdapter:
    return AshbyAdapter()


async def test_discover_success_maps_fields(adapter: AshbyAdapter) -> None:
    board_url = _job_board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, SAMPLE_BOARD_JSON)})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert len(result.value) == 2

    first = result.value[0]
    assert first.company_id == "acme"
    assert first.source_platform == "ashby"
    assert first.source_job_id == "d3bc1ced-3ce4-4086-a050-555055dbb1ff"
    assert first.posting_url == "https://jobs.ashbyhq.com/acme/d3bc1ced-3ce4-4086-a050-555055dbb1ff"
    assert first.is_hydrated is True

    # raw_payload preserves the native Ashby shape untouched (spec §6.4) — no
    # unescaping needed, same as Lever, unlike Greenhouse's `content` field.
    assert first.raw_payload["title"] == "Senior Backend Engineer"
    assert first.raw_payload["workplaceType"] == "Remote"
    assert first.raw_payload["department"] == "Engineering"
    assert "<p>" in first.raw_payload["descriptionHtml"]


async def test_token_resolved_directly_from_careers_url_without_extra_fetch(adapter: AshbyAdapter) -> None:
    board_url = _job_board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, SAMPLE_BOARD_JSON)})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    # exactly one request — the job-board API — since the token was already in the URL
    assert fetcher.requested_urls == [board_url]


async def test_token_resolution_falls_back_to_redirect_target(adapter: AshbyAdapter) -> None:
    careers_url = "https://acme.com/careers"
    redirect_target = "https://jobs.ashbyhq.com/acme"
    board_url = _job_board_url("acme")

    fetcher = FakeFetcher(
        {
            careers_url: _result(redirect_target, 200, "<html>redirected</html>"),
            board_url: _result(board_url, 200, SAMPLE_BOARD_JSON),
        }
    )
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert fetcher.requested_urls == [careers_url, board_url]


async def test_token_unresolvable_returns_board_not_found_without_hitting_job_board_api(
    adapter: AshbyAdapter,
) -> None:
    careers_url = "https://acme.com/careers"
    fetcher = FakeFetcher({careers_url: _result(careers_url, 200, "<html>no ats here</html>")})
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND
    assert result.value is None
    assert all("api.ashbyhq.com" not in url for url in fetcher.requested_urls)


async def test_discover_empty_board_is_a_real_success_not_a_failure(adapter: AshbyAdapter) -> None:
    board_url = _job_board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, json.dumps({"jobs": [], "apiVersion": 1}))})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    # spec §2.3: a validated, empty board is real information, not a failure.
    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_404_returns_board_not_found(adapter: AshbyAdapter) -> None:
    board_url = _job_board_url("acme")
    # Ashby's real 404 body is plain text ("Not Found"), not JSON.
    fetcher = FakeFetcher({board_url: _result(board_url, 404, "Not Found")})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND


async def test_discover_blocked_403(adapter: AshbyAdapter) -> None:
    board_url = _job_board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 403, "")})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BLOCKED_403


async def test_discover_robots_disallowed(adapter: AshbyAdapter) -> None:
    board_url = _job_board_url("acme")
    fetcher = FakeFetcher(raise_error_for={board_url: RobotsDisallowedError(f"{board_url} disallowed")})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.ROBOTS_DISALLOWED


async def test_discover_malformed_json_returns_schema_violation(adapter: AshbyAdapter) -> None:
    board_url = _job_board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, "{not valid json")})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_missing_jobs_key_returns_schema_violation(adapter: AshbyAdapter) -> None:
    board_url = _job_board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, json.dumps({"apiVersion": 1}))})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    # spec §17: an ATS API shape change must fail loudly, never be mistaken for zero jobs.
    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_non_dict_body_returns_schema_violation(adapter: AshbyAdapter) -> None:
    board_url = _job_board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, json.dumps(["unexpected", "shape"]))})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_304_is_treated_as_unchanged_not_an_error(adapter: AshbyAdapter) -> None:
    # A conditional request (spec §6.3) returns 304 with an empty body — this
    # must not be parsed as JSON and must not be mistaken for a schema violation.
    board_url = _job_board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 304, "")})
    source = _source("https://jobs.ashbyhq.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_hydrate_is_a_noop(adapter: AshbyAdapter) -> None:
    from gtm_agent.models.job import RawPosting

    posting = RawPosting(
        company_id="acme",
        source_platform="ashby",
        source_job_id="d3bc1ced-3ce4-4086-a050-555055dbb1ff",
        posting_url="https://jobs.ashbyhq.com/acme/d3bc1ced-3ce4-4086-a050-555055dbb1ff",
        raw_payload={"title": "Engineer"},
        fetched_at=datetime.now(UTC),
        is_hydrated=True,
    )
    fetcher = FakeFetcher()  # no responses registered — a real call would fail loudly

    result = await adapter.hydrate(posting, fetcher)

    assert result is posting
    assert fetcher.requested_urls == []
