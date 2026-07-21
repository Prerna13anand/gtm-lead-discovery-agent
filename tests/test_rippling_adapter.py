"""Stage 3 Rippling adapter tests — fixture-based, no network (spec §20.1).

Mirrors tests/test_smartrecruiters_adapter.py in structure (both are
paginated, two-phase adapters). Rippling-specific cases (parsing job data
out of an embedded `__NEXT_DATA__` blob rather than a bare JSON API, and
page-based pagination) are new — see discovery/extraction/rippling.py's
module docstring for what was verified live and why. Fixtures are built
programmatically rather than as static files, since a "fixture" here is a
JSON payload wrapped in a thin HTML shell, not a real full page.
"""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult, RobotsDisallowedError
from gtm_agent.discovery.extraction.rippling import RipplingAdapter, _jobs_page_url
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus


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


def _html(next_data: dict[str, Any]) -> str:
    return (
        '<html><body><div id="__next"></div>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>'
        "</body></html>"
    )


def _list_next_data(items: list[dict[str, Any]], *, page: int, total_pages: int, company: str = "acme") -> dict[str, Any]:
    return {
        "props": {
            "pageProps": {
                "apiData": {"jobBoard": {"slug": company}, "jobBoardSlug": company},
                "dehydratedState": {
                    "queries": [
                        {
                            "queryKey": ["board", company, "locations"],
                            "state": {"data": {"items": []}},
                        },
                        {
                            "queryKey": [
                                "board",
                                company,
                                "job-posts",
                                False,
                                {"searchQuery": "", "page": page, "pageSize": 20},
                            ],
                            "state": {
                                "data": {
                                    "items": items,
                                    "page": page,
                                    "pageSize": 20,
                                    "totalItems": len(items) if total_pages <= 1 else 25,
                                    "totalPages": total_pages,
                                }
                            },
                        },
                    ]
                },
            }
        }
    }


def _detail_next_data(job_post: dict[str, Any]) -> dict[str, Any]:
    return {"props": {"pageProps": {"apiData": {"jobPost": job_post}}}}


_ITEM_1 = {
    "id": "aaaa1111-1111-1111-1111-111111111111",
    "name": "Senior Backend Engineer",
    "url": "https://ats.rippling.com/acme/jobs/aaaa1111-1111-1111-1111-111111111111",
    "department": {"name": "Engineering"},
    "locations": [
        {
            "name": "Remote (US)",
            "country": "United States",
            "countryCode": "US",
            "state": "",
            "stateCode": "",
            "city": "",
            "workplaceType": "REMOTE",
        }
    ],
    "language": "en-US",
}

_ITEM_2 = {
    "id": "bbbb2222-2222-2222-2222-222222222222",
    "name": "Founding Designer",
    "url": "https://ats.rippling.com/acme/jobs/bbbb2222-2222-2222-2222-222222222222",
    "department": {"name": "Design"},
    "locations": [
        {
            "name": "Hybrid (San Francisco, CA, US)",
            "country": "United States",
            "countryCode": "US",
            "state": "California",
            "stateCode": "CA",
            "city": "San Francisco",
            "workplaceType": "HYBRID",
        }
    ],
    "language": "en-US",
}

_ITEM_3 = {
    "id": "cccc3333-3333-3333-3333-333333333333",
    "name": "Support Engineer",
    "url": "https://ats.rippling.com/acme/jobs/cccc3333-3333-3333-3333-333333333333",
    "department": {"name": "Support"},
    "locations": [{"name": "Onsite (NYC)", "country": "United States", "countryCode": "US", "state": "New York", "stateCode": "NY", "city": "New York", "workplaceType": "ONSITE"}],
    "language": "en-US",
}


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
def adapter() -> RipplingAdapter:
    return RipplingAdapter()


# --- discover() ---


