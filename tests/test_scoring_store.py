"""Part III persistence tests — spec §15.2, §14.3, §14.4."""

from datetime import UTC, datetime
from pathlib import Path

from gtm_agent.core.scoring_store import GtmLeadStore, PublicationEventStore, ScoredLeadStore
from gtm_agent.models.publication import (
    CompanySummary,
    GtmLead,
    JobSummary,
    LeadSummary,
    PublicationEvent,
    PublicationEventType,
)
from gtm_agent.models.scoring import ScoredLead

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _scored(**overrides) -> ScoredLead:
    base = dict(
        id="s1", match_id="m1", relevance_score=0.9, confidence_score=0.85, rationale="x",
        cited_signals=[], prompt_version="v1", job_version="jv1", lead_version="lv1", scored_at=NOW,
    )
    base.update(overrides)
    return ScoredLead(**base)


def test_scored_lead_store_cache_miss_on_fresh_store(tmp_path: Path):
    store = ScoredLeadStore(tmp_path / "scored.jsonl")
    assert store.get_cached(match_id="m1", prompt_version="v1", job_version="jv1", lead_version="lv1") is None


def test_scored_lead_store_cache_hit_on_matching_key(tmp_path: Path):
    store = ScoredLeadStore(tmp_path / "scored.jsonl")
    store.save(_scored())
    cached = store.get_cached(match_id="m1", prompt_version="v1", job_version="jv1", lead_version="lv1")
    assert cached is not None
    assert cached.relevance_score == 0.9


def test_scored_lead_store_cache_miss_when_job_version_changed(tmp_path: Path):
    store = ScoredLeadStore(tmp_path / "scored.jsonl")
    store.save(_scored())
    assert store.get_cached(match_id="m1", prompt_version="v1", job_version="jv2", lead_version="lv1") is None


def test_scored_lead_store_cache_miss_when_prompt_version_changed(tmp_path: Path):
    store = ScoredLeadStore(tmp_path / "scored.jsonl")
    store.save(_scored())
    assert store.get_cached(match_id="m1", prompt_version="v2", job_version="jv1", lead_version="lv1") is None


def test_publication_event_store_round_trips_and_filters(tmp_path: Path):
    store = PublicationEventStore(tmp_path / "events.jsonl")
    store.append(
        [
            PublicationEvent(id="e1", event_type=PublicationEventType.LEAD_READY, job_id="j1", lead_id="l1", occurred_at=NOW),
            PublicationEvent(id="e2", event_type=PublicationEventType.JOB_CLOSED, job_id="j2", occurred_at=NOW),
        ]
    )
    assert len(store.list_events()) == 2
    assert len(store.list_events(job_id="j1")) == 1


def _gtm_lead(**overrides) -> GtmLead:
    base = dict(
        company=CompanySummary(name="Acme", domain="acme.com"),
        job=JobSummary(title="Engineer", posting_url="https://acme.com/jobs/1"),
        lead=LeadSummary(name="Jamie", title="CTO", contactability="verified email"),
        relevance_score=0.9, confidence_score=0.85, rationale="x", rank=1, generated_at=NOW,
    )
    base.update(overrides)
    return GtmLead(**base)


def test_gtm_lead_store_round_trips(tmp_path: Path):
    store = GtmLeadStore(tmp_path / "gtm.jsonl")
    store.append([_gtm_lead()])
    assert len(store.list_all()) == 1


def test_gtm_lead_store_latest_collapses_repeated_publishes(tmp_path: Path):
    store = GtmLeadStore(tmp_path / "gtm.jsonl")
    store.append([_gtm_lead(rank=2)])
    store.append([_gtm_lead(rank=1)])  # same (company, job, lead) key, republished with a better rank
    latest = store.latest()
    assert len(latest) == 1
    assert latest[0].rank == 1
