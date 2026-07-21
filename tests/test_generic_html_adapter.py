"""Stage 3 Generic-HTML adapter tests — deterministic, no network (spec §20.1).

Fixture shapes here mirror two real structural patterns confirmed during live
validation (see `generic_html.py`'s module docstring for the full live case
against helpscout.com/company/careers/):

    - "inline": title and location both live inside the job anchor itself
      (the verified live shape — `<a><h6>Title</h6><p>$X, Remote</p></a>`).
    - "sibling": the anchor wraps only the title; location is a sibling
      element inside a shared, single-job container.

Both must resolve to the correct per-job location without leaking a
neighboring job's text — the inline case is exactly where an earlier version
of this adapter had a real (not hypothetical) cross-contamination bug, fixed
by scoping the search to `Node.iter()` (descendants only) instead of
`Node.css("*")` (which also matches the node passed in).
"""

from datetime import UTC, datetime

import httpx
import pytest

from gtm_agent.core.fetch import FetchError, FetchResult, RobotsDisallowedError
from gtm_agent.discovery.extraction.generic_html import GenericHtmlAdapter
from gtm_agent.discovery.normalization import normalize_batch
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus


def _source(url: str = "https://acme.com/careers") -> CareersSource:
    return CareersSource(
        company_id="acme",
        careers_url=url,
        resolution_strategy=ResolutionStrategy.HOMEPAGE_LINK,
        resolution_confidence=0.5,
        created_at=datetime.now(UTC),
    )


class FakeFetcher:
    """Serves canned responses by exact URL; raises AssertionError on any
    unexpected request (same test double pattern as test_rendered_dom_adapter.py).
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


def _fetch_result(url: str, status_code: int, text: str) -> FetchResult:
    return FetchResult(url=url, status_code=status_code, text=text, headers=httpx.Headers({}))


@pytest.fixture
def adapter() -> GenericHtmlAdapter:
    return GenericHtmlAdapter()


# Mirrors the verified live helpscout.com structure: title and comp/location
# both inside the anchor, repeated as siblings under one shared container.
_INLINE_STRUCTURE_HTML = """
<html><body>
  <div class="items">
    <a href="/careers/11111111-aaaa/" class="FeatureGridBlock--Item">
      <h6>Sr. Product Analyst</h6>
      <p>$125K-$130K. 100% remote. Open to US or Canada.</p>
    </a>
    <a href="/careers/22222222-bbbb/" class="FeatureGridBlock--Item">
      <h6>Staff Product Engineer</h6>
      <p>$187K-$214K. 100% remote in the US.</p>
    </a>
    <a href="/careers/33333333-cccc/" class="FeatureGridBlock--Item">
      <h6>Founding Designer</h6>
      <p>$150K-$170K, Hybrid, NYC.</p>
    </a>
  </div>
</body></html>
"""

# A single-job-per-container pattern: the anchor only wraps the title, and
# location is a sibling element outside the anchor.
_SIBLING_STRUCTURE_HTML = """
<html><body>
  <ul>
    <li class="job-row"><a href="/jobs/backend-engineer">Backend Engineer</a><span class="location">San Francisco, CA</span></li>
    <li class="job-row"><a href="/jobs/frontend-engineer">Frontend Engineer</a><span class="location">Remote</span></li>
  </ul>
