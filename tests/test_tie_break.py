"""Stage 7 — LLM tie-break tests (spec §10.7).

The LLM call is mocked (same fake-service pattern as
test_scoring_rationale.py / test_llm_residue.py). A real, live tie-break
call against Azure OpenAI was exercised manually outside the test suite —
see the project report for that result.
"""

from __future__ import annotations

from datetime import UTC, datetime

from gtm_agent.leads.tie_break import TieBreakChoice, break_tie, resolve_tie_breaks
from gtm_agent.models.common import JobFunction, Seniority
from gtm_agent.models.company import Company
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EnrichmentStatus, LeadRecord, LeadSource
from gtm_agent.models.matching import LeadJobMatch

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job(job_id: str = "j1") -> JobPosting:
    return JobPosting(
        job_id=job_id, company_id="acme", source_platform="greenhouse", posting_url=f"https://acme.com/{job_id}",
        title_raw="Sales Development Representative", title_canonical="Sales Development Representative",
        description_text="Reports to the Head of Sales.", description_markdown="",
        function=JobFunction.SALES, seniority=Seniority.ENTRY, first_seen_at=NOW, last_seen_at=NOW,
    )


def _lead(lead_id: str, **overrides) -> LeadRecord:
    base = dict(
        lead_id=lead_id, company_id="acme", source=LeadSource.APOLLO, full_name=f"Person {lead_id}",
        title_raw="Head of Sales", title_canonical="Head of Sales", retrieved_at=NOW,
        enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )
    base.update(overrides)
    return LeadRecord(**base)


def _match(job_id: str, lead_id: str, score: float, rank: int) -> LeadJobMatch:
    return LeadJobMatch(
        id=f"{job_id}-{lead_id}", job_id=job_id, lead_id=lead_id, match_score=score, match_confidence=0.8,
        signals={}, rank_within_job=rank, computed_at=NOW, rules_version="v1",
    )


def _company() -> Company:
    return Company(id="acme", name="Acme", domain="acme.com", added_at=NOW, headcount=40, funding_stage="series a")


class _FakeMessage:
    def __init__(self, parsed):
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed):
        self.message = _FakeMessage(parsed)


class _FakeCompletion:
    def __init__(self, parsed):
        self.choices = [_FakeChoice(parsed)]


class _FakeParseCallable:
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        result = self._results[min(self.calls, len(self._results)) - 1]
        if isinstance(result, Exception):
            raise result
        return _FakeCompletion(result)


class _FakeAzureOpenAIService:
    def __init__(self, results, *, configured: bool = True):
        self.is_configured = configured
        self.deployment = "test-deployment"
        self._parse = _FakeParseCallable(results)

    def get_client(self):
        client = type("Client", (), {})()
        client.beta = type("Beta", (), {})()
        client.beta.chat = type("Chat", (), {})()
        client.beta.chat.completions = type("Completions", (), {})()
        client.beta.chat.completions.parse = self._parse
        return client


# --- break_tie ---------------------------------------------------------


async def test_not_configured_returns_none():
    service = _FakeAzureOpenAIService([], configured=False)
    lead_a, lead_b = _lead("l1"), _lead("l2")
    match_a, match_b = _match("j1", "l1", 0.8, 1), _match("j1", "l2", 0.78, 2)
    result = await break_tie(job=_job(), company=_company(), top_two=[(lead_a, match_a), (lead_b, match_b)], llm=service)
    assert result is None


async def test_valid_choice_is_returned():
    service = _FakeAzureOpenAIService([TieBreakChoice(preferred_lead_id="l2", rationale="better fit")])
    lead_a, lead_b = _lead("l1"), _lead("l2")
    match_a, match_b = _match("j1", "l1", 0.8, 1), _match("j1", "l2", 0.78, 2)
    result = await break_tie(job=_job(), company=_company(), top_two=[(lead_a, match_a), (lead_b, match_b)], llm=service)
    assert result == "l2"


async def test_grounding_violation_is_rejected_and_retried():
    bad = TieBreakChoice(preferred_lead_id="not-a-real-lead-id", rationale="x")
    good = TieBreakChoice(preferred_lead_id="l1", rationale="x")
    service = _FakeAzureOpenAIService([bad, good])
    lead_a, lead_b = _lead("l1"), _lead("l2")
    match_a, match_b = _match("j1", "l1", 0.8, 1), _match("j1", "l2", 0.78, 2)
    result = await break_tie(job=_job(), company=_company(), top_two=[(lead_a, match_a), (lead_b, match_b)], llm=service)
    assert result == "l1"
    assert service._parse.calls == 2


