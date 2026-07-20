"""SmartRecruiters adapter — spec §6.2.1, Appendix A.

Real implementation against SmartRecruiters' public Posting API. Verified
live during the Phase 2 build (against a real board — Sandisk,
`careers.smartrecruiters.com/sandisk` — not just taken from the spec's
Appendix A table, per the §5.3 build note):

    GET https://api.smartrecruiters.com/v1/companies/{company}/postings?offset={o}&limit=100
      -> {"offset": o, "limit": 100, "totalFound": N, "content": [...]}

    - **Offset pagination confirmed live**: the test board had 301 postings
      against a 100-per-page default; three additional paged requests
      returned distinct, non-overlapping results. This is the first adapter
      in this codebase where pagination is actually exercised — Greenhouse,
      Lever, Ashby, and Workable all observed a single response covering the
      whole board.
    - **The list endpoint carries no canonical posting URL.** Unlike the
      other three ATS adapters, there is no `absolute_url`/`hostedUrl`/`url`
      field here — `posting_url` stays `None` until `hydrate()` runs.
      Guessing one (e.g. reconstructing `jobs.smartrecruiters.com/{company}/
      {id}-{slug}` from the title) was deliberately avoided — SmartRecruiters'
      exact slugification rules aren't documented and getting it wrong would
      silently produce a broken link, which is worse than leaving the field
      unset. What each item *does* carry is `ref`: the absolute URL of that
      posting's own detail endpoint — `hydrate()` uses it directly rather
      than reconstructing anything.
    - **Discrepancy from the spec/from every other adapter here, found live
      and documented rather than guessed around**: an unknown company does
      **not** 404. `GET .../companies/{bogus}/postings` returns `200` with
      `{"totalFound": 0, "content": []}` — the exact same shape a real,
      validated board with zero currently-open roles would return. There is
      no reliable live signal available on this endpoint to tell "board
      doesn't exist" apart from "board exists, nothing open right now".
      Rather than invent a heuristic, this adapter treats `totalFound: 0` as
      `SUCCESS` with an empty list either way — consistent with spec §2.3's
      own stated preference ("no open jobs" over a guess), and defensible
      here specifically because Stage 1/2 already validated the careers page
      before this adapter ever runs. A `404` status is still handled
      defensively (mapped to `BOARD_NOT_FOUND`) in case some other malformed
      input reaches this endpoint, but it was not the live-observed path for
      a nonexistent company.
    - Job fields observed on the list endpoint: `id`, `name`, `uuid`,
      `jobAdId`, `refNumber`, `ref` (detail-endpoint URL), `company`
      (`{identifier, name}`), `releasedDate`, `location` (structured:
      `city`/`region`/`country`/`postalCode`/`remote`/`hybrid`/
      `fullLocation`), `industry`, `department`, `function`,
      `typeOfEmployment`, `experienceLevel`, `customField` (array).
      **No description field** — confirms the spec table's "Two-phase"
      column and its "Detail fetch per posting" note.

Board token resolution (spec §5.2) follows the same approach as
`discovery.extraction.greenhouse`/`.workable` — see those modules'
docstrings for the full rationale; only the SmartRecruiters-specific
difference is repeated here:

    1. Try extracting a token directly from `source.careers_url`
       (`careers.smartrecruiters.com/{company}` — common case).
    2. If that fails, fetch `source.careers_url` once, follow the redirect,
       and try again against the final URL.
    3. If neither yields a token, return `BOARD_NOT_FOUND`.

Two-phase hydration (`hydrate()`): fetches the posting's own `ref` URL
(`https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}`),
which returns the same fields as the list entry plus `postingUrl` (the real
canonical human-facing URL, on a *third* SmartRecruiters subdomain —
`jobs.smartrecruiters.com`, distinct from both `careers.` and `api.`) and
`jobAd.sections`, a `{title, text}` map. `jobDescription` and
`qualifications` (verified live, both present and HTML-formatted on every
posting checked) are joined into the injected `description` field;
`companyDescription`/`additionalInformation` are left out as boilerplate,
matching spec §7.6's preference for isolating substantive role content.

KNOWN FOLLOW-UP for the next milestone (same caveat as Greenhouse/Lever/
Ashby/Workable): `discovery.normalization.normalize()` has no
SmartRecruiters-specific branch, so it falls through to the schema.org-shaped
default extraction. Verified against this adapter's real shape:
    - `title` does **not** match — SmartRecruiters' key is `name`, so
      `title_raw`/`title_canonical` come out empty and rules-based
      classification (§7.3) has nothing to work with. This is the *opposite*
      coincidental-overlap pattern from Greenhouse/Ashby/Workable (all of
      which use `title`).
    - `description` matches by coincidence (like Lever/Workable-after-hydrate)
      once `hydrate()` has run, since the injected key is deliberately named
      `description`.
    - `department` is a flat dict (`{"id": ..., "label": ...}` when present,
      `{}` when not — verified live, e.g. the Packaging Engineer posting had
      an empty `department: {}`), not the plain string the default lookup
      expects, so `department_raw` stays empty even when populated.
    - `locations`, `workplace_type`, `employment_type`, and `posted_at` all
      stay empty/`None`/inferred: SmartRecruiters uses a structured
      `location` object (not schema.org's `jobLocation`), `typeOfEmployment`
      as a `{id, label}` pair (not a flat enum string), and `releasedDate`
      (not `datePosted`).
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

_API_BASE = "https://api.smartrecruiters.com/v1/companies"
_PAGE_LIMIT = 100  # verified live default/working page size
_MAX_PAGES = 20  # safety bound, not spec-mandated — segment expects 1-30 jobs total (spec §1.5)


def _postings_url(company: str, offset: int) -> str:
    return f"{_API_BASE}/{company}/postings?offset={offset}&limit={_PAGE_LIMIT}"


async def _resolve_board_token(source: CareersSource, fetcher: Fetcher) -> str | None:
    """See module docstring, "Board token resolution"."""
    token = extract_board_token(AtsPlatform.SMARTRECRUITERS, source.careers_url)
    if token:
        return token

    try:
        result = await fetcher.get(source.careers_url)
    except FetchError as exc:
        logger.info(
            "smartrecruiters_token_resolution_fetch_failed",
            company_id=source.company_id,
            url=source.careers_url,
            error=str(exc),
        )
        return None

    return extract_board_token(AtsPlatform.SMARTRECRUITERS, result.url)


def _to_raw_posting(company_id: str, job: dict[str, Any], fetched_at: datetime) -> RawPosting:
    payload = dict(job)
    job_id = payload.get("id")

    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.SMARTRECRUITERS.value,
        source_job_id=str(job_id) if job_id is not None else None,
        posting_url=None,  # not available until hydrate() — see module docstring
        raw_payload=payload,
        fetched_at=fetched_at,
        is_hydrated=False,  # two-phase — discover() has no description (spec §6.1, Appendix A)
    )


def _extract_description(detail: dict[str, Any]) -> str | None:
    job_ad = detail.get("jobAd")
    if not isinstance(job_ad, dict):
        return None
    sections = job_ad.get("sections")
    if not isinstance(sections, dict):
        return None

    parts: list[str] = []
    for key in ("jobDescription", "qualifications"):
        section = sections.get(key)
        if isinstance(section, dict):
            text = section.get("text")
            if isinstance(text, str) and text:
                parts.append(text)

    return "\n\n".join(parts) if parts else None


class SmartRecruitersAdapter:
    platform = AtsPlatform.SMARTRECRUITERS

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        company = await _resolve_board_token(source, fetcher)
        if not company:
            logger.info(
                "smartrecruiters_board_token_unresolved", company_id=source.company_id, url=source.careers_url
            )
            return StageResult(
                status=ExtractionStatus.BOARD_NOT_FOUND,
                detail=f"could not resolve a SmartRecruiters company from {source.careers_url}",
            )

        all_jobs: list[dict[str, Any]] = []
        offset = 0
        for _page in range(_MAX_PAGES):
            url = _postings_url(company, offset)
            try:
                result = await fetcher.get(url)
            except FetchError as exc:
                logger.warning("smartrecruiters_fetch_failed", company_id=source.company_id, url=url, error=str(exc))
                return StageResult(status=ExtractionStatus.RATE_LIMITED, detail=str(exc))

            if result.status_code == 404:
                # Defensive only — the live-observed path for an unknown
                # company is 200 + empty content, not 404. See module docstring.
                return StageResult(
                    status=ExtractionStatus.BOARD_NOT_FOUND,
                    detail=f"SmartRecruiters returned 404 for company '{company}'",
                )
            if result.status_code in (401, 403):
                return StageResult(status=ExtractionStatus.BLOCKED_403, detail=f"HTTP {result.status_code}")
            if result.status_code >= 400:
                return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"HTTP {result.status_code}")
            if result.status_code == 304:
                # Conditional request (spec §6.3): no new data from this page
                # onward — nothing to parse, and this is not an error.
                break

            try:
                payload: dict[str, Any] = json.loads(result.text)
            except json.JSONDecodeError as exc:
                logger.error("smartrecruiters_invalid_json", company_id=source.company_id, error=str(exc))
                return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"invalid JSON response: {exc}")

            content = payload.get("content") if isinstance(payload, dict) else None
            total_found = payload.get("totalFound") if isinstance(payload, dict) else None
            if not isinstance(content, list) or not isinstance(total_found, int):
                # spec §17: an ATS API shape drift is the correlated-blast-radius
                # failure mode, so this must fail loudly, not silently as "no jobs".
                return StageResult(
                    status=ExtractionStatus.SCHEMA_VIOLATION,
                    detail="response missing 'content'/'totalFound' — SmartRecruiters API shape may have changed",
                )

            all_jobs.extend(job for job in content if isinstance(job, dict))

            offset += len(content)
            if not content or offset >= total_found:
                break

        now = datetime.now(UTC)
        postings = [_to_raw_posting(source.company_id, job, now) for job in all_jobs]
        return StageResult(status=ExtractionStatus.SUCCESS, value=postings)

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        if not isinstance(posting.raw_payload, dict):
            return posting

        detail_url = posting.raw_payload.get("ref")
        if not isinstance(detail_url, str) or not detail_url:
            return posting

        try:
            result = await fetcher.get(detail_url)
        except FetchError as exc:
            logger.info("smartrecruiters_hydrate_fetch_failed", posting_url=detail_url, error=str(exc))
            return posting

        if result.status_code >= 400:
            return posting

        try:
            detail: Any = json.loads(result.text)
        except json.JSONDecodeError:
            return posting
        if not isinstance(detail, dict):
            return posting

        description = _extract_description(detail)
        if description is None:
            return posting

        updated_payload = dict(posting.raw_payload)
        updated_payload["description"] = description

        updates: dict[str, Any] = {"raw_payload": updated_payload, "is_hydrated": True}
        posting_url = detail.get("postingUrl")
        if isinstance(posting_url, str) and posting_url:
            updates["posting_url"] = posting_url

        return posting.model_copy(update=updates)
