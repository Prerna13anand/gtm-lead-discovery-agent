"""Stage 3 Rendered-DOM adapter tests — deterministic, no browser/network
(spec §20.1). `RenderedDomAdapter` is injected with a `FakeRenderer` test
double (mirroring the `FakeTavilyClient` pattern used for Strategy D), so
these tests exercise the adapter's decision logic — endpoint learning/
promotion, DOM-link fallback, status classification — without touching a
real browser. `BrowserRenderer` itself (the real Playwright mechanics) is
covered separately in tests/test_browser.py and by live validation.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from gtm_agent.core.browser import CapturedResponse, RenderResult, RenderTimeoutError
from gtm_agent.core.fetch import FetchError, FetchResult
from gtm_agent.discovery.extraction.rendered_dom import RenderedDomAdapter
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.results import ExtractionStatus


def _source(url: str = "https://acme.com/careers") -> CareersSource:
    return CareersSource(
        company_id="acme",
        careers_url=url,
        resolution_strategy=ResolutionStrategy.HOMEPAGE_LINK,
        resolution_confidence=0.5,
        created_at=datetime.now(UTC),
    )


def _fetch_result(url: str, status_code: int, text: str) -> FetchResult:
    return FetchResult(url=url, status_code=status_code, text=text, headers=httpx.Headers({}))


class FakeRenderer:
    """Test double for `BrowserRenderer` — returns canned `RenderResult`s
    (or raises `RenderTimeoutError`) instead of launching a real browser.
    """

    def __init__(self, result: RenderResult | None = None, raise_timeout: bool = False) -> None:
        self.result = result
        self.raise_timeout = raise_timeout
        self.render_calls: list[str] = []

    async def render(self, url: str, *, wait_js: str | None = None) -> RenderResult:
        self.render_calls.append(url)
        if self.raise_timeout:
            raise RenderTimeoutError(f"simulated timeout for {url}")
        assert self.result is not None
        return self.result


class FakeFetcher:
    """Serves canned responses by exact URL; raises AssertionError on any
    unexpected request.
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


_JOB_LIST_JSON = json.dumps(
    {
        "jobs": [
            {"id": "1", "title": "Senior Backend Engineer", "url": "/jobs/1", "description": "<p>Build things.</p>"},
            {"id": "2", "name": "Founding Designer", "link": "/jobs/2"},
        ]
    }
)

_NON_JOB_JSON = json.dumps({"features": {"darkMode": True}, "dimensions": {"i18n": False}})

_RENDERED_HTML_WITH_JOB_LINKS = """
<html><body>
  <a href="/careers/senior-backend-engineer--engineering--sf">Senior Backend Engineer</a>
  <a href="/careers/founding-designer--design--remote">Founding Designer</a>
  <a href="/about">About</a>
</body></html>
"""

_RENDERED_HTML_NO_JOB_LINKS = "<html><body><p>No open roles right now.</p></body></html>"


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


# --- learning a JSON endpoint from captured XHR responses ---


async def test_discover_learns_endpoint_from_captured_json_and_returns_success(fetcher: FakeFetcher) -> None:
    render_result = RenderResult(
        html=_RENDERED_HTML_NO_JOB_LINKS,
        final_url="https://acme.com/careers",
        xhr_responses=[
            CapturedResponse(
                url="https://acme.com/api/jobs", status=200, content_type="application/json", body=_JOB_LIST_JSON
            )
        ],
    )
    renderer = FakeRenderer(result=render_result)
    adapter = RenderedDomAdapter(renderer=renderer)

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.value is not None
    assert len(result.value) == 2

    first = result.value[0]
    assert first.source_platform == "rendered_dom"
    assert first.source_job_id == "1"
    assert first.posting_url == "https://acme.com/jobs/1"
    assert first.raw_payload["title"] == "Senior Backend Engineer"
    assert first.is_hydrated is True  # had a description key

    second = result.value[1]
    assert second.raw_payload["title"] == "Founding Designer"  # mapped from "name"
    assert second.posting_url == "https://acme.com/jobs/2"  # mapped from "link", resolved absolute
    assert second.is_hydrated is False  # no description-shaped key present


