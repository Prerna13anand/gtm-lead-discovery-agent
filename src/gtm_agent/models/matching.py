"""Stage 7 (spec §10) outputs — the persisted `lead_job_match` and `unmatched_job` rows (spec §15.2)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class UnmatchedReason(StrEnum):
    """Spec §10.6 / §17.2 — why a job has no lead attached."""

    NO_PLAUSIBLE_OWNER = "no_plausible_owner"
    """Leads exist; none cleared the match floor. Usually a function we
    retrieved no personas for (spec §10.6)."""

    NO_LEADS_RETRIEVED = "no_leads_retrieved"
    """Apollo returned nobody for this company (spec §10.6)."""

    LEAD_DISCOVERY_FAILED = "lead_discovery_failed"
    """Stage 6 errored — unknown, not "nobody" (spec §2.3, §10.6)."""


class LeadJobMatch(BaseModel):
    """Spec §15.2's `lead_job_match` — "the inspectable record of *why*."

    `signals` is the §10.3 breakdown, retained (not just the total) so a
    downstream LLM (Phase 4) or a human can see which signal drove the
    score, not just the number. `rules_version` is what lets a later score
    change be attributed to changed data vs. changed weights (spec §15.2).
    """

    id: str
    job_id: str
    lead_id: str
    match_score: float = Field(ge=0.0, le=1.0)
    match_confidence: float = Field(ge=0.0, le=1.0)
    signals: dict[str, float] = Field(default_factory=dict)
    rank_within_job: int
    computed_at: datetime
    rules_version: str


class UnmatchedJob(BaseModel):
    """Spec §15.2's `unmatched_job` — "deliberately its own table rather
    than a null-lead row: these records are a **work queue for
    persona-coverage bugs** (§10.6), not just an output state."
    """

    job_id: str
    reason: UnmatchedReason
    recorded_at: datetime
    run_id: str
