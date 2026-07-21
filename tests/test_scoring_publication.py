"""Stage 11 — Publication tests (spec §14)."""

from datetime import UTC, datetime
from pathlib import Path

from gtm_agent.models.common import JobFunction, Seniority
from gtm_agent.models.company import Company
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EmailStatus, EnrichmentStatus, LeadRecord, LeadSource
from gtm_agent.models.matching import LeadJobMatch, UnmatchedJob, UnmatchedReason
from gtm_agent.models.publication import PublicationEventType
from gtm_agent.models.scoring import ScoredLead
from gtm_agent.scoring.publication import (
    assemble_gtm_lead,
    publish,
    publish_job_closed,
    publish_unmatched,
    to_csv_rows,
    write_csv,
)
from gtm_agent.scoring.ranking import RankedEntry

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job() -> JobPosting:
    return JobPosting(
        job_id="j1", company_id="acme", source_platform="greenhouse", posting_url="https://acme.com/jobs/1",
        title_raw="Senior Backend Engineer", title_canonical="Senior Backend Engineer",
        description_text="", description_markdown="", function=JobFunction.ENGINEERING, seniority=Seniority.SENIOR,
        location_raw="Remote", first_seen_at=NOW, last_seen_at=NOW,
    )


def _lead() -> LeadRecord:
    return LeadRecord(
        lead_id="l1", company_id="acme", source=LeadSource.APOLLO, full_name="Jamie Chen", title_raw="CTO",
        title_canonical="CTO", is_founder=True, email="jamie@acme.com", email_status=EmailStatus.VERIFIED,
        retrieved_at=NOW, enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )


def _company() -> Company:
    return Company(id="acme", name="Acme", domain="acme.com", added_at=NOW, headcount=18, funding_stage="seed")


def _match() -> LeadJobMatch:
    return LeadJobMatch(
        id="m1", job_id="j1", lead_id="l1", match_score=0.9, match_confidence=0.85,
        signals={"function_alignment": 1.0}, rank_within_job=1, computed_at=NOW, rules_version="v1",
    )


def _scored() -> ScoredLead:
    return ScoredLead(
        id="s1", match_id="m1", relevance_score=0.9, confidence_score=0.85, rationale="Great fit.",
        cited_signals=["function_alignment"], prompt_version="v1", job_version="v1", lead_version="v1", scored_at=NOW,
    )


def test_assemble_gtm_lead_maps_all_fields():
    entry = RankedEntry(scored=_scored(), job=_job(), lead=_lead(), priority=0.9)
    gtm_lead = assemble_gtm_lead(entry, company=_company(), match=_match(), context=None, rank=1)
    assert gtm_lead.company.name == "Acme"
    assert gtm_lead.job.title == "Senior Backend Engineer"
    assert gtm_lead.lead.name == "Jamie Chen"
    assert gtm_lead.lead.contactability == "verified email"
    assert gtm_lead.rank == 1
    assert gtm_lead.match_signals == {"function_alignment": 1.0}


def test_publish_numbers_rank_per_company_and_emits_lead_ready():
    entry = RankedEntry(scored=_scored(), job=_job(), lead=_lead(), priority=0.9)
    gtm_leads, events = publish(
        [entry],
        matches_by_id={"m1": _match()},
        company_by_id={"acme": _company()},
        company_id_by_job_id={"j1": "acme"},
        context_by_company={},
    )
    assert len(gtm_leads) == 1
    assert gtm_leads[0].rank == 1
    assert events[0].event_type == PublicationEventType.LEAD_READY
    assert events[0].job_id == "j1"
    assert events[0].lead_id == "l1"


def test_publish_resets_rank_counter_per_company():
    job_a1, job_a2 = _job(), _job()
    job_a2.job_id = "j2"
    match_a2 = _match().model_copy(update={"id": "m2", "job_id": "j2"})
    scored_a2 = _scored().model_copy(update={"id": "s2", "match_id": "m2"})

    entries = [
        RankedEntry(scored=_scored(), job=job_a1, lead=_lead(), priority=0.9),
        RankedEntry(scored=scored_a2, job=job_a2, lead=_lead(), priority=0.8),
    ]
    gtm_leads, _ = publish(
        entries,
        matches_by_id={"m1": _match(), "m2": match_a2},
        company_by_id={"acme": _company()},
        company_id_by_job_id={"j1": "acme", "j2": "acme"},
        context_by_company={},
    )
    assert [g.rank for g in gtm_leads] == [1, 2]


def test_publish_unmatched_emits_job_unmatched_with_reason():
    unmatched = [UnmatchedJob(job_id="j1", reason=UnmatchedReason.NO_PLAUSIBLE_OWNER, recorded_at=NOW, run_id="r1")]
    events = publish_unmatched(unmatched, now=NOW)
    assert events[0].event_type == PublicationEventType.JOB_UNMATCHED
    assert events[0].payload["reason"] == "no_plausible_owner"


def test_publish_job_closed_emits_one_event_per_job():
    events = publish_job_closed(["j1", "j2"], now=NOW)
    assert len(events) == 2
    assert all(e.event_type == PublicationEventType.JOB_CLOSED for e in events)


def test_to_csv_rows_and_write_csv(tmp_path: Path):
    entry = RankedEntry(scored=_scored(), job=_job(), lead=_lead(), priority=0.9)
    gtm_leads, _ = publish(
        [entry],
        matches_by_id={"m1": _match()},
        company_by_id={"acme": _company()},
        company_id_by_job_id={"j1": "acme"},
        context_by_company={},
    )
    rows = to_csv_rows(gtm_leads)
    assert rows[0]["company_name"] == "Acme"
    assert rows[0]["lead_email"] == "jamie@acme.com"

    csv_path = tmp_path / "out.csv"
    write_csv(csv_path, gtm_leads)
    content = csv_path.read_text(encoding="utf-8")
    assert "Acme" in content
    assert "Jamie Chen" in content
