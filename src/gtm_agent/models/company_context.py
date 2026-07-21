"""Stage 9 (spec §12) output — the persisted `company_context` row (spec §15.2)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CompanyContextStatus(StrEnum):
    """Spec §17.2's Stage 9 terminal states."""

    CONTEXT_OK = "context_ok"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    """Tavily failed. Non-blocking (spec §12.4) — leads still publish and
    score on the remaining signals."""


class CompanyContext(BaseModel):
    """Spec §15.2: "one row per company with TTL." §12.3: "prioritisation
    signal, not matching signal" — this must never feed Stage 7 matching,
    only Stage 10 ranking/rationale (Phase 4) and §13.6 ranking weights.
    """

    company_id: str
    summary: str
    funding_signal: str | None = None
    hiring_signal: str | None = None
    sources: list[str] = Field(default_factory=list)
    fetched_at: datetime
    expires_at: datetime
