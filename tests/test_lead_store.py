"""Part II persistence tests — spec §15.2. Uses tmp_path so nothing is ever
written to the real `.data/` directory (same discipline as
test_lifecycle_store.py / test_run_ledger.py).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from gtm_agent.core.lead_store import (
    CompanyContextStore,
    LeadDiscoveryRunLedger,
    LeadFeedbackStore,
    LeadJobMatchStore,
    LeadStore,
    UnmatchedJobStore,
)
from gtm_agent.models.company_context import CompanyContext
from gtm_agent.models.feedback import FeedbackRating, LeadFeedback
from gtm_agent.models.lead import EnrichmentStatus, LeadDiscoveryStatus, LeadRecord, LeadSource
from gtm_agent.models.matching import LeadJobMatch, UnmatchedJob, UnmatchedReason

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _lead(lead_id: str, company_id: str = "acme") -> LeadRecord:
    return LeadRecord(
        lead_id=lead_id, company_id=company_id, source=LeadSource.APOLLO, full_name="X",
        title_raw="X", title_canonical="X", retrieved_at=NOW, enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )


# --- LeadStore --------------------------------------------------------


def test_lead_store_empty_by_default(tmp_path: Path):
    store = LeadStore(tmp_path / "leads.jsonl")
    assert store.current_leads("acme") == {}


def test_lead_store_round_trips(tmp_path: Path):
    store = LeadStore(tmp_path / "leads.jsonl")
    store.save([_lead("l1"), _lead("l2")])
    current = store.current_leads("acme")
    assert set(current) == {"l1", "l2"}


def test_lead_store_last_write_wins(tmp_path: Path):
    store = LeadStore(tmp_path / "leads.jsonl")
    store.save([_lead("l1")])
    updated = _lead("l1").model_copy(update={"full_name": "Updated"})
    store.save([updated])
    assert store.current_leads("acme")["l1"].full_name == "Updated"


def test_lead_store_scopes_by_company(tmp_path: Path):
    store = LeadStore(tmp_path / "leads.jsonl")
    store.save([_lead("l1", company_id="acme"), _lead("l2", company_id="other")])
    assert set(store.current_leads("acme")) == {"l1"}


# --- LeadDiscoveryRunLedger --------------------------------------------


def test_lead_discovery_run_ledger_round_trips(tmp_path: Path):
    ledger = LeadDiscoveryRunLedger(tmp_path / "runs.jsonl")
    run = ledger.begin_run("acme", started_at=NOW)
    ledger.close_run(
        run, status=LeadDiscoveryStatus.LEADS_OK, finished_at=NOW + timedelta(seconds=1),
        personas_requested=["CEO"], leads_returned=3, apollo_credits_used=1, cache_hit=False,
    )
    runs = ledger.list_runs(company_id="acme")
    assert len(runs) == 1
    assert runs[0].status == LeadDiscoveryStatus.LEADS_OK
    assert runs[0].leads_returned == 3


def test_lead_discovery_run_ledger_empty_by_default(tmp_path: Path):
    ledger = LeadDiscoveryRunLedger(tmp_path / "runs.jsonl")
    assert ledger.list_runs() == []


# --- LeadJobMatchStore --------------------------------------------------


def _match(job_id: str, lead_id: str) -> LeadJobMatch:
    return LeadJobMatch(
        id=f"{job_id}-{lead_id}", job_id=job_id, lead_id=lead_id, match_score=0.8, match_confidence=0.9,
        signals={}, rank_within_job=1, computed_at=NOW, rules_version="v1",
    )


def test_lead_job_match_store_round_trips_and_filters(tmp_path: Path):
    store = LeadJobMatchStore(tmp_path / "matches.jsonl")
    store.append([_match("j1", "l1"), _match("j2", "l2")])
    assert len(store.list_matches()) == 2
    assert len(store.list_matches(job_id="j1")) == 1


# --- UnmatchedJobStore ----------------------------------------------------


def test_unmatched_job_store_round_trips(tmp_path: Path):
    store = UnmatchedJobStore(tmp_path / "unmatched.jsonl")
    store.append(
        [UnmatchedJob(job_id="j1", reason=UnmatchedReason.NO_PLAUSIBLE_OWNER, recorded_at=NOW, run_id="r1")]
    )
    entries = store.list_unmatched()
    assert len(entries) == 1
    assert entries[0].reason == UnmatchedReason.NO_PLAUSIBLE_OWNER


# --- CompanyContextStore ------------------------------------------------


def test_company_context_store_get_returns_none_when_absent(tmp_path: Path):
    store = CompanyContextStore(tmp_path / "context.jsonl")
    assert store.get("acme") is None


def test_company_context_store_last_write_wins(tmp_path: Path):
    store = CompanyContextStore(tmp_path / "context.jsonl")
    store.save(CompanyContext(company_id="acme", summary="v1", fetched_at=NOW, expires_at=NOW + timedelta(days=7)))
    store.save(CompanyContext(company_id="acme", summary="v2", fetched_at=NOW, expires_at=NOW + timedelta(days=7)))
    assert store.get("acme").summary == "v2"


# --- LeadFeedbackStore ----------------------------------------------------


def test_lead_feedback_store_round_trips_and_filters(tmp_path: Path):
    store = LeadFeedbackStore(tmp_path / "feedback.jsonl")
    store.append(
        [
            LeadFeedback(
                id="f1", match_id="m1", job_id="j1", lead_id="l1",
                rating=FeedbackRating.USEFUL, submitted_at=NOW,
            ),
            LeadFeedback(
                id="f2", match_id="m2", job_id="j2", lead_id="l2",
                rating=FeedbackRating.NOT_USEFUL, submitted_at=NOW,
            ),
        ]
    )
    assert len(store.list_feedback()) == 2
    assert len(store.list_feedback(job_id="j1")) == 1
    assert store.list_feedback(lead_id="l2")[0].rating == FeedbackRating.NOT_USEFUL
