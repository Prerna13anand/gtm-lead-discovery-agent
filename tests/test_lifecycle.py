"""Stage 5 — Change Detection & Identity tests (spec §8), deterministic and
offline (spec §20.1) — no network, no filesystem (persistence is covered
separately in test_lifecycle_store.py).
"""

from datetime import UTC, datetime, timedelta

from gtm_agent.discovery.lifecycle import (
    ZeroJobsDecision,
    apply_lifecycle,
    evaluate_zero_jobs_suspicious,
    is_first_successful_scrape,
    pair_with_identity_strategy,
    previous_run_was_zero_jobs_suspicious,
    previously_open_count,
    run_stage5,
)
from gtm_agent.models.job import JobPosting, RawPosting
from gtm_agent.models.lifecycle import IdentityStrategy, JobLifecycleStatus, JobPostingRecord, ScrapeEventType
from gtm_agent.models.scrape_run import ScrapeRun, ScrapeRunStatus

_COMPANY = "acme"


def _raw(job_id: str | None = "1", *, url: str | None = "https://acme.com/jobs/1") -> RawPosting:
    return RawPosting(
        company_id=_COMPANY,
        source_platform="greenhouse",
        source_job_id=job_id,
        posting_url=url,
        raw_payload={},
        fetched_at=datetime.now(UTC),
    )


def _job(job_id: str = "acme:greenhouse:1", *, title: str = "Engineer", t: datetime, **overrides) -> JobPosting:
    defaults = dict(
        job_id=job_id,
        company_id=_COMPANY,
        source_platform="greenhouse",
        posting_url="https://acme.com/jobs/1",
        title_raw=title,
        title_canonical=title,
        description_text="",
        description_markdown="",
        first_seen_at=t,
        last_seen_at=t,
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


def _record(job: JobPosting, *, status: JobLifecycleStatus, absences: int = 0, missing_since=None) -> JobPostingRecord:
    return JobPostingRecord(
        **job.model_dump(),
        identity_strategy=IdentityStrategy.ATS_NATIVE_ID,
        status=status,
        consecutive_absences=absences,
        missing_since=missing_since,
    )


# --- pair_with_identity_strategy (spec §8.1) ---


def test_pair_uses_ats_native_id_when_source_job_id_present():
    raw = [_raw("123", url="https://acme.com/jobs/123")]
    jobs = [_job(t=datetime.now(UTC))]
    [obs] = pair_with_identity_strategy(raw, jobs)
    assert obs.identity_strategy == IdentityStrategy.ATS_NATIVE_ID


def test_pair_uses_canonical_url_when_no_source_job_id():
    raw = [_raw(None, url="https://acme.com/jobs/123")]
    jobs = [_job(t=datetime.now(UTC))]
    [obs] = pair_with_identity_strategy(raw, jobs)
    assert obs.identity_strategy == IdentityStrategy.CANONICAL_URL


def test_pair_uses_content_hash_when_neither_present():
    raw = [_raw(None, url=None)]
    jobs = [_job(t=datetime.now(UTC))]
    [obs] = pair_with_identity_strategy(raw, jobs)
    assert obs.identity_strategy == IdentityStrategy.CONTENT_HASH


# --- new jobs / board_first_seen ---


def test_new_job_opens_and_emits_job_opened():
    t = datetime.now(UTC)
    obs = pair_with_identity_strategy([_raw()], [_job(t=t)])
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run1", observed_at=t, new_observations=obs,
        previous_records={}, is_first_successful_scrape=False,
    )
    assert len(result.records) == 1
    assert result.records[0].status == JobLifecycleStatus.OPEN
    event_types = {e.event_type for e in result.events}
    assert ScrapeEventType.JOB_OPENED in event_types
    assert ScrapeEventType.BOARD_FIRST_SEEN not in event_types


def test_first_successful_scrape_emits_board_first_seen_even_with_zero_jobs():
    t = datetime.now(UTC)
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run1", observed_at=t, new_observations=[],
        previous_records={}, is_first_successful_scrape=True,
    )
    assert len(result.events) == 1
    assert result.events[0].event_type == ScrapeEventType.BOARD_FIRST_SEEN
    assert result.events[0].job_id is None


def test_not_first_scrape_does_not_emit_board_first_seen():
    t = datetime.now(UTC)
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run1", observed_at=t, new_observations=[],
        previous_records={}, is_first_successful_scrape=False,
    )
    assert result.events == []


# --- re-observed while OPEN ---