async def test_discover_success_maps_fields(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    html = _html(_list_next_data([_ITEM_1, _ITEM_2], page=0, total_pages=1))
    fetcher = FakeFetcher({page0_url: _result(page0_url, 200, html)})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert len(result.value) == 2

    first = result.value[0]
    assert first.company_id == "acme"
    assert first.source_platform == "rippling"
    assert first.source_job_id == "aaaa1111-1111-1111-1111-111111111111"
    assert first.posting_url == "https://ats.rippling.com/acme/jobs/aaaa1111-1111-1111-1111-111111111111"
    # two-phase: discover() alone never has a description (spec Appendix A)
    assert first.is_hydrated is False

    # raw_payload preserves the native Rippling shape untouched otherwise (spec §6.4)
    assert first.raw_payload["name"] == "Senior Backend Engineer"
    assert first.raw_payload["department"]["name"] == "Engineering"


async def test_discover_paginates_across_pages(adapter: RipplingAdapter) -> None:
    # totalPages=2 forces a second request at page=1 (verified live via ?page=N
    # re-rendering server-side) — see module docstring.
    page0_url = _jobs_page_url("acme", 0)
    page1_url = _jobs_page_url("acme", 1)
    fetcher = FakeFetcher(
        {
            page0_url: _result(page0_url, 200, _html(_list_next_data([_ITEM_1, _ITEM_2], page=0, total_pages=2))),
            page1_url: _result(page1_url, 200, _html(_list_next_data([_ITEM_3], page=1, total_pages=2))),
        }
    )
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert [p.source_job_id for p in result.value] == [
        "aaaa1111-1111-1111-1111-111111111111",
        "bbbb2222-2222-2222-2222-222222222222",
        "cccc3333-3333-3333-3333-333333333333",
    ]
    assert fetcher.requested_urls == [page0_url, page1_url]


async def test_discover_single_page_when_totalPages_is_1(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    fetcher = FakeFetcher({page0_url: _result(page0_url, 200, _html(_list_next_data([_ITEM_1], page=0, total_pages=1)))})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert len(result.value) == 1
    # no second page fetched — totalPages said there wasn't one
    assert fetcher.requested_urls == [page0_url]


async def test_token_resolved_directly_from_careers_url_without_extra_fetch(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    fetcher = FakeFetcher({page0_url: _result(page0_url, 200, _html(_list_next_data([], page=0, total_pages=1)))})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    # exactly one request — the jobs page — since the token was already in the URL
    assert fetcher.requested_urls == [page0_url]


async def test_token_resolution_falls_back_to_redirect_target(adapter: RipplingAdapter) -> None:
    careers_url = "https://acme.com/careers"
    redirect_target = "https://ats.rippling.com/acme/jobs"
    page0_url = _jobs_page_url("acme", 0)

    fetcher = FakeFetcher(
        {
            careers_url: _result(redirect_target, 200, "<html>redirected</html>"),
            page0_url: _result(page0_url, 200, _html(_list_next_data([], page=0, total_pages=1))),
        }
    )
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert fetcher.requested_urls == [careers_url, page0_url]


async def test_token_unresolvable_returns_board_not_found_without_hitting_jobs_page(
    adapter: RipplingAdapter,
) -> None:
    careers_url = "https://acme.com/careers"
    fetcher = FakeFetcher({careers_url: _result(careers_url, 200, "<html>no ats here</html>")})
    source = _source(careers_url)

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND
    assert result.value is None
    assert all("ats.rippling.com" not in url for url in fetcher.requested_urls)


async def test_discover_empty_board_is_a_real_success_not_a_failure(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    fetcher = FakeFetcher({page0_url: _result(page0_url, 200, _html(_list_next_data([], page=0, total_pages=1)))})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    # spec §2.3: a validated, empty board is real information, not a failure.
    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_404_returns_board_not_found(adapter: RipplingAdapter) -> None:
    # Live-verified: an unknown company 404s cleanly, unlike SmartRecruiters'
    # 200-with-empty-content anomaly (see module docstring).
    page0_url = _jobs_page_url("acme", 0)
    fetcher = FakeFetcher({page0_url: _result(page0_url, 404, "")})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BOARD_NOT_FOUND


async def test_discover_blocked_403(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    fetcher = FakeFetcher({page0_url: _result(page0_url, 403, "")})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.BLOCKED_403


async def test_discover_robots_disallowed(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    fetcher = FakeFetcher(raise_error_for={page0_url: RobotsDisallowedError(f"{page0_url} disallowed")})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.ROBOTS_DISALLOWED


async def test_discover_missing_next_data_returns_schema_violation(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    fetcher = FakeFetcher({page0_url: _result(page0_url, 200, "<html><body>no next data here</body></html>")})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    # spec §17: a page-structure change must fail loudly, never be mistaken for zero jobs.
    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_missing_job_posts_query_returns_schema_violation(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    next_data = {"props": {"pageProps": {"dehydratedState": {"queries": [{"queryKey": ["board", "acme", "locations"], "state": {"data": {"items": []}}}]}}}}
    fetcher = FakeFetcher({page0_url: _result(page0_url, 200, _html(next_data))})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_missing_items_returns_schema_violation(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    next_data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "queryKey": ["board", "acme", "job-posts", False, {}],
                            "state": {"data": {"page": 0, "pageSize": 20}},
                        }
                    ]
                }
            }
        }
    }
    fetcher = FakeFetcher({page0_url: _result(page0_url, 200, _html(next_data))})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_304_is_treated_as_unchanged_not_an_error(adapter: RipplingAdapter) -> None:
    # A conditional request (spec §6.3) returns 304 with an empty body — this
    # must not be parsed and must not be mistaken for a schema violation.
    page0_url = _jobs_page_url("acme", 0)
    fetcher = FakeFetcher({page0_url: _result(page0_url, 304, "")})
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_304_on_second_page_stops_pagination_without_error(adapter: RipplingAdapter) -> None:
    page0_url = _jobs_page_url("acme", 0)
    page1_url = _jobs_page_url("acme", 1)
    fetcher = FakeFetcher(
        {
            page0_url: _result(page0_url, 200, _html(_list_next_data([_ITEM_1, _ITEM_2], page=0, total_pages=2))),
            page1_url: _result(page1_url, 304, ""),
        }
    )
    source = _source("https://ats.rippling.com/acme/jobs")

    result = await adapter.discover(source, fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    # page 0's two postings are kept; page 1 contributed nothing but didn't error
    assert len(result.value) == 2


# --- hydrate() ---


def _posting(posting_url: str | None = "https://ats.rippling.com/acme/jobs/aaaa1111-1111-1111-1111-111111111111") -> RawPosting:
    return RawPosting(
        company_id="acme",
        source_platform="rippling",
        source_job_id="aaaa1111-1111-1111-1111-111111111111",
        posting_url=posting_url,
        raw_payload={"name": "Senior Backend Engineer", "id": "aaaa1111-1111-1111-1111-111111111111"},
        fetched_at=datetime.now(UTC),
        is_hydrated=False,
    )


async def test_hydrate_extracts_description_and_marks_hydrated(adapter: RipplingAdapter) -> None:
    detail_url = "https://ats.rippling.com/acme/jobs/aaaa1111-1111-1111-1111-111111111111"
    job_post = {"name": "Senior Backend Engineer", "description": {"company": "<p>About Acme.</p>", "role": "<p>Build the backend.</p>"}}
    html = _html(_detail_next_data(job_post))
    fetcher = FakeFetcher({detail_url: _result(detail_url, 200, html)})

    result = await adapter.hydrate(_posting(detail_url), fetcher)

    assert result.is_hydrated is True
    assert result.raw_payload["description"] == "<p>Build the backend.</p>"
    # boilerplate "about the company" half is deliberately excluded (spec §7.6)
    assert "About Acme" not in result.raw_payload["description"]
    # original fields are preserved, not replaced
    assert result.raw_payload["name"] == "Senior Backend Engineer"


async def test_hydrate_missing_role_returns_posting_unchanged(adapter: RipplingAdapter) -> None:
    detail_url = "https://ats.rippling.com/acme/jobs/aaaa1111-1111-1111-1111-111111111111"
    job_post = {"name": "Senior Backend Engineer", "description": {"company": "<p>About Acme.</p>", "role": ""}}
    fetcher = FakeFetcher({detail_url: _result(detail_url, 200, _html(_detail_next_data(job_post)))})
    original = _posting(detail_url)

    result = await adapter.hydrate(original, fetcher)

    assert result == original
    assert result.is_hydrated is False


async def test_hydrate_missing_next_data_returns_posting_unchanged(adapter: RipplingAdapter) -> None:
    detail_url = "https://ats.rippling.com/acme/jobs/aaaa1111-1111-1111-1111-111111111111"
    fetcher = FakeFetcher({detail_url: _result(detail_url, 200, "<html><body>no next data</body></html>")})
    original = _posting(detail_url)

    result = await adapter.hydrate(original, fetcher)

    assert result == original


async def test_hydrate_missing_jobPost_returns_posting_unchanged(adapter: RipplingAdapter) -> None:
    detail_url = "https://ats.rippling.com/acme/jobs/aaaa1111-1111-1111-1111-111111111111"
    next_data = {"props": {"pageProps": {"apiData": {}}}}
    fetcher = FakeFetcher({detail_url: _result(detail_url, 200, _html(next_data))})
    original = _posting(detail_url)

    result = await adapter.hydrate(original, fetcher)

    assert result == original


async def test_hydrate_fetch_failure_returns_posting_unchanged(adapter: RipplingAdapter) -> None:
    detail_url = "https://ats.rippling.com/acme/jobs/aaaa1111-1111-1111-1111-111111111111"
    fetcher = FakeFetcher(raise_for={detail_url})
    original = _posting(detail_url)

    result = await adapter.hydrate(original, fetcher)

    assert result == original


async def test_hydrate_http_error_returns_posting_unchanged(adapter: RipplingAdapter) -> None:
    detail_url = "https://ats.rippling.com/acme/jobs/aaaa1111-1111-1111-1111-111111111111"
    fetcher = FakeFetcher({detail_url: _result(detail_url, 404, "")})
    original = _posting(detail_url)

    result = await adapter.hydrate(original, fetcher)

    assert result == original


async def test_hydrate_without_posting_url_is_a_noop(adapter: RipplingAdapter) -> None:
    original = _posting(posting_url=None)
    fetcher = FakeFetcher()  # no responses registered — a real call would fail loudly

    result = await adapter.hydrate(original, fetcher)

    assert result == original
    assert fetcher.requested_urls == []