async def test_gives_up_after_max_attempts():
    bad = TieBreakChoice(preferred_lead_id="not-a-real-lead-id", rationale="x")
    service = _FakeAzureOpenAIService([bad, bad, bad])
    lead_a, lead_b = _lead("l1"), _lead("l2")
    match_a, match_b = _match("j1", "l1", 0.8, 1), _match("j1", "l2", 0.78, 2)
    result = await break_tie(job=_job(), company=_company(), top_two=[(lead_a, match_a), (lead_b, match_b)], llm=service)
    assert result is None
    assert service._parse.calls == 2


# --- resolve_tie_breaks --------------------------------------------------


async def test_resolve_tie_breaks_swaps_rank_when_llm_prefers_second():
    service = _FakeAzureOpenAIService([TieBreakChoice(preferred_lead_id="l2", rationale="x")])
    matches = [_match("j1", "l1", 0.80, 1), _match("j1", "l2", 0.78, 2)]  # within TIE_BREAK_BAND (0.05)
    leads_by_id = {"l1": _lead("l1"), "l2": _lead("l2")}
    jobs_by_id = {"j1": _job()}

    outcome = await resolve_tie_breaks(matches, jobs_by_id=jobs_by_id, leads_by_id=leads_by_id, company=_company(), llm=service)

    assert outcome.ties_detected == 1
    assert outcome.ties_resolved == 1
    ranked = {m.lead_id: m.rank_within_job for m in outcome.matches}
    assert ranked["l2"] == 1
    assert ranked["l1"] == 2


async def test_resolve_tie_breaks_keeps_order_when_llm_agrees_with_rules():
    service = _FakeAzureOpenAIService([TieBreakChoice(preferred_lead_id="l1", rationale="x")])
    matches = [_match("j1", "l1", 0.80, 1), _match("j1", "l2", 0.78, 2)]
    leads_by_id = {"l1": _lead("l1"), "l2": _lead("l2")}
    jobs_by_id = {"j1": _job()}

    outcome = await resolve_tie_breaks(matches, jobs_by_id=jobs_by_id, leads_by_id=leads_by_id, company=_company(), llm=service)

    ranked = {m.lead_id: m.rank_within_job for m in outcome.matches}
    assert ranked["l1"] == 1
    assert ranked["l2"] == 2


async def test_resolve_tie_breaks_skips_jobs_outside_the_band():
    service = _FakeAzureOpenAIService([])  # would fail if called
    matches = [_match("j1", "l1", 0.90, 1), _match("j1", "l2", 0.50, 2)]  # far apart
    leads_by_id = {"l1": _lead("l1"), "l2": _lead("l2")}
    jobs_by_id = {"j1": _job()}

    outcome = await resolve_tie_breaks(matches, jobs_by_id=jobs_by_id, leads_by_id=leads_by_id, company=_company(), llm=service)

    assert outcome.ties_detected == 0
    assert service._parse.calls == 0


async def test_resolve_tie_breaks_handles_single_match_job_without_calling_llm():
    service = _FakeAzureOpenAIService([])
    matches = [_match("j1", "l1", 0.90, 1)]
    leads_by_id = {"l1": _lead("l1")}
    jobs_by_id = {"j1": _job()}

    outcome = await resolve_tie_breaks(matches, jobs_by_id=jobs_by_id, leads_by_id=leads_by_id, company=_company(), llm=service)

    assert outcome.ties_detected == 0
    assert len(outcome.matches) == 1


async def test_resolve_tie_breaks_llm_failure_preserves_rules_order():
    service = _FakeAzureOpenAIService([None, None])  # no parsed output -> break_tie gives up -> None
    matches = [_match("j1", "l1", 0.80, 1), _match("j1", "l2", 0.78, 2)]
    leads_by_id = {"l1": _lead("l1"), "l2": _lead("l2")}
    jobs_by_id = {"j1": _job()}

    outcome = await resolve_tie_breaks(matches, jobs_by_id=jobs_by_id, leads_by_id=leads_by_id, company=_company(), llm=service)

    ranked = {m.lead_id: m.rank_within_job for m in outcome.matches}
    assert ranked["l1"] == 1  # unchanged
    assert outcome.ties_detected == 1
    assert outcome.ties_resolved == 0


async def test_resolve_tie_breaks_preserves_matches_below_top_two():
    service = _FakeAzureOpenAIService([TieBreakChoice(preferred_lead_id="l1", rationale="x")])
    matches = [
        _match("j1", "l1", 0.80, 1),
        _match("j1", "l2", 0.78, 2),
        _match("j1", "l3", 0.40, 3),
    ]
    leads_by_id = {"l1": _lead("l1"), "l2": _lead("l2"), "l3": _lead("l3")}
    jobs_by_id = {"j1": _job()}

    outcome = await resolve_tie_breaks(matches, jobs_by_id=jobs_by_id, leads_by_id=leads_by_id, company=_company(), llm=service)

    ranked = {m.lead_id: m.rank_within_job for m in outcome.matches}
    assert ranked["l3"] == 3
