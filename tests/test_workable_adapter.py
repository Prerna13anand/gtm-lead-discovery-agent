"""Stage 3 Workable adapter tests — fixture-based, no network (spec §20.1).

Mirrors tests/test_greenhouse_adapter.py, tests/test_lever_adapter.py, and
tests/test_ashby_adapter.py in structure; see those files for the rationale
behind the shared cases. Workable-specific cases (multi-location entries
sharing a shortcode, the `/j/{shortcode}` shortlink exclusion, and the
two-phase `hydrate()` meta-description extraction) are new — see
discovery/extraction/workable.py's module docstring for what was verified
live and why.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult
from gtm_agent.discovery.extraction.workable import WorkableAdapter, _widget_url
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus

FIXTURES = Path(__file__).parent / "fixtures" / "workable"
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
def adapter() -> WorkableAdapter:
    return WorkableAdapter()


# --- discover() ---


async def test_discover_success_maps_fields(adapter: WorkableAdapter) -> None:
    widget_url = _widget_url("acme")
    fetcher = FakeFetcher({widget_url: _result(widget_url, 200, SAMPLE_BOARD_JSON)})
    source = _source("https://apply.workable.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert len(result.value) == 3

    first = result.value[0]
    assert first.company_id == "acme"
    assert first.source_platform == "workable"
    assert first.source_job_id == "AB12CD34EF"
    assert first.posting_url == "https://apply.workable.com/j/AB12CD34EF"
    # two-phase: discover() alone never has a description (spec Appendix A)
    assert first.is_hydrated is False

    # raw_payload preserves the native Workable shape untouched otherwise (spec §6.4)
    assert first.raw_payload["title"] == "Senior Backend Engineer"
    assert first.raw_payload["department"] == "Engineering"
    assert first.raw_payload["city"] == "New York"


async def test_discover_multi_location_job_appears_once_per_location_entry(
    adapter: WorkableAdapter,
) -> None:
    # Verified live: a job open in several locations repeats in the jobs
    # array once per location, sharing the same shortcode/url. Not
    # deduplicated here — that's Stage 5 identity work (spec §8.1), not Stage 3.
    widget_url = _widget_url("acme")
    fetcher = FakeFetcher({widget_url: _result(widget_url, 200, SAMPLE_BOARD_JSON)})
    source = _source("https://apply.workable.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.value is not None
    shortcodes = [posting.source_job_id for posting in result.value]
    assert shortcodes == ["AB12CD34EF", "AB12CD34EF", "GH56IJ78KL"]
    assert result.value[0].raw_payload["city"] == "New York"
    assert result.value[1].raw_payload["city"] == "London"


async def test_token_resolved_directly_from_careers_url_without_extra_fetch(
    adapter: WorkableAdapter,
) -> None:
    widget_url = _widget_url("acme")
    fetcher = FakeFetcher({widget_url: _result(widget_url, 200, SAMPLE_BOARD_JSON)})
    source = _source("https://apply.workable.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    # exactly one request — the widget API — since the token was already in the URL
    assert fetcher.requested_urls == [widget_url]


async def test_token_resolution_falls_back_to_redirect_target(adapter: WorkableAdapter) -> None:
    careers_url = "https://acme.com/careers"
    redirect_target = "https://apply.workable.com/acme"
    widget_url = _widget_url("acme")

    fetcher = FakeFetcher(
        {
            careers_url: _result(redirect_target, 200, "<html>redirected</html>"),
            widget_url: _result(widget_url, 200, SAMPLE_BOARD_JSON),
        }
    )
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert fetcher.requested_urls == [careers_url, widget_url]


async def test_token_unresolvable_returns_board_not_found_without_hitting_widget_api(
    adapter: WorkableAdapter,
) -> None:
    careers_url = "https://acme.com/careers"
    fetcher = FakeFetcher({careers_url: _result(careers_url, 200, "<html>no ats here</html>")})
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND
    assert result.value is None
    assert all("apply.workable.com/api" not in url for url in fetcher.requested_urls)


async def test_shortlink_url_is_not_mistaken_for_an_account_token(adapter: WorkableAdapter) -> None:
    # https://apply.workable.com/j/{shortcode} has no account segment — "j"
    # must not be extracted as if it were one (see ats_platforms.py).
    shortlink_url = "https://apply.workable.com/j/AB12CD34EF"
    redirect_target = "https://apply.workable.com/acme/j/AB12CD34EF"
    widget_url = _widget_url("acme")

    fetcher = FakeFetcher(
        {
            shortlink_url: _result(redirect_target, 200, "<html>redirected</html>"),
            widget_url: _result(widget_url, 200, SAMPLE_BOARD_JSON),
        }
    )
    source = _source(shortlink_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    # token resolution had to fall back to the redirect-target fetch, then
    # correctly extracted "acme" (not "j") from the redirected URL
    assert fetcher.requested_urls == [shortlink_url, widget_url]


async def test_discover_empty_board_is_a_real_success_not_a_failure(adapter: WorkableAdapter) -> None:
    widget_url = _widget_url("acme")
    fetcher = FakeFetcher({widget_url: _result(widget_url, 200, json.dumps({"name": "Acme", "jobs": []}))})
    source = _source("https://apply.workable.com/acme")

    result = await adapter.discover(source, fetcher)

    # spec §2.3: a validated, empty board is real information, not a failure.
    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_404_returns_board_not_found(adapter: WorkableAdapter) -> None:
    widget_url = _widget_url("acme")
    fetcher = FakeFetcher({widget_url: _result(widget_url, 404, "")})
    source = _source("https://apply.workable.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND


async def test_discover_blocked_403(adapter: WorkableAdapter) -> None:
    widget_url = _widget_url("acme")
    fetcher = FakeFetcher({widget_url: _result(widget_url, 403, "")})
    source = _source("https://apply.workable.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BLOCKED_403


async def test_discover_malformed_json_returns_schema_violation(adapter: WorkableAdapter) -> None:
    widget_url = _widget_url("acme")
    fetcher = FakeFetcher({widget_url: _result(widget_url, 200, "{not valid json")})
    source = _source("https://apply.workable.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_missing_jobs_key_returns_schema_violation(adapter: WorkableAdapter) -> None:
    widget_url = _widget_url("acme")
    fetcher = FakeFetcher({widget_url: _result(widget_url, 200, json.dumps({"name": "Acme"}))})
    source = _source("https://apply.workable.com/acme")

    result = await adapter.discover(source, fetcher)

    # spec §17: an ATS API shape change must fail loudly, never be mistaken for zero jobs.
    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_304_is_treated_as_unchanged_not_an_error(adapter: WorkableAdapter) -> None:
    # A conditional request (spec §6.3) returns 304 with an empty body — this
    # must not be parsed as JSON and must not be mistaken for a schema violation.
    widget_url = _widget_url("acme")
    fetcher = FakeFetcher({widget_url: _result(widget_url, 304, "")})
    source = _source("https://apply.workable.com/acme")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


# --- hydrate() ---


def _posting(posting_url: str | None = "https://apply.workable.com/j/AB12CD34EF") -> RawPosting:
    return RawPosting(
        company_id="acme",
        source_platform="workable",
        source_job_id="AB12CD34EF",
        posting_url=posting_url,
        raw_payload={"title": "Senior Backend Engineer", "shortcode": "AB12CD34EF"},
        fetched_at=datetime.now(UTC),
        is_hydrated=False,
    )


async def test_hydrate_extracts_meta_description_and_marks_hydrated(adapter: WorkableAdapter) -> None:
    detail_url = "https://apply.workable.com/j/AB12CD34EF"
    html = '<html><head><meta name="description" content="Build the backend at Acme."></head></html>'
    fetcher = FakeFetcher({detail_url: _result(detail_url, 200, html)})

    result = await adapter.hydrate(_posting(detail_url), fetcher)

    assert result.is_hydrated is True
    assert result.raw_payload["description"] == "Build the backend at Acme."
    # original fields are preserved, not replaced
    assert result.raw_payload["title"] == "Senior Backend Engineer"


async def test_hydrate_falls_back_to_og_description(adapter: WorkableAdapter) -> None:
    detail_url = "https://apply.workable.com/j/AB12CD34EF"
    html = '<html><head><meta property="og:description" content="Via og:description."></head></html>'
    fetcher = FakeFetcher({detail_url: _result(detail_url, 200, html)})

    result = await adapter.hydrate(_posting(detail_url), fetcher)

    assert result.is_hydrated is True
    assert result.raw_payload["description"] == "Via og:description."


async def test_hydrate_no_meta_tags_returns_posting_unchanged(adapter: WorkableAdapter) -> None:
    detail_url = "https://apply.workable.com/j/AB12CD34EF"
    fetcher = FakeFetcher({detail_url: _result(detail_url, 200, "<html><body>no meta here</body></html>")})
    original = _posting(detail_url)

    result = await adapter.hydrate(original, fetcher)

    assert result == original
    assert result.is_hydrated is False


async def test_hydrate_fetch_failure_returns_posting_unchanged(adapter: WorkableAdapter) -> None:
    detail_url = "https://apply.workable.com/j/AB12CD34EF"
    fetcher = FakeFetcher(raise_for={detail_url})
    original = _posting(detail_url)

    result = await adapter.hydrate(original, fetcher)

    assert result == original


async def test_hydrate_http_error_returns_posting_unchanged(adapter: WorkableAdapter) -> None:
    detail_url = "https://apply.workable.com/j/AB12CD34EF"
    fetcher = FakeFetcher({detail_url: _result(detail_url, 404, "")})
    original = _posting(detail_url)

    result = await adapter.hydrate(original, fetcher)

    assert result == original


async def test_hydrate_without_posting_url_is_a_noop(adapter: WorkableAdapter) -> None:
    original = _posting(posting_url=None)
    fetcher = FakeFetcher()  # no responses registered — a real call would fail loudly

    result = await adapter.hydrate(original, fetcher)

    assert result == original
    assert fetcher.requested_urls == []
