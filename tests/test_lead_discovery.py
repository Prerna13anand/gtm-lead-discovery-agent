"""Stage 6 — Lead Discovery tests (spec §9)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from gtm_agent.config.settings import Settings
from gtm_agent.core.compliance_store import PersonSuppressionStore
from gtm_agent.core.fetch import Fetcher
from gtm_agent.leads.budget import BudgetMeter, CreditBudget
from gtm_agent.leads.compliance import erase_lead
from gtm_agent.leads.discovery import (
    is_stale,
    needs_refresh,
    person_to_lead,
    personas_uncovered,
    run_stage6,
)
from gtm_agent.models.common import JobFunction
from gtm_agent.models.company import Company
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EnrichmentStatus, LeadDiscoveryStatus, LeadRecord, LeadSource
from gtm_agent.services.apollo import ApolloClient

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job(function: JobFunction | None) -> JobPosting:
    return JobPosting(
        job_id="j1", company_id="acme", source_platform="greenhouse", posting_url="https://acme.com/jobs/1",
        title_raw="Role", title_canonical="Role", description_text="", description_markdown="",
        function=function, first_seen_at=NOW, last_seen_at=NOW,
    )


def _cached_lead(function: JobFunction | None) -> LeadRecord:
    return LeadRecord(
        lead_id="l1", company_id="acme", source=LeadSource.APOLLO, full_name="X",
        title_raw="X", title_canonical="X", function=function, retrieved_at=NOW,
        enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )


# --- personas_uncovered / is_stale / needs_refresh (pure) ---------------


def test_personas_uncovered_true_when_new_function_not_in_cache():
    cached = [_cached_lead(JobFunction.ENGINEERING)]
    jobs = [_job(JobFunction.DESIGN)]
    assert personas_uncovered(cached, jobs) is True


def test_personas_uncovered_false_when_function_already_covered():
    cached = [_cached_lead(JobFunction.ENGINEERING)]
    jobs = [_job(JobFunction.ENGINEERING)]
    assert personas_uncovered(cached, jobs) is False


def test_personas_uncovered_false_for_jobs_with_no_function():
    cached = [_cached_lead(JobFunction.ENGINEERING)]
    jobs = [_job(None)]
    assert personas_uncovered(cached, jobs) is False


def test_is_stale_none_is_stale():
    assert is_stale(None, now=NOW) is True


def test_is_stale_within_30_days_is_fresh():
    assert is_stale(NOW - timedelta(days=10), now=NOW) is False


def test_is_stale_past_30_days_is_stale():
    assert is_stale(NOW - timedelta(days=31), now=NOW) is True


def test_needs_refresh_none_cached_is_true():
    assert needs_refresh(cached_leads=None, cache_retrieved_at=None, open_jobs=[], now=NOW) is True


def test_needs_refresh_force_refresh_overrides_everything():
    cached = [_cached_lead(JobFunction.ENGINEERING)]
    assert (
        needs_refresh(
            cached_leads=cached, cache_retrieved_at=NOW, open_jobs=[_job(JobFunction.ENGINEERING)],
            now=NOW, force_refresh=True,
        )
        is True
    )


def test_needs_refresh_fresh_and_covered_is_false():
    cached = [_cached_lead(JobFunction.ENGINEERING)]
    assert (
        needs_refresh(
            cached_leads=cached, cache_retrieved_at=NOW, open_jobs=[_job(JobFunction.ENGINEERING)], now=NOW
        )
        is False
    )


# --- person_to_lead mapping ------------------------------------------------


def test_person_to_lead_maps_core_fields():
    person = {
        "id": "p1", "name": "Jane Doe", "title": "VP Engineering",
        "linkedin_url": "https://linkedin.com/in/jane", "email": "jane@acme.com",
        "email_status": "verified", "city": "SF", "state": "CA", "country": "US",
    }
    lead = person_to_lead(person, company_id="acme", retrieved_at=NOW)
    assert lead is not None
    assert lead.full_name == "Jane Doe"
    assert lead.function == JobFunction.ENGINEERING
    assert lead.location_raw == "SF, CA, US"
    assert lead.lead_id == "p1"


def test_person_to_lead_missing_name_or_title_returns_none():
    assert person_to_lead({"name": "Jane"}, company_id="acme", retrieved_at=NOW) is None
    assert person_to_lead({"title": "CEO"}, company_id="acme", retrieved_at=NOW) is None


def test_person_to_lead_founder_and_recruiter_flags():
    founder = person_to_lead({"name": "A", "title": "Co-Founder"}, company_id="acme", retrieved_at=NOW)
    recruiter = person_to_lead({"name": "B", "title": "Technical Recruiter"}, company_id="acme", retrieved_at=NOW)
    assert founder.is_founder is True
    assert recruiter.is_recruiter is True


# --- run_stage6 orchestration ----------------------------------------------


def _apollo_client(monkeypatch: pytest.MonkeyPatch, api_key: str = "key") -> ApolloClient:
    monkeypatch.setattr("gtm_agent.services.apollo.get_settings", lambda: Settings(apollo_api_key=api_key))
    return ApolloClient()


def _company() -> Company:
    return Company(id="acme", name="Acme", domain="acme.com", added_at=NOW)


async def test_run_stage6_success_maps_leads(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"people": [{"id": "p1", "name": "Jane", "title": "CEO"}]}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    budget = CreditBudget()
    try:
        outcome = await run_stage6(
            company=_company(), open_jobs=[_job(JobFunction.ENGINEERING)], fetcher=fetcher,
            budget=budget, apollo_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()

    assert outcome.status == LeadDiscoveryStatus.LEADS_OK
    assert len(outcome.leads) == 1
    assert budget.used(BudgetMeter.APOLLO_CREDITS) == 1


async def test_run_stage6_empty_result_is_no_leads_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"people": []}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        outcome = await run_stage6(
            company=_company(), open_jobs=[_job(JobFunction.ENGINEERING)], fetcher=fetcher,
            budget=CreditBudget(), apollo_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()
    assert outcome.status == LeadDiscoveryStatus.NO_LEADS_FOUND
    assert outcome.leads == []


async def test_run_stage6_apollo_error_is_lead_discovery_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        outcome = await run_stage6(
            company=_company(), open_jobs=[_job(JobFunction.ENGINEERING)], fetcher=fetcher,
            budget=CreditBudget(), apollo_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()
    assert outcome.status == LeadDiscoveryStatus.LEAD_DISCOVERY_FAILED


async def test_run_stage6_not_configured_is_lead_discovery_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch, api_key="")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"people": []}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        outcome = await run_stage6(
            company=_company(), open_jobs=[_job(JobFunction.ENGINEERING)], fetcher=fetcher,
            budget=CreditBudget(), apollo_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()
    assert outcome.status == LeadDiscoveryStatus.LEAD_DISCOVERY_FAILED


async def test_run_stage6_exceeding_cap_via_total_entries_is_identity_suspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _apollo_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "people": [{"id": f"p{i}", "name": f"P{i}", "title": "Engineer"} for i in range(25)],
            "pagination": {"total_entries": 500},
        }
        return httpx.Response(200, text=json.dumps(payload))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        outcome = await run_stage6(
            company=_company(), open_jobs=[_job(JobFunction.ENGINEERING)], fetcher=fetcher,
            budget=CreditBudget(), apollo_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()
    assert outcome.status == LeadDiscoveryStatus.COMPANY_IDENTITY_SUSPECT
    assert outcome.leads == []


async def test_run_stage6_filters_out_suppressed_leads(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    client = _apollo_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "people": [
                        {"id": "p1", "name": "Jane Doe", "title": "CEO", "email": "jane@acme.com"},
                        {"id": "p2", "name": "Bob Smith", "title": "CTO", "email": "bob@acme.com"},
                    ]
                }
            ),
        )

    suppression_store = PersonSuppressionStore(tmp_path / "suppression.jsonl")
    erase_lead(
        person_to_lead({"name": "Jane Doe", "title": "CEO", "email": "jane@acme.com"}, company_id="acme", retrieved_at=NOW),
        company_domain="acme.com",
        suppression_store=suppression_store,
    )

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        outcome = await run_stage6(
            company=_company(), open_jobs=[_job(JobFunction.ENGINEERING)], fetcher=fetcher,
            budget=CreditBudget(), apollo_client=client, now=NOW, suppression_store=suppression_store,
        )
    finally:
        await fetcher.aclose()

    assert outcome.status == LeadDiscoveryStatus.LEADS_OK
    assert [lead.full_name for lead in outcome.leads] == ["Bob Smith"]


async def test_run_stage6_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"people": []}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    budget = CreditBudget(ceilings={BudgetMeter.APOLLO_CREDITS: 0})
    try:
        outcome = await run_stage6(
            company=_company(), open_jobs=[_job(JobFunction.ENGINEERING)], fetcher=fetcher,
            budget=budget, apollo_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()
    assert outcome.status == LeadDiscoveryStatus.BUDGET_EXHAUSTED
    assert calls["n"] == 0  # never even attempted the call
