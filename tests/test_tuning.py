"""Matching-weight tuning input tests — spec §19.5, §23.15."""

from datetime import UTC, datetime

from gtm_agent.leads.tuning import compute_feedback_agreement
from gtm_agent.models.feedback import FeedbackRating, LeadFeedback
from gtm_agent.models.matching import LeadJobMatch

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _match(match_id: str, rank: int) -> LeadJobMatch:
    return LeadJobMatch(
        id=match_id, job_id="j1", lead_id="l1", match_score=0.8, match_confidence=0.8,
        signals={}, rank_within_job=rank, computed_at=NOW, rules_version="v1",
    )


def _feedback(match_id: str, rating: FeedbackRating) -> LeadFeedback:
    return LeadFeedback(id=f"f-{match_id}", match_id=match_id, job_id="j1", lead_id="l1", rating=rating, submitted_at=NOW)


def test_no_feedback_returns_none_rate():
    result = compute_feedback_agreement([], {})
    assert result.rate is None
    assert result.total == 0


def test_top_ranked_and_useful_agrees():
    matches = {"m1": _match("m1", rank=1)}
    feedback = [_feedback("m1", FeedbackRating.USEFUL)]
    result = compute_feedback_agreement(feedback, matches)
    assert result.agreements == 1
    assert result.rate == 1.0


def test_top_ranked_and_not_useful_disagrees():
    matches = {"m1": _match("m1", rank=1)}
    feedback = [_feedback("m1", FeedbackRating.NOT_USEFUL)]
    result = compute_feedback_agreement(feedback, matches)
    assert result.agreements == 0
    assert result.rate == 0.0


def test_non_top_ranked_and_not_useful_agrees():
    matches = {"m2": _match("m2", rank=2)}
    feedback = [_feedback("m2", FeedbackRating.NOT_USEFUL)]
    result = compute_feedback_agreement(feedback, matches)
    assert result.agreements == 1
    assert result.rate == 1.0


def test_feedback_for_unknown_match_is_skipped():
    result = compute_feedback_agreement([_feedback("missing", FeedbackRating.USEFUL)], {})
    assert result.total == 0
    assert result.rate is None


def test_mixed_feedback_computes_partial_rate():
    matches = {"m1": _match("m1", rank=1), "m2": _match("m2", rank=2)}
    feedback = [_feedback("m1", FeedbackRating.USEFUL), _feedback("m2", FeedbackRating.USEFUL)]
    result = compute_feedback_agreement(feedback, matches)
    assert result.total == 2
    assert result.agreements == 1  # m1 agrees (top+useful), m2 disagrees (non-top+useful)
    assert result.rate == 0.5
