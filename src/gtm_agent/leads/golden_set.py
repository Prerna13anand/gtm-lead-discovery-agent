"""Matching golden-set evaluation — spec §19.4, §22 Phase 5 ("golden-set automation").

"~50 `(job, lead)` pairs across a headcount range, hand-labelled by someone
with GTM judgement as *correct owner / plausible / wrong*. Matching accuracy
measured against it on every rules change."

Shared between the test suite (`tests/test_matching_golden_set.py`, run on
every CI change — the automation spec §22 Phase 5 asks for) and the CLI
(`main.py golden-set`, an on-demand operator-triggered run of the same
check) — one evaluation implementation, two call sites, per this project's
instruction to reuse rather than duplicate.

See `tests/fixtures/matching_golden_set.json`'s own note and the project
report: this is an 18-pair starting corpus this codebase constructed from
its own domain reasoning, not real GTM-labelled ground truth — that input
doesn't exist yet (spec §19.4 assumes a human builds it). Automating
evaluation of a not-yet-real-world-validated set is still real, useful
infrastructure: it's the harness the real set drops into once it exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gtm_agent.leads.matching import MATCH_FLOOR, combine, compute_signals
from gtm_agent.models.common import JobFunction, Seniority
from gtm_agent.models.company import Company
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EnrichmentStatus, LeadRecord, LeadSource

# This test/CLI-shared bucketing threshold (not a spec value — see module
# docstring): a score at or above this is called "correct_owner", between
# this and `MATCH_FLOOR` is "plausible", below `MATCH_FLOOR` is "wrong". The
# §10.8 worked example's own score spread (Staff Engineer 0.65, published;
# Head of Ops 0.237, excluded) is what motivates the specific value.
CORRECT_OWNER_THRESHOLD = 0.6

DEFAULT_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "matching_golden_set.json"


def load_cases(path: Path | str = DEFAULT_FIXTURE_PATH) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _job_from_case(case: dict, *, now: datetime) -> JobPosting:
    j = case["job"]
    return JobPosting(
        job_id="job-1", company_id="acme", source_platform="greenhouse", posting_url="https://acme.com/jobs/1",
        title_raw="Role", title_canonical="Role", description_text="", description_markdown="",
        function=JobFunction(j["function"]) if j.get("function") else None,
        seniority=Seniority(j["seniority"]) if j.get("seniority") else None,
        first_seen_at=now, last_seen_at=now,
    )


def _lead_from_case(case: dict, *, now: datetime) -> LeadRecord:
    lead = case["lead"]
    return LeadRecord(
        lead_id=case["case_id"], company_id="acme", source=LeadSource.APOLLO, full_name=lead["title_raw"],
        title_raw=lead["title_raw"], title_canonical=lead["title_raw"],
        function=JobFunction(lead["function"]) if lead.get("function") else None,
        seniority=Seniority(lead["seniority"]) if lead.get("seniority") else None,
        is_founder=lead.get("is_founder", False), is_recruiter=lead.get("is_recruiter", False),
        retrieved_at=now, enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )


def predicted_label(score: float) -> str:
    if score >= CORRECT_OWNER_THRESHOLD:
        return "correct_owner"
    if score >= MATCH_FLOOR:
        return "plausible"
    return "wrong"


@dataclass(frozen=True)
class Mismatch:
    case_id: str
    expected_label: str
    predicted_label: str
    score: float


@dataclass(frozen=True)
class GoldenSetReport:
    total: int
    correct: int
    accuracy: float
    mismatches: list[Mismatch] = field(default_factory=list)


def evaluate(cases: list[dict], *, now: datetime) -> GoldenSetReport:
    correct = 0
    mismatches: list[Mismatch] = []
    for case in cases:
        company = Company(id="acme", name="Acme", domain="acme.com", added_at=now, headcount=case["headcount"])
        job = _job_from_case(case, now=now)
        lead = _lead_from_case(case, now=now)
        score = combine(compute_signals(lead, job, company))
        predicted = predicted_label(score)
        if predicted == case["expected_label"]:
            correct += 1
        else:
            mismatches.append(
                Mismatch(case_id=case["case_id"], expected_label=case["expected_label"], predicted_label=predicted, score=score)
            )

    total = len(cases)
    accuracy = correct / total if total else 0.0
    return GoldenSetReport(total=total, correct=correct, accuracy=accuracy, mismatches=mismatches)
