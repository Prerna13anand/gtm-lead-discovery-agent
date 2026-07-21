"""Stage 10 — Scoring & Rationale tests (spec §13, §20.6).

The LLM call itself is mocked (a fake `AzureOpenAIService`) so these tests
are deterministic and offline, per spec §20.1's "no network in unit tests,
ever." A real, live call against Azure OpenAI was exercised manually outside
the test suite — see the project report for that result and its output.
"""

from __future__ import annotations

import httpx
import pytest
from openai import APIError

from gtm_agent.models.common import JobFunction, Seniority
from gtm_agent.models.company import Company
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EmailStatus, EnrichmentStatus, LeadRecord, LeadSource
from gtm_agent.models.matching import LeadJobMatch
from gtm_agent.models.scoring import ScoringStatus
from gtm_agent.scoring.rationale import (
    LLMScoreOutput,
    build_user_prompt,
    fallback_scored_lead,
    grounding_violation,
    score_pair,
)
from datetime import UTC, datetime

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job() -> JobPosting:
    return JobPosting(
        job_id="j1", company_id="acme", source_platform="greenhouse", posting_url="https://acme.com/jobs/1",
        title_raw="Senior Backend Engineer", title_canonical="Senior Backend Engineer",
        description_text="Reports to the CTO.", description_markdown="",
        function=JobFunction.ENGINEERING, seniority=Seniority.SENIOR, first_seen_at=NOW, last_seen_at=NOW,
    )


def _lead() -> LeadRecord:
    return LeadRecord(
        lead_id="l1", company_id="acme", source=LeadSource.APOLLO, full_name="Jamie Chen",
        title_raw="CTO", title_canonical="CTO", seniority=Seniority.EXECUTIVE, is_founder=True,
        email="jamie@acme.com", email_status=EmailStatus.VERIFIED, retrieved_at=NOW,
        enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )


def _company() -> Company:
    return Company(id="acme", name="Acme", domain="acme.com", added_at=NOW, headcount=18, funding_stage="seed")


def _match() -> LeadJobMatch:
    return LeadJobMatch(
        id="m1", job_id="j1", lead_id="l1", match_score=0.94, match_confidence=0.8,
        signals={"function_alignment": 0.8, "seniority_relationship": 0.79, "founder_bonus": 0.35},
        rank_within_job=1, computed_at=NOW, rules_version="v1",
    )


# --- Pure logic ------------------------------------------------------------


def test_build_user_prompt_includes_job_lead_and_signals():
    prompt = build_user_prompt(job=_job(), lead=_lead(), company=_company(), match=_match(), context=None)
    assert "Senior Backend Engineer" in prompt
    assert "Jamie Chen" in prompt
    assert "function_alignment" in prompt


def test_grounding_violation_none_when_all_cited_signals_supplied():
    output = LLMScoreOutput(
        relevance_score=0.9, confidence_score=0.8, rationale="Fits well.",
        cited_signals=["function_alignment", "founder_bonus"], disagrees_with_rules=False,
    )
    assert grounding_violation(output, _match()) is None


def test_grounding_violation_detects_fabricated_citation():
    output = LLMScoreOutput(
        relevance_score=0.9, confidence_score=0.8, rationale="Fits well.",
        cited_signals=["function_alignment", "a_signal_never_supplied"], disagrees_with_rules=False,
    )
    violation = grounding_violation(output, _match())
    assert violation is not None
    assert "a_signal_never_supplied" in violation


def test_fallback_scored_lead_uses_rules_score_and_empty_rationale():
    fallback = fallback_scored_lead(_match(), _job(), _lead(), now=NOW)
    assert fallback.relevance_score == _match().match_score
    assert fallback.confidence_score == _match().match_confidence
    assert fallback.rationale == ""
    assert fallback.cited_signals == []


# --- score_pair orchestration (mocked LLM) ---------------------------------


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
    """Stands in for `client.beta.chat.completions.parse` — a plain
    callable (not a coroutine, matching the real SDK's sync client), since
    `score_pair` wraps it in `asyncio.to_thread`.
    """

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


def _valid_output(**overrides) -> LLMScoreOutput:
    base = dict(
        relevance_score=0.9, confidence_score=0.85, rationale="Founder/CTO with a direct reporting-line match.",
        cited_signals=["founder_bonus", "function_alignment"], disagrees_with_rules=False,
    )
    base.update(overrides)
    return LLMScoreOutput(**base)


async def test_score_pair_not_configured_is_scoring_failed():
    service = _FakeAzureOpenAIService([], configured=False)
    outcome = await score_pair(job=_job(), lead=_lead(), company=_company(), match=_match(), context=None, llm=service, now=NOW)
    assert outcome.status == ScoringStatus.SCORING_FAILED
    assert outcome.scored_lead is None


async def test_score_pair_success_on_first_attempt():
    service = _FakeAzureOpenAIService([_valid_output()])
    outcome = await score_pair(job=_job(), lead=_lead(), company=_company(), match=_match(), context=None, llm=service, now=NOW)
    assert outcome.status == ScoringStatus.SCORED
    assert outcome.scored_lead is not None
    assert outcome.scored_lead.relevance_score == 0.9
    assert outcome.scored_lead.match_id == "m1"
    assert service._parse.calls == 1


async def test_score_pair_retries_once_on_grounding_violation_then_succeeds():
    bad = _valid_output(cited_signals=["not_a_real_signal"])
    good = _valid_output()
    service = _FakeAzureOpenAIService([bad, good])
    outcome = await score_pair(job=_job(), lead=_lead(), company=_company(), match=_match(), context=None, llm=service, now=NOW)
    assert outcome.status == ScoringStatus.SCORED
    assert service._parse.calls == 2


async def test_score_pair_gives_up_after_max_attempts():
    bad = _valid_output(cited_signals=["not_a_real_signal"])
    service = _FakeAzureOpenAIService([bad, bad, bad])
    outcome = await score_pair(job=_job(), lead=_lead(), company=_company(), match=_match(), context=None, llm=service, now=NOW)
    assert outcome.status == ScoringStatus.SCORING_FAILED
    assert service._parse.calls == 2  # spec §13.5: retried once, not indefinitely


async def test_score_pair_retries_once_on_api_error_then_succeeds():
    error = APIError("boom", httpx.Request("POST", "https://example.com"), body=None)
    service = _FakeAzureOpenAIService([error, _valid_output()])
    outcome = await score_pair(job=_job(), lead=_lead(), company=_company(), match=_match(), context=None, llm=service, now=NOW)
    assert outcome.status == ScoringStatus.SCORED
    assert service._parse.calls == 2


async def test_score_pair_no_parsed_output_is_treated_as_failure_and_retried():
    service = _FakeAzureOpenAIService([None, _valid_output()])
    outcome = await score_pair(job=_job(), lead=_lead(), company=_company(), match=_match(), context=None, llm=service, now=NOW)
    assert outcome.status == ScoringStatus.SCORED
    assert service._parse.calls == 2