def test_reobserved_unchanged_job_stays_open_with_no_event():
    t0 = datetime.now(UTC)
    old = _record(_job(t=t0), status=JobLifecycleStatus.OPEN)
    t1 = t0 + timedelta(days=1)
    obs = pair_with_identity_strategy([_raw()], [_job(t=t1)])
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run2", observed_at=t1, new_observations=obs,
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    assert result.records[0].status == JobLifecycleStatus.OPEN
    assert result.events == []
    assert result.versions == []
    # first_seen_at preserved, not overwritten to "now" (spec §16.4)
    assert result.records[0].first_seen_at == t0


def test_reobserved_material_change_emits_job_updated_and_version():
    t0 = datetime.now(UTC)
    old = _record(_job(t=t0, title="Engineer"), status=JobLifecycleStatus.OPEN)
    t1 = t0 + timedelta(days=1)
    obs = pair_with_identity_strategy([_raw()], [_job(t=t1, title="Senior Engineer")])
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run2", observed_at=t1, new_observations=obs,
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    assert [e.event_type for e in result.events] == [ScrapeEventType.JOB_UPDATED]
    assert result.events[0].payload["title_canonical"] == {"old": "Engineer", "new": "Senior Engineer"}
    assert len(result.versions) == 1
    assert result.versions[0].changed_fields["title_canonical"]["new"] == "Senior Engineer"


def test_reobserved_non_material_change_emits_no_event():
    # description_text isn't a material field (spec §8.5: title/location/seniority).
    t0 = datetime.now(UTC)
    old = _record(_job(t=t0, description_text="v1"), status=JobLifecycleStatus.OPEN)
    t1 = t0 + timedelta(days=1)
    obs = pair_with_identity_strategy([_raw()], [_job(t=t1, description_text="v2, a typo fix")])
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run2", observed_at=t1, new_observations=obs,
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    assert result.events == []
    assert result.versions == []


# --- OPEN -> MISSING ---


def test_open_job_not_observed_becomes_missing_with_no_event():
    t0 = datetime.now(UTC)
    old = _record(_job(t=t0), status=JobLifecycleStatus.OPEN)
    t1 = t0 + timedelta(days=1)
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run2", observed_at=t1, new_observations=[],
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    assert result.events == []
    [record] = result.records
    assert record.status == JobLifecycleStatus.MISSING
    assert record.consecutive_absences == 1
    assert record.missing_since == t1


# --- MISSING -> OPEN (reappears within grace window) ---


def test_missing_job_reappearing_within_grace_window_returns_to_open_silently():
    t0 = datetime.now(UTC)
    old = _record(_job(t=t0), status=JobLifecycleStatus.MISSING, absences=1, missing_since=t0 + timedelta(days=1))
    t2 = t0 + timedelta(days=2)
    obs = pair_with_identity_strategy([_raw()], [_job(t=t2)])
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run3", observed_at=t2, new_observations=obs,
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    # Spec §8.2 diagram: the MISSING -> OPEN loop is unlabelled -- no event.
    assert result.events == []
    [record] = result.records
    assert record.status == JobLifecycleStatus.OPEN
    assert record.consecutive_absences == 0
    assert record.missing_since is None
    assert record.first_seen_at == t0  # preserved, this was never a full close/reopen


# --- MISSING -> CLOSED (grace window) ---


def test_missing_job_closes_once_both_grace_conditions_are_met():
    t0 = datetime.now(UTC)
    missing_since = t0
    old = _record(_job(t=t0), status=JobLifecycleStatus.MISSING, absences=1, missing_since=missing_since)
    # Second consecutive absence AND >= 7 days since it went missing.
    t1 = missing_since + timedelta(days=7)
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run3", observed_at=t1, new_observations=[],
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    [record] = result.records
    assert record.status == JobLifecycleStatus.CLOSED
    assert record.closed_at == t1
    assert [e.event_type for e in result.events] == [ScrapeEventType.JOB_CLOSED]


def test_missing_job_stays_missing_if_absence_count_met_but_under_7_days():
    # Daily cadence: 2 consecutive absences is only 2 days -- the 7-day floor
    # is what actually gates closure here ("whichever is longer").
    t0 = datetime.now(UTC)
    missing_since = t0
    old = _record(_job(t=t0), status=JobLifecycleStatus.MISSING, absences=1, missing_since=missing_since)
    t1 = missing_since + timedelta(days=1)  # 2nd absence, but only 1 day elapsed
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run3", observed_at=t1, new_observations=[],
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    [record] = result.records
    assert record.status == JobLifecycleStatus.MISSING
    assert record.consecutive_absences == 2
    assert result.events == []


