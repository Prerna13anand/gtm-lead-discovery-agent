"""Stage 7 — Lead-Job Matching (spec §10).

**Goal:** given one company's `list[Lead]` and `list[JobPosting]`, produce
scored `LeadJobMatch` pairs — who plausibly owns which role, and how sure we
are. Entirely local and free (spec §10.1) — no API calls, no I/O, which is
what makes every function in this module directly unit-testable (spec
§20.5: "Matching is pure, deterministic, and free — which makes it the most
testable component in the agent").

Weights below are explicitly "starting points to be tuned against feedback"
(spec §10.3) — the spec deliberately does not pin down exact numbers (open
question §23.12: "needs the §19.4 labelled set to set empirically"). The
values chosen here are internally consistent with the §10.3 signal ordering
(function alignment highest, seniority high, ownership language high but
sparse, location low) and reproduce the *qualitative* result of the §10.8
worked example (CTO > CEO > Staff Engineer > Head of Ops) — they are not a
literal fit to that example's specific numbers, which the spec itself never
claims are exact (rounding aside, the example is illustrative of the
*mechanism*, not a fixture to hit precisely).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from gtm_agent.leads.personas import PRIMARY_OWNER_TITLES
from gtm_agent.models.common import JobFunction, Seniority
from gtm_agent.models.company import Company
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import LeadRecord
from gtm_agent.models.matching import LeadJobMatch, UnmatchedJob, UnmatchedReason

RULES_VERSION = "v1"

# Spec §10.2: "the first result above the confidence floor wins" doesn't
# apply here (that's §4.1's resolution ladder) — this is §10.2's own match
# floor and top-K. Neither is given a number by the spec (open questions
# §23.10, §23.12); these defaults are chosen to match the §10.8 worked
# example's outcome (exactly 3 of 4 candidates published, the weakest
# excluded) and are meant to be tuned once the §19.4 golden set exists.
MATCH_FLOOR = 0.3
TOP_K = 3

_WEIGHTS = {
    "function_alignment": 0.35,
    "seniority_relationship": 0.30,
    "ownership_language": 0.20,
    "location_alignment": 0.05,
}

_RECRUITER_BONUS = 0.15
_TENURE_PENALTY = -0.10
_SHORT_TENURE_MONTHS = 3

_ADJACENT_FUNCTIONS: frozenset[frozenset[JobFunction]] = frozenset(
    {
        frozenset({JobFunction.DATA, JobFunction.ENGINEERING}),
        frozenset({JobFunction.PRODUCT, JobFunction.DESIGN}),
    }
)

# Ordered by hiring authority (spec Appendix C.3 / models.common.Seniority
# docstring). `FOUNDING` has "no natural rank" per that docstring and is
# treated as `SENIOR`'s rank for delta computation, same convention.
_SENIORITY_RANK: dict[Seniority, int] = {
    Seniority.INTERN: 0,
    Seniority.ENTRY: 1,
    Seniority.MID: 2,
    Seniority.SENIOR: 3,
    Seniority.STAFF: 4,
    Seniority.PRINCIPAL: 5,
    Seniority.LEAD: 6,
    Seniority.MANAGER: 7,
    Seniority.DIRECTOR: 8,
    Seniority.VP: 9,
    Seniority.EXECUTIVE: 10,
    Seniority.FOUNDING: 3,  # == SENIOR's rank, per common.Seniority docstring
}

# Spec §10.3 Signal 2's table, keyed by delta = rank(lead) - rank(job).
_SENIORITY_BASE_SCORE: dict[int, float] = {
    0: 0.3,
    1: 1.0,
    2: 0.9,
    3: 0.6,
}


class HeadcountTier(StrEnum):
    """Spec §10.4's four bands."""

    UNDER_20 = "under_20"
    UNDER_50 = "under_50"
    UNDER_150 = "under_150"
    OVER_150 = "over_150"


@dataclass(frozen=True)
class HeadcountModulation:
    founder_bonus: float
    seniority_penalty_multiplier: float
    is_guessed: bool
    """`True` when headcount was unavailable and this tier was inferred from
    `funding_stage` instead — spec §10.4: "record lower confidence" in that
    case."""