async def test_discover_ignores_non_job_json_and_falls_back_to_dom_links(fetcher: FakeFetcher) -> None:
    render_result = RenderResult(
        html=_RENDERED_HTML_WITH_JOB_LINKS,
        final_url="https://acme.com/careers",
        xhr_responses=[
            CapturedResponse(
                url="https://edge.example.com/flags", status=200, content_type="application/json", body=_NON_JOB_JSON
            )
        ],
    )
    renderer = FakeRenderer(result=render_result)
    adapter = RenderedDomAdapter(renderer=renderer)

    result = await adapter.discover(_source(), fetcher)

    # verified-live finding: a feature-flag/config JSON response must not be
    # mistaken for job data (spec §6.2.3's "clean JSON endpoint").
    assert result.status == ExtractionStatus.PARSE_DEGRADED
    assert result.value is not None
    assert len(result.value) == 2
    assert {p.raw_payload["title"] for p in result.value} == {"Senior Backend Engineer", "Founding Designer"}
    assert all(p.source_job_id is None for p in result.value)
    assert all(p.is_hydrated is False for p in result.value)


async def test_discover_second_call_uses_learned_endpoint_without_rendering_again(fetcher: FakeFetcher) -> None:
    render_result = RenderResult(
        html=_RENDERED_HTML_NO_JOB_LINKS,
        final_url="https://acme.com/careers",
        xhr_responses=[
            CapturedResponse(
                url="https://acme.com/api/jobs", status=200, content_type="application/json", body=_JOB_LIST_JSON
            )
        ],
    )
    renderer = FakeRenderer(result=render_result)
    adapter = RenderedDomAdapter(renderer=renderer)

    first_result = await adapter.discover(_source(), fetcher)
    assert first_result.status == ExtractionStatus.SUCCESS
    assert renderer.render_calls == ["https://acme.com/careers"]

    # Second sweep: the fetcher (not the renderer) serves the learned endpoint.
    fetcher.responses["https://acme.com/api/jobs"] = _fetch_result(
        "https://acme.com/api/jobs", 200, _JOB_LIST_JSON
    )
    second_result = await adapter.discover(_source(), fetcher)

    assert second_result.status == ExtractionStatus.SUCCESS
    assert second_result.value is not None
    assert len(second_result.value) == 2
    # no second render — this is the whole point of the pattern (spec §6.2.3)
    assert renderer.render_calls == ["https://acme.com/careers"]
    assert fetcher.requested_urls == ["https://acme.com/api/jobs"]


async def test_learned_endpoint_304_returns_empty_success_without_forgetting_it(fetcher: FakeFetcher) -> None:
    render_result = RenderResult(
        html=_RENDERED_HTML_NO_JOB_LINKS,
        final_url="https://acme.com/careers",
        xhr_responses=[
            CapturedResponse(
                url="https://acme.com/api/jobs", status=200, content_type="application/json", body=_JOB_LIST_JSON
            )
        ],
    )
    renderer = FakeRenderer(result=render_result)
    adapter = RenderedDomAdapter(renderer=renderer)
    await adapter.discover(_source(), fetcher)

    fetcher.responses["https://acme.com/api/jobs"] = _fetch_result("https://acme.com/api/jobs", 304, "")
    result = await adapter.discover(_source(), fetcher)

    # spec §6.3: unchanged since last time — a real, valid outcome, not an error.
    assert result.status == ExtractionStatus.SUCCESS
    assert result.value == []
    assert renderer.render_calls == ["https://acme.com/careers"]  # still no second render


