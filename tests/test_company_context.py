"""Stage 9 — Company Context tests (spec §12)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from gtm_agent.config.settings import Settings
from gtm_agent.core.fetch import Fetcher
from gtm_agent.leads.budget import BudgetMeter, CreditBudget
from gtm_agent.leads.company_context import is_context_stale, run_stage9, summarize_context
from gtm_agent.models.company_context import CompanyContextStatus
from gtm_agent.services.tavily import TavilyClient


def _tavily_client(monkeypatch: pytest.MonkeyPatch, api_key: str = "tvly-key") -> TavilyClient:
    monkeypatch.setattr("gtm_agent.services.tavily.get_settings", lambda: Settings(tavily_api_key=api_key))
    return TavilyClient()


# --- Pure logic --------------------------------------------------------


def test_is_context_stale_none_is_stale():
    assert is_context_stale(None, now=datetime.now(UTC)) is True


def test_is_context_stale_within_ttl_is_fresh():
    now = datetime.now(UTC)
    assert is_context_stale(now - timedelta(days=3), now=now) is False


def test_is_context_stale_past_ttl_is_stale():
    now = datetime.now(UTC)
    assert is_context_stale(now - timedelta(days=8), now=now) is True


def test_summarize_context_prefers_funding_keyword_snippet():
    raw = {
        "funding_results": [
            {"url": "https://news.example/1", "content": "Acme launches new logo."},
            {"url": "https://news.example/2", "content": "Acme raised a $10M Series A round."},
        ],
        "hiring_results": [],
        "careers_results": [],
    }
    summary, funding_signal, hiring_signal, sources = summarize_context(raw)
    assert funding_signal == "Acme raised a $10M Series A round."
    assert hiring_signal is None
    assert "https://news.example/1" in sources


def test_summarize_context_empty_results_has_no_signals():
    summary, funding_signal, hiring_signal, sources = summarize_context(
        {"funding_results": [], "hiring_results": [], "careers_results": []}
    )
    assert funding_signal is None
    assert hiring_signal is None
    assert sources == []
    assert summary == "No public context found."


# --- Orchestration (spec §12.4: non-blocking failure) -------------------


async def test_run_stage9_success_returns_context_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _tavily_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"results": [{"url": "https://x", "content": "raised Series A"}]}')

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    budget = CreditBudget()
    try:
        status, context = await run_stage9(
            company_id="acme",
            company_domain="acme.com",
            company_name="Acme",
            fetcher=fetcher,
            budget=budget,
            tavily_client=client,
        )
    finally:
        await fetcher.aclose()

    assert status == CompanyContextStatus.CONTEXT_OK
    assert context is not None
    assert context.company_id == "acme"
    assert budget.used(BudgetMeter.TAVILY_CALLS) == 1


async def test_run_stage9_not_configured_is_non_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _tavily_client(monkeypatch, api_key="")  # not configured

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"results": []}')

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    budget = CreditBudget()
    try:
        status, context = await run_stage9(
            company_id="acme",
            company_domain="acme.com",
            company_name="Acme",
            fetcher=fetcher,
            budget=budget,
            tavily_client=client,
        )
    finally:
        await fetcher.aclose()

    assert status == CompanyContextStatus.CONTEXT_UNAVAILABLE
    assert context is None


async def test_run_stage9_budget_exhausted_is_non_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _tavily_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"results": []}')

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    budget = CreditBudget(ceilings={BudgetMeter.TAVILY_CALLS: 0})
    try:
        status, context = await run_stage9(
            company_id="acme",
            company_domain="acme.com",
            company_name="Acme",
            fetcher=fetcher,
            budget=budget,
            tavily_client=client,
        )
    finally:
        await fetcher.aclose()

    assert status == CompanyContextStatus.CONTEXT_UNAVAILABLE
    assert context is None