# Spec §10.4's table, verbatim.
_TIER_MODULATION: dict[HeadcountTier, tuple[float, float]] = {
    HeadcountTier.UNDER_20: (0.35, 0.3),
    HeadcountTier.UNDER_50: (0.20, 0.6),
    HeadcountTier.UNDER_150: (0.05, 1.0),
    HeadcountTier.OVER_150: (-0.10, 1.0),
}

# This codebase's own conservative mapping for the funding-stage fallback
# (spec §10.4: "fall back to funding stage" — no stage->tier table is given
# in the spec itself). Deliberately coarse and biased toward the smaller
# tiers, consistent with the segment ceiling (spec §1.5: "Series A and
# below") — an unrecognised or missing stage defaults to `UNDER_50` rather
# than the largest tier, so an unusual label doesn't silently suppress the
# founder-owns-everything insight (§9.2) that matters most at this segment.
_FUNDING_STAGE_TIER: dict[str, HeadcountTier] = {
    "pre-seed": HeadcountTier.UNDER_20,
    "seed": HeadcountTier.UNDER_20,
    "series a": HeadcountTier.UNDER_50,
    "series b": HeadcountTier.UNDER_150,
}


def resolve_headcount_tier(company: Company) -> HeadcountModulation:
    """Spec §10.4. Prefers `company.headcount`; falls back to
    `company.funding_stage` when headcount is unknown, flagging the result
    as guessed (spec: "record lower confidence").
    """
    if company.headcount is not None:
        if company.headcount < 20:
            tier = HeadcountTier.UNDER_20
        elif company.headcount < 50:
            tier = HeadcountTier.UNDER_50
        elif company.headcount < 150:
            tier = HeadcountTier.UNDER_150
        else:
            tier = HeadcountTier.OVER_150
        founder_bonus, multiplier = _TIER_MODULATION[tier]
        return HeadcountModulation(founder_bonus, multiplier, is_guessed=False)

    stage = (company.funding_stage or "").strip().lower()
    tier = _FUNDING_STAGE_TIER.get(stage, HeadcountTier.UNDER_50)
    founder_bonus, multiplier = _TIER_MODULATION[tier]
    return HeadcountModulation(founder_bonus, multiplier, is_guessed=True)


# --- Signal 1 — Function alignment (spec §10.3) ----------------------------


def _are_adjacent(a: JobFunction, b: JobFunction) -> bool:
    return frozenset({a, b}) in _ADJACENT_FUNCTIONS


def score_function_alignment(lead: LeadRecord, job: JobPosting) -> float:
    if lead.function is not None and job.function is not None and lead.function == job.function:
        return 1.0
    if lead.is_founder:
        return 0.8
    if lead.is_recruiter:
        return 0.7
    if lead.function is not None and job.function is not None and _are_adjacent(lead.function, job.function):
        return 0.5
    return 0.0


# --- Signal 2 — Seniority relationship (spec §10.3, §10.4) -----------------


def score_seniority_relationship(
    lead: LeadRecord, job: JobPosting, modulation: HeadcountModulation
) -> float:
    """Missing seniority on either side can't compute a real delta; treated
    as the neutral `delta == 0` base score (0.3) rather than 0 or 1 — an
    unknown relationship shouldn't read as either an ideal or a disqualifying
    one. This is a deliberate, documented simplification, not a spec value.
    """
    if lead.seniority is None or job.seniority is None:
        base = 0.3
    else:
        delta = _SENIORITY_RANK[lead.seniority] - _SENIORITY_RANK[job.seniority]
        if delta < 0:
            base = 0.0
        elif delta > 3:
            base = 0.3
        else:
            base = _SENIORITY_BASE_SCORE[delta]

    penalty = 1.0 - base
    modulated_penalty = penalty * modulation.seniority_penalty_multiplier
    return 1.0 - modulated_penalty


# --- Signal 3 — Explicit ownership language (spec §10.3) -------------------

_REPORTS_TO_RE = re.compile(r"reports?\s+to\s+(?:the\s+)?([A-Z][A-Za-z/&\- ]{2,60})", re.IGNORECASE)


def _extract_reporting_line(description_text: str) -> str | None:
    match = _REPORTS_TO_RE.search(description_text)
    if not match:
        return None
    return match.group(1).strip().rstrip(".,;")