async def test_learned_endpoint_gone_falls_back_to_rendering_again(fetcher: FakeFetcher) -> None:
    render_result = RenderResult(
        html=_RENDERED_HTML_NO_JOB_LINKS,
        final_url="https://acme.com/careers",
        xhr_responses=[
            CapturedResponse(
                url="https://acme.com/api/jobs", status=200, content_type="application/json", body=_JOB_LIST_JSON
            )
        ],
    )
    renderer = FakeRenderer(result=render_result)
    adapter = RenderedDomAdapter(renderer=renderer)
    await adapter.discover(_source(), fetcher)

    # The learned endpoint now 404s — the adapter must forget it and render again.
    fetcher.responses["https://acme.com/api/jobs"] = _fetch_result("https://acme.com/api/jobs", 404, "")
    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.SUCCESS
    assert renderer.render_calls == ["https://acme.com/careers", "https://acme.com/careers"]


# --- DOM-link fallback ---


async def test_discover_falls_back_to_dom_links_when_no_json_captured_at_all(fetcher: FakeFetcher) -> None:
    render_result = RenderResult(html=_RENDERED_HTML_WITH_JOB_LINKS, final_url="https://acme.com/careers")
    renderer = FakeRenderer(result=render_result)
    adapter = RenderedDomAdapter(renderer=renderer)

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.PARSE_DEGRADED
    assert result.value is not None
    urls = {p.posting_url for p in result.value}
    assert urls == {
        "https://acme.com/careers/senior-backend-engineer--engineering--sf",
        "https://acme.com/careers/founding-designer--design--remote",
    }


async def test_discover_dom_link_extraction_ignores_non_job_links(fetcher: FakeFetcher) -> None:
    render_result = RenderResult(html=_RENDERED_HTML_WITH_JOB_LINKS, final_url="https://acme.com/careers")
    renderer = FakeRenderer(result=render_result)
    adapter = RenderedDomAdapter(renderer=renderer)

    result = await adapter.discover(_source(), fetcher)

    assert result.value is not None
    assert all("/about" not in (p.posting_url or "") for p in result.value)


async def test_discover_dom_link_extraction_dedupes_repeated_hrefs(fetcher: FakeFetcher) -> None:
    html = """
    <html><body>
      <a href="/jobs/engineer">Engineer</a>
      <a href="/jobs/engineer">Engineer (apply now)</a>
    </body></html>
    """
    render_result = RenderResult(html=html, final_url="https://acme.com/careers")
    renderer = FakeRenderer(result=render_result)
    adapter = RenderedDomAdapter(renderer=renderer)

    result = await adapter.discover(_source(), fetcher)

    assert result.value is not None
    assert len(result.value) == 1


async def test_discover_no_job_links_after_render_is_still_degraded_not_success(fetcher: FakeFetcher) -> None:
    render_result = RenderResult(html=_RENDERED_HTML_NO_JOB_LINKS, final_url="https://acme.com/careers")
    renderer = FakeRenderer(result=render_result)
    adapter = RenderedDomAdapter(renderer=renderer)

    result = await adapter.discover(_source(), fetcher)

    # Honest about confidence (spec §2.3 spirit): finding nothing via this
    # inherently heuristic path isn't the same as a confirmed empty board.
    assert result.status == ExtractionStatus.PARSE_DEGRADED
    assert result.value == []


# --- render failure ---


async def test_discover_render_timeout_returns_render_timeout_status(fetcher: FakeFetcher) -> None:
    renderer = FakeRenderer(raise_timeout=True)
    adapter = RenderedDomAdapter(renderer=renderer)

    result = await adapter.discover(_source(), fetcher)

    assert result.status == ExtractionStatus.RENDER_TIMEOUT
    assert result.value is None


# --- hydrate() ---


async def test_hydrate_is_a_noop(fetcher: FakeFetcher) -> None:
    from gtm_agent.models.job import RawPosting

    adapter = RenderedDomAdapter(renderer=FakeRenderer())
    posting = RawPosting(
        company_id="acme",
        source_platform="rendered_dom",
        source_job_id=None,
        posting_url="https://acme.com/careers/engineer",
        raw_payload={"title": "Engineer"},
        fetched_at=datetime.now(UTC),
        is_hydrated=False,
    )

    result = await adapter.hydrate(posting, fetcher)

    assert result is posting
    assert fetcher.requested_urls == []
