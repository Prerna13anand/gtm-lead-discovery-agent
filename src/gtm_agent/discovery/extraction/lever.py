"""Lever adapter — spec §6.2.1, Appendix A.

Real implementation against the public Lever Postings API. Verified live
during the Phase 2B build (against a real board — AngelList/Wellfound — not
just taken from the spec's Appendix A table, per the §5.3 build note):

    GET https://api.lever.co/v0/postings/{company}?mode=json
      -> a bare JSON array of postings (NOT wrapped like Greenhouse's
         {"jobs": [...], "meta": {...}})

    - No pagination: a 22-job board came back whole in one response.
    - An unknown company returns a clean 404 with body
      {"ok": false, "error": "Document not found"}.
    - A real, empty board returns 200 with body `[]` — same "board exists
      but has nothing" vs. "board doesn't exist" distinction as Greenhouse,
      just signalled via status code rather than a wrapper key.
    - Job fields include `id` (stable UUID), `text` (title), `createdAt`
      (epoch **milliseconds**), `workplaceType` (`"remote"` / `"hybrid"` /
      `"onsite"` — directly usable, no title-regex needed), `country`,
      `hostedUrl` (canonical URL), and `categories` (department/location/
      team/commitment/allLocations). Description is spread across
      `description` / `opening` / `lists` / `additional` (+ `...Plain`
      variants).
    - Unlike Greenhouse's `content` field, Lever's HTML fields are NOT
      double-entity-escaped — `additional`/`opening` contain literal `<h3>`,
      `<div>` tags directly, so no `html.unescape()` step is needed here.

Board token resolution and the Stage 3/4 boundary gap follow the same
approach as `discovery.extraction.greenhouse` — see that module's docstring
for the full rationale; only the Lever-specific differences are repeated
here to avoid the two docstrings drifting apart on the shared parts:

    1. Try extracting a token directly from `source.careers_url` (common
       case — Stage 1 often resolves straight to an ATS URL).
    2. If that fails, fetch `source.careers_url` once, follow the redirect,
       and try again against the final URL.
    3. If neither yields a token, return `BOARD_NOT_FOUND`.

KNOWN FOLLOW-UP for the next milestone (explicitly out of scope for this
task, same as Greenhouse's): `discovery.normalization.normalize()` only
reads schema.org field names (`title`, `description`, `jobLocation`,
`baseSalary`, ...). A Lever `RawPosting` hits the *opposite* coincidental
overlap Greenhouse did: Lever's title key is `text`, not `title`, so
`title_raw`/`title_canonical` come out **empty** — which in turn means
rules-based function/seniority classification (§7.3) has nothing to match
against and also returns `None`. `description_text` is, by coincidence,
correctly populated (Lever's payload happens to use the key `description`
too), but `locations` and `department_raw` stay empty, since those live
under `categories.location`/`categories.department`, not the schema.org
shapes `normalize()` looks for. Verified empirically against this adapter's
own fixture during the Phase 2B build — not just asserted by analogy with
Greenhouse. Deferred, per instruction to keep this phase scoped to the
adapter.
"""

from __future__ import annotations

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

_API_BASE = "https://api.lever.co/v0/postings"


def _postings_url(board_token: str) -> str:
    return f"{_API_BASE}/{board_token}?mode=json"


async def _resolve_board_token(source: CareersSource, fetcher: Fetcher) -> str | None:
    """See module docstring, "Board token resolution"."""
    token = extract_board_token(AtsPlatform.LEVER, source.careers_url)
    if token:
        return token

    try:
        result = await fetcher.get(source.careers_url)
    except FetchError as exc:
        logger.info(
            "lever_token_resolution_fetch_failed",
            company_id=source.company_id,
            url=source.careers_url,
            error=str(exc),
        )
        return None

    return extract_board_token(AtsPlatform.LEVER, result.url)


def _to_raw_posting(company_id: str, job: dict[str, Any], fetched_at: datetime) -> RawPosting:
    payload = dict(job)

    job_id = payload.get("id")
    posting_url = payload.get("hostedUrl")

    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.LEVER.value,
        source_job_id=str(job_id) if job_id is not None else None,
        posting_url=posting_url if isinstance(posting_url, str) else None,
        raw_payload=payload,
        fetched_at=fetched_at,
        is_hydrated=True,  # inline — discover() already has everything (spec §6.1, §6.2.1)
    )


class LeverAdapter:
    platform = AtsPlatform.LEVER

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        board_token = await _resolve_board_token(source, fetcher)
        if not board_token:
            logger.info("lever_board_token_unresolved", company_id=source.company_id, url=source.careers_url)
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"could not resolve a Lever board token from {source.careers_url}",
            )

        url = _postings_url(board_token)
        try:
            result = await fetcher.get(url)
        except FetchError as exc:
            logger.warning("lever_fetch_failed", company_id=source.company_id, url=url, error=str(exc))
            return StageResult(status=ExtractionStatus.RATE_LIMITED, detail=str(exc))

        if result.status_code == 404:
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"Lever returned 404 for board token '{board_token}'",
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
            payload: Any = json.loads(result.text)
        except json.JSONDecodeError as exc:
            logger.error("lever_invalid_json", company_id=source.company_id, error=str(exc))
            return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"invalid JSON response: {exc}")

        if not isinstance(payload, list):
            # spec §17: an ATS API shape drift is the correlated-blast-radius
            # failure mode, so this must fail loudly, not silently as "no jobs".
            # Lever's top level is a bare array (unlike Greenhouse's {"jobs": [...]}),
            # so anything other than a list here means the shape has changed.
            return StageResult(
                status=ExtractionStatus.SCHEMA_VIOLATION,
                detail="response was not a JSON array — Lever API shape may have changed",
            )

        now = datetime.now(UTC)
        postings = [_to_raw_posting(source.company_id, job, now) for job in payload if isinstance(job, dict)]
        return StageResult(status=ExtractionStatus.SUCCESS, value=postings)

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        # No-op — Lever postings are inline; discover() already has everything.
        return posting
