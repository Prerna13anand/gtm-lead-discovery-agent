"""Stage 7 — Lead-Job Matching tests (spec §10, §20.5).

Table-driven signal tests, headcount modulation tests (the "highest-value
matching test" per spec §20.5), the §10.8 worked example, empty-case tests,
and ordering stability.
"""

from datetime import UTC, datetime

from gtm_agent.leads.matching import (
    MATCH_FLOOR,
    TOP_K,
    HeadcountTier,
    combine,
    compute_signals,
    match,
    needs_tie_break,
    resolve_headcount_tier,
    score_function_alignment,
    score_location_alignment,
    score_ownership_language,
    score_seniority_relationship,
)
from gtm_agent.models.common import JobFunction, Location, Seniority
from gtm_agent.models.company import Company
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EmailStatus, EnrichmentStatus, LeadRecord, LeadSource
from gtm_agent.models.matching import UnmatchedReason

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _company(headcount: int | None = 100, funding_stage: str | None = None) -> Company:
    return Company(
        id="acme", name="Acme", domain="acme.com", added_at=NOW, headcount=headcount, funding_stage=funding_stage
    )


def _job(
    function: JobFunction | None = JobFunction.ENGINEERING,
    seniority: Seniority | None = Seniority.SENIOR,
    *,
    description_text: str = "",
    locations: list[Location] | None = None,
    location_raw: str | None = None,
    is_degraded: bool = False,
) -> JobPosting:
    return JobPosting(
        job_id="job-1",
        company_id="acme",
        source_platform="greenhouse",
        posting_url="https://acme.com/jobs/1",
        title_raw="Senior Backend Engineer",
        title_canonical="Senior Backend Engineer",
        description_text=description_text,
        description_markdown="",
        function=function,
        seniority=seniority,
        location_raw=location_raw,
        locations=locations or [],
        is_degraded=is_degraded,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def _lead(
    lead_id: str,
    *,
    full_name: str = "Some Person",
    title_raw: str = "Engineering Manager",
    function: JobFunction | None = JobFunction.ENGINEERING,
    seniority: Seniority | None = Seniority.MANAGER,
    is_founder: bool = False,
    is_recruiter: bool = False,
    location_raw: str | None = None,
    tenure_months: int | None = 24,
    email_status: EmailStatus | None = EmailStatus.VERIFIED,
    phone: str | None = "+1-555-0100",
) -> LeadRecord:
    return LeadRecord(
        lead_id=lead_id,
        company_id="acme",
        source=LeadSource.APOLLO,
        full_name=full_name,
        title_raw=title_raw,
        title_canonical=title_raw,
        function=function,
        seniority=seniority,
        is_founder=is_founder,
        is_recruiter=is_recruiter,
        location_raw=location_raw,
        tenure_months=tenure_months,
        email_status=email_status,
        phone=phone,
        retrieved_at=NOW,
        enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )


# --- Signal 1 — Function alignment (table-driven) ---------------------


def test_function_alignment_exact_match():
    lead = _lead("l1", function=JobFunction.ENGINEERING, is_founder=False, is_recruiter=False)
    job = _job(function=JobFunction.ENGINEERING)
    assert score_function_alignment(lead, job) == 1.0


def test_function_alignment_founder_mismatched_function():
    lead = _lead("l1", function=JobFunction.OTHER, is_founder=True)
    job = _job(function=JobFunction.ENGINEERING)
    assert score_function_alignment(lead, job) == 0.8


def test_function_alignment_recruiter_mismatched_function():
    lead = _lead("l1", function=JobFunction.PEOPLE, is_recruiter=True)
    job = _job(function=JobFunction.ENGINEERING)
    assert score_function_alignment(lead, job) == 0.7


def test_function_alignment_adjacent_function():
    lead = _lead("l1", function=JobFunction.DATA)
    job = _job(function=JobFunction.ENGINEERING)
    assert score_function_alignment(lead, job) == 0.5


def test_function_alignment_unrelated_function():
    lead = _lead("l1", function=JobFunction.SALES)
    job = _job(function=JobFunction.ENGINEERING)
    assert score_function_alignment(lead, job) == 0.0


def test_function_alignment_function_match_beats_founder_case():
    # A founder whose own classified function *does* match the job wins the
    # 1.0 case, not the 0.8 founder case — spec §10.8's CTO row (Fn=1.0).
    lead = _lead("l1", function=JobFunction.ENGINEERING, is_founder=True)
    job = _job(function=JobFunction.ENGINEERING)
    assert score_function_alignment(lead, job) == 1.0


# --- Signal 2 — Seniority relationship (table-driven, no headcount modulation) ---

_NO_MODULATION = resolve_headcount_tier(_company(headcount=100))  # UNDER_150 -> multiplier 1.0


def test_seniority_junior_to_role_scores_zero():
    lead = _lead("l1", seniority=Seniority.ENTRY)
    job = _job(seniority=Seniority.SENIOR)
    assert score_seniority_relationship(lead, job, _NO_MODULATION) == 0.0


def test_seniority_peer_scores_low():
    lead = _lead("l1", seniority=Seniority.SENIOR)
    job = _job(seniority=Seniority.SENIOR)
    assert round(score_seniority_relationship(lead, job, _NO_MODULATION), 2) == 0.3


def test_seniority_one_level_up_is_ideal():
    lead = _lead("l1", seniority=Seniority.STAFF)
    job = _job(seniority=Seniority.SENIOR)
    assert score_seniority_relationship(lead, job, _NO_MODULATION) == 1.0


def test_seniority_two_levels_up_is_skip_level():
    lead = _lead("l1", seniority=Seniority.PRINCIPAL)
    job = _job(seniority=Seniority.SENIOR)
    assert round(score_seniority_relationship(lead, job, _NO_MODULATION), 2) == 0.9


def test_seniority_three_levels_up_is_plausible_in_flat_org():
    lead = _lead("l1", seniority=Seniority.LEAD)
    job = _job(seniority=Seniority.SENIOR)
    assert round(score_seniority_relationship(lead, job, _NO_MODULATION), 2) == 0.6


def test_seniority_more_than_three_levels_up_is_low():
    lead = _lead("l1", seniority=Seniority.EXECUTIVE)
    job = _job(seniority=Seniority.SENIOR)
    assert round(score_seniority_relationship(lead, job, _NO_MODULATION), 2) == 0.3


def test_seniority_missing_is_neutral():
    lead = _lead("l1", seniority=None)
    job = _job(seniority=Seniority.SENIOR)
    assert round(score_seniority_relationship(lead, job, _NO_MODULATION), 2) == 0.3


# --- Signal 3 — Ownership language --------------------------------------


def test_ownership_language_reporting_line_in_description():
    lead = _lead("l1", title_raw="VP of Product")
    job = _job(function=JobFunction.ENGINEERING, description_text="This role reports to the VP of Product.")
    assert score_ownership_language(lead, job) == 1.0


def test_ownership_language_exact_primary_owner_title():
    lead = _lead("l1", title_raw="Head of Engineering")
    job = _job(function=JobFunction.ENGINEERING)
    assert score_ownership_language(lead, job) == 0.8


def test_ownership_language_secondary_title_is_not_ownership_evidence():
    # "Staff Engineer" is a Stage 6 retrieval persona (Appendix C secondary
    # column) but must NOT count as explicit ownership language.
    lead = _lead("l1", title_raw="Staff Engineer")
    job = _job(function=JobFunction.ENGINEERING)
    assert score_ownership_language(lead, job) == 0.0


def test_ownership_language_absent():
    lead = _lead("l1", title_raw="Account Executive")
    job = _job(function=JobFunction.ENGINEERING)
    assert score_ownership_language(lead, job) == 0.0


# --- Signal 5 — Location alignment ---------------------------------------


def test_location_alignment_match_is_weak_positive():
    lead = _lead("l1", location_raw="San Francisco, CA, US")
    job = _job(locations=[Location(city="San Francisco", country="US")])
    assert score_location_alignment(lead, job) == 0.3


def test_location_alignment_mismatch_is_never_negative():
    lead = _lead("l1", location_raw="London, UK")
    job = _job(locations=[Location(city="San Francisco", country="US")])
    assert score_location_alignment(lead, job) == 0.0


def test_location_alignment_missing_lead_location_is_zero_not_negative():
    lead = _lead("l1", location_raw=None)
    job = _job(locations=[Location(city="San Francisco", country="US")])
    assert score_location_alignment(lead, job) == 0.0


# --- Signal 6 — Tenure sanity (via compute_signals) ----------------------


def test_short_tenure_applies_small_negative_adjustment():
    lead = _lead("l1", tenure_months=1)
    job = _job()
    signals = compute_signals(lead, job, _company())
    assert signals.tenure_penalty < 0


def test_long_tenure_gets_no_bonus():
    lead = _lead("l1", tenure_months=60)
    job = _job()
    signals = compute_signals(lead, job, _company())
    assert signals.tenure_penalty == 0.0


def test_unknown_tenure_gets_no_penalty():
    lead = _lead("l1", tenure_months=None)
    job = _job()
    signals = compute_signals(lead, job, _company())
    assert signals.tenure_penalty == 0.0


# --- Headcount modulation (spec §10.4) — the highest-value matching test ---


def test_headcount_tiers_resolve_correctly():
    assert resolve_headcount_tier(_company(headcount=10)).founder_bonus == 0.35
    assert resolve_headcount_tier(_company(headcount=40)).founder_bonus == 0.20
    assert resolve_headcount_tier(_company(headcount=100)).founder_bonus == 0.05
    assert resolve_headcount_tier(_company(headcount=300)).founder_bonus == -0.10


def test_headcount_unknown_falls_back_to_funding_stage_and_flags_guessed():
    modulation = resolve_headcount_tier(_company(headcount=None, funding_stage="seed"))
    assert modulation.is_guessed is True
    assert modulation.founder_bonus == 0.35  # seed -> UNDER_20


def test_founders_rank_moves_down_as_headcount_grows():
    """The same founder-vs-non-founder pair, scored at headcounts 10/40/100/300
    (spec §20.5): the founder's rank relative to a seniority-appropriate
    non-founder must move down monotonically as headcount grows, never up.
    """
    founder = _lead(
        "founder", title_raw="CEO", function=JobFunction.OTHER, seniority=Seniority.EXECUTIVE, is_founder=True
    )
    # Seniority delta of exactly 1 (ideal, ceil §10.3 case) has zero penalty
    # to modulate, so this lead's own score is headcount-invariant — isolating
    # the founder_bonus as the only thing that should vary with headcount.
    manager = _lead(
        "manager", title_raw="Staff Engineer", function=JobFunction.ENGINEERING, seniority=Seniority.STAFF
    )
    job = _job(function=JobFunction.ENGINEERING, seniority=Seniority.SENIOR)

    founder_scores = []
    manager_scores = []
    for headcount in (10, 40, 100, 300):
        company = _company(headcount=headcount)
        founder_scores.append(combine(compute_signals(founder, job, company)))
        manager_scores.append(combine(compute_signals(manager, job, company)))

    # Founder's own score strictly decreases as headcount grows (founder_bonus
    # shrinks from +0.35 to -0.10) while the manager's score is unaffected by
    # headcount (no founder bonus applies).
    assert founder_scores == sorted(founder_scores, reverse=True)
    assert len(set(manager_scores)) == 1  # headcount-invariant for a non-founder

    # At the smallest tier the founder must outrank the manager; at the
    # largest tier the relationship must have flipped.
    assert founder_scores[0] > manager_scores[0]
    assert founder_scores[-1] < manager_scores[-1]


# --- §10.8 worked example -------------------------------------------------


def test_worked_example_ordering_and_exclusion():
    """Spec §10.8: 18-person seed startup, "Senior Backend Engineer" role.
    Result: CTO > CEO > Staff Engineer, published; Head of Ops excluded.
    """
    company = _company(headcount=18)
    job = _job(function=JobFunction.ENGINEERING, seniority=Seniority.SENIOR)

    cto = _lead("cto", title_raw="CTO", is_founder=True)  # function unclassified by rules -> None
    ceo = _lead("ceo", title_raw="CEO", is_founder=True)
    staff = _lead(
        "staff", title_raw="Staff Engineer", function=JobFunction.ENGINEERING, seniority=Seniority.STAFF
    )
    ops = _lead(
        "ops", title_raw="Head of Ops", function=JobFunction.OPERATIONS, seniority=Seniority.DIRECTOR
    )

    result = match(company=company, leads=[cto, ceo, staff, ops], jobs=[job], run_id="run-1", computed_at=NOW)

    ranked_ids = [m.lead_id for m in sorted(result.matches, key=lambda m: m.rank_within_job)]
    assert ranked_ids == ["cto", "ceo", "staff"]
    # Ops clears no floor-crossing signal (function mismatch dominates, per
    # spec §10.8) and is excluded — but the *job* still has other matches, so
    # no `unmatched_job` row is emitted for it (that's only for a job with
    # zero qualifying leads at all, spec §10.6).
    assert not any(m.lead_id == "ops" for m in result.matches)
    assert result.unmatched == []


# --- Empty-case tests (spec §20.5) ---------------------------------------


def test_no_leads_at_all_emits_configured_empty_reason():
    company = _company()
    job = _job()
    result = match(
        company=company,
        leads=[],
        jobs=[job],
        run_id="run-1",
        computed_at=NOW,
        empty_leads_reason=UnmatchedReason.LEAD_DISCOVERY_FAILED,
    )
    assert result.matches == []
    assert result.unmatched[0].reason == UnmatchedReason.LEAD_DISCOVERY_FAILED


def test_no_jobs_produces_no_matches_and_no_unmatched():
    company = _company()
    lead = _lead("l1")
    result = match(company=company, leads=[lead], jobs=[], run_id="run-1", computed_at=NOW)
    assert result.matches == []
    assert result.unmatched == []


def test_all_leads_below_floor_emits_no_plausible_owner():
    company = _company()
    job = _job(function=JobFunction.ENGINEERING, seniority=Seniority.SENIOR)
    unrelated = _lead("l1", function=JobFunction.SALES, seniority=Seniority.ENTRY)
    result = match(company=company, leads=[unrelated], jobs=[job], run_id="run-1", computed_at=NOW)
    assert result.matches == []
    assert result.unmatched[0].reason == UnmatchedReason.NO_PLAUSIBLE_OWNER


def test_top_k_caps_matches_per_job():
    company = _company()
    job = _job(function=JobFunction.ENGINEERING, seniority=Seniority.STAFF)
    leads = [
        _lead(f"l{i}", function=JobFunction.ENGINEERING, seniority=Seniority.PRINCIPAL)
        for i in range(TOP_K + 3)
    ]
    result = match(company=company, leads=leads, jobs=[job], run_id="run-1", computed_at=NOW)
    assert len(result.matches) == TOP_K


# --- Ordering stability (spec §20.5) --------------------------------------


def test_ordering_is_stable_regardless_of_input_lead_order():
    company = _company(headcount=18)
    job = _job(function=JobFunction.ENGINEERING, seniority=Seniority.SENIOR)
    cto = _lead("cto", title_raw="CTO", is_founder=True)
    staff = _lead("staff", title_raw="Staff Engineer", function=JobFunction.ENGINEERING, seniority=Seniority.STAFF)

    result_a = match(company=company, leads=[cto, staff], jobs=[job], run_id="r1", computed_at=NOW)
    result_b = match(company=company, leads=[staff, cto], jobs=[job], run_id="r1", computed_at=NOW)

    order_a = [m.lead_id for m in sorted(result_a.matches, key=lambda m: m.rank_within_job)]
    order_b = [m.lead_id for m in sorted(result_b.matches, key=lambda m: m.rank_within_job)]
    assert order_a == order_b


def test_identical_inputs_produce_identical_scores_across_repeated_calls():
    company = _company()
    job = _job()
    lead = _lead("l1")
    result_1 = match(company=company, leads=[lead], jobs=[job], run_id="r1", computed_at=NOW)
    result_2 = match(company=company, leads=[lead], jobs=[job], run_id="r1", computed_at=NOW)
    assert result_1.matches[0].match_score == result_2.matches[0].match_score


# --- match_floor / TOP_K are respected as parameters ----------------------


def test_custom_match_floor_and_top_k_are_honoured():
    company = _company()
    job = _job(function=JobFunction.ENGINEERING, seniority=Seniority.SENIOR)
    weak_lead = _lead("l1", function=JobFunction.SALES, seniority=Seniority.ENTRY)
    result = match(
        company=company, leads=[weak_lead], jobs=[job], run_id="r1", computed_at=NOW, match_floor=0.0, top_k=1
    )
    assert len(result.matches) == 1  # would have been excluded at the default floor


# --- Optional LLM tie-break detection (spec §10.7) ------------------------


def test_needs_tie_break_within_band():
    assert needs_tie_break((0.80, 0.77)) is True


def test_needs_tie_break_outside_band():
    assert needs_tie_break((0.90, 0.60)) is False


def test_match_floor_and_top_k_defaults_are_sane():
    assert 0.0 < MATCH_FLOOR < 1.0
    assert TOP_K >= 1
