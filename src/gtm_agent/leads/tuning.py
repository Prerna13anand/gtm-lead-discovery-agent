"""Matching-weight tuning inputs — spec §22 Phase 5, §19.5, §23.15 (Phase 5).

"Who owns matching-weight tuning once feedback data exists? Without a named
owner the weights ossify at their initial guesses" (spec §23.15). This
module does not itself tune `leads.matching`'s weights — that's an owned,
human-in-the-loop process this codebase can't invent an owner for. What it
provides is the first input such a process needs: whether the rules'
existing ranking already agrees with what the GTM team marked useful.
"""

from __future__ import annotations

from dataclasses import dataclass

from gtm_agent.models.feedback import FeedbackRating, LeadFeedback
from gtm_agent.models.matching import LeadJobMatch


@dataclass(frozen=True)
class FeedbackAgreement:
    agreements: int
    total: int
    rate: float | None
    """`None` when no feedback references a still-known match — spec
    §19.5's capture-now-consume-later posture means this can legitimately
    be empty for a long time before Phase 5 tooling exists to read it.
    """


def compute_feedback_agreement(
    feedback: list[LeadFeedback], matches_by_id: dict[str, LeadJobMatch]
) -> FeedbackAgreement:
    """This codebase's own starting definition of "agreement" (the spec
    names the *capability* — capture feedback, tune weights against it —
    without prescribing a formula): a rules-based rank-1 pick "agrees" with
    feedback marking it useful, and a non-rank-1 pick "agrees" with
    feedback marking it not useful. Feedback whose `match_id` no longer
    resolves (the match was superseded or the store was pruned) is skipped
    rather than guessed at.
    """
    agreements = 0
    total = 0
    for entry in feedback:
        match = matches_by_id.get(entry.match_id)
        if match is None:
            continue
        total += 1
        is_top_ranked = match.rank_within_job == 1
        marked_useful = entry.rating == FeedbackRating.USEFUL
        if is_top_ranked == marked_useful:
            agreements += 1

    rate = agreements / total if total else None
    return FeedbackAgreement(agreements=agreements, total=total, rate=rate)
