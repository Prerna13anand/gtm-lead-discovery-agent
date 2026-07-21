"""Stage 11 — Ranking tests (spec §13.6)."""

from datetime import UTC, datetime, timedelta

from gtm_agent.models.common import Location
from gtm_agent.models.company_context import CompanyContext
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EmailStatus, EnrichmentStatus, LeadRecord, LeadSource
from gtm_agent.models.scoring import ScoredLead
from gtm_agent.scoring.ranking import (
    company_context_weight,
    contactability_label,
    contactability_weight,
    priority_for,
    rank,
    recency_weight,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job(job_id: str = "j1", *, first_seen_at: datetime = NOW) -> JobPosting:
    return JobPosting(
        job_id=job_id, company_id="acme", source_platform="greenhouse", posting_url=f"https://acme.com/jobs/{job_id}",
        title_raw="Role", title_canonical="Role", description_text="", description_markdown="",
        first_seen_at=first_seen_at, last_seen_at=NOW,
    )


def _lead(lead_id: str = "l1", **overrides) -> LeadRecord:
    base = dict(
        lead_id=lead_id, company_id="acme", source=LeadSource.APOLLO, full_name="X", title_raw="X",
        title_canonical="X", retrieved_at=NOW, enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )
    base.update(overrides)
    return LeadRecord(**base)


def _scored(lead_id: str = "l1", relevance: float = 0.8, confidence: float = 0.8) -> ScoredLead:
    return ScoredLead(
        id=f"s-{lead_id}", match_id=f"m-{lead_id}", relevance_score=relevance, confidence_score=confidence,
        rationale="", cited_signals=[], prompt_version="v1", job_version="v1", lead_version="v1", scored_at=NOW,
    )


# --- recency_weight -------------------------------------------------------


def test_recency_weight_is_1_for_brand_new_job():
    assert recency_weight(NOW, now=NOW) == 1.0


def test_recency_weight_decays_with_age():
    older = recency_weight(NOW - timedelta(days=30), now=NOW)
    newer = recency_weight(NOW - timedelta(days=1), now=NOW)
    assert newer > older


def test_recency_weight_has_a_floor():
    ancient = recency_weight(NOW - timedelta(days=3650), now=NOW)
    assert ancient == 0.3


# --- contactability --------------------------------------------------------


def test_contactability_verified_email_scores_highest():
    lead = _lead(email="x@acme.com", email_status=EmailStatus.VERIFIED)
    assert contactability_weight(lead) == 1.0
    assert contactability_label(lead) == "verified email"


def test_contactability_guessed_email_scores_below_verified():
    lead = _lead(email="x@acme.com", email_status=EmailStatus.GUESSED)
    assert 0 < contactability_weight(lead) < 1.0
    assert contactability_label(lead) == "guessed email"


def test_contactability_phone_only():
    lead = _lead(email=None, phone="+1-555-0100")
    assert contactability_label(lead) == "phone only"


def test_contactability_none_scores_lowest():
    lead = _lead(email=None, phone=None)
    verified = _lead(email="x@acme.com", email_status=EmailStatus.VERIFIED)
    assert contactability_weight(lead) < contactability_weight(verified)
    assert contactability_label(lead) == "no contact on file"


# --- company_context_weight ------------------------------------------------


def test_company_context_weight_none_is_neutral():
    assert company_context_weight(None) == 1.0


def test_company_context_weight_funding_signal_boosts():
    context = CompanyContext(company_id="acme", summary="x", funding_signal="raised Series A", fetched_at=NOW, expires_at=NOW)
    assert company_context_weight(context) > 1.0


def test_company_context_weight_funding_and_hiring_compound():
    both = CompanyContext(
        company_id="acme", summary="x", funding_signal="raised", hiring_signal="hiring", fetched_at=NOW, expires_at=NOW
    )
    funding_only = CompanyContext(company_id="acme", summary="x", funding_signal="raised", fetched_at=NOW, expires_at=NOW)
    assert company_context_weight(both) > company_context_weight(funding_only)


# --- priority_for / rank ---------------------------------------------------


def test_priority_for_combines_all_four_factors():
    job = _job(first_seen_at=NOW)
    lead = _lead(email="x@acme.com", email_status=EmailStatus.VERIFIED)
    scored = _scored(relevance=0.8, confidence=0.5)
    priority = priority_for(scored, job, lead, None, now=NOW)
    assert priority == 0.8 * 0.5 * 1.0 * 1.0 * 1.0


def test_rank_orders_by_priority_within_company():
    job_a = _job("j1", first_seen_at=NOW)
    job_b = _job("j2", first_seen_at=NOW)
    lead_a = _lead("l1", email="a@acme.com", email_status=EmailStatus.VERIFIED)
    lead_b = _lead("l2", email=None, phone=None)  # weakest contactability
    scored_a = _scored("l1", relevance=0.9, confidence=0.9)
    scored_b = _scored("l2", relevance=0.9, confidence=0.9)

    entries = [(scored_b, job_b, lead_b), (scored_a, job_a, lead_a)]
    ranked = rank(
        entries,
        context_by_company={"acme": None},
        company_id_by_job_id={"j1": "acme", "j2": "acme"},
        now=NOW,
    )
    assert [e.lead.lead_id for e in ranked] == ["l1", "l2"]  # verified contact ranks above no-contact


def test_rank_groups_by_company_first():
    job_acme = _job("j1", first_seen_at=NOW)
    job_other = _job("j2", first_seen_at=NOW)
    lead = _lead("l1", email="x@acme.com", email_status=EmailStatus.VERIFIED)
    scored = _scored("l1", relevance=0.5, confidence=0.5)  # lower priority than the "other" company's entry
    scored_other = _scored("l1", relevance=0.99, confidence=0.99)

    entries = [(scored, job_acme, lead), (scored_other, job_other, lead)]
    ranked = rank(
        entries,
        context_by_company={"acme": None, "other": None},
        company_id_by_job_id={"j1": "acme", "j2": "other"},
        now=NOW,
    )
    companies_in_order = [
        {"j1": "acme", "j2": "other"}[e.job.job_id] for e in ranked
    ]
    # Entries for the same company stay contiguous even though the other
    # company's single entry has higher raw priority.
    assert companies_in_order == sorted(companies_in_order)
