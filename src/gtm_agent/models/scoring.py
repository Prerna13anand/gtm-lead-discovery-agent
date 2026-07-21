"""Stage 10 — Scoring, Rationale & Ranking (spec §13) — the persisted `scored_lead` row (spec §15.2)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ScoringStatus(StrEnum):
    """Spec §17.2's Stage 10 terminal states."""

    SCORED = "scored"
    SCORING_FAILED = "scoring_failed"
    """LLM error or schema violation after retry (spec §17.2) — "publish with
    rules score only, no rationale, flagged."""


class ScoredLead(BaseModel):
    """Spec §13.4's output contract, plus the persistence/caching fields
    spec §15.2 adds: `match_id`, `prompt_version`, `scored_at`, and the
    `job_version`/`lead_version` cache-key components (spec §13.5: "cached
    by `(job_id, lead_id, job_version, lead_version)`" — extended here with
    `match_id` since one job/lead pair has exactly one current match, and
    `prompt_version` since "prompt changes invalidate the cache explicitly").

    `job_version`/`lead_version` are this codebase's own stand-in for a
    concept the spec doesn't fully specify a source for on the lead side
    (`job_posting_version` exists for jobs, spec §8.5; no equivalent
    `lead_version` counter exists for leads) — see `scoring.rationale`'s
    module docstring for what's actually used as each.
    """

    id: str
    match_id: str
    relevance_score: float = Field(ge=0.0, le=1.0)
    confidence_score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=240)
    cited_signals: list[str] = Field(default_factory=list)
    disagrees_with_rules: bool = False
    prompt_version: str
    job_version: str
    lead_version: str
    scored_at: datetime
