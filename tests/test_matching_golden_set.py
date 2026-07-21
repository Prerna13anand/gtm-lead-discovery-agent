"""Matching golden-set regression test — spec §19.4, §22 Phase 5.

"~50 `(job, lead)` pairs across a headcount range, hand-labelled by someone
with GTM judgement as *correct owner / plausible / wrong*. Matching accuracy
measured against it on every rules change."

This codebase has no access to real GTM judgement — that is a real
limitation, documented in the project report, not something a coding agent
can invent. `tests/fixtures/matching_golden_set.json` is therefore a
starting corpus (18 pairs, not 50) hand-constructed from this codebase's own
domain reasoning about the segment (§9.2's "founders are the hiring
managers" insight, function/seniority alignment, the §10.8 worked example),
over-sampling sub-20-headcount companies per spec §19.4's own guidance. Its
job today is exactly what spec §19.4 asks of it even at this size: "measured
... on every rules change" — a regression lock for §10's weights, not a
substitute for the real labelled set a GTM person would eventually build.

Evaluation logic lives in `gtm_agent.leads.golden_set` (shared with
`main.py`'s `golden-set` CLI command, spec §22 Phase 5's "golden-set
automation") — this test is a thin wrapper asserting the shared evaluator's
output meets a bar, not a second implementation of it.
"""

from datetime import UTC, datetime

import pytest

from gtm_agent.leads.golden_set import evaluate, load_cases

NOW = datetime(2026, 1, 1, tzinfo=UTC)
_MIN_ACCURACY = 0.85

_CASES = load_cases()


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["case_id"])
def test_golden_set_case_is_computed(case: dict) -> None:
    """Smoke test: every case evaluates without error, individually, so a
    single bad fixture entry is immediately identifiable, separate from the
    aggregate accuracy test below.
    """
    report = evaluate([case], now=NOW)
    assert report.total == 1


def test_golden_set_accuracy_meets_threshold() -> None:
    report = evaluate(_CASES, now=NOW)
    assert report.accuracy >= _MIN_ACCURACY, (
        f"accuracy {report.accuracy:.2%} below {_MIN_ACCURACY:.0%}; mismatches: {report.mismatches}"
    )
