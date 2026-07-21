"""Part II/III metrics tests — spec §19.3, §13.4."""

from datetime import UTC, datetime

from gtm_agent.core.metrics import compute_disagreement_rate, compute_matching_metrics
from gtm_agent.models.common import JobFunction
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EnrichmentStatus, LeadRecord, LeadSource
from gtm_agent.models.matching import LeadJobMatch, UnmatchedJob, UnmatchedReason
from gtm_agent.models.scoring import ScoredLead

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job(job_id: str, function: JobFunction | None) -> JobPosting:
    return JobPosting(
        job_id=job_id, company_id="acme", source_platform="greenhouse", posting_url=f"https://acme.com/{job_id}",
        title_raw="Role", title_canonical="Role", description_text="", description_markdown="",
        function=function, first_seen_at=NOW, last_seen_at=NOW,
    )


def _lead(lead_id: str, is_founder: bool = False) -> LeadRecord:
    return LeadRecord(
        lead_id=lead_id, company_id="acme", source=LeadSource.APOLLO, full_name="X", title_raw="X",
        title_canonical="X", is_founder=is_founder, retrieved_at=NOW, enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )


def _match(job_id: str, lead_id: str, rank: int = 1) -> LeadJobMatch:
    return LeadJobMatch(
        id=f"{job_id}-{lead_id}", job_id=job_id, lead_id=lead_id, match_score=0.8, match_confidence=0.8,
        signals={}, rank_within_job=rank, computed_at=NOW, rules_version="v1",
    )


def test_jobs_with_lead_rate():
    matches = [_match("j1", "l1")]
    unmatched = [UnmatchedJob(job_id="j2", reason=UnmatchedReason.NO_PLAUSIBLE_OWNER, recorded_at=NOW, run_id="r1")]
    metrics = compute_matching_metrics(
        matches=matches, unmatched=unmatched,
        jobs_by_id={"j1": _job("j1", JobFunction.ENGINEERING), "j2": _job("j2", JobFunction.DESIGN)},
        leads_by_id={"l1": _lead("l1")},
    )
    assert metrics.jobs_with_lead_count == 1
    assert metrics.jobs_without_lead_count == 1
    assert metrics.jobs_with_lead_rate == 0.5


def test_no_plausible_owner_by_function():
    unmatched = [
        UnmatchedJob(job_id="j1", reason=UnmatchedReason.NO_PLAUSIBLE_OWNER, recorded_at=NOW, run_id="r1"),
        UnmatchedJob(job_id="j2", reason=UnmatchedReason.NO_PLAUSIBLE_OWNER, recorded_at=NOW, run_id="r1"),
        UnmatchedJob(job_id="j3", reason=UnmatchedReason.NO_LEADS_RETRIEVED, recorded_at=NOW, run_id="r1"),
    ]
    metrics = compute_matching_metrics(
        matches=[], unmatched=unmatched,
        jobs_by_id={
            "j1": _job("j1", JobFunction.DESIGN), "j2": _job("j2", JobFunction.DESIGN), "j3": _job("j3", JobFunction.SALES),
        },
        leads_by_id={},
    )
    # Only NO_PLAUSIBLE_OWNER counts — NO_LEADS_RETRIEVED is a different bug class (spec §17.2).
    assert metrics.no_plausible_owner_by_function == {"design": 2}


def test_founder_match_share_only_counts_rank_1():
    matches = [_match("j1", "l1", rank=1), _match("j1", "l2", rank=2)]
    metrics = compute_matching_metrics(
        matches=matches, unmatched=[], jobs_by_id={"j1": _job("j1", JobFunction.ENGINEERING)},
        leads_by_id={"l1": _lead("l1", is_founder=True), "l2": _lead("l2", is_founder=False)},
    )
    assert metrics.founder_match_share == 1.0  # the only rank-1 match is a founder


def test_no_data_returns_none_rates():
    metrics = compute_matching_metrics(matches=[], unmatched=[], jobs_by_id={}, leads_by_id={})
    assert metrics.jobs_with_lead_rate is None
    assert metrics.founder_match_share is None


def test_disagreement_rate_empty_is_none():
    assert compute_disagreement_rate([]) is None


def test_disagreement_rate_computed():
    scored = [
        ScoredLead(
            id="s1", match_id="m1", relevance_score=0.9, confidence_score=0.9, rationale="", cited_signals=[],
            disagrees_with_rules=True, prompt_version="v1", job_version="v1", lead_version="v1", scored_at=NOW,
        ),
        ScoredLead(
            id="s2", match_id="m2", relevance_score=0.9, confidence_score=0.9, rationale="", cited_signals=[],
            disagrees_with_rules=False, prompt_version="v1", job_version="v1", lead_version="v1", scored_at=NOW,
        ),
    ]
    assert compute_disagreement_rate(scored) == 0.5