def test_missing_job_stays_missing_if_7_days_elapsed_but_only_one_absence():
    # Monthly cadence: 7+ days elapsed after just one absence isn't enough on
    # its own -- 2 consecutive absences is still required.
    t0 = datetime.now(UTC)
    missing_since = t0
    old = _record(_job(t=t0), status=JobLifecycleStatus.MISSING, absences=0, missing_since=None)
    t1 = t0 + timedelta(days=10)
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run2", observed_at=t1, new_observations=[],
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    [record] = result.records
    assert record.status == JobLifecycleStatus.MISSING
    assert record.consecutive_absences == 1
    assert result.events == []


# --- CLOSED -> OPEN (reopened) ---


def test_closed_job_reappearing_emits_job_reopened_with_new_first_seen():
    t0 = datetime.now(UTC)
    closed_at = t0 + timedelta(days=10)
    old = _record(_job(t=t0), status=JobLifecycleStatus.CLOSED, absences=2, missing_since=t0)
    old = old.model_copy(update={"closed_at": closed_at})
    t1 = closed_at + timedelta(days=5)
    obs = pair_with_identity_strategy([_raw()], [_job(t=t1)])
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run4", observed_at=t1, new_observations=obs,
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    [record] = result.records
    assert record.status == JobLifecycleStatus.OPEN
    assert record.consecutive_absences == 0
    assert record.missing_since is None
    # Spec §8.2 diagram's explicit "(reopened, new first_seen)" annotation.
    assert record.first_seen_at == t1
    assert [e.event_type for e in result.events] == [ScrapeEventType.JOB_REOPENED]


def test_closed_job_not_observed_is_carried_forward_unchanged():
    t0 = datetime.now(UTC)
    old = _record(_job(t=t0), status=JobLifecycleStatus.CLOSED, absences=2, missing_since=t0)
    t1 = t0 + timedelta(days=30)
    result = apply_lifecycle(
        company_id=_COMPANY, run_id="run5", observed_at=t1, new_observations=[],
        previous_records={old.job_id: old}, is_first_successful_scrape=False,
    )
    assert result.records == [old]
    assert result.events == []


# --- previously_open_count ---


def test_previously_open_count_excludes_closed():
    t0 = datetime.now(UTC)
    records = {
        "1": _record(_job("acme:greenhouse:1", t=t0), status=JobLifecycleStatus.OPEN),
        "2": _record(_job("acme:greenhouse:2", t=t0), status=JobLifecycleStatus.MISSING, absences=1, missing_since=t0),
        "3": _record(_job("acme:greenhouse:3", t=t0), status=JobLifecycleStatus.CLOSED, absences=2, missing_since=t0),
    }
    assert previously_open_count(records) == 2


# --- zero_jobs_suspicious decision table (spec §17.1) ---


def test_zero_jobs_not_suspicious_when_jobs_found():
    decision = evaluate_zero_jobs_suspicious(
        new_job_count=5, previously_open_count=3, previous_run_was_zero_jobs_suspicious=False
    )
    assert decision == ZeroJobsDecision.NOT_SUSPICIOUS


def test_zero_jobs_not_suspicious_when_nothing_was_open_before():
    # A genuinely empty board with no prior open jobs is a real negative (spec §4.4).
    decision = evaluate_zero_jobs_suspicious(
        new_job_count=0, previously_open_count=0, previous_run_was_zero_jobs_suspicious=False
    )
    assert decision == ZeroJobsDecision.NOT_SUSPICIOUS


def test_zero_jobs_held_for_review_on_first_occurrence():
    decision = evaluate_zero_jobs_suspicious(
        new_job_count=0, previously_open_count=12, previous_run_was_zero_jobs_suspicious=False
    )
    assert decision == ZeroJobsDecision.HOLD_FOR_REVIEW


def test_zero_jobs_confirmed_board_emptied_on_second_verified_occurrence():
    decision = evaluate_zero_jobs_suspicious(
        new_job_count=0, previously_open_count=12, previous_run_was_zero_jobs_suspicious=True
    )
    assert decision == ZeroJobsDecision.CONFIRMED_BOARD_EMPTIED


# --- ledger-derived helpers ---


def _run(status: ScrapeRunStatus | None, started_at: datetime) -> ScrapeRun:
    return ScrapeRun(id="r", company_id=_COMPANY, started_at=started_at, status=status)


def test_is_first_successful_scrape_true_with_no_prior_runs():
    assert is_first_successful_scrape([]) is True


