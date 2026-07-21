"""Stage 5 — Change Detection & Identity (spec §8).

**Goal:** determine what changed since the last run, and emit the events
that trigger the downstream pipeline — "what makes the component an *agent*
rather than a batch scraper" (spec §8).

This module is pure decision logic: given this run's normalised `JobPosting`s
(Stage 4 output) and the company's previously-persisted lifecycle state, it
computes the new state, the events to emit, and the version rows to record.
It does no I/O itself — persistence lives in `core.lifecycle_store`, and the
caller (`main.py`) is responsible for loading previous state before calling
`run_stage5` (the module's main entry point, combining the §17.1
`zero_jobs_suspicious` check with `apply_lifecycle`) and persisting the
result after. This mirrors the split already established between `main.py`'s
`_extraction_reached_stage4` / `_final_run_status` (pure) and `ScrapeRunLedger`
(I/O) for Stage 3 — and, same as there, keeping the decision logic pure and
I/O-free here is what makes it directly unit-testable without a real ledger
or filesystem.

### The lifecycle (spec §8.2)

```
                  ┌──────────────────────────────────────┐
                  ▼                                      │
   (unseen) ──► OPEN ──► MISSING ──► CLOSED ──► (reopened, new first_seen)
                  ▲         │
                  └─────────┘
                  reappears within grace window
```

- A job re-observed while OPEN or MISSING returns to/stays OPEN. Reappearing
  from MISSING (i.e. within the grace window) is silent — no event, per the
  diagram: only the CLOSED -> "(reopened, new first_seen)" arc is labelled.
- A job whose grace window (§8.3) elapses while MISSING transitions to
  CLOSED and emits `job_closed`.
- A job re-observed after CLOSED is a `job_reopened` event, and — per the
  diagram's explicit "new first_seen" annotation — `first_seen_at` is reset
  to this run's timestamp rather than preserved, unlike every other
  re-observation (spec §16.4: "`first_seen_at` is set once and never
  updated" describes the ordinary case; a full close/reopen cycle is what
  that annotation calls out as the exception).

### The grace window (spec §8.3)

**"Absent from 2 consecutive successful scrapes, or 7 days, whichever is
longer."** Read as: a job is not CLOSED until *both* conditions hold — at
least 2 consecutive absences *and* at least 7 days since it first went
missing. This is the reading that actually matches "whichever is longer":
on a daily-cadence company, 2 scrapes is only 2 days, so the 7-day floor is
what actually gates closure; on a monthly-cadence company, 2 scrapes is
~60 days, so the absence count is what gates it. Requiring both conditions
is equivalent to waiting for the longer of the two implied durations.

Spec §8.3 also states plainly: **"Absence observed during a failed scrape
does not count toward the window at all... only successful scrapes advance
the lifecycle."** `apply_lifecycle` enforces this by construction — it is
only ever meant to be called after a `SUCCESS` or `PARSE_DEGRADED` Stage 3/4
run (see `main.py`); a hard-failure run never reaches this module at all, so
no absence is ever recorded against a company whose board we simply
couldn't read that day.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from gtm_agent.discovery.normalization import identity_strategy_for
from gtm_agent.models.job import JobPosting, RawPosting
from gtm_agent.models.lifecycle import (
    IdentityStrategy,
    JobLifecycleStatus,
    JobPostingRecord,
    JobPostingVersion,
    ScrapeEvent,
    ScrapeEventType,
)
from gtm_agent.models.scrape_run import ScrapeRun, ScrapeRunStatus

# Spec §8.3: both conditions must hold before a MISSING job closes.
_GRACE_WINDOW_MIN_CONSECUTIVE_ABSENCES = 2
_GRACE_WINDOW_MIN_DAYS = 7

# Spec §8.5's own examples — a retitle ("Engineer" -> "Senior Engineer") and a
# relocation (onsite -> remote) — map onto these fields. The spec doesn't
# give an exhaustive list; these four are the fields a GTM-relevant "this
# role materially changed" signal should hinge on. `description_text` is
# deliberately excluded, same as the §8.1 content-hash identity fallback:
# copy edits and typo fixes aren't material changes worth a version row.
_MATERIAL_FIELDS = ("title_canonical", "seniority", "workplace_type", "location_raw")


@dataclass
class NewJobObservation:
    """One posting from this run, paired with which identity-ladder rung
    produced its `job_id` (spec §8.1) — computed from the `RawPosting` that
    produced it, since `JobPosting` itself doesn't carry that (see
    `normalization.identity_strategy_for`).
    """

    job: JobPosting
    identity_strategy: IdentityStrategy


def pair_with_identity_strategy(
    raw_postings: list[RawPosting], job_postings: list[JobPosting]
) -> list[NewJobObservation]:
    """`normalize_batch` produces `job_postings` in the same order, 1:1, as
    the `raw_postings` it was given — this zips them back together so Stage 5
    knows each job's identity strategy without re-deriving it.
    """
    return [
        NewJobObservation(job=job, identity_strategy=identity_strategy_for(raw))
        for raw, job in zip(raw_postings, job_postings, strict=True)
    ]


@dataclass
class LifecycleResult:
    records: list[JobPostingRecord] = field(default_factory=list)
    events: list[ScrapeEvent] = field(default_factory=list)
    versions: list[JobPostingVersion] = field(default_factory=list)


def apply_lifecycle(
    *,
    company_id: str,
    run_id: str,
    observed_at: datetime,
    new_observations: list[NewJobObservation],
    previous_records: dict[str, JobPostingRecord],
    is_first_successful_scrape: bool,
) -> LifecycleResult:
    """Diff this run's postings against the company's previous lifecycle
    state and compute the new state (spec §8).

    `previous_records` is this company's current `job_posting` rows, keyed
    by `job_id` (from `JobPostingStore.current_records`). `new_observations`
    is empty for a validated, genuinely empty board — that's a legitimate
    input, not an error (spec §2.3); every previously-open job then goes
    through the ordinary MISSING/CLOSED path exactly as if one job had
    disappeared, just for all of them at once. Deciding *whether* an empty
    result should even reach this function is `evaluate_zero_jobs_suspicious`'s
    job, not this one's — by the time `apply_lifecycle` runs, the caller has
    already decided this result is trustworthy enough to commit.
    """
    result = LifecycleResult()

    if is_first_successful_scrape:
        result.events.append(
            _event(
                ScrapeEventType.BOARD_FIRST_SEEN,
                company_id=company_id,
                job_id=None,
                run_id=run_id,
                occurred_at=observed_at,
                payload={"job_count": len(new_observations)},
            )
        )

    new_by_id = {obs.job.job_id: obs for obs in new_observations}
    all_ids = set(new_by_id) | set(previous_records)

    for job_id in all_ids:
        old = previous_records.get(job_id)
        new = new_by_id.get(job_id)

        if old is None:
            result.records.append(_open_new_job(new, run_id, result))
            continue

        if new is not None:
            _handle_reobserved(old, new, run_id, observed_at, result)
            continue

        _handle_absent(old, observed_at, run_id, result)

    return result


def _open_new_job(obs: NewJobObservation, run_id: str, result: LifecycleResult) -> JobPostingRecord:
    record = _to_record(
        obs.job,
        identity_strategy=obs.identity_strategy,
        status=JobLifecycleStatus.OPEN,
        consecutive_absences=0,
        missing_since=None,
    )
    result.events.append(
        _job_event(
            ScrapeEventType.JOB_OPENED,
            record,
            run_id,
            record.first_seen_at,
            payload={"title": record.title_canonical, "source_platform": record.source_platform},
        )
    )
    return record


def _handle_reobserved(
    old: JobPostingRecord,
    new: NewJobObservation,
    run_id: str,
    observed_at: datetime,
    result: LifecycleResult,
) -> None:
    if old.status == JobLifecycleStatus.CLOSED:
        # Spec §8.2 diagram: CLOSED -> "(reopened, new first_seen)".
        record = _to_record(
            new.job,
            identity_strategy=old.identity_strategy,
            status=JobLifecycleStatus.OPEN,
            consecutive_absences=0,
            missing_since=None,
            first_seen_at=observed_at,
        )
        result.records.append(record)
        result.events.append(_job_event(ScrapeEventType.JOB_REOPENED, record, run_id, observed_at))
        return

    # OPEN or MISSING -> OPEN. `first_seen_at` is preserved (spec §16.4),
    # unlike the CLOSED-reopen case above.
    record = _to_record(
        new.job,
        identity_strategy=old.identity_strategy,
        status=JobLifecycleStatus.OPEN,
        consecutive_absences=0,
        missing_since=None,
        first_seen_at=old.first_seen_at,
    )
    result.records.append(record)

    # A MISSING job reappearing within its grace window is silent (spec
    # §8.2 diagram: the MISSING->OPEN loop carries no event label) — only an
    # OPEN job's *content* changing is `job_updated`.
    if old.status == JobLifecycleStatus.OPEN:
        changed = _material_changes(old, new.job)
        if changed:
            result.events.append(
                _job_event(ScrapeEventType.JOB_UPDATED, record, run_id, observed_at, payload=changed)
            )
            result.versions.append(
                JobPostingVersion(
                    id=str(uuid.uuid4()),
                    job_id=record.job_id,
                    company_id=record.company_id,
                    observed_at=observed_at,
                    run_id=run_id,
                    changed_fields=changed,
                    snapshot=record.model_dump(mode="json"),
                )
            )


def _handle_absent(
    old: JobPostingRecord, observed_at: datetime, run_id: str, result: LifecycleResult
) -> None:
    if old.status == JobLifecycleStatus.CLOSED:
        # Nothing changed; carry the row forward unchanged.
        result.records.append(old)
        return

    consecutive_absences = old.consecutive_absences + 1
    missing_since = old.missing_since or observed_at
    elapsed_days = (observed_at - missing_since).days

    if (
        consecutive_absences >= _GRACE_WINDOW_MIN_CONSECUTIVE_ABSENCES
        and elapsed_days >= _GRACE_WINDOW_MIN_DAYS
    ):
        record = old.model_copy(
            update={
                "status": JobLifecycleStatus.CLOSED,
                "consecutive_absences": consecutive_absences,
                "missing_since": missing_since,
                "closed_at": observed_at,
            }
        )
        result.records.append(record)
        result.events.append(_job_event(ScrapeEventType.JOB_CLOSED, record, run_id, observed_at))
        return

    record = old.model_copy(
        update={
            "status": JobLifecycleStatus.MISSING,
            "consecutive_absences": consecutive_absences,
            "missing_since": missing_since,
        }
    )
    result.records.append(record)


def _material_changes(old: JobPostingRecord, new: JobPosting) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    for name in _MATERIAL_FIELDS:
        old_value = getattr(old, name)
        new_value = getattr(new, name)
        if old_value != new_value:
            changed[name] = {"old": old_value, "new": new_value}
    return changed


def _to_record(
    job: JobPosting,
    *,
    identity_strategy: IdentityStrategy,
    status: JobLifecycleStatus,
    consecutive_absences: int,
    missing_since: datetime | None,
    first_seen_at: datetime | None = None,
) -> JobPostingRecord:
    data = job.model_dump()
    if first_seen_at is not None:
        data["first_seen_at"] = first_seen_at
    return JobPostingRecord(
        **data,
        identity_strategy=identity_strategy,
        status=status,
        consecutive_absences=consecutive_absences,
        missing_since=missing_since,
    )


def _event(
    event_type: ScrapeEventType,
    *,
    company_id: str,
    job_id: str | None,
    run_id: str,
    occurred_at: datetime,
    payload: dict[str, Any] | None = None,
    identity_strategy: IdentityStrategy | None = None,
) -> ScrapeEvent:
    return ScrapeEvent(
        id=str(uuid.uuid4()),
        company_id=company_id,
        job_id=job_id,
        event_type=event_type,
        occurred_at=occurred_at,
        run_id=run_id,
        payload=payload or {},
        identity_strategy=identity_strategy,
    )


def _job_event(
    event_type: ScrapeEventType,
    record: JobPostingRecord,
    run_id: str,
    occurred_at: datetime,
    payload: dict[str, Any] | None = None,
) -> ScrapeEvent:
    return _event(
        event_type,
        company_id=record.company_id,
        job_id=record.job_id,
        run_id=run_id,
        occurred_at=occurred_at,
        payload=payload,
        identity_strategy=record.identity_strategy,
    )


def previously_open_count(previous_records: dict[str, JobPostingRecord]) -> int:
    """"0 jobs where N > 0 last time" (spec §17.1) — N is the number of jobs
    we currently believe are still live on the board, i.e. anything not yet
    CLOSED. A MISSING job counts too: it's still within its grace window and
    hasn't been accepted as gone, so it's still part of "N" for this check.
    """
    return sum(1 for record in previous_records.values() if record.status != JobLifecycleStatus.CLOSED)


# --- zero_jobs_suspicious (spec §17, §17.1) ---


class ZeroJobsDecision(StrEnum):
    """The three outcomes of the §17.1 anomaly check."""

    NOT_SUSPICIOUS = "not_suspicious"
    """Either real postings were found, or there was nothing open before —
    an empty board with nothing previously open is a genuine negative
    (spec §4.4), not an anomaly."""

    HOLD_FOR_REVIEW = "hold_for_review"
    """First zero-after-nonzero occurrence this cycle. Spec §17.1 steps 1-4:
    do not publish the empty result; the caller re-verifies fingerprinting
    (see module note below) and retries once next sweep."""

    CONFIRMED_BOARD_EMPTIED = "confirmed_board_emptied"
    """Zero across two verified sweeps in a row — spec §17.1 step 5: "accept
    it as a genuine `board_emptied`." The caller commits this result
    normally (through `apply_lifecycle`, same as any other run) and additionally
    emits one company-level `board_emptied` event."""


def evaluate_zero_jobs_suspicious(
    *,
    new_job_count: int,
    previously_open_count: int,
    previous_run_was_zero_jobs_suspicious: bool,
) -> ZeroJobsDecision:
    """Spec §17.1: "A company that returned 12 jobs yesterday and 0 today is
    *far* more likely to have migrated ATS... than to have closed every role
    overnight."

    Spec §17.1 step 2 ("re-run fingerprinting from scratch — ignore the
    cached identification entirely") is handled by the caller, not this
    function — and in this codebase's current architecture that step is
    already unconditionally true: `main.py` calls Stage 2 fingerprinting
    fresh on every invocation (there is no persistent `AtsIdentification`
    cache yet to ignore). This function's job is purely the decision spec
    §17.1 steps 1 and 4-5 describe: hold vs. publish vs. accept.
    """
    if new_job_count > 0 or previously_open_count == 0:
        return ZeroJobsDecision.NOT_SUSPICIOUS
    if previous_run_was_zero_jobs_suspicious:
        return ZeroJobsDecision.CONFIRMED_BOARD_EMPTIED
    return ZeroJobsDecision.HOLD_FOR_REVIEW


def is_first_successful_scrape(prior_runs: list[ScrapeRun]) -> bool:
    """No prior `SUCCESS` or `PARSE_DEGRADED` run for this company — spec
    §8.4's `board_first_seen` trigger. `prior_runs` should already exclude
    the run currently in progress.
    """
    return not any(
        run.status in (ScrapeRunStatus.SUCCESS, ScrapeRunStatus.PARSE_DEGRADED) for run in prior_runs
    )


def previous_run_was_zero_jobs_suspicious(prior_runs: list[ScrapeRun]) -> bool:
    """Was this company's most recent *closed* run (excluding the one in
    progress) a `zero_jobs_suspicious` hold? Reuses the existing `scrape_run`
    ledger as the record of "already verified once" rather than inventing a
    separate counter — the second verified sweep (spec §17.1 step 5) is
    detected purely from ledger history.
    """
    closed = [run for run in prior_runs if run.status is not None]
    if not closed:
        return False
    most_recent = max(closed, key=lambda run: run.started_at)
    return most_recent.status == ScrapeRunStatus.ZERO_JOBS_SUSPICIOUS


@dataclass
class Stage5Outcome:
    decision: ZeroJobsDecision
    lifecycle: LifecycleResult | None
    """`None` exactly when `decision` is `HOLD_FOR_REVIEW` — nothing to
    persist, since spec §17.1 step 1 is "do not publish the empty result"."""


def run_stage5(
    *,
    company_id: str,
    run_id: str,
    observed_at: datetime,
    raw_postings: list[RawPosting],
    job_postings: list[JobPosting],
    previous_records: dict[str, JobPostingRecord],
    prior_runs: list[ScrapeRun],
) -> Stage5Outcome:
    """Stage 5's single entry point: the §17.1 `zero_jobs_suspicious` check,
    then (unless it holds the result for review) `apply_lifecycle`, with a
    company-level `board_emptied` event appended on confirmed emptying (spec
    §17.1 step 5). `prior_runs` should be every previously *closed* run for
    this company — excluding the run currently in progress — from which both
    `is_first_successful_scrape` and `previous_run_was_zero_jobs_suspicious`
    are derived.
    """
    decision = evaluate_zero_jobs_suspicious(
        new_job_count=len(job_postings),
        previously_open_count=previously_open_count(previous_records),
        previous_run_was_zero_jobs_suspicious=previous_run_was_zero_jobs_suspicious(prior_runs),
    )
    if decision == ZeroJobsDecision.HOLD_FOR_REVIEW:
        return Stage5Outcome(decision=decision, lifecycle=None)

    observations = pair_with_identity_strategy(raw_postings, job_postings)
    result = apply_lifecycle(
        company_id=company_id,
        run_id=run_id,
        observed_at=observed_at,
        new_observations=observations,
        previous_records=previous_records,
        is_first_successful_scrape=is_first_successful_scrape(prior_runs),
    )

    if decision == ZeroJobsDecision.CONFIRMED_BOARD_EMPTIED:
        result.events.append(
            _event(
                ScrapeEventType.BOARD_EMPTIED,
                company_id=company_id,
                job_id=None,
                run_id=run_id,
                occurred_at=observed_at,
                payload={"previously_open_count": previously_open_count(previous_records)},
            )
        )

    return Stage5Outcome(decision=decision, lifecycle=result)
