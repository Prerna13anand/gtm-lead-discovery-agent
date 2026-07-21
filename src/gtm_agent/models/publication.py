"""Stage 11 — Publication & Output Contract (spec §14)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CompanySummary(BaseModel):
    name: str
    domain: str
    stage: str | None = None
    headcount: int | None = None


class JobSummary(BaseModel):
    title: str
    function: str | None = None
    seniority: str | None = None
    location: str | None = None
    posting_url: str


class LeadSummary(BaseModel):
    name: str
    title: str
    linkedin_url: str | None = None
    email: str | None = None
    phone: str | None = None
    contactability: str
    """A short human-readable reachability label (e.g. "verified email",
    "guessed email", "no contact on file") — spec §13.6's `contactability_weight`
    drives ranking from the same underlying data; this field is the
    GTM-facing rendering of it, not a duplicate scoring signal.
    """


class GtmLead(BaseModel):
    """Spec §14.1, field for field."""

    company: CompanySummary
    job: JobSummary
    lead: LeadSummary
    relevance_score: float = Field(ge=0.0, le=1.0)
    confidence_score: float = Field(ge=0.0, le=1.0)
    rationale: str
    match_signals: dict[str, float] = Field(default_factory=dict)
    company_context: str | None = None
    rank: int
    generated_at: datetime
    data_provenance: dict[str, str] = Field(default_factory=dict)


class PublicationEventType(StrEnum):
    """Spec §14.3."""

    LEAD_READY = "lead_ready"
    JOB_UNMATCHED = "job_unmatched"
    LEAD_SUPERSEDED = "lead_superseded"
    JOB_CLOSED = "job_closed"


class PublicationEvent(BaseModel):
    """Spec §14.3's event stream — the Part III analogue of Stage 5's
    `scrape_event` (spec §15.1), same "downstream-facing event log" role.
    """

    id: str
    event_type: PublicationEventType
    job_id: str
    lead_id: str | None = None
    """`None` for `job_unmatched`/`job_closed` — those are job-level events
    with no specific lead attached."""
    occurred_at: datetime
    payload: dict[str, str] = Field(default_factory=dict)
