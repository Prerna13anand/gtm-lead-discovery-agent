"""Stage 4 — LLM residue classification tests (spec §7.3).

The LLM call is mocked (same fake-service pattern as
test_scoring_rationale.py) so these are deterministic and offline. A real,
live call against Azure OpenAI was exercised manually outside the test
suite — see the project report for that result.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gtm_agent.core.title_classification_cache import TitleClassificationCache
from gtm_agent.discovery.llm_residue import ResidueClassification, classify_title_residue, resolve_unclassified
from gtm_agent.models.common import JobFunction, Seniority
from gtm_agent.models.job import JobPosting

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job(job_id: str, title: str, *, function=None, seniority=None) -> JobPosting:
    return JobPosting(
        job_id=job_id, company_id="acme", source_platform="greenhouse", posting_url=f"https://acme.com/{job_id}",
        title_raw=title, title_canonical=title, description_text="", description_markdown="",
        function=function, seniority=seniority, first_seen_at=NOW, last_seen_at=NOW,
    )


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


def _output(**overrides) -> ResidueClassification:
    base = dict(function=JobFunction.MARKETING, seniority=Seniority.MID)
    base.update(overrides)
    return ResidueClassification(**base)


# --- classify_title_residue -------------------------------------------------


async def test_not_configured_returns_none_without_calling():
    service = _FakeAzureOpenAIService([], configured=False)
    result = await classify_title_residue("Growth Ninja", None, llm=service)
    assert result is None


async def test_success_on_first_attempt():
    service = _FakeAzureOpenAIService([_output()])
    result = await classify_title_residue("Growth Ninja", None, llm=service)
    assert result is not None
    assert result.function == JobFunction.MARKETING
    assert service._parse.calls == 1


async def test_no_parsed_output_retries_once_then_succeeds():
    service = _FakeAzureOpenAIService([None, _output()])
    result = await classify_title_residue("Growth Ninja", None, llm=service)
    assert result is not None
    assert service._parse.calls == 2


async def test_gives_up_after_max_attempts():
    service = _FakeAzureOpenAIService([None, None, None])
    result = await classify_title_residue("Growth Ninja", None, llm=service)
    assert result is None
    assert service._parse.calls == 2


# --- resolve_unclassified ----------------------------------------------------


async def test_resolve_unclassified_skips_fully_classified_jobs(tmp_path: Path):
    cache = TitleClassificationCache(tmp_path / "cache.jsonl")
    service = _FakeAzureOpenAIService([_output()])
    job = _job("j1", "Senior Backend Engineer", function=JobFunction.ENGINEERING, seniority=Seniority.SENIOR)

    resolved = await resolve_unclassified([job], cache=cache, llm=service)

    assert resolved[0] is job  # untouched
    assert service._parse.calls == 0


async def test_resolve_unclassified_fills_in_missing_fields(tmp_path: Path):
    cache = TitleClassificationCache(tmp_path / "cache.jsonl")
    service = _FakeAzureOpenAIService([_output(function=JobFunction.MARKETING, seniority=Seniority.MID)])
    job = _job("j1", "Growth Ninja")

    resolved = await resolve_unclassified([job], cache=cache, llm=service)

    assert resolved[0].function == JobFunction.MARKETING
    assert resolved[0].seniority == Seniority.MID
    assert resolved[0].field_provenance["function"].source == "llm_residue_classifier"


async def test_resolve_unclassified_caches_by_title_across_jobs(tmp_path: Path):
    cache = TitleClassificationCache(tmp_path / "cache.jsonl")
    service = _FakeAzureOpenAIService([_output()])
    job_a = _job("a", "Growth Ninja")
    job_b = _job("b", "Growth Ninja")  # same title, different job/company

    resolved = await resolve_unclassified([job_a, job_b], cache=cache, llm=service)

    assert service._parse.calls == 1  # classified once, reused for the second
    assert resolved[1].function == JobFunction.MARKETING


async def test_resolve_unclassified_reuses_persisted_cache_across_calls(tmp_path: Path):
    cache = TitleClassificationCache(tmp_path / "cache.jsonl")
    service_first = _FakeAzureOpenAIService([_output()])
    await resolve_unclassified([_job("a", "Growth Ninja")], cache=cache, llm=service_first)

    # A brand-new cache instance pointed at the same file, and a service
    # that would fail if called — proves the second run reads from disk.
    cache_second = TitleClassificationCache(tmp_path / "cache.jsonl")
    service_second = _FakeAzureOpenAIService([])
    resolved = await resolve_unclassified([_job("b", "Growth Ninja")], cache=cache_second, llm=service_second)

    assert resolved[0].function == JobFunction.MARKETING
    assert service_second._parse.calls == 0


async def test_resolve_unclassified_stays_none_when_llm_fails(tmp_path: Path):
    cache = TitleClassificationCache(tmp_path / "cache.jsonl")
    service = _FakeAzureOpenAIService([None, None])
    job = _job("j1", "Growth Ninja")

    resolved = await resolve_unclassified([job], cache=cache, llm=service)

    assert resolved[0].function is None
    assert resolved[0].seniority is None
