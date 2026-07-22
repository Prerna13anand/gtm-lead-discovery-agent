"""Apollo reveal tests — a real API behavior discovered live (see
services.apollo and leads.apollo_reveal module docstrings), not spec-named
as its own stage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from gtm_agent.config.settings import Settings
from gtm_agent.core.fetch import Fetcher
from gtm_agent.leads.apollo_reveal import apply_reveal, reveal_lead, run_apollo_reveal
from gtm_agent.leads.budget import BudgetMeter, CreditBudget
from gtm_agent.models.lead import EmailStatus, EnrichmentStatus, LeadRecord, LeadSource
from gtm_agent.services.apollo import ApolloClient

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _lead(**overrides) -> LeadRecord:
    base = dict(
        lead_id="p1", company_id="acme", source=LeadSource.APOLLO, source_person_id="p1",
        full_name="Tuomas Ar***n", title_raw="Co-Founder", title_canonical="Co-Founder",
        is_founder=True, retrieved_at=NOW, enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )
    base.update(overrides)
    return LeadRecord(**base)


_REVEALED_PERSON = {
    "name": "Tuomas Artman",
    "email": "tuomas@linear.app",
    "email_status": "verified",
    "linkedin_url": "http://www.linkedin.com/in/tuomasartman",
    "city": "Valencia",
    "state": "Valencian Community",
    "country": "Spain",
}


# --- apply_reveal (pure) ----------------------------------------------------


def test_apply_reveal_replaces_obfuscated_name_and_fills_gaps():
    lead = _lead(email=None, linkedin_url=None, location_raw=None)
    updated = apply_reveal(lead, _REVEALED_PERSON, now=NOW)
    assert updated.full_name == "Tuomas Artman"
    assert updated.email == "tuomas@linear.app"
    assert updated.email_status == EmailStatus.VERIFIED
    assert updated.linkedin_url == "http://www.linkedin.com/in/tuomasartman"
    assert updated.location_raw == "Valencia, Valencian Community, Spain"


def test_apply_reveal_never_overwrites_existing_contact_fields():
    lead = _lead(email="already@known.com", email_status=EmailStatus.VERIFIED)
    updated = apply_reveal(lead, _REVEALED_PERSON, now=NOW)
    assert updated.email == "already@known.com"  # kept, per §11.2's waterfall


def test_apply_reveal_always_replaces_the_obfuscated_name():
    """Unlike contact fields, `full_name` is never "good data" pre-reveal —
    it's always the masked search-step name.
    """
    lead = _lead(full_name="Tuomas Ar***n")
    updated = apply_reveal(lead, _REVEALED_PERSON, now=NOW)
    assert updated.full_name == "Tuomas Artman"


def test_apply_reveal_no_changes_returns_same_object_when_nothing_new():
    lead = _lead(email="x@y.com", linkedin_url="https://linkedin.com/in/x", location_raw="Somewhere")
    person_without_extras = {"name": lead.full_name}
    updated = apply_reveal(lead, person_without_extras, now=NOW)
    assert updated.email == "x@y.com"
    assert updated.linkedin_url == "https://linkedin.com/in/x"


# --- reveal_lead / run_apollo_reveal (I/O) ----------------------------------


def _apollo_client(monkeypatch: pytest.MonkeyPatch, api_key: str = "key") -> ApolloClient:
    monkeypatch.setattr("gtm_agent.services.apollo.get_settings", lambda: Settings(apollo_api_key=api_key))
    return ApolloClient()


async def test_reveal_lead_skips_non_apollo_source():
    lead = _lead(source=LeadSource.PDL, source_person_id=None)
    fetcher = Fetcher(respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await reveal_lead(
            lead, fetcher=fetcher, apollo_client=ApolloClient(), budget=CreditBudget(), now=NOW
        )
    finally:
        await fetcher.aclose()
    assert result is lead


async def test_reveal_lead_skips_when_no_source_person_id():
    lead = _lead(source_person_id=None)
    fetcher = Fetcher(respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await reveal_lead(
            lead, fetcher=fetcher, apollo_client=ApolloClient(), budget=CreditBudget(), now=NOW
        )
    finally:
        await fetcher.aclose()
    assert result is lead


async def test_reveal_lead_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=json.dumps({"person": _REVEALED_PERSON}))

    lead = _lead()
    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    budget = CreditBudget()
    try:
        result = await reveal_lead(lead, fetcher=fetcher, apollo_client=client, budget=budget, now=NOW)
    finally:
        await fetcher.aclose()

    assert result.full_name == "Tuomas Artman"
    assert result.email == "tuomas@linear.app"
    assert seen["body"]["id"] == "p1"
    assert budget.used(BudgetMeter.APOLLO_CREDITS) == 1


async def test_reveal_lead_404_returns_lead_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    lead = _lead()
    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await reveal_lead(lead, fetcher=fetcher, apollo_client=client, budget=CreditBudget(), now=NOW)
    finally:
        await fetcher.aclose()
    assert result is lead


async def test_reveal_lead_budget_exhausted_skips_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"person": _REVEALED_PERSON}))

    lead = _lead()
    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    budget = CreditBudget(ceilings={BudgetMeter.APOLLO_CREDITS: 0})
    try:
        result = await reveal_lead(lead, fetcher=fetcher, apollo_client=client, budget=budget, now=NOW)
    finally:
        await fetcher.aclose()
    assert calls["n"] == 0
    assert result is lead


async def test_reveal_lead_error_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    lead = _lead()
    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await reveal_lead(lead, fetcher=fetcher, apollo_client=client, budget=CreditBudget(), now=NOW)
    finally:
        await fetcher.aclose()
    assert result is lead


async def test_run_apollo_reveal_only_reveals_matched_leads(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _apollo_client(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"person": _REVEALED_PERSON}))

    matched = _lead(lead_id="matched", source_person_id="matched")
    unmatched = _lead(lead_id="unmatched", source_person_id="unmatched")
    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await run_apollo_reveal(
            leads=[matched, unmatched], matched_lead_ids={"matched"},
            fetcher=fetcher, budget=CreditBudget(), apollo_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()

    assert calls["n"] == 1
    result_by_id = {lead.lead_id: lead for lead in result}
    assert result_by_id["matched"].full_name == "Tuomas Artman"
    assert result_by_id["unmatched"].full_name == "Tuomas Ar***n"  # untouched
