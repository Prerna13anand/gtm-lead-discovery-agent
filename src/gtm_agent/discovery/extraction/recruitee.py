"""Recruitee adapter — spec §6.2.1, Appendix A.

Real implementation against Recruitee's public per-subdomain offers API.
Verified live during the Phase 2 build (against several real boards —
`aihr.recruitee.com` (1 offer), `signode.recruitee.com` (33),
`veocareers.recruitee.com` (59), and others — not just taken from the spec's
Appendix A table, per the §5.3 build note):

    GET https://{company}.recruitee.com/api/offers/
      -> {"offers": [...]}

    - **Descriptions are inline — the spec's "(verify)" flag resolves to
      true.** Every offer checked across all boards above carried a
      populated top-level `description` (real HTML, 11k+ characters on the
      board checked in most detail) and `requirements` field directly in the
      list response. No detail-page fetch is needed, unlike Workable/
      SmartRecruiters — this adapter is single-phase, like Greenhouse/Lever.
    - **No pagination, confirmed live**: the 59-offer board came back whole
      in one response with no `limit`/`page`/`total` wrapper of any kind —
      just a bare `{"offers": [...]}`. Matches the spec table's "None".
    - An unknown subdomain returns a clean `404` (`{"error": "Not Found"}`) —
      same pattern as Greenhouse/Lever/Ashby/Workable, unlike
      SmartRecruiters' documented 200-with-empty-content anomaly.
    - The live endpoint sends no `ETag`/`Last-Modified` (`Cache-Control:
      max-age=0, private, must-revalidate` — explicitly telling clients not
      to cache), so a real conditional-request 304 was not observed here.
      The 304 branch is still implemented for consistency with every other
      adapter and in case that changes — spec §6.3's conditional-request
      contract is a property of the shared `Fetcher`, not something this
      adapter should special-case away.
    - Job fields observed: `id`, `title`, `slug`, `careers_url` (the real
      canonical human-facing URL — already present, no reconstruction
      needed), `careers_apply_url`, `department` (flat string), `location`
      (a combined human string, e.g. "Rotterdam, Zuid-Holland,
      Netherlands"), `city`/`country`/`country_code`/`state_name`,
      `remote`/`hybrid`/`on_site` (booleans), `employment_type_code` (e.g.
      "fulltime_fixed_term"), `published_at`/`created_at`, `description`,
      `requirements`, `status` (e.g. "published"), plus a `translations`
      object carrying the same fields per-locale — the top-level fields
      already reflect the board's default language, so `translations` is
      left untouched in `raw_payload` rather than re-parsed.

Board token resolution (spec §5.2) differs structurally from every adapter
so far: the token is the **subdomain**, not a path segment after a fixed
host (`{company}.recruitee.com`, not `fixedhost.example/{company}`). The
same three-step approach still applies — see
`discovery.extraction.greenhouse` for the general rationale:

    1. Try extracting the subdomain directly from `source.careers_url`
       (common case).
    2. If that fails, fetch `source.careers_url` once, follow the redirect,
       and try again against the final URL.
    3. If neither yields a token, return `BOARD_NOT_FOUND`.

`hydrate()` is a no-op — inline descriptions mean `discover()` already has
everything (same as Greenhouse/Lever).

KNOWN FOLLOW-UP for the next milestone (same caveat as every adapter here):
`discovery.normalization.normalize()` has no Recruitee-specific branch, so it
falls through to the schema.org-shaped default extraction. Verified against
this adapter's real shape:
    - `title` matches by coincidence (like Greenhouse/Ashby/Workable) — title
      canonicalisation and rules-based classification (§7.3) both work.
    - `department` is a flat top-level string, matching the default lookup
      by coincidence (like Ashby/Workable) — `department_raw` comes out
      populated.
    - `description` matches by coincidence (like Lever/Workable-after-
      hydrate/SmartRecruiters-after-hydrate) — populates correctly, and
      unlike those two, needs no hydration step to get there. `requirements`
      (a separate, substantial field — 3k+ characters on the board checked)
      is not read by the default extraction at all and is currently lost;
      folding it in is native-mapping work, deferred with the rest of this list.
    - `locations`, `workplace_type`, `employment_type`, and `posted_at` all
      stay empty/`None`/inferred: Recruitee uses flat `city`/`country`/
      `location` fields and `remote`/`hybrid`/`on_site` booleans (not
      schema.org's `jobLocation`), an `employment_type_code` vocabulary (not
      schema.org's `"FULL_TIME"` enum), and `published_at`/`created_at` (not
      `datePosted`) — the last of which is a real, authoritative posting
      date sitting unused, worth prioritising over Greenhouse/Lever/Ashby's
      equivalent gap once this work is picked up.
Deferred, per instruction to keep this phase scoped to the adapter.
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


def _offers_url(company: str) -> str:
    return f"https://{company}.recruitee.com/api/offers/"


async def _resolve_board_token(source: CareersSource, fetcher: Fetcher) -> str | None:
    """See module docstring, "Board token resolution"."""
    token = extract_board_token(AtsPlatform.RECRUITEE, source.careers_url)
    if token:
        return token

    try:
        result = await fetcher.get(source.careers_url)
    except FetchError as exc:
        logger.info(
            "recruitee_token_resolution_fetch_failed",
            company_id=source.company_id,
            url=source.careers_url,
            error=str(exc),
        )
        return None

    return extract_board_token(AtsPlatform.RECRUITEE, result.url)


def _to_raw_posting(company_id: str, offer: dict[str, Any], fetched_at: datetime) -> RawPosting:
    payload = dict(offer)

    job_id = payload.get("id")
    posting_url = payload.get("careers_url")

    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.RECRUITEE.value,
        source_job_id=str(job_id) if job_id is not None else None,
        posting_url=posting_url if isinstance(posting_url, str) else None,
        raw_payload=payload,
        fetched_at=fetched_at,
        is_hydrated=True,  # inline — discover() already has everything (spec §6.1, §6.2.1)
    )


class RecruiteeAdapter:
    platform = AtsPlatform.RECRUITEE

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        company = await _resolve_board_token(source, fetcher)
        if not company:
            logger.info(
                "recruitee_board_token_unresolved", company_id=source.company_id, url=source.careers_url
            )
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"could not resolve a Recruitee subdomain from {source.careers_url}",
            )

        url = _offers_url(company)
        try:
            result = await fetcher.get(url)
        except FetchError as exc:
            logger.warning("recruitee_fetch_failed", company_id=source.company_id, url=url, error=str(exc))
            return StageResult(status=ExtractionStatus.RATE_LIMITED, detail=str(exc))

        if result.status_code == 404:
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"Recruitee returned 404 for subdomain '{company}'",
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
            logger.error("recruitee_invalid_json", company_id=source.company_id, error=str(exc))
            return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"invalid JSON response: {exc}")

        offers = payload.get("offers") if isinstance(payload, dict) else None
        if not isinstance(offers, list):
            # spec §17: an ATS API shape drift is the correlated-blast-radius
            # failure mode, so this must fail loudly, not silently as "no jobs".
            return StageResult(
                status=ExtractionStatus.SCHEMA_VIOLATION,
                detail="response missing an 'offers' list — Recruitee API shape may have changed",
            )

        now = datetime.now(UTC)
        postings = [_to_raw_posting(source.company_id, offer, now) for offer in offers if isinstance(offer, dict)]
        return StageResult(status=ExtractionStatus.SUCCESS, value=postings)

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        # No-op — offers are inline; discover() already has everything.
        return posting
