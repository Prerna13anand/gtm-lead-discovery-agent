"""Feedback capture tests — spec §19.5."""

from datetime import UTC, datetime

from gtm_agent.leads.feedback import record_feedback
from gtm_agent.models.feedback import FeedbackRating


def test_record_feedback_builds_a_valid_record():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    feedback = record_feedback(
        match_id="m1", job_id="j1", lead_id="l1", rating=FeedbackRating.USEFUL,
        notes="Great contact", submitted_by="gtm@acme.com", now=now,
    )
    assert feedback.match_id == "m1"
    assert feedback.rating == FeedbackRating.USEFUL
    assert feedback.submitted_at == now
    assert feedback.id  # a UUID was generated


def test_record_feedback_defaults_now_when_omitted():
    feedback = record_feedback(match_id="m1", job_id="j1", lead_id="l1", rating=FeedbackRating.NOT_USEFUL)
    assert feedback.submitted_at is not None
