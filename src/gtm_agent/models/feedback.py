"""Human feedback capture — spec §19.5.

"The highest-quality signal available is the GTM team marking leads useful
or not... Build the capture mechanism in Phase 3 even if nothing consumes it
until Phase 5. Feedback not captured is permanently lost."

No table shape is specified for this — the spec only describes the
capability ("capture it"), not a schema — so this is this codebase's own,
conservative design: one row per (job, lead) pair a person actually judged,
keyed to the `lead_job_match` it was shown against so a later consumer can
join back to the exact signal breakdown that produced the recommendation
being rated.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class FeedbackRating(StrEnum):
    USEFUL = "useful"
    NOT_USEFUL = "not_useful"


class LeadFeedback(BaseModel):
    id: str
    match_id: str  # LeadJobMatch.id this feedback rates
    job_id: str
    lead_id: str
    rating: FeedbackRating
    notes: str | None = None
    submitted_by: str | None = None
    submitted_at: datetime
