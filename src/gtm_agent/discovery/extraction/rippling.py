"""Rippling adapter — spec §6.2.1, Appendix A.

The spec's Appendix A row for Rippling says only "Verify current public
API" / "Verify" / "Verify" — every column left for implementation-time
research. Verified live during the Phase 2 build (against a real board —
`ats.rippling.com/rearc/jobs`, 18 postings — not just taken from the spec's
table, per the §5.3 build note).

**Central discrepancy from every other adapter in this codebase, found live
and documented rather than guessed around**: Rippling-hosted boards are not
a JSON API at all. `ats.rippling.com/{company}/jobs` is a server-rendered
Next.js page (`x-powered-by: Next.js`), and its job data is not fetched from
a separate documented endpoint — it is embedded directly in the HTML response
as a `<script id="__NEXT_DATA__" type="application/json">` blob, Next.js's
own standard hydration mechanism (`getServerSideProps` re-runs on every
request, confirmed via the response's `"gssp": true` marker, so this is not
a stale build-time snapshot). No separate REST/GraphQL endpoint for this
data was found after checking the page's own JS bundles for one. This
adapter therefore fetches the plain HTML page and parses `__NEXT_DATA__`
out of it — the same "embedded structured data on an already-fetched page"
principle spec §6.2.2 describes for JSON-LD, applied to a different, but
equally standard and stable, embedding convention.

    GET https://ats.rippling.com/{company}/jobs?page={n}
      -> HTML containing __NEXT_DATA__.props.pageProps.dehydratedState
         .queries[] where one entry's queryKey is
         ["board", company, "job-posts", ..., {..., "page": n, "pageSize": 20}]
         and its .state.data is {items, page, pageSize, totalItems, totalPages}

    - **Pagination is real and page-based, confirmed live**: appending
      `?page=1` to the URL re-renders the page server-side with `page: 1` in
      the embedded query (verified against the test board, which only has
      one page — `totalPages: 1` — so `?page=1` correctly came back with
      zero items, the expected out-of-range behaviour). `pageSize` observed
      as 20. Matches the spec table's "Verify" resolving to "yes, page-based".
    - **Descriptions are two-phase, confirmed live** — resolving the spec's
      "Verify": the list page's `job-posts` items carry only `id`, `name`,
      `url`, `department`, `locations`, `language`. The full description
      lives only on the individual job's own page
      (`{item.url}`, e.g. `.../jobs/{uuid}`), under
      `__NEXT_DATA__.props.pageProps.apiData.jobPost.description` — itself a
      `{company, role}` pair of HTML fragments, not a single string.
      `hydrate()` uses `role` (the substantive "about this role" content,
      11k+ characters on the posting checked) and leaves `company` out as
      boilerplate, matching spec §7.6's preference for isolating substantive
      role content — same choice made for SmartRecruiters' equivalent split.
    - `jobPost.jsonLd` exists as a key but was `null` on the posting checked
      — not a reliable alternative path, so not used here.
    - An unknown company path returns a clean `404` — same pattern as
      Greenhouse/Lever/Ashby/Workable/Recruitee, unlike SmartRecruiters'
      documented 200-with-empty-content anomaly.
    - The live response sends `Cache-Control: private, no-cache, no-store,
      max-age=0, must-revalidate` and no `ETag`/`Last-Modified` (same
      situation as Recruitee), so a real conditional-request 304 was not
      observed. The `discover()` 304 branch is included regardless, per the
      existing pattern shared by every other adapter here; `hydrate()` needs
      no explicit 304 branch — a 304's empty body already yields no
      `__NEXT_DATA__` match, which is handled as a safe no-op regardless of
      why the body was empty.

Board token resolution (spec §5.2): a Rippling board's token is the first
path segment after the host — `ats.rippling.com/{company}` 308-redirects to
`/{company}/jobs` (verified live), so the same segment works whether
`source.careers_url` points at the bare company path, `/jobs`, or a specific
`/jobs/{id}` posting. Same three-step approach as every other adapter here
(direct extraction, then a redirect-following fetch, then `BOARD_NOT_FOUND`)
— see `discovery.extraction.greenhouse` for the general rationale.

KNOWN FOLLOW-UP for the next milestone (same caveat as every adapter here):
`discovery.normalization.normalize()` has no Rippling-specific branch, so it
falls through to the schema.org-shaped default extraction. Verified against
this adapter's real shape:
    - `title` does **not** match — Rippling's key is `name`, so
      `title_raw`/`title_canonical` come out empty and rules-based
      classification (§7.3) has nothing to work with, the same gap Lever has.
    - `description` matches by coincidence (like Lever/Workable-after-
      hydrate/SmartRecruiters-after-hydrate) once `hydrate()` has run, since
      the injected key is deliberately named `description`.
    - `department` is a nested object (`{"name": ..., "base_department": ...,
      "department_tree": [...]}` on the detail page; just `{"name": ...}` on
      the list page), not the plain string the default lookup expects, so
      `department_raw` stays empty.
    - `locations`, `workplace_type`, `employment_type`, and `posted_at` all
      stay empty/`None`/inferred: Rippling uses a `locations` array of
      structured objects with a `workplaceType` enum (`"HYBRID"` etc. — not
      schema.org's `jobLocationType`), `employmentType` as a `{id, label}`
      pair (not a flat enum string), and `createdOn` (not `datePosted`) only
      available post-hydration on the detail page, not the list.
Deferred, per instruction to keep this phase scoped to the adapter.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.discovery.ats_platforms import extract_board_token
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult

logger = get_logger(__name__)

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_PAGE_SIZE = 20  # observed live default; the page itself controls this, not a request param we set
_MAX_PAGES = 20  # safety bound, not spec-mandated — segment expects 1-30 jobs total (spec §1.5)


def _jobs_page_url(company: str, page: int) -> str:
    return f"https://ats.rippling.com/{company}/jobs?page={page}"


def _extract_next_data(html: str) -> dict[str, Any] | None:
    """Pull Next.js's embedded hydration payload out of a server-rendered
    page — see module docstring for why this is this adapter's equivalent
    of the JSON-LD adapter's `<script type="application/ld+json">` parse.
    """
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _find_job_posts_query(next_data: dict[str, Any]) -> dict[str, Any] | None:
    """Locate the react-query cache entry holding the job list, identified by
    its queryKey's `"job-posts"` marker rather than a fixed array index —
    query ordering isn't a contract Next.js/react-query make any promise about.
    """
    try:
        queries = next_data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError):
        return None
    if not isinstance(queries, list):
        return None

    for query in queries:
        if not isinstance(query, dict):
            continue
        key = query.get("queryKey")
        if isinstance(key, list) and len(key) > 2 and key[2] == "job-posts":
            state = query.get("state")
            if isinstance(state, dict) and isinstance(state.get("data"), dict):
                return state["data"]
    return None


async def _resolve_board_token(source: CareersSource, fetcher: Fetcher) -> str | None:
    """See module docstring, "Board token resolution"."""
    token = extract_board_token(AtsPlatform.RIPPLING, source.careers_url)
    if token:
        return token

    try:
        result = await fetcher.get(source.careers_url)
    except FetchError as exc:
        logger.info(
            "rippling_token_resolution_fetch_failed",
            company_id=source.company_id,
            url=source.careers_url,
            error=str(exc),
        )
        return None

    return extract_board_token(AtsPlatform.RIPPLING, result.url)


def _to_raw_posting(company_id: str, item: dict[str, Any], fetched_at: datetime) -> RawPosting:
    payload = dict(item)

    job_id = payload.get("id")
    posting_url = payload.get("url")

    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.RIPPLING.value,
        source_job_id=str(job_id) if job_id is not None else None,
        posting_url=posting_url if isinstance(posting_url, str) else None,
        raw_payload=payload,
        fetched_at=fetched_at,
        is_hydrated=False,  # two-phase — verified live, list has no description
    )


def _extract_description(job_post: dict[str, Any]) -> str | None:
    description = job_post.get("description")
    if not isinstance(description, dict):
        return None
    role = description.get("role")
    return role if isinstance(role, str) and role else None


class RipplingAdapter:
    platform = AtsPlatform.RIPPLING

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        company = await _resolve_board_token(source, fetcher)
        if not company:
            logger.info(
                "rippling_board_token_unresolved", company_id=source.company_id, url=source.careers_url
            )
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"could not resolve a Rippling company from {source.careers_url}",
            )

        all_items: list[dict[str, Any]] = []
        for page in range(_MAX_PAGES):
            url = _jobs_page_url(company, page)
            try:
                result = await fetcher.get(url)
            except FetchError as exc:
                logger.warning("rippling_fetch_failed", company_id=source.company_id, url=url, error=str(exc))
                return StageResult(status=ExtractionStatus.RATE_LIMITED, detail=str(exc))

            if result.status_code == 404:
                return StageResult(
                    status=ExtractionStatus.BOARD_NOT_FOUND,
                    detail=f"Rippling returned 404 for company '{company}'",
                )
            if result.status_code in (401, 403):
                return StageResult(status=ExtractionStatus.BLOCKED_403, detail=f"HTTP {result.status_code}")
            if result.status_code >= 400:
                return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"HTTP {result.status_code}")
            if result.status_code == 304:
                # Conditional request (spec §6.3): no new data from this page
                # onward — nothing to parse, and this is not an error.
                break

            next_data = _extract_next_data(result.text)
            if next_data is None:
                # spec §17: a page-structure change is this adapter's version
                # of an ATS API shape drift — fail loudly, not silently as "no jobs".
                return StageResult(
                    status=ExtractionStatus.SCHEMA_VIOLATION,
                    detail="no __NEXT_DATA__ found — Rippling page structure may have changed",
                )

            job_posts = _find_job_posts_query(next_data)
            if job_posts is None:
                return StageResult(
                    status=ExtractionStatus.SCHEMA_VIOLATION,
                    detail="no job-posts query found in __NEXT_DATA__ — Rippling page structure may have changed",
                )

            items = job_posts.get("items")
            total_pages = job_posts.get("totalPages")
            if not isinstance(items, list) or not isinstance(total_pages, int):
                return StageResult(
                    status=ExtractionStatus.SCHEMA_VIOLATION,
                    detail="job-posts query missing 'items'/'totalPages' — Rippling API shape may have changed",
                )

            all_items.extend(item for item in items if isinstance(item, dict))

            if not items or page + 1 >= total_pages:
                break

        now = datetime.now(UTC)
        postings = [_to_raw_posting(source.company_id, item, now) for item in all_items]
        return StageResult(status=ExtractionStatus.SUCCESS, value=postings)

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        if not posting.posting_url or not isinstance(posting.raw_payload, dict):
            return posting

        try:
            result = await fetcher.get(posting.posting_url)
        except FetchError as exc:
            logger.info("rippling_hydrate_fetch_failed", posting_url=posting.posting_url, error=str(exc))
            return posting

        if result.status_code >= 400:
            return posting

        next_data = _extract_next_data(result.text)
        if next_data is None:
            return posting

        try:
            job_post = next_data["props"]["pageProps"]["apiData"]["jobPost"]
        except (KeyError, TypeError):
            return posting
        if not isinstance(job_post, dict):
            return posting

        description = _extract_description(job_post)
        if description is None:
            return posting

        updated_payload = dict(posting.raw_payload)
        updated_payload["description"] = description
        return posting.model_copy(update={"raw_payload": updated_payload, "is_hydrated": True})
