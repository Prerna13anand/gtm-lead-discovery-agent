"""Human feedback capture — spec §19.5.

"Build the capture mechanism in Phase 3 even if nothing consumes it until
Phase 5." This module is deliberately thin: it only constructs a
`LeadFeedback` record for `core.lead_store.LeadFeedbackStore` to persist.
Nothing in this codebase reads feedback back yet — by design, per the spec
quote above; a consumer (matching-weight tuning) is Phase 5 scope.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from gtm_agent.models.feedback import FeedbackRating, LeadFeedback


def record_feedback(
    *,
    match_id: str,
    job_id: str,
    lead_id: str,
    rating: FeedbackRating,
    notes: str | None = None,
    submitted_by: str | None = None,
    now: datetime | None = None,
) -> LeadFeedback:
    return LeadFeedback(
        id=str(uuid.uuid4()),
        match_id=match_id,
        job_id=job_id,
        lead_id=lead_id,
        rating=rating,
        notes=notes,
        submitted_by=submitted_by,
        submitted_at=now or datetime.now(UTC),
    )
