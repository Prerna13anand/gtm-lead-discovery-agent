"""Stage 10 — Scoring, Rationale & Ranking (spec §13).

**Goal:** for each `(job, lead)` pair above the match floor, produce a
relevance score, a confidence score, and a short human-readable rationale —
then rank (ranking itself is `scoring.ranking`, spec §13.6).

Spec §13.1: "The LLM does **not** decide who matches whom... [it] judges the
rules-based match against the full text... explains the match... adjusts
the score where the text contradicts the structural signals." This module
enforces that boundary structurally: the LLM only ever sees one
already-computed `LeadJobMatch`+its `signals` breakdown at a time; it is
never given the candidate pool to choose from.

Caching key (spec §13.5, §15.2: "cached by `(job_id, lead_id, job_version,
lead_version)`"): this codebase has a real `job_posting_version` counter
(Stage 5, spec §8.5) but no equivalent for leads — Stage 6/8 never version
leads, they overwrite in place (spec §15.2's `lead` table is "cached, not
per-run"). Absent a real lead-version counter, `job_version` here is the
job's `last_seen_at` timestamp and `lead_version` is the lead's
`enriched_at` (falling back to `retrieved_at` if never enriched) — both
change exactly when the underlying record does, which is what a version
counter needs to guarantee for the cache to invalidate correctly, even
though neither is literally a version number.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from openai import APIError
from pydantic import BaseModel, Field, ValidationError

from gtm_agent.core.logging import get_logger
from gtm_agent.models.company import Company
from gtm_agent.models.company_context import CompanyContext
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import LeadRecord
from gtm_agent.models.matching import LeadJobMatch
from gtm_agent.models.scoring import ScoredLead, ScoringStatus
from gtm_agent.services.azure_openai import AzureOpenAIConfigError, AzureOpenAIService

logger = get_logger(__name__)

PROMPT_VERSION = "v1"
_DESCRIPTION_EXCERPT_CHARS = 800
_RATIONALE_MAX_CHARS = 240
_MAX_ATTEMPTS = 2  # spec §13.5: "retried once on schema violation"


class LLMScoreOutput(BaseModel):
    """The model's structured output — spec §13.4's contract minus the
    fields this codebase, not the model, is responsible for stamping
    (`match_id`/`prompt_version`/versions/`scored_at`).
    """

    relevance_score: float = Field(ge=0.0, le=1.0)
    confidence_score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=_RATIONALE_MAX_CHARS)
    cited_signals: list[str]
    disagrees_with_rules: bool


_SYSTEM_PROMPT = """You are validating a rules-computed hiring-lead match for a GTM (go-to-market) team. \
A deterministic rules engine has already decided this lead plausibly owns this job and computed a match \
score from named signals. Your job is narrower than matching from scratch:

1. Judge the rules-based match against the full job description and lead profile text, which the rules \
engine never reads.
2. Explain the match in one or two sentences a GTM person can act on (<= 240 characters).
3. Adjust the relevance/confidence score only where the text contradicts the structural signals.

Rules, not suggestions:
- Do not invent a match the rules didn't find. You are scoring the pair you were given, not searching for \
a better one.
- `cited_signals` MUST be a subset of the signal names you were given in the input. Never cite a signal \
that was not supplied to you.
- The rationale must not assert facts absent from the input. No inferring seniority from a name, no \
speculating about reporting lines that were not stated.
- When the input evidence is thin (few non-zero signals, low match_confidence, degraded job data), your \
confidence_score must be low. A confident-sounding rationale for weak evidence is worse than an honest low \
score.
- Set disagrees_with_rules to true only when your relevance_score differs materially from the rules' own \
match_score."""


def _excerpt(text: str) -> str:
    return text[:_DESCRIPTION_EXCERPT_CHARS]


def _contactability_note(lead: LeadRecord) -> str:
    if lead.email_status is not None and lead.email_status.value == "verified":
        return "verified email on file"
    if lead.email:
        return "unverified/guessed email on file"
    if lead.phone:
        return "phone on file, no email"
    return "no verified contact method on file"


def build_user_prompt(
    *, job: JobPosting, lead: LeadRecord, company: Company, match: LeadJobMatch, context: CompanyContext | None
) -> str:
    """Spec §13.3's prompt inputs, assembled into one user message."""
    lines = [
        "## Job",
        f"- title: {job.title_canonical}",
        f"- function: {job.function}",
        f"- seniority: {job.seniority}",
        f"- location: {job.location_raw}",
        f"- description excerpt: {_excerpt(job.description_text)}",
        f"- is_degraded (heuristically extracted, treat with suspicion): {job.is_degraded}",
        "",
        "## Lead",
        f"- name: {lead.full_name}",
        f"- title: {lead.title_raw}",
        f"- function: {lead.function}",
        f"- seniority: {lead.seniority}",
        f"- tenure_months: {lead.tenure_months}",
        f"- is_founder: {lead.is_founder}",
        f"- is_recruiter: {lead.is_recruiter}",
        f"- contactability: {_contactability_note(lead)}",
        "",
        "## Rules-based match (already computed — do not re-derive, judge it)",
        f"- rules match_score: {match.match_score:.3f}",
        f"- rules match_confidence: {match.match_confidence:.3f}",
        f"- signal breakdown: {match.signals}",
        "",
        "## Company",
        f"- headcount: {company.headcount}",
        f"- funding_stage: {company.funding_stage}",
        f"- context summary (prioritisation only, not matching evidence): {context.summary if context else 'unavailable'}",
    ]
    return "\n".join(lines)


