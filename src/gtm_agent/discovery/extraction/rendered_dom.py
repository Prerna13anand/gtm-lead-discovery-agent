"""Rendered-DOM adapter — spec §6.2.3.

For boards that exist only after JavaScript execution and expose no ATS API
— the terminal case for companies that aren't on a known ATS and whose
static HTML shows no job content. Routed to from
`discovery.ats_detection.route_extraction` when a static fetch shows no
job-like content but does show a known SPA root or ATS embed script (the
detection check spec §6.2.3 asks for; implemented in `ats_detection.py`
alongside `has_jsonld_job_posting`, the analogous routing check for JSON-LD).

Verified live during the Phase 2 build against a real bespoke board —
`retool.com/careers` (no known ATS; a Next.js App Router site with no
embedded `__NEXT_DATA__`/JSON-LD and zero job content in the static HTML,
confirmed with both a default and a Googlebot user-agent) — not just taken
from the spec's description, per the §5.3 build-note spirit applied to this
adapter too.

**"Render once, learn the endpoint" (spec §6.2.3), and what live testing
found about it**: the spec frames this as the adapter's main cost
justification — capture the XHR/fetch calls a render makes, and if one
returns a clean JSON job list, call it directly next time instead of
rendering again. On the verified board, this did **not** find anything to
learn: the only same-origin, non-asset network activity during a full
render was the initial document request itself and an unrelated
feature-flag call — no job-listing XHR at all. The job postings (real
`/careers/{slug}` links, confirmed present only after rendering, absent from
the static HTML) render from data that arrives with the initial HTML
response itself — consistent with React Server Components streaming, which
has no separate observable JSON call for this adapter to capture. This is a
real, live-verified limit on the pattern, not a bug in the capture logic:
**endpoint-learning depends on the target site actually using a
client-side-visible JSON API; sites that stream server-rendered data
in-band (RSC and similar) have nothing to learn, and DOM-link extraction is
the correct, and only, path for them on every run.** The endpoint-learning
code path itself is fully implemented and exercised in this module's tests
against a site shaped the way the spec describes, since the verified board
happens to be the other kind.

Because there is, by definition, no fixed native shape for an arbitrary
bespoke site — unlike every ATS adapter, there's nothing to verify a field
mapping against — this adapter does the minimum defensible thing at each of
its two possible outcomes:
    - A learned/captured JSON endpoint: field mapping is a best-effort
      guess across common key names (`title`/`name`/`position`/... for the
      title, `url`/`link`/`href`/... for the URL, and so on) since there is
      no schema to trust. Returned as `SUCCESS` — the data came from a real
      structured API call, whatever its shape.
    - No learnable endpoint: falls back to matching job-like links
      (`/jobs?/`, `/careers?/`, `/positions?/`, `/openings?/` followed by a
      slug) in the rendered DOM and taking each link's anchor text as the
      title. This is deliberately minimal — the actual clustering/heuristic
      extraction spec §6.2.4 describes is the separate, out-of-scope
      generic-HTML adapter's job, not this one's. Returned as
      `PARSE_DEGRADED` (spec §17), honestly reflecting that a link-text
      guess is not the same confidence as a structured response.

`hydrate()` is a no-op: there is no generic, reliable way to fetch a fuller
description for an arbitrary bespoke site without site-specific heuristics,
which is the same out-of-scope territory as the DOM-link fallback above.

Rendering itself (browser lifecycle, resource/analytics blocking, the
content-presence wait, response capture) lives in `core.browser` — this
module only decides what counts as job-like content and what to do with
what comes back, matching the separation of concerns documented in that
module's docstring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from gtm_agent.core.browser import BrowserRenderer, RenderTimeoutError
from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.discovery.ats_detection import is_job_like_href
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult

logger = get_logger(__name__)

# Mirrors is_job_like_href (ats_detection.py) as a JS predicate for
# page.wait_for_function — spec §6.2.3: "wait on a content-presence
# condition (job-like elements appear), not a fixed sleep". Includes the
# same asset-extension exclusion is_job_like_href does — without it, a
# page's own already-present icon/image links under the same URL prefix
# (verified live — see ats_detection.py's _ASSET_EXTENSION_RE comment)
# would satisfy this predicate before real job content ever hydrates in,
# defeating the point of waiting at all.
_CONTENT_WAIT_JS = r"""
() => {
  const links = document.querySelectorAll('a[href]');
  const jobPattern = /\/(jobs?|careers?|positions?|openings?)\/[\w-]{3,}/i;
  const assetPattern = /\.(svg|png|jpe?g|gif|ico|css|js|mjs|woff2?|ttf|eot|webp|avif|json|map)([?#]|$)/i;
  for (const link of links) {
    const href = link.getAttribute('href') || '';
    if (jobPattern.test(href) && !assetPattern.test(href)) {
      return true;
    }
  }
  return false;
}
"""

# Best-effort key names for mapping an arbitrary learned-endpoint job object
# — see module docstring on why there's no fixed schema to check against here.
_TITLE_KEYS = ("title", "name", "position", "job_title", "jobTitle", "role", "position_title")
_URL_KEYS = ("url", "link", "href", "posting_url", "job_url", "jobUrl", "apply_url", "applyUrl", "postingUrl")
_ID_KEYS = ("id", "uuid", "job_id", "jobId", "guid", "slug")
_DESCRIPTION_KEYS = ("description", "content", "body", "details", "job_description")
_JOB_LIST_KEYS = ("jobs", "postings", "positions", "openings", "results", "items", "data")


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value:
            return value
    return None


def _looks_like_job_list(data: Any) -> list[dict[str, Any]] | None:
    """Best-effort recognition that a JSON body is a list of job postings
    (spec §6.2.3's "clean JSON endpoint"). See module docstring: there is no
    fixed shape to check against, so this looks for a plausible list of
    objects where at least half carry a title-shaped key, rather than any
    specific schema.
    """
    candidates: Any = data if isinstance(data, list) else None
    if candidates is None and isinstance(data, dict):
        for key in _JOB_LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                candidates = value
                break
    if not isinstance(candidates, list) or not candidates:
        return None

    dict_items = [item for item in candidates if isinstance(item, dict)]
    if not dict_items:
        return None

    matching = [item for item in dict_items if any(key in item for key in _TITLE_KEYS)]
    if len(matching) < max(1, len(dict_items) // 2):
        return None
    return dict_items


def _to_raw_posting_from_learned(
    company_id: str, item: dict[str, Any], base_url: str, fetched_at: datetime
) -> RawPosting:
    payload = dict(item)

    title = _first_present(item, _TITLE_KEYS)
    if title is not None and "title" not in payload:
        payload["title"] = title  # matches normalize()'s default-path key (spec §7)

    description = _first_present(item, _DESCRIPTION_KEYS)
    if description is not None and "description" not in payload:
        payload["description"] = description

    url = _first_present(item, _URL_KEYS)
    posting_url = urljoin(base_url, url) if isinstance(url, str) and url else None

    job_id = _first_present(item, _ID_KEYS)

    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.RENDERED_DOM.value,
        source_job_id=str(job_id) if job_id is not None else None,
        posting_url=posting_url,
        raw_payload=payload,
        fetched_at=fetched_at,
        is_hydrated=description is not None,
    )


def _extract_dom_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Best-effort title+URL extraction from rendered HTML when no learnable
    JSON endpoint exists. Deliberately minimal — see module docstring on why
    the generic-HTML adapter's DOM-clustering algorithm (spec §6.2.4) is not
    reimplemented here.
    """
    tree = HTMLParser(html)
    seen_urls: set[str] = set()
    results: list[tuple[str, str]] = []
    for anchor in tree.css("a[href]"):
        href = anchor.attributes.get("href") or ""
        if not is_job_like_href(href):
            continue
        absolute_url = urljoin(base_url, href)
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)
        text = anchor.text(strip=True) or ""
        results.append((text, absolute_url))
    return results


def _to_raw_posting_from_link(company_id: str, title: str, url: str, fetched_at: datetime) -> RawPosting:
    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.RENDERED_DOM.value,
        source_job_id=None,  # no stable ID available from a bare link (spec §8.1 identity fallback territory)
        posting_url=url,
        raw_payload={"title": title},
        fetched_at=fetched_at,
        is_hydrated=False,
    )


class RenderedDomAdapter:
    platform = AtsPlatform.RENDERED_DOM

    def __init__(self, renderer: BrowserRenderer | None = None) -> None:
        self._renderer = renderer or BrowserRenderer()

        # company_id -> a JSON endpoint URL captured during a previous
        # render that looked like job data — spec §6.2.3's "render once,
        # learn the endpoint, never render again". In-memory, per-instance,
        # same honest limitation as Fetcher's own conditional-request cache
        # (core/fetch.py): this is not the persistent, cross-sweep store the
        # pattern's full promise needs, which requires a durable store this
        # codebase doesn't have yet. What's here is real within one
        # instance's lifetime — a sweep that reuses one `RenderedDomAdapter`
        # across companies (as the "reuse a browser instance" discipline
        # already requires) gets the benefit for repeat visits within that
        # sweep; surviving between sweeps is a persistence-layer follow-up.
        self._learned_endpoints: dict[str, str] = {}

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        # Spec §21.1: the Playwright renderer navigates the page directly,
        # outside `Fetcher`'s own request path — checked explicitly here so
        # a disallowed careers URL is never rendered, matching every other
        # adapter's guarantee (see `Fetcher.is_allowed`'s docstring).
        if not await fetcher.is_allowed(source.careers_url):
            return StageResult(
                status=ExtractionStatus.ROBOTS_DISALLOWED,
                detail=f"{source.careers_url} disallowed by robots.txt (spec §21.1)",
            )

        learned_url = self._learned_endpoints.get(source.company_id)
        if learned_url:
            postings = await self._try_learned_endpoint(source, learned_url, fetcher)
            if postings is not None:
                return StageResult(status=ExtractionStatus.SUCCESS, value=postings)
            # Endpoint no longer returns job-shaped data — forget it and fall through to a real render.
            del self._learned_endpoints[source.company_id]

        try:
            result = await self._renderer.render(source.careers_url, wait_js=_CONTENT_WAIT_JS)
        except RenderTimeoutError as exc:
            logger.warning("rendered_dom_render_failed", company_id=source.company_id, error=str(exc))
            return StageResult(status=ExtractionStatus.RENDER_TIMEOUT, detail=str(exc))

        now = datetime.now(UTC)

        for captured in result.xhr_responses:
            try:
                data = json.loads(captured.body)
            except json.JSONDecodeError:
                continue
            items = _looks_like_job_list(data)
            if items is None:
                continue
            self._learned_endpoints[source.company_id] = captured.url
            postings = [
                _to_raw_posting_from_learned(source.company_id, item, result.final_url, now) for item in items
            ]
            return StageResult(status=ExtractionStatus.SUCCESS, value=postings)

        # No learnable endpoint — fall back to DOM link extraction (spec
        # §2.3: honestly low-confidence, never silently authoritative).
        links = _extract_dom_links(result.html, result.final_url)
        postings = [_to_raw_posting_from_link(source.company_id, title, url, now) for title, url in links]
        return StageResult(status=ExtractionStatus.PARSE_DEGRADED, value=postings)

    async def _try_learned_endpoint(
        self, source: CareersSource, url: str, fetcher: Fetcher
    ) -> list[RawPosting] | None:
        try:
            result = await fetcher.get(url)
        except FetchError as exc:
            logger.info("rendered_dom_learned_endpoint_failed", company_id=source.company_id, url=url, error=str(exc))
            return None

        if result.status_code == 304:
            # Conditional request (spec §6.3): unchanged since our last
            # fetch — the endpoint is still valid, just nothing new this sweep.
            return []
        if result.status_code >= 400:
            return None

        try:
            data = json.loads(result.text)
        except json.JSONDecodeError:
            return None

        items = _looks_like_job_list(data)
        if items is None:
            return None

        now = datetime.now(UTC)
        return [_to_raw_posting_from_learned(source.company_id, item, url, now) for item in items]

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        # No-op — see module docstring for why a generic fuller-description
        # fetch isn't reliable for an arbitrary bespoke site.
        return posting
