"""Greenhouse adapter — spec §6.2.1, Appendix A.

Real implementation against the public Greenhouse Job Board API. Verified
live during the Phase 2A build (against a real board, not just taken from
the spec's Appendix A table, per the §5.3 build note):

    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
      -> {"jobs": [...], "meta": {"total": N}}

    - No pagination: `meta.total` always equals `len(jobs)`.
    - `content` (the full HTML description) is present only when
      `content=true` is passed, and is HTML-entity-escaped (e.g. "&lt;div&gt;")
      rather than raw HTML — it needs `html.unescape()` once before anything
      downstream treats it as markup.
    - An unknown or stale board token returns a clean 404.

Board token resolution (spec §5.2): the `BoardAdapter` interface (spec §6.1)
receives only `CareersSource`, not the `AtsIdentification` that carries the
board token Stage 2 already resolved — that Protocol was deliberately left
unchanged for this task, so this adapter resolves its own token instead:

    1. Try extracting a token directly from `source.careers_url` — the
       common case, since Stage 1's homepage-link strategy frequently
       resolves straight to an ATS URL (spec §4.1 Strategy A), so the token
       is already sitting in the URL.
    2. If that fails (the source is still the company's own domain — e.g.
       Stage 2 identified Greenhouse via a redirect, but `CareersSource`
       doesn't persist the redirect target), fetch `source.careers_url`
       once, follow the redirect, and try again against the final URL.
    3. If neither yields a token, return `BOARD_NOT_FOUND` rather than
       guessing at one.

Case 2 duplicates an HTTP request Stage 2 already made and discarded — a
known inefficiency, acceptable for now because there is no persistent
`careers_source.ats_board_token` store yet (spec §15.1 models the board
token as living on the same row as the careers source; the in-memory
`CareersSource` / `AtsIdentification` split from Phase 1 doesn't carry it
through). Worth revisiting once a real orchestrator persists Stage 2 output
back onto the source record instead of recomputing it.

KNOWN FOLLOW-UP for the next milestone (explicitly out of scope for this
task): `discovery.normalization.normalize()` only reads schema.org field
names (`description`, `jobLocation`, `baseSalary`, ...), because JSON-LD was
the only real payload shape before this adapter existed. A Greenhouse
`RawPosting` (native fields: `content`, `location.name`, `departments`, no
`baseSalary`) will pass `normalize()`'s `isinstance(dict)` check but come out
under-populated — `description_text`, `locations`, and `department_raw` will
be empty even though the raw payload has the data. Fixing this needs
per-platform field mapping in Stage 4; deferred per explicit instruction to
keep this phase scoped to the adapter.
"""

from __future__ import annotations

import html
import json
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

_API_BASE = "https://boards-api.greenhouse.io/v1/boards"


def _board_url(board_token: str) -> str:
    return f"{_API_BASE}/{board_token}/jobs?content=true"


async def _resolve_board_token(source: CareersSource, fetcher: Fetcher) -> str | None:
    """See module docstring, "Board token resolution"."""
    token = extract_board_token(AtsPlatform.GREENHOUSE, source.careers_url)
    if token:
        return token

    try:
        result = await fetcher.get(source.careers_url)
    except FetchError as exc:
        logger.info(
            "greenhouse_token_resolution_fetch_failed",
            company_id=source.company_id,
            url=source.careers_url,
            error=str(exc),
        )
        return None

    return extract_board_token(AtsPlatform.GREENHOUSE, result.url)


def _to_raw_posting(company_id: str, job: dict[str, Any], fetched_at: datetime) -> RawPosting:
    payload = dict(job)
    content = payload.get("content")
    if isinstance(content, str):
        payload["content"] = html.unescape(content)

    job_id = payload.get("id")
    posting_url = payload.get("absolute_url")

    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.GREENHOUSE.value,
        source_job_id=str(job_id) if job_id is not None else None,
        posting_url=posting_url if isinstance(posting_url, str) else None,
        raw_payload=payload,
        fetched_at=fetched_at,
        is_hydrated=True,  # content=true means discover() already has everything (spec §6.1, §6.2.1)
    )


class GreenhouseAdapter:
    platform = AtsPlatform.GREENHOUSE

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        board_token = await _resolve_board_token(source, fetcher)
        if not board_token:
            logger.info(
                "greenhouse_board_token_unresolved", company_id=source.company_id, url=source.careers_url
            )
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"could not resolve a Greenhouse board token from {source.careers_url}",
            )

        url = _board_url(board_token)
        try:
            result = await fetcher.get(url)
        except FetchError as exc:
            logger.warning("greenhouse_fetch_failed", company_id=source.company_id, url=url, error=str(exc))
            return StageResult(status=ExtractionStatus.RATE_LIMITED, detail=str(exc))

        if result.status_code == 404:
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"Greenhouse returned 404 for board token '{board_token}'",
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
            logger.error("greenhouse_invalid_json", company_id=source.company_id, error=str(exc))
            return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"invalid JSON response: {exc}")

        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            # spec §17: an ATS API shape drift is the correlated-blast-radius
            # failure mode, so this must fail loudly, not silently as "no jobs".
            return StageResult(
                status=ExtractionStatus.SCHEMA_VIOLATION,
                detail="response missing a 'jobs' list — Greenhouse API shape may have changed",
            )

        now = datetime.now(UTC)
        postings = [_to_raw_posting(source.company_id, job, now) for job in jobs if isinstance(job, dict)]
        return StageResult(status=ExtractionStatus.SUCCESS, value=postings)

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        # No-op — content=true means discover() already has the full description.
        return posting