@dataclass
class ScoringOutcome:
    status: ScoringStatus
    scored_lead: ScoredLead | None
    detail: str | None = None


def _job_version(job: JobPosting) -> str:
    return job.last_seen_at.isoformat()


def _lead_version(lead: LeadRecord) -> str:
    return (lead.enriched_at or lead.retrieved_at).isoformat()


def grounding_violation(output: LLMScoreOutput, match: LeadJobMatch) -> str | None:
    """Spec §20.6: "every `cited_signals` entry must correspond to a signal
    actually supplied. A citation of a signal that wasn't in the prompt is a
    fabrication, and it is mechanically detectable." Returns a description of
    the violation, or `None` if grounded.
    """
    unsupplied = set(output.cited_signals) - set(match.signals.keys())
    if unsupplied:
        return f"cited_signals references signals not supplied: {sorted(unsupplied)}"
    return None


async def score_pair(
    *,
    job: JobPosting,
    lead: LeadRecord,
    company: Company,
    match: LeadJobMatch,
    context: CompanyContext | None,
    llm: AzureOpenAIService | None = None,
    now: datetime | None = None,
) -> ScoringOutcome:
    """Stage 10 for one `(job, lead)` pair. Spec §13.5: "Temperature low,
    output validated, retried once on schema violation" — a grounding
    violation (`grounding_violation`) counts as a schema violation for
    retry purposes, same as a raw API/validation error, since both mean the
    output can't be trusted as-is.
    """
    now = now or datetime.now(UTC)
    service = llm or AzureOpenAIService()

    if not service.is_configured:
        return ScoringOutcome(
            status=ScoringStatus.SCORING_FAILED, scored_lead=None, detail="Azure OpenAI is not configured"
        )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(job=job, lead=lead, company=company, match=match, context=context)},
    ]

    last_detail: str | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            client = service.get_client()
            completion = await asyncio.to_thread(
                client.beta.chat.completions.parse,
                model=service.deployment,
                messages=messages,
                response_format=LLMScoreOutput,
            )
        except (APIError, AzureOpenAIConfigError) as exc:
            last_detail = f"Azure OpenAI request failed: {exc}"
            logger.warning("scoring_attempt_failed", attempt=attempt, error=str(exc))
            continue

        parsed = completion.choices[0].message.parsed
        if parsed is None:
            last_detail = "model refused or returned no parsed output"
            logger.warning("scoring_attempt_failed", attempt=attempt, detail=last_detail)
            continue

        try:
            output = LLMScoreOutput.model_validate(parsed.model_dump())
        except ValidationError as exc:
            last_detail = f"schema violation: {exc}"
            logger.warning("scoring_attempt_failed", attempt=attempt, detail=last_detail)
            continue

        violation = grounding_violation(output, match)
        if violation:
            last_detail = violation
            logger.warning("scoring_attempt_failed", attempt=attempt, detail=violation)
            continue

        scored = ScoredLead(
            id=str(uuid.uuid4()),
            match_id=match.id,
            relevance_score=output.relevance_score,
            confidence_score=output.confidence_score,
            rationale=output.rationale,
            cited_signals=output.cited_signals,
            disagrees_with_rules=output.disagrees_with_rules,
            prompt_version=PROMPT_VERSION,
            job_version=_job_version(job),
            lead_version=_lead_version(lead),
            scored_at=now,
        )
        return ScoringOutcome(status=ScoringStatus.SCORED, scored_lead=scored)

    logger.warning("scoring_failed", match_id=match.id, detail=last_detail)
    return ScoringOutcome(status=ScoringStatus.SCORING_FAILED, scored_lead=None, detail=last_detail)


def fallback_scored_lead(
    match: LeadJobMatch, job: JobPosting, lead: LeadRecord, *, now: datetime | None = None
) -> ScoredLead:
    """Spec §17.2's `scoring_failed` downstream handling: "publish with
    rules score only, no rationale, flagged." Used by the caller (Stage 11)
    when `score_pair` returns `SCORING_FAILED` — a job/lead pair is never
    silently dropped just because the LLM call failed, consistent with
    §2.3 applied one stage further down the pipeline.
    """
    now = now or datetime.now(UTC)
    return ScoredLead(
        id=str(uuid.uuid4()),
        match_id=match.id,
        relevance_score=match.match_score,
        confidence_score=match.match_confidence,
        rationale="",
        cited_signals=[],
        disagrees_with_rules=False,
        prompt_version=PROMPT_VERSION,
        job_version=_job_version(job),
        lead_version=_lead_version(lead),
        scored_at=now,
    )
