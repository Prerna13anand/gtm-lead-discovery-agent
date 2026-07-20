"""Stage 3 Greenhouse adapter tests — fixture-based, no network (spec §20.1)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult
from gtm_agent.discovery.extraction.greenhouse import GreenhouseAdapter, _board_url
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.results import ExtractionStatus

FIXTURES = Path(__file__).parent / "fixtures" / "greenhouse"
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
def adapter() -> GreenhouseAdapter:
    return GreenhouseAdapter()


async def test_discover_success_maps_fields_and_unescapes_content(adapter: GreenhouseAdapter) -> None:
    board_url = _board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, SAMPLE_BOARD_JSON)})
    source = _source("https://job-boards.greenhouse.io/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert len(result.value) == 2

    first = result.value[0]
    assert first.company_id == "acme"
    assert first.source_platform == "greenhouse"
    assert first.source_job_id == "1111111"
    assert first.posting_url == "https://job-boards.greenhouse.io/acme/jobs/1111111"
    assert first.is_hydrated is True

    # content must be unescaped once (spec: live-verified double-entity-escaping)
    assert "&lt;" not in first.raw_payload["content"]
    assert "<div>" in first.raw_payload["content"]
    assert "5+ years experience" in first.raw_payload["content"]

    # raw_payload preserves the native Greenhouse shape untouched otherwise (spec §6.4)
    assert first.raw_payload["title"] == "Senior Backend Engineer"
    assert first.raw_payload["departments"][0]["name"] == "Engineering"


async def test_token_resolved_directly_from_careers_url_without_extra_fetch(adapter: GreenhouseAdapter) -> None:
    board_url = _board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, SAMPLE_BOARD_JSON)})
    source = _source("https://job-boards.greenhouse.io/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    # exactly one request — the jobs API — since the token was already in the URL
    assert fetcher.requested_urls == [board_url]


async def test_token_resolution_falls_back_to_redirect_target(adapter: GreenhouseAdapter) -> None:
    careers_url = "https://acme.com/careers"
    redirect_target = "https://job-boards.greenhouse.io/acme"
    board_url = _board_url("acme")

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


async def test_token_unresolvable_returns_board_not_found_without_hitting_jobs_api(
    adapter: GreenhouseAdapter,
) -> None:
    careers_url = "https://acme.com/careers"
    fetcher = FakeFetcher({careers_url: _result(careers_url, 200, "<html>no ats here</html>")})
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND
    assert result.value is None
    assert all("boards-api.greenhouse.io" not in url for url in fetcher.requested_urls)


async def test_discover_empty_board_is_a_real_success_not_a_failure(adapter: GreenhouseAdapter) -> None:
    board_url = _board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, json.dumps({"jobs": [], "meta": {"total": 0}}))})
    source = _source("https://job-boards.greenhouse.io/acme")

    result = await adapter.discover(source, fetcher)

    # spec §2.3: a validated, empty board is real information, not a failure.
    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_404_returns_board_not_found(adapter: GreenhouseAdapter) -> None:
    board_url = _board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 404, "")})
    source = _source("https://job-boards.greenhouse.io/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND


async def test_discover_blocked_403(adapter: GreenhouseAdapter) -> None:
    board_url = _board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 403, "")})
    source = _source("https://job-boards.greenhouse.io/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BLOCKED_403


async def test_discover_malformed_json_returns_schema_violation(adapter: GreenhouseAdapter) -> None:
    board_url = _board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, "{not valid json")})
    source = _source("https://job-boards.greenhouse.io/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_missing_jobs_key_returns_schema_violation(adapter: GreenhouseAdapter) -> None:
    board_url = _board_url("acme")
    fetcher = FakeFetcher({board_url: _result(board_url, 200, json.dumps({"meta": {"total": 0}}))})
    source = _source("https://job-boards.greenhouse.io/acme")

    result = await adapter.discover(source, fetcher)

    # spec §17: an ATS API shape change must fail loudly, never be mistaken for zero jobs.
    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_hydrate_is_a_noop(adapter: GreenhouseAdapter) -> None:
    from gtm_agent.models.job import RawPosting

    posting = RawPosting(
        company_id="acme",
        source_platform="greenhouse",
        source_job_id="1",
        posting_url="https://job-boards.greenhouse.io/acme/jobs/1",
        raw_payload={"title": "Engineer"},
        fetched_at=datetime.now(UTC),
        is_hydrated=True,
    )
    fetcher = FakeFetcher()  # no responses registered — a real call would fail loudly

    result = await adapter.hydrate(posting, fetcher)

    assert result is posting
    assert fetcher.requested_urls == []