</body></html>
"""

_NO_JOB_LINKS_HTML = "<html><body><p>Nothing to see here.</p></body></html>"


# --- discover(): inline structure (verified live shape) ---


async def test_discover_extracts_title_and_location_from_inline_structure() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, _INLINE_STRUCTURE_HTML)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.PARSE_DEGRADED
    assert result.value is not None
    by_title = {p.raw_payload["title"]: p for p in result.value}
    assert set(by_title) == {"Sr. Product Analyst", "Staff Product Engineer", "Founding Designer"}

    # The regression this guards: each job's own location, never another
    # job's — and never the concatenated title+location text of itself.
    assert by_title["Sr. Product Analyst"].raw_payload["location"] == "$125K-$130K. 100% remote. Open to US or Canada."
    assert by_title["Staff Product Engineer"].raw_payload["location"] == "$187K-$214K. 100% remote in the US."
    assert by_title["Founding Designer"].raw_payload["location"] == "$150K-$170K, Hybrid, NYC."


async def test_discover_inline_structure_urls_are_absolute_and_distinct() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, _INLINE_STRUCTURE_HTML)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.value is not None
    urls = {p.posting_url for p in result.value}
    assert urls == {
        "https://acme.com/careers/11111111-aaaa/",
        "https://acme.com/careers/22222222-bbbb/",
        "https://acme.com/careers/33333333-cccc/",
    }


# --- discover(): sibling structure ---


async def test_discover_extracts_location_from_sibling_element() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, _SIBLING_STRUCTURE_HTML)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.PARSE_DEGRADED
    assert result.value is not None
    by_title = {p.raw_payload["title"]: p for p in result.value}
    assert by_title["Backend Engineer"].raw_payload["location"] == "San Francisco, CA"
    assert by_title["Frontend Engineer"].raw_payload["location"] == "Remote"


async def test_discover_title_falls_back_to_anchor_text_when_no_heading() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, _SIBLING_STRUCTURE_HTML)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.value is not None
    titles = {p.raw_payload["title"] for p in result.value}
    assert titles == {"Backend Engineer", "Frontend Engineer"}


# --- discover(): clustering / noise reduction ---


async def test_discover_drops_stray_anchor_outside_dominant_cluster() -> None:
    html = """
    <html><body>
      <a href="/careers/apply-now" class="hero-cta">Apply now for open roles</a>
      <ul>
        <li class="job-row"><a href="/jobs/backend-engineer">Backend Engineer</a><span class="location">SF</span></li>
        <li class="job-row"><a href="/jobs/frontend-engineer">Frontend Engineer</a><span class="location">Remote</span></li>
        <li class="job-row"><a href="/jobs/designer">Designer</a><span class="location">NYC</span></li>
      </ul>
    </body></html>
    """
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, html)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.value is not None
    titles = {p.raw_payload["title"] for p in result.value}
    # the lone hero CTA anchor (its own, differently-classed container) is
    # noise next to the three-member repeated <li> cluster — dropped.
    assert titles == {"Backend Engineer", "Frontend Engineer", "Designer"}
    assert "Apply now for open roles" not in titles


async def test_discover_keeps_everything_when_no_dominant_cluster() -> None:
    # Two isolated single-job containers, no repeated structural pattern —
    # the conservative default (spec §6.2.4 wants low-confidence, not
    # over-eager filtering) is to keep both rather than guess which is noise.
    html = """
    <html><body>
      <div class="role-a"><a href="/careers/only-role-a">Only Role A</a></div>
      <div class="role-b"><a href="/careers/only-role-b">Only Role B</a></div>
    </body></html>
    """
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, html)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.value is not None
    titles = {p.raw_payload["title"] for p in result.value}
    assert titles == {"Only Role A", "Only Role B"}


# --- discover(): dedup, empty, and non-job links ---


async def test_discover_dedupes_repeated_hrefs() -> None:
    html = """
    <html><body>
      <a href="/jobs/engineer">Engineer</a>
      <a href="/jobs/engineer">Engineer (apply now)</a>
    </body></html>
    """
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, html)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.value is not None
    assert len(result.value) == 1


async def test_discover_ignores_non_job_links() -> None:
    html = """
    <html><body>
      <a href="/jobs/engineer">Engineer</a>
      <a href="/about">About us</a>
      <a href="/blog/post-1">Blog post</a>
    </body></html>
    """
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, html)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.value is not None
    urls = {p.posting_url for p in result.value}
    assert urls == {"https://acme.com/jobs/engineer"}


async def test_discover_no_job_links_is_still_degraded_not_success() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, _NO_JOB_LINKS_HTML)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    # Honest about confidence (spec §2.3 spirit): a heuristic path finding
    # nothing is not the same as a confirmed empty board.
    assert result.status == ExtractionStatus.PARSE_DEGRADED
    assert result.value == []


# --- discover(): HTTP status handling ---


async def test_discover_403_returns_blocked_403() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 403, "")})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.BLOCKED_403
    assert result.value is None


async def test_discover_robots_disallowed() -> None:
    fetcher = FakeFetcher(
        raise_error_for={"https://acme.com/careers": RobotsDisallowedError("https://acme.com/careers disallowed")}
    )
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.ROBOTS_DISALLOWED


async def test_discover_401_returns_blocked_403() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 401, "")})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.BLOCKED_403


async def test_discover_5xx_returns_schema_violation() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 500, "")})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.SCHEMA_VIOLATION


async def test_discover_304_returns_success_with_empty_list() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 304, "")})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    # Conditional request (spec §6.3): unchanged since last fetch — a real
    # success, not an error, and not degraded (no parsing happened at all).
    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []


async def test_discover_fetch_error_returns_rate_limited() -> None:
    fetcher = FakeFetcher(raise_for={"https://acme.com/careers"})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.RATE_LIMITED
    assert result.value is None


# --- hydrate() ---


async def test_hydrate_fetches_detail_page_and_fills_description() -> None:
    detail_html = """
    <html><body>
      <nav>Skip nav</nav>
      <script>trackPageView();</script>
      <main><h1>Backend Engineer</h1><p>Build great things.</p></main>
      <footer>Footer copy</footer>
    </body></html>
    """
    fetcher = FakeFetcher({"https://acme.com/jobs/backend-engineer": _fetch_result(
        "https://acme.com/jobs/backend-engineer", 200, detail_html
    )})
    adapter = GenericHtmlAdapter()
    posting = RawPosting(
        company_id="acme",
        source_platform="generic_html",
        source_job_id=None,
        posting_url="https://acme.com/jobs/backend-engineer",
        raw_payload={"title": "Backend Engineer"},
        fetched_at=datetime.now(UTC),
        is_hydrated=False,
    )

    result = await adapter.hydrate(posting, fetcher)

    assert result.is_hydrated is True
    assert isinstance(result.raw_payload, dict)
    description = result.raw_payload["description"]
    assert "Build great things." in description
    assert "Backend Engineer" in description
    # stripped boilerplate tags must not leak into the description
    assert "Skip nav" not in description
    assert "trackPageView" not in description
    assert "Footer copy" not in description
    # discover()'s original field is preserved, not dropped
    assert result.raw_payload["title"] == "Backend Engineer"


async def test_hydrate_is_noop_when_already_hydrated() -> None:
    fetcher = FakeFetcher()
    adapter = GenericHtmlAdapter()
    posting = RawPosting(
        company_id="acme",
        source_platform="generic_html",
        posting_url="https://acme.com/jobs/backend-engineer",
        raw_payload={"title": "Backend Engineer", "description": "already here"},
        fetched_at=datetime.now(UTC),
        is_hydrated=True,
    )

    result = await adapter.hydrate(posting, fetcher)

    assert result is posting
    assert fetcher.requested_urls == []


async def test_hydrate_is_noop_when_no_posting_url() -> None:
    fetcher = FakeFetcher()
    adapter = GenericHtmlAdapter()
    posting = RawPosting(
        company_id="acme",
        source_platform="generic_html",
        posting_url=None,
        raw_payload={"title": "Backend Engineer"},
        fetched_at=datetime.now(UTC),
        is_hydrated=False,
    )

    result = await adapter.hydrate(posting, fetcher)

    assert result is posting
    assert fetcher.requested_urls == []


async def test_hydrate_returns_posting_unchanged_on_fetch_error() -> None:
    fetcher = FakeFetcher(raise_for={"https://acme.com/jobs/backend-engineer"})
    adapter = GenericHtmlAdapter()
    posting = RawPosting(
        company_id="acme",
        source_platform="generic_html",
        posting_url="https://acme.com/jobs/backend-engineer",
        raw_payload={"title": "Backend Engineer"},
        fetched_at=datetime.now(UTC),
        is_hydrated=False,
    )

    result = await adapter.hydrate(posting, fetcher)

    assert result is posting
    assert result.is_hydrated is False


async def test_hydrate_returns_posting_unchanged_on_http_error_status() -> None:
    fetcher = FakeFetcher({"https://acme.com/jobs/backend-engineer": _fetch_result(
        "https://acme.com/jobs/backend-engineer", 404, ""
    )})
    adapter = GenericHtmlAdapter()
    posting = RawPosting(
        company_id="acme",
        source_platform="generic_html",
        posting_url="https://acme.com/jobs/backend-engineer",
        raw_payload={"title": "Backend Engineer"},
        fetched_at=datetime.now(UTC),
        is_hydrated=False,
    )

    result = await adapter.hydrate(posting, fetcher)

    assert result is posting
    assert result.is_hydrated is False


# --- Stage 3 -> Stage 4 integration ---
#
# Regression coverage for the code-review finding: a PARSE_DEGRADED discover()
# result must survive into normalize_batch() as real, degraded, low-confidence
# JobPostings -- not get discarded before Stage 4, and not get normalised as
# if it were a fully-trusted structured ATS response.


async def test_discover_output_normalizes_as_degraded_with_location_preserved() -> None:
    fetcher = FakeFetcher({"https://acme.com/careers": _fetch_result("https://acme.com/careers", 200, _INLINE_STRUCTURE_HTML)})
    adapter = GenericHtmlAdapter()

    result = await adapter.discover(_source(), fetcher)
    assert result.status == ExtractionStatus.PARSE_DEGRADED
    assert result.value is not None

    job_postings = normalize_batch(result.value)

    assert len(job_postings) == 3
    by_title = {job.title_raw: job for job in job_postings}
    assert set(by_title) == {"Sr. Product Analyst", "Staff Product Engineer", "Founding Designer"}

    for job in job_postings:
        assert job.is_degraded is True
        assert job.extraction_confidence < 0.5

    # The location each posting carried out of discover() must not have been
    # silently dropped by normalization's default (schema.org) location parser.
    assert by_title["Sr. Product Analyst"].location_raw == "$125K-$130K. 100% remote. Open to US or Canada."
    assert by_title["Founding Designer"].location_raw == "$150K-$170K, Hybrid, NYC."