def score_ownership_language(lead: LeadRecord, job: JobPosting) -> float:
    """Spec §10.3 Signal 3: "the job description names the reporting line...
    match it against retrieved titles" (1.0, near-decisive), or the lead's
    own title is literally one of Appendix C.1's primary owner titles for
    this job's function (0.8 — stronger evidence than the generic keyword
    match that already drives Signal 1, since it's an exact canonical title,
    not just a substring hit).
    """
    reporting_line = _extract_reporting_line(job.description_text)
    if reporting_line and reporting_line.lower() in lead.title_canonical.lower():
        return 1.0

    if job.function is not None:
        owner_titles = PRIMARY_OWNER_TITLES.get(job.function, ())
        if any(title.lower() == lead.title_canonical.lower() for title in owner_titles):
            return 0.8

    return 0.0


# --- Signal 5 — Location alignment (spec §10.3) -----------------------------


def score_location_alignment(lead: LeadRecord, job: JobPosting) -> float:
    """"Weak positive when lead and job share a location; never negative"
    (spec §10.3 Signal 5).
    """
    if not lead.location_raw:
        return 0.0
    lead_location = lead.location_raw.lower()
    for location in job.locations:
        for part in (location.city, location.region, location.country):
            if part and part.lower() in lead_location:
                return 0.3
    if job.location_raw and job.location_raw.lower() in lead_location:
        return 0.3
    return 0.0


# --- Combine and calibrate (spec §10.5) -------------------------------------


@dataclass(frozen=True)
class SignalBreakdown:
    function_alignment: float
    seniority_relationship: float
    ownership_language: float
    location_alignment: float
    founder_bonus: float
    recruiter_bonus: float
    tenure_penalty: float

    def as_dict(self) -> dict[str, float]:
        return {
            "function_alignment": self.function_alignment,
            "seniority_relationship": self.seniority_relationship,
            "ownership_language": self.ownership_language,
            "location_alignment": self.location_alignment,
            "founder_bonus": self.founder_bonus,
            "recruiter_bonus": self.recruiter_bonus,
            "tenure_penalty": self.tenure_penalty,
        }


def compute_signals(lead: LeadRecord, job: JobPosting, company: Company) -> SignalBreakdown:
    modulation = resolve_headcount_tier(company)
    tenure_penalty = (
        _TENURE_PENALTY
        if lead.tenure_months is not None and lead.tenure_months < _SHORT_TENURE_MONTHS
        else 0.0
    )
    return SignalBreakdown(
        function_alignment=score_function_alignment(lead, job),
        seniority_relationship=score_seniority_relationship(lead, job, modulation),
        ownership_language=score_ownership_language(lead, job),
        location_alignment=score_location_alignment(lead, job),
        founder_bonus=modulation.founder_bonus if lead.is_founder else 0.0,
        recruiter_bonus=_RECRUITER_BONUS if lead.is_recruiter else 0.0,
        tenure_penalty=tenure_penalty,
    )


def combine(signals: SignalBreakdown) -> float:
    """Spec §10.5: `raw = Σ(weight_i × signal_i) + founder_bonus +
    recruiter_bonus`; `total = clamp(raw, 0, 1)`. Tenure (Signal 6) is
    likewise corrective-additive, not part of the weighted sum — spec §10.3
    describes it as "a small negative adjustment", not a weighted signal.
    """
    raw = (
        _WEIGHTS["function_alignment"] * signals.function_alignment
        + _WEIGHTS["seniority_relationship"] * signals.seniority_relationship
        + _WEIGHTS["ownership_language"] * signals.ownership_language
        + _WEIGHTS["location_alignment"] * signals.location_alignment
        + signals.founder_bonus
        + signals.recruiter_bonus
        + signals.tenure_penalty
    )
    return max(0.0, min(1.0, raw))


