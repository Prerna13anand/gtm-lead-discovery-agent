"""Ashby adapter — spec §6.2.1, Appendix A.

Real implementation against the public Ashby Job Board API. Verified live
during the Phase 2C build (against a real board — Linear — not just taken
from the spec's Appendix A table, per the §5.3 build note):

    GET https://api.ashbyhq.com/posting-api/job-board/{board_name}
      -> {"jobs": [...], "apiVersion": 1}

    - No pagination: a 24-job board came back whole in one response.
    - An unknown board name returns a clean 404 (plain-text body "Not
      Found", not JSON — status code is the only thing to check here).
    - Job fields: `id` (stable UUID), `title` (matches schema.org's key by
      coincidence, like Greenhouse), `department` (a flat string — also a
      coincidental schema.org match, unique to Ashby among the three ATS
      adapters), `team`, `employmentType` (PascalCase, e.g. `"FullTime"` —
      not schema.org's `"FULL_TIME"`), `location` (a flat string, unlike
      Lever's `categories.location` or Greenhouse's `location.name`),
      `secondaryLocations`, `isRemote` (boolean), `workplaceType`
      (PascalCase, e.g. `"Remote"`), `publishedAt` (ISO 8601 string),
      `jobUrl` (canonical URL), `descriptionHtml` / `descriptionPlain`.
    - `descriptionHtml` is normal, non-escaped HTML (like Lever, unlike
      Greenhouse's double-entity-escaped `content`) — no `html.unescape()`
      needed.

Board token resolution and the general adapter shape follow
`discovery.extraction.greenhouse` and `discovery.extraction.lever` — see
those modules for the full rationale; only Ashby-specific differences are
repeated here to avoid the docstrings drifting apart on the shared parts:

    1. Try extracting a token directly from `source.careers_url`.
    2. If that fails, fetch `source.careers_url` once, follow the redirect,
       and try again against the final URL.
    3. If neither yields a token, return `BOARD_NOT_FOUND`.

KNOWN FOLLOW-UP for the next milestone (explicitly out of scope for this
task, same as Greenhouse's and Lever's): `discovery.normalization.normalize()`
only reads schema.org field names. Verified empirically (not just asserted
by analogy) against this adapter's real shape — a third, distinct
coincidental-match pattern:

    - `title` matches by coincidence (like Greenhouse) — title canonicalisation
      and rules-based function/seniority classification (§7.3) both work.
    - `department` is a flat top-level string in both Ashby's native shape
      and `normalize()`'s expectation — the *only* one of the three adapters
      where `department_raw` comes out populated without any fix.
    - `description_text`, `locations`, `workplace_type`, `employment_type`,
      and `posted_at` all stay empty/`None`/inferred: Ashby uses
      `descriptionHtml` (not `description`), a flat `location` string (not
      `jobLocation`), PascalCase `workplaceType`/`employmentType` (not
      schema.org's enums), and `publishedAt` (not `datePosted`).

Deferred, per instruction to keep this phase scoped to the adapter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from gtm_agent.core.fetch import FetchError, Fetcher, RobotsDisallowedError
from gtm_agent.core.logging import get_logger
from gtm_agent.discovery.ats_platforms import extract_board_token
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult

logger = get_logger(__name__)

_API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


def _job_board_url(board_token: str) -> str:
    return f"{_API_BASE}/{board_token}"


async def _resolve_board_token(source: CareersSource, fetcher: Fetcher) -> str | None:
    """See module docstring, "Board token resolution"."""
    token = extract_board_token(AtsPlatform.ASHBY, source.careers_url)
    if token:
        return token

    try:
        result = await fetcher.get(source.careers_url)
    except FetchError as exc:
        logger.info(
            "ashby_token_resolution_fetch_failed",
            company_id=source.company_id,
            url=source.careers_url,
            error=str(exc),
        )
        return None

    return extract_board_token(AtsPlatform.ASHBY, result.url)


def _to_raw_posting(company_id: str, job: dict[str, Any], fetched_at: datetime) -> RawPosting:
    payload = dict(job)

    job_id = payload.get("id")
    posting_url = payload.get("jobUrl")

    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.ASHBY.value,
        source_job_id=str(job_id) if job_id is not None else None,
        posting_url=posting_url if isinstance(posting_url, str) else None,
        raw_payload=payload,
        fetched_at=fetched_at,
        is_hydrated=True,  # inline — discover() already has everything (spec §6.1, §6.2.1)
    )


class AshbyAdapter:
    platform = AtsPlatform.ASHBY

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        board_token = await _resolve_board_token(source, fetcher)
        if not board_token:
            logger.info("ashby_board_token_unresolved", company_id=source.company_id, url=source.careers_url)
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"could not resolve an Ashby board token from {source.careers_url}",
            )

        url = _job_board_url(board_token)
        try:
            result = await fetcher.get(url)
        except RobotsDisallowedError as exc:
            return StageResult(status=ExtractionStatus.ROBOTS_DISALLOWED, detail=str(exc))
        except FetchError as exc:
            logger.warning("ashby_fetch_failed", company_id=source.company_id, url=url, error=str(exc))
            return StageResult(status=ExtractionStatus.RATE_LIMITED, detail=str(exc))

        if result.status_code == 404:
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"Ashby returned 404 for board token '{board_token}'",
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
            logger.error("ashby_invalid_json", company_id=source.company_id, error=str(exc))
            return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"invalid JSON response: {exc}")

        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            # spec §17: an ATS API shape drift is the correlated-blast-radius
            # failure mode, so this must fail loudly, not silently as "no jobs".
            return StageResult(
                status=ExtractionStatus.SCHEMA_VIOLATION,
                detail="response missing a 'jobs' list — Ashby API shape may have changed",
            )

        now = datetime.now(UTC)
        postings = [_to_raw_posting(source.company_id, job, now) for job in jobs if isinstance(job, dict)]
        return StageResult(status=ExtractionStatus.SUCCESS, value=postings)

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        # No-op — Ashby postings are inline; discover() already has everything.
        return posting
