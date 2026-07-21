"""Stage 7 — Optional LLM tie-break (spec §10.7).

"An LLM is invoked only when the top candidates are within a narrow band
(default 0.05) and the distinction matters — typically choosing between a
functional lead and a founder at a mid-sized company... Consistent with
§2.8 and §7.3: deterministic where possible, LLM only for genuine residue.
Keeps cost near zero and behaviour reproducible in tests."

`leads.matching.needs_tie_break` already implements the *detection* (spec
§10.7's band check); this module is the piece that was previously left
undone — see that function's own docstring, which flagged the gap
explicitly rather than silently pretending it was finished.

Deliberately **not** part of `leads.matching.match()` — that function stays
synchronous and network-free, so every existing call site (this codebase's
~40 matching tests, `main.py`'s Stage 7 section) keeps working completely
unchanged. This is an optional, explicitly-invoked async second pass over
`match()`'s output, following the identical pattern
`discovery.llm_residue.resolve_unclassified` uses for Stage 4's LLM residue
classification: a caller that never invokes it (or has no Azure OpenAI
credentials configured) gets exactly the rules-only ranking spec §22's
Phase 3 describes ("deliberately rules-only... this establishes a
deterministic, measurable matching baseline").
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from openai import APIError
from pydantic import BaseModel, ValidationError

from gtm_agent.core.logging import get_logger
from gtm_agent.leads.matching import TIE_BREAK_BAND
from gtm_agent.models.company import Company
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import LeadRecord
from gtm_agent.models.matching import LeadJobMatch
from gtm_agent.services.azure_openai import AzureOpenAIConfigError, AzureOpenAIService

logger = get_logger(__name__)

_MAX_ATTEMPTS = 2  # same "retry once" convention as Stage 10 (spec §13.5)
_DESCRIPTION_EXCERPT_CHARS = 500

_SYSTEM_PROMPT = """A deterministic rules engine matched two leads to one job with nearly identical scores — \
too close to call from structural signals alone. You judge which of the two is more likely the actual hiring \
process owner for this specific role, reading the job description and both lead profiles.

You MUST set preferred_lead_id to exactly one of the two lead_id values you are given. Do not invent a third \
option, and do not refuse to choose — if genuinely unsure, prefer the candidate whose title more directly names \
the job's function."""


class TieBreakChoice(BaseModel):
    preferred_lead_id: str
    rationale: str


def _candidate_summary(lead: LeadRecord, match: LeadJobMatch) -> str:
    return (
        f"lead_id={lead.lead_id} name={lead.full_name!r} title={lead.title_raw!r} "
        f"function={lead.function} seniority={lead.seniority} is_founder={lead.is_founder} "
        f"is_recruiter={lead.is_recruiter} rules_score={match.match_score:.3f}"
    )


def _build_prompt(job: JobPosting, company: Company, candidates: list[tuple[LeadRecord, LeadJobMatch]]) -> str:
    lines = [
        f"Job: {job.title_canonical}  function={job.function}  seniority={job.seniority}",
        f"Description excerpt: {job.description_text[:_DESCRIPTION_EXCERPT_CHARS]}",
        f"Company headcount: {company.headcount}  funding_stage: {company.funding_stage}",
        "",
        "Tied candidates:",
    ]
    for lead, match in candidates:
        lines.append(f"  - {_candidate_summary(lead, match)}")
    return "\n".join(lines)