def test_is_first_successful_scrape_false_after_a_prior_success():
    t0 = datetime.now(UTC)
    runs = [_run(ScrapeRunStatus.SUCCESS, t0)]
    assert is_first_successful_scrape(runs) is False


def test_is_first_successful_scrape_false_after_a_prior_parse_degraded():
    t0 = datetime.now(UTC)
    runs = [_run(ScrapeRunStatus.PARSE_DEGRADED, t0)]
    assert is_first_successful_scrape(runs) is False


def test_is_first_successful_scrape_true_when_prior_runs_all_failed():
    t0 = datetime.now(UTC)
    runs = [_run(ScrapeRunStatus.BLOCKED_403, t0), _run(ScrapeRunStatus.RATE_LIMITED, t0 + timedelta(days=1))]
    assert is_first_successful_scrape(runs) is True


def test_previous_run_was_zero_jobs_suspicious_true_when_most_recent_run_was():
    t0 = datetime.now(UTC)
    runs = [
        _run(ScrapeRunStatus.SUCCESS, t0),
        _run(ScrapeRunStatus.ZERO_JOBS_SUSPICIOUS, t0 + timedelta(days=1)),
    ]
    assert previous_run_was_zero_jobs_suspicious(runs) is True


def test_previous_run_was_zero_jobs_suspicious_false_when_most_recent_run_was_not():
    t0 = datetime.now(UTC)
    runs = [
        _run(ScrapeRunStatus.ZERO_JOBS_SUSPICIOUS, t0),
        _run(ScrapeRunStatus.SUCCESS, t0 + timedelta(days=1)),
    ]
    assert previous_run_was_zero_jobs_suspicious(runs) is False


def test_previous_run_was_zero_jobs_suspicious_false_with_no_history():
    assert previous_run_was_zero_jobs_suspicious([]) is False


# --- run_stage5 (the combined entry point main.py calls) ---


def test_run_stage5_normal_path_commits_lifecycle():
    t = datetime.now(UTC)
    raw = [_raw()]
    jobs = [_job(t=t)]
    outcome = run_stage5(
        company_id=_COMPANY, run_id="run1", observed_at=t,
        raw_postings=raw, job_postings=jobs, previous_records={}, prior_runs=[],
    )
    assert outcome.decision == ZeroJobsDecision.NOT_SUSPICIOUS
    assert outcome.lifecycle is not None
    assert len(outcome.lifecycle.records) == 1
    assert ScrapeEventType.JOB_OPENED in {e.event_type for e in outcome.lifecycle.events}
    assert ScrapeEventType.BOARD_FIRST_SEEN in {e.event_type for e in outcome.lifecycle.events}


def test_run_stage5_holds_for_review_and_returns_no_lifecycle():
    t0 = datetime.now(UTC)
    previous = {"acme:greenhouse:1": _record(_job(t=t0), status=JobLifecycleStatus.OPEN)}
    prior_runs = [_run(ScrapeRunStatus.SUCCESS, t0)]
    outcome = run_stage5(
        company_id=_COMPANY, run_id="run2", observed_at=t0 + timedelta(days=1),
        raw_postings=[], job_postings=[], previous_records=previous, prior_runs=prior_runs,
    )
    assert outcome.decision == ZeroJobsDecision.HOLD_FOR_REVIEW
    assert outcome.lifecycle is None


def test_run_stage5_confirms_board_emptied_and_emits_company_level_event():
    t0 = datetime.now(UTC)
    previous = {"acme:greenhouse:1": _record(_job(t=t0), status=JobLifecycleStatus.OPEN)}
    prior_runs = [
        _run(ScrapeRunStatus.SUCCESS, t0),
        _run(ScrapeRunStatus.ZERO_JOBS_SUSPICIOUS, t0 + timedelta(days=1)),
    ]
    t2 = t0 + timedelta(days=2)
    outcome = run_stage5(
        company_id=_COMPANY, run_id="run3", observed_at=t2,
        raw_postings=[], job_postings=[], previous_records=previous, prior_runs=prior_runs,
    )
    assert outcome.decision == ZeroJobsDecision.CONFIRMED_BOARD_EMPTIED
    assert outcome.lifecycle is not None
    board_emptied = [e for e in outcome.lifecycle.events if e.event_type == ScrapeEventType.BOARD_EMPTIED]
    assert len(board_emptied) == 1
    assert board_emptied[0].job_id is None
    assert board_emptied[0].company_id == _COMPANY
    # the previously-open job still goes through the ordinary MISSING transition
    assert outcome.lifecycle.records[0].status == JobLifecycleStatus.MISSING
