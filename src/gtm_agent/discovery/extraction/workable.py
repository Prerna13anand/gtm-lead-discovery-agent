"""Workable adapter — spec §6.2.1, Appendix A.

Real implementation against Workable's public "widget" job-board API.
Verified live during the Phase 2 build (against a real board — Hospitable,
`apply.workable.com/hospitable` — not just taken from the spec's Appendix A
table, per the §5.3 build note):

    GET https://apply.workable.com/api/v1/widget/accounts/{account}
      -> {"name": ..., "description": ..., "jobs": [...]}

    - No pagination observed: a 15-entry board came back whole in one
      response. The spec's Appendix A row lists "Cursor/offset" for this
      platform — that wasn't exercised against the live endpoint above, so
      it may describe a different (partner/OAuth) API rather than this
      public one. Flagging the discrepancy rather than silently resolving it.
    - An unknown account returns a clean 404.
    - **A job open in multiple locations appears once per location**, all
      entries sharing the same `shortcode` and `url` — verified live: one
      real board returned 15 job entries but only 11 unique shortcodes. This
      adapter does not deduplicate; each array entry becomes its own
      `RawPosting`, exactly like Greenhouse/Lever/Ashby's flat mapping.
      Recognising same-shortcode entries as one job is identity/dedup work
      (spec §8.1, Stage 5), not Stage 3.
    - Job fields observed: `title`, `shortcode` (stable ID), `code`,
      `employment_type` (e.g. "Full-time"), `telecommuting` (bool),
      `department` (flat string), `url` / `shortlink` / `application_url`,
      `published_on`, `created_at`, `country`, `city`, `state`, `education`,
      `experience`, `function`, `industry`, `locations` (array of
      `{country, countryCode, city, region, hidden}`). **No description
      field** — confirms the spec table's "Two-phase" descriptions column.

Board token resolution (spec §5.2) follows the same approach as
`discovery.extraction.greenhouse` — see that module's docstring for the full
rationale; only the Workable-specific difference is repeated here: the
`/j/{shortcode}` shortlink form (no account segment) must not be mistaken for
an account slug, so `ats_platforms.BOARD_TOKEN_PATTERNS[WORKABLE]` excludes it.

Two-phase hydration (`hydrate()`): the job detail page
(`https://apply.workable.com/{account}/j/{shortcode}/`) is a client-rendered
SPA — verified live with both a default and a Googlebot user-agent, and by
inspecting the page's own `"prerender"` feature flag and JS bundle: neither
produced server-rendered body content or a `schema.org/JobPosting` JSON-LD
block. The only description-shaped content available from a static fetch is
the page's `<meta name="description">` / `og:description` tag — a search
engine snippet, truncated to roughly 255 characters with a trailing "...".
`hydrate()` uses that as a best-effort description. Recovering the full,
untruncated posting body requires executing the page's JS, which is the
rendered-DOM adapter's job (spec §6.2.3) — a separate, not-yet-built Phase 2
component, out of scope here.

KNOWN FOLLOW-UP for the next milestone (same caveat as Greenhouse/Lever/Ashby):
`discovery.normalization.normalize()` has no Workable-specific branch, so it
falls through to the schema.org-shaped default extraction. Verified against
this adapter's real shape:
    - `title` matches by coincidence (like Greenhouse/Ashby) — title
      canonicalisation and rules-based classification (§7.3) both work.
    - `department` is a flat top-level string, matching the default lookup
      by coincidence (like Ashby) — `department_raw` comes out populated.
    - `description_text`/`description_markdown` populate correctly *after*
      `hydrate()` has run, since the injected key is named `description` to
      match the default lookup — but only ever contain the truncated SEO
      snippet described above, not the full posting body.
    - `locations`, `workplace_type`, `employment_type`, and `posted_at` all
      stay empty/`None`/inferred: Workable uses flat `city`/`country`/`state`
      fields (not schema.org's `jobLocation`), a `telecommuting` boolean and
      human-phrased `employment_type` (e.g. "Full-time", not schema.org's
      `"FULL_TIME"` enum), and `published_on`/`created_at` (not `datePosted`).
Deferred, per instruction to keep this phase scoped to the adapter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.discovery.ats_platforms import extract_board_token
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult

logger = get_logger(__name__)

_API_BASE = "https://apply.workable.com/api/v1/widget/accounts"


def _widget_url(account: str) -> str:
    return f"{_API_BASE}/{account}"


async def _resolve_board_token(source: CareersSource, fetcher: Fetcher) -> str | None:
    """See module docstring, "Board token resolution"."""
    token = extract_board_token(AtsPlatform.WORKABLE, source.careers_url)
    if token:
        return token

    try:
        result = await fetcher.get(source.careers_url)
    except FetchError as exc:
        logger.info(
            "workable_token_resolution_fetch_failed",
            company_id=source.company_id,
            url=source.careers_url,
            error=str(exc),
        )
        return None

    return extract_board_token(AtsPlatform.WORKABLE, result.url)


def _to_raw_posting(company_id: str, job: dict[str, Any], fetched_at: datetime) -> RawPosting:
    payload = dict(job)

    job_id = payload.get("shortcode")
    posting_url = payload.get("url")

    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.WORKABLE.value,
        source_job_id=str(job_id) if job_id is not None else None,
        posting_url=posting_url if isinstance(posting_url, str) else None,
        raw_payload=payload,
        fetched_at=fetched_at,
        is_hydrated=False,  # two-phase — discover() has no description (spec §6.1, Appendix A)
    )


def _extract_meta_description(html: str) -> str | None:
    """Best-effort description from the job detail page's static <meta> tags.

    See module docstring, "Two-phase hydration" — this is a truncated SEO
    snippet, not the full posting body.
    """
    tree = HTMLParser(html)
    for selector in ('meta[name="description"]', 'meta[property="og:description"]'):
        node = tree.css_first(selector)
        if node is None:
            continue
        content = node.attributes.get("content")
        if content:
            return content
    return None


class WorkableAdapter:
    platform = AtsPlatform.WORKABLE

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        account = await _resolve_board_token(source, fetcher)
        if not account:
            logger.info(
                "workable_board_token_unresolved", company_id=source.company_id, url=source.careers_url
            )
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"could not resolve a Workable account from {source.careers_url}",
            )

        url = _widget_url(account)
        try:
            result = await fetcher.get(url)
        except FetchError as exc:
            logger.warning("workable_fetch_failed", company_id=source.company_id, url=url, error=str(exc))
            return StageResult(status=ExtractionStatus.RATE_LIMITED, detail=str(exc))

        if result.status_code == 404:
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"Workable returned 404 for account '{account}'",
            )
        if result.status_code in (401, 403):
            return StageResult(status=ExtractionStatus.BLOCKED_403, detail=f"HTTP {result.status_code}")
        if result.status_code >= 400:
            return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"HTTP {result.status_code}")
        if result.status_code == 304:
            # Conditional request (spec §6.3): board unchanged since our last
            # fetch. The body is empty by HTTP definition — nothing to parse,
            # and this is not an error.
            return StageResult(status=ExtractionStatus.SUCCESS, value=[])

        try:
            payload: dict[str, Any] = json.loads(result.text)
        except json.JSONDecodeError as exc:
            logger.error("workable_invalid_json", company_id=source.company_id, error=str(exc))
            return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"invalid JSON response: {exc}")

        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            # spec §17: an ATS API shape drift is the correlated-blast-radius
            # failure mode, so this must fail loudly, not silently as "no jobs".
            return StageResult(
                status=ExtractionStatus.SCHEMA_VIOLATION,
                detail="response missing a 'jobs' list — Workable API shape may have changed",
            )

        now = datetime.now(UTC)
        postings = [_to_raw_posting(source.company_id, job, now) for job in jobs if isinstance(job, dict)]
        return StageResult(status=ExtractionStatus.SUCCESS, value=postings)

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        if not posting.posting_url or not isinstance(posting.raw_payload, dict):
            return posting

        try:
            result = await fetcher.get(posting.posting_url)
        except FetchError as exc:
            logger.info("workable_hydrate_fetch_failed", posting_url=posting.posting_url, error=str(exc))
            return posting

        if result.status_code >= 400:
            return posting

        description = _extract_meta_description(result.text)
        if description is None:
            return posting

        updated_payload = dict(posting.raw_payload)
        updated_payload["description"] = description
        return posting.model_copy(update={"raw_payload": updated_payload, "is_hydrated": True})
