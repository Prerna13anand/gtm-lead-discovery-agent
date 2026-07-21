"""Stage 5 persistence tests — spec §15.1. Uses tmp_path so nothing is ever
written to the real `.data/` directory during tests (same discipline as
test_run_ledger.py).
"""

from datetime import UTC, datetime
from pathlib import Path

from gtm_agent.core.lifecycle_store import JobPostingStore, JobPostingVersionLedger, ScrapeEventLog
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lifecycle import (
    IdentityStrategy,
    JobLifecycleStatus,
    JobPostingRecord,
    JobPostingVersion,
    ScrapeEvent,
    ScrapeEventType,
)


def _record(company_id: str, job_id: str, *, status: JobLifecycleStatus = JobLifecycleStatus.OPEN) -> JobPostingRecord:
    t = datetime.now(UTC)
    job = JobPosting(
        job_id=job_id,
        company_id=company_id,
        source_platform="greenhouse",
        posting_url="https://acme.com/jobs/1",
        title_raw="Engineer",
        title_canonical="Engineer",
        description_text="",
        description_markdown="",
        first_seen_at=t,
        last_seen_at=t,
    )
    return JobPostingRecord(
        **job.model_dump(), identity_strategy=IdentityStrategy.ATS_NATIVE_ID, status=status,
    )


# --- JobPostingStore ---


def test_current_records_on_a_fresh_store_is_empty(tmp_path: Path):
    store = JobPostingStore(tmp_path / "job_postings.jsonl")
    assert store.current_records("acme") == {}


def test_save_and_read_back_round_trips(tmp_path: Path):
    store = JobPostingStore(tmp_path / "job_postings.jsonl")
    record = _record("acme", "acme:greenhouse:1")
    store.save([record])

    records = store.current_records("acme")
    assert set(records) == {"acme:greenhouse:1"}
    assert records["acme:greenhouse:1"].status == JobLifecycleStatus.OPEN


def test_later_save_wins_as_current_state(tmp_path: Path):
    store = JobPostingStore(tmp_path / "job_postings.jsonl")
    store.save([_record("acme", "acme:greenhouse:1", status=JobLifecycleStatus.OPEN)])
    store.save([_record("acme", "acme:greenhouse:1", status=JobLifecycleStatus.MISSING)])

    records = store.current_records("acme")
    assert records["acme:greenhouse:1"].status == JobLifecycleStatus.MISSING


def test_current_records_scoped_to_company(tmp_path: Path):
    store = JobPostingStore(tmp_path / "job_postings.jsonl")
    store.save([_record("acme", "acme:greenhouse:1"), _record("beta", "beta:greenhouse:1")])

    assert set(store.current_records("acme")) == {"acme:greenhouse:1"}
    assert set(store.current_records("beta")) == {"beta:greenhouse:1"}


def test_save_with_empty_list_is_a_noop(tmp_path: Path):
    path = tmp_path / "job_postings.jsonl"
    store = JobPostingStore(path)
    store.save([])
    assert not path.exists()


# --- JobPostingVersionLedger ---


def test_version_ledger_round_trips_and_filters_by_job_id(tmp_path: Path):
    ledger = JobPostingVersionLedger(tmp_path / "versions.jsonl")
    v1 = JobPostingVersion(
        id="v1", job_id="acme:greenhouse:1", company_id="acme", observed_at=datetime.now(UTC),
        run_id="run1", changed_fields={"title_canonical": {"old": "Engineer", "new": "Senior Engineer"}},
        snapshot={},
    )
    v2 = JobPostingVersion(
        id="v2", job_id="acme:greenhouse:2", company_id="acme", observed_at=datetime.now(UTC),
        run_id="run1", changed_fields={}, snapshot={},
    )
    ledger.append([v1, v2])

    assert [v.id for v in ledger.list_versions("acme:greenhouse:1")] == ["v1"]
    assert [v.id for v in ledger.list_versions("acme:greenhouse:2")] == ["v2"]
    assert ledger.list_versions("acme:greenhouse:999") == []


def test_version_ledger_fresh_is_empty(tmp_path: Path):
    ledger = JobPostingVersionLedger(tmp_path / "versions.jsonl")
    assert ledger.list_versions("anything") == []


# --- ScrapeEventLog ---


def test_event_log_round_trips_and_filters_by_company(tmp_path: Path):
    log = ScrapeEventLog(tmp_path / "events.jsonl")
    e1 = ScrapeEvent(
        id="e1", company_id="acme", job_id="acme:greenhouse:1", event_type=ScrapeEventType.JOB_OPENED,
        occurred_at=datetime.now(UTC), run_id="run1",
    )
    e2 = ScrapeEvent(
        id="e2", company_id="beta", job_id=None, event_type=ScrapeEventType.BOARD_FIRST_SEEN,
        occurred_at=datetime.now(UTC), run_id="run2",
    )
    log.append([e1, e2])

    assert [e.id for e in log.list_events()] == ["e1", "e2"]
    assert [e.id for e in log.list_events(company_id="acme")] == ["e1"]
    assert [e.id for e in log.list_events(company_id="beta")] == ["e2"]


def test_event_log_fresh_is_empty(tmp_path: Path):
    log = ScrapeEventLog(tmp_path / "events.jsonl")
    assert log.list_events() == []
