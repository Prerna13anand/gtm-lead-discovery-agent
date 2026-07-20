"""Stage 3 Lever adapter tests — fixture-based, no network (spec §20.1).

Mirrors tests/test_greenhouse_adapter.py in structure; see that file for the
rationale behind each case. Differences follow from Lever's actual shape
(verified live during Phase 2B): a bare JSON array at the top level instead
of a `{"jobs": [...]}` wrapper, and no HTML-entity double-escaping to undo.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult
from gtm_agent.discovery.extraction.lever import LeverAdapter, _postings_url
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.results import ExtractionStatus

FIXTURES = Path(__file__).parent / "fixtures" / "lever"
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
def adapter() -> LeverAdapter:
    return LeverAdapter()


async def test_discover_success_maps_fields(adapter: LeverAdapter) -> None:
    postings_url = _postings_url("acme")
    fetcher = FakeFetcher({postings_url: _result(postings_url, 200, SAMPLE_BOARD_JSON)})
    source = _source("https://jobs.lever.co/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert len(result.value) == 2

    first = result.value[0]
    assert first.company_id == "acme"
    assert first.source_platform == "lever"
    assert first.source_job_id == "e0c43af6-5bc2-4a3a-a3c6-9b6448e1d594"
    assert first.posting_url == "https://jobs.lever.co/acme/e0c43af6-5bc2-4a3a-a3c6-9b6448e1d594"
    assert first.is_hydrated is True

    # raw_payload preserves the native Lever shape untouched (spec §6.4) — no
    # unescaping needed, unlike Greenhouse's `content` field.
    assert first.raw_payload["text"] == "Senior Backend Engineer"
    assert first.raw_payload["workplaceType"] == "remote"
    assert first.raw_payload["categories"]["department"] == "Engineering"
    assert "<h3>" in first.raw_payload["description"]


async def test_token_resolved_directly_from_careers_url_without_extra_fetch(adapter: LeverAdapter) -> None:
    postings_url = _postings_url("acme")
    fetcher = FakeFetcher({postings_url: _result(postings_url, 200, SAMPLE_BOARD_JSON)})
    source = _source("https://jobs.lever.co/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    # exactly one request — the postings API — since the token was already in the URL
    assert fetcher.requested_urls == [postings_url]


async def test_token_resolution_falls_back_to_redirect_target(adapter: LeverAdapter) -> None:
    careers_url = "https://acme.com/careers"
    redirect_target = "https://jobs.lever.co/acme"
    postings_url = _postings_url("acme")

    fetcher = FakeFetcher(
        {
            careers_url: _result(redirect_target, 200, "<html>redirected</html>"),
            postings_url: _result(postings_url, 200, SAMPLE_BOARD_JSON),
        }
    )
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert fetcher.requested_urls == [careers_url, postings_url]


async def test_token_unresolvable_returns_board_not_found_without_hitting_postings_api(
    adapter: LeverAdapter,
) -> None:
    careers_url = "https://acme.com/careers"
    fetcher = FakeFetcher({careers_url: _result(careers_url, 200, "<html>no ats here</html>")})
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND
    assert result.value is None
    assert all("api.lever.co" not in url for url in fetcher.requested_urls)


async def test_discover_empty_board_is_a_real_success_not_a_failure(adapter: LeverAdapter) -> None:
    postings_url = _postings_url("acme")
    fetcher = FakeFetcher({postings_url: _result(postings_url, 200, "[]")})
    source = _source("https://jobs.lever.co/acme")

    result = await adapter.discover(source, fetcher)

    # spec §2.3: a validated, empty board is real information, not a failure.
    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_404_returns_board_not_found(adapter: LeverAdapter) -> None:
    postings_url = _postings_url("acme")
    fetcher = FakeFetcher(
        {postings_url: _result(postings_url, 404, json.dumps({"ok": False, "error": "Document not found"}))}
    )
    source = _source("https://jobs.lever.co/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND


async def test_discover_blocked_403(adapter: LeverAdapter) -> None:
    postings_url = _postings_url("acme")
    fetcher = FakeFetcher({postings_url: _result(postings_url, 403, "")})
    source = _source("https://jobs.lever.co/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BLOCKED_403


async def test_discover_malformed_json_returns_schema_violation(adapter: LeverAdapter) -> None:
    postings_url = _postings_url("acme")
    fetcher = FakeFetcher({postings_url: _result(postings_url, 200, "[not valid json")})
    source = _source("https://jobs.lever.co/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_non_array_body_returns_schema_violation(adapter: LeverAdapter) -> None:
    postings_url = _postings_url("acme")
    fetcher = FakeFetcher({postings_url: _result(postings_url, 200, json.dumps({"unexpected": "shape"}))})
    source = _source("https://jobs.lever.co/acme")

    result = await adapter.discover(source, fetcher)

    # spec §17: an ATS API shape change must fail loudly, never be mistaken for zero jobs.
    # Lever's top level is a bare array, so a dict body means the shape changed.
    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_hydrate_is_a_noop(adapter: LeverAdapter) -> None:
    from gtm_agent.models.job import RawPosting

    posting = RawPosting(
        company_id="acme",
        source_platform="lever",
        source_job_id="e0c43af6-5bc2-4a3a-a3c6-9b6448e1d594",
        posting_url="https://jobs.lever.co/acme/e0c43af6-5bc2-4a3a-a3c6-9b6448e1d594",
        raw_payload={"text": "Engineer"},
        fetched_at=datetime.now(UTC),
        is_hydrated=True,
    )
    fetcher = FakeFetcher()  # no responses registered — a real call would fail loudly

    result = await adapter.hydrate(posting, fetcher)

    assert result is posting
    assert fetcher.requested_urls == []
