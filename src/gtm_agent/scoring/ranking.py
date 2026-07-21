"""Stage 11 — Ranking (spec §13.6).

"Final ordering is computed, not model-produced — models rank inconsistently
across separate calls, and a stable order matters to a team working a list
top-down." Entirely local and free, same "pure decision logic" convention as
`leads.matching` — no I/O, directly unit-testable.

None of the four weight functions below have a spec-given formula — §13.6
names what each should reward (newer roles, verified contact, funding/hiring
momentum) without pinning down a curve. Same posture as `leads.matching`'s
weights (spec §10.3: "starting points to be tuned against feedback"): these
are reasonable, documented defaults, not spec values, and are the first
things to tune once the §19.5 feedback loop has real data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from gtm_agent.models.company_context import CompanyContext
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EmailStatus, LeadRecord
from gtm_agent.models.scoring import ScoredLead

# Recency: exponential decay with a 14-day half-life, floored at 0.3 so an
# old-but-still-open role is deprioritised, never zeroed out — it's still a
# real open job (spec §2.3), just a less time-sensitive one to lead with.
_RECENCY_HALF_LIFE_DAYS = 14.0
_RECENCY_FLOOR = 0.3

_CONTACTABILITY_VERIFIED = 1.0
_CONTACTABILITY_GUESSED_EMAIL = 0.7
_CONTACTABILITY_PHONE_ONLY = 0.6
_CONTACTABILITY_NONE = 0.4

_CONTEXT_BASE = 1.0
_CONTEXT_FUNDING_BOOST = 0.15
_CONTEXT_HIRING_BOOST = 0.10


def recency_weight(first_seen_at: datetime, *, now: datetime) -> float:
    age_days = max(0.0, (now - first_seen_at).total_seconds() / 86400.0)
    weight = 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)
    return max(_RECENCY_FLOOR, weight)


def contactability_weight(lead: LeadRecord) -> float:
    """Spec §13.6: "the right person you cannot reach is worth less... That
    is a real prioritisation judgement, so it belongs in ranking...rather
    than being smuggled into the relevance score."
    """
    if lead.email_status == EmailStatus.VERIFIED and lead.email:
        return _CONTACTABILITY_VERIFIED
    if lead.email:
        return _CONTACTABILITY_GUESSED_EMAIL
    if lead.phone:
        return _CONTACTABILITY_PHONE_ONLY
    return _CONTACTABILITY_NONE


def contactability_label(lead: LeadRecord) -> str:
    """The GTM-facing rendering of the same judgement — `publication.GtmLead.lead.contactability`."""
    if lead.email_status == EmailStatus.VERIFIED and lead.email:
        return "verified email"
    if lead.email:
        return "guessed email"
    if lead.phone:
        return "phone only"
    return "no contact on file"


def company_context_weight(context: CompanyContext | None) -> float:
    """Spec §13.6 / §12.3: "funding/hiring momentum." Purely a ranking
    input — this must never reach Stage 7 matching (spec §12.3's boundary),
    and it doesn't: nothing in `leads.matching` imports `CompanyContext`.
    """
    if context is None:
        return _CONTEXT_BASE
    weight = _CONTEXT_BASE
    if context.funding_signal:
        weight += _CONTEXT_FUNDING_BOOST
    if context.hiring_signal:
        weight += _CONTEXT_HIRING_BOOST
    return weight


@dataclass(frozen=True)
class RankedEntry:
    scored: ScoredLead
    job: JobPosting
    lead: LeadRecord
    priority: float


def priority_for(
    scored: ScoredLead, job: JobPosting, lead: LeadRecord, context: CompanyContext | None, *, now: datetime
) -> float:
    return (
        scored.relevance_score
        * scored.confidence_score
        * recency_weight(job.first_seen_at, now=now)
        * contactability_weight(lead)
        * company_context_weight(context)
    )


def rank(
    entries: list[tuple[ScoredLead, JobPosting, LeadRecord]],
    *,
    context_by_company: dict[str, CompanyContext | None],
    company_id_by_job_id: dict[str, str],
    now: datetime,
) -> list[RankedEntry]:
    """Spec §13.6: "Ranked within company, then across companies. Ties
    broken by job recency." Returns entries in final publication order —
    `rank` (1-indexed, per-company) is assigned by the caller
    (`scoring.publication`), since this function's job is ordering, not
    numbering.
    """
    ranked = [
        RankedEntry(
            scored=scored,
            job=job,
            lead=lead,
            priority=priority_for(
                scored, job, lead, context_by_company.get(company_id_by_job_id.get(job.job_id, ""), None), now=now
            ),
        )
        for scored, job, lead in entries
    ]
    ranked.sort(
        key=lambda e: (
            company_id_by_job_id.get(e.job.job_id, ""),
            -e.priority,
            -e.job.first_seen_at.timestamp(),
        )
    )
    return ranked