def compute_confidence(
    lead: LeadRecord, job: JobPosting, signals: SignalBreakdown, *, headcount_guessed: bool
) -> float:
    """Spec §10.5's confidence axis — "separate from relevance... reflects
    evidence quality, not match strength."
    """
    confidence = 1.0

    if lead.function is None or lead.seniority is None:
        confidence -= 0.15  # derived, not observed — rules-classifier residue (spec §7.3/§9.5)
    if job.is_degraded:
        confidence -= 0.20  # generic-HTML path; the job record itself may be wrong (spec §6.2.4)
    if headcount_guessed:
        confidence -= 0.10  # §10.4 modulation was guessed from funding stage, not real headcount

    nonzero_signals = sum(
        1
        for value in (
            signals.function_alignment,
            signals.seniority_relationship,
            signals.ownership_language,
            signals.location_alignment,
        )
        if value > 0.0
    )
    if nonzero_signals <= 1:
        confidence -= 0.15  # thin evidence, even if the one signal is strong

    has_verified_contact = lead.email_status is not None and lead.email_status.value == "verified"
    if not has_verified_contact and not lead.phone:
        confidence -= 0.15  # correct person, unreachable — actionability matters to GTM

    return max(0.0, min(1.0, confidence))


# --- Pipeline (spec §10.2) --------------------------------------------------


@dataclass
class MatchResult:
    matches: list[LeadJobMatch] = field(default_factory=list)
    unmatched: list[UnmatchedJob] = field(default_factory=list)


def match(
    *,
    company: Company,
    leads: list[LeadRecord],
    jobs: list[JobPosting],
    run_id: str,
    computed_at: datetime,
    empty_leads_reason: UnmatchedReason = UnmatchedReason.NO_LEADS_RETRIEVED,
    match_floor: float = MATCH_FLOOR,
    top_k: int = TOP_K,
) -> MatchResult:
    """Spec §10.2's pipeline. `empty_leads_reason` distinguishes "Apollo
    returned nobody" (`NO_LEADS_RETRIEVED`) from "Stage 6 errored"
    (`LEAD_DISCOVERY_FAILED`) for the case `leads` is empty — that
    distinction is Stage 6's to know, not this function's, so the caller
    passes it through (spec §2.3: never collapse "we don't know" into
    "nobody").
    """
    result = MatchResult()
    modulation = resolve_headcount_tier(company)

    for job in jobs:
        scored: list[tuple[float, float, dict[str, float], LeadRecord]] = []
        for lead in leads:
            signals = compute_signals(lead, job, company)
            total = combine(signals)
            if total < match_floor:
                continue
            confidence = compute_confidence(lead, job, signals, headcount_guessed=modulation.is_guessed)
            scored.append((total, confidence, signals.as_dict(), lead))

        if not scored:
            reason = empty_leads_reason if not leads else UnmatchedReason.NO_PLAUSIBLE_OWNER
            result.unmatched.append(
                UnmatchedJob(job_id=job.job_id, reason=reason, recorded_at=computed_at, run_id=run_id)
            )
            continue

        # Stable ordering (spec §20.5): sort by score desc, then by lead_id
        # so ties never depend on incidental list/dict ordering.
        scored.sort(key=lambda item: (-item[0], item[3].lead_id))

        for rank, (total, confidence, signal_dict, lead) in enumerate(scored[:top_k], start=1):
            result.matches.append(
                LeadJobMatch(
                    id=str(uuid.uuid4()),
                    job_id=job.job_id,
                    lead_id=lead.lead_id,
                    match_score=total,
                    match_confidence=confidence,
                    signals=signal_dict,
                    rank_within_job=rank,
                    computed_at=computed_at,
                    rules_version=RULES_VERSION,
                )
            )

    return result


# --- Optional LLM tie-break (spec §10.7) ------------------------------------

_TIE_BREAK_BAND = 0.05


def needs_tie_break(top_two_scores: tuple[float, float]) -> bool:
    """Spec §10.7: "invoked only when the top candidates are within a narrow
    band (default 0.05)." Detection only — this codebase does not invoke an
    LLM to resolve it. Phase 3 is deliberately rules-only (spec §22: "No LLM
    in the loop yet... this establishes a deterministic, measurable matching
    baseline"); actually calling `services.azure_openai` for a tie-break
    belongs with Phase 4's Stage 10 LLM integration, which is where this
    codebase first wires any LLM into Part II. This function exists now
    (free, deterministic) so Phase 4 can call it without redesigning this
    module — it is not itself a partial LLM integration.
    """
    top, second = top_two_scores
    return (top - second) < _TIE_BREAK_BAND