async def break_tie(
    *,
    job: JobPosting,
    company: Company,
    top_two: list[tuple[LeadRecord, LeadJobMatch]],
    llm: AzureOpenAIService | None = None,
) -> str | None:
    """Returns the `lead_id` the LLM prefers for rank 1, or `None` if the
    LLM is unavailable, fails after retry, or answers with a `lead_id`
    that wasn't one of the two candidates offered (a grounding violation,
    same principle as Stage 10's `cited_signals` check — never trust an
    answer that isn't traceable to what was actually supplied).
    """
    service = llm or AzureOpenAIService()
    if not service.is_configured:
        return None

    valid_ids = {lead.lead_id for lead, _ in top_two}
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_prompt(job, company, top_two)},
    ]

    for attempt in range(_MAX_ATTEMPTS):
        try:
            client = service.get_client()
            completion = await asyncio.to_thread(
                client.beta.chat.completions.parse,
                model=service.deployment,
                messages=messages,
                response_format=TieBreakChoice,
            )
        except (APIError, AzureOpenAIConfigError) as exc:
            logger.warning("tie_break_attempt_failed", attempt=attempt, job_id=job.job_id, error=str(exc))
            continue

        parsed = completion.choices[0].message.parsed
        if parsed is None:
            continue

        try:
            choice = TieBreakChoice.model_validate(parsed.model_dump())
        except ValidationError:
            continue

        if choice.preferred_lead_id not in valid_ids:
            logger.warning(
                "tie_break_grounding_violation", job_id=job.job_id, chosen=choice.preferred_lead_id, valid=list(valid_ids)
            )
            continue

        return choice.preferred_lead_id

    logger.warning("tie_break_failed", job_id=job.job_id)
    return None


@dataclass
class TieBreakOutcome:
    matches: list[LeadJobMatch]
    ties_detected: int
    ties_resolved: int


async def resolve_tie_breaks(
    matches: list[LeadJobMatch],
    *,
    jobs_by_id: dict[str, JobPosting],
    leads_by_id: dict[str, LeadRecord],
    company: Company,
    llm: AzureOpenAIService | None = None,
) -> TieBreakOutcome:
    """Spec §10.7: for each job, if the top two ranked matches are within
    `TIE_BREAK_BAND`, ask the LLM which should be rank 1. Re-orders only
    the top two — everything below them keeps its rules-computed rank.
    Never drops a match; a failed/unavailable tie-break just leaves the
    rules' own ordering in place (spec §2.9's "say we don't know" applied
    to preference, not just to score).
    """
    by_job: dict[str, list[LeadJobMatch]] = {}
    for match in matches:
        by_job.setdefault(match.job_id, []).append(match)

    ties_detected = 0
    ties_resolved = 0
    result: list[LeadJobMatch] = []

    for job_id, job_matches in by_job.items():
        job_matches_sorted = sorted(job_matches, key=lambda m: m.rank_within_job)
        if len(job_matches_sorted) < 2:
            result.extend(job_matches_sorted)
            continue

        top, second = job_matches_sorted[0], job_matches_sorted[1]
        if (top.match_score - second.match_score) >= TIE_BREAK_BAND:
            result.extend(job_matches_sorted)
            continue

        ties_detected += 1
        job = jobs_by_id.get(job_id)
        if job is None:
            result.extend(job_matches_sorted)
            continue

        top_lead = leads_by_id.get(top.lead_id)
        second_lead = leads_by_id.get(second.lead_id)
        if top_lead is None or second_lead is None:
            result.extend(job_matches_sorted)
            continue

        preferred = await break_tie(
            job=job, company=company, top_two=[(top_lead, top), (second_lead, second)], llm=llm
        )
        if preferred is None or preferred == top.lead_id:
            result.extend(job_matches_sorted)  # no change, or LLM agreed with the rules
            if preferred is not None:
                ties_resolved += 1
            continue

        # LLM preferred the second-ranked candidate — swap rank 1 and rank 2.
        swapped_top = second.model_copy(update={"rank_within_job": top.rank_within_job})
        swapped_second = top.model_copy(update={"rank_within_job": second.rank_within_job})
        result.append(swapped_top)
        result.append(swapped_second)
        result.extend(job_matches_sorted[2:])
        ties_resolved += 1

    return TieBreakOutcome(matches=result, ties_detected=ties_detected, ties_resolved=ties_resolved)
