"""The `company` table — spec §15.1. The input list to the whole pipeline."""

from datetime import datetime

from pydantic import BaseModel


class Company(BaseModel):
    id: str
    name: str
    domain: str
    funding_stage: str | None = None
    added_at: datetime
    is_active: bool = True

    headcount: int | None = None
    """Spec §10.4: "Headcount comes from Apollo Company Search (SDD §8.2)."
    Added in Phase 3 for Stage 7's headcount modulation — optional and
    defaulting to `None` so every existing `Company(...)` call site from
    Part I stays valid unchanged. `None` falls back to `funding_stage`-based
    tiering (spec §10.4: "When unavailable, fall back to funding stage, and
    record lower confidence") — see `leads.matching.resolve_headcount_tier`.
    """
