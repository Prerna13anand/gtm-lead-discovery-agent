"""Stage 8 — Enrichment tests (spec §11)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from gtm_agent.config.settings import Settings
from gtm_agent.core.fetch import Fetcher
from gtm_agent.leads.budget import BudgetMeter, CreditBudget
from gtm_agent.leads.enrichment import (
    apply_enrichment,
    has_corroboration,
    is_enrichment_stale,
    needs_enrichment,
    run_stage8,
    should_attempt_enrichment,
)
from gtm_agent.models.company import Company
from gtm_agent.models.lead import EmailStatus, EnrichmentStatus, LeadRecord, LeadSource
from gtm_agent.services.pdl import PDLClient

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _lead(**overrides) -> LeadRecord:
    base = dict(
        lead_id="l1", company_id="acme", source=LeadSource.APOLLO, full_name="Jane Doe",
        title_raw="VP Engineering", title_canonical="VP Engineering", retrieved_at=NOW,
        enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )
    base.update(overrides)
    return LeadRecord(**base)


# --- needs_enrichment / staleness / should_attempt (pure) -----------------


def test_needs_enrichment_true_when_incomplete():
    lead = _lead(email_status=None, phone=None)
    assert needs_enrichment(lead) is True


def test_needs_enrichment_false_when_complete():
    lead = _lead(email_status=EmailStatus.VERIFIED, phone="+1-555-0100", email="jane@acme.com")
    assert needs_enrichment(lead) is False


def test_is_enrichment_stale_none_is_stale():
    assert is_enrichment_stale(None, now=NOW) is True


def test_is_enrichment_stale_within_90_days_is_fresh():
    assert is_enrichment_stale(NOW - timedelta(days=30), now=NOW) is False


def test_is_enrichment_stale_past_90_days_is_stale():
    assert is_enrichment_stale(NOW - timedelta(days=91), now=NOW) is True


def test_should_attempt_enrichment_skips_complete_and_fresh():
    lead = _lead(
        email_status=EmailStatus.VERIFIED, phone="+1-555-0100", email="jane@acme.com", enriched_at=NOW
    )
    assert should_attempt_enrichment(lead, now=NOW) is False


def test_should_attempt_enrichment_re_enriches_complete_but_stale():
    lead = _lead(
        email_status=EmailStatus.VERIFIED, phone="+1-555-0100", email="jane@acme.com",
        enriched_at=NOW - timedelta(days=100),
    )
    assert should_attempt_enrichment(lead, now=NOW) is True


# --- has_corroboration / apply_enrichment (pure) --------------------------


def test_has_corroboration_matches_on_title():
    lead = _lead(title_raw="VP Engineering")
    assert has_corroboration(lead, {"job_title": "VP of Engineering"}) is True


def test_has_corroboration_matches_on_location():
    lead = _lead(location_raw="San Francisco")
    assert has_corroboration(lead, {"location_name": "San Francisco, CA"}) is True


def test_has_corroboration_false_when_nothing_matches():
    lead = _lead(title_raw="VP Engineering", location_raw="San Francisco")
    assert has_corroboration(lead, {"job_title": "Sales Rep", "location_name": "London"}) is False


def test_apply_enrichment_fills_missing_fields():
    lead = _lead(email=None, email_status=None, phone=None, linkedin_url=None)
    pdl_record = {
        "work_email": "jane@acme.com", "work_email_status": "verified",
        "mobile_phone": "+1-555-0199", "linkedin_url": "https://linkedin.com/in/jane",
    }
    outcome = apply_enrichment(lead, pdl_record, now=NOW)
    assert outcome.lead.email == "jane@acme.com"
    assert outcome.lead.email_status == EmailStatus.VERIFIED
    assert outcome.lead.phone == "+1-555-0199"
    assert outcome.lead.enrichment_status == EnrichmentStatus.ENRICHED
    assert outcome.lead.enriched_at == NOW


def test_apply_enrichment_never_overwrites_verified_apollo_email():
    lead = _lead(email="jane@apollo.com", email_status=EmailStatus.VERIFIED)
    pdl_record = {"work_email": "jane@pdl-guess.com"}
    outcome = apply_enrichment(lead, pdl_record, now=NOW)
    assert outcome.lead.email == "jane@apollo.com"  # kept, per §11.2's waterfall


def test_apply_enrichment_detects_title_conflict_as_job_change_signal():
    lead = _lead(title_raw="VP Engineering")
    pdl_record = {"job_title": "Independent Consultant"}
    outcome = apply_enrichment(lead, pdl_record, now=NOW)
    assert outcome.job_change_signal is True
    assert outcome.conflicts["title"] == {"apollo": "VP Engineering", "pdl": "Independent Consultant"}


# --- run_stage8 orchestration ----------------------------------------------


def _pdl_client(monkeypatch: pytest.MonkeyPatch, api_key: str = "key") -> PDLClient:
    monkeypatch.setattr("gtm_agent.services.pdl.get_settings", lambda: Settings(pdl_api_key=api_key))
    return PDLClient()


def _company() -> Company:
    return Company(id="acme", name="Acme", domain="acme.com", added_at=NOW)


async def test_run_stage8_only_enriches_matched_leads(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _pdl_client(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"data": {"job_title": "VP Engineering"}}))

    matched = _lead(lead_id="matched", email=None, email_status=None, phone=None, linkedin_url="https://linkedin.com/in/x")
    unmatched = _lead(lead_id="unmatched", email=None, email_status=None, phone=None)

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        result = await run_stage8(
            leads=[matched, unmatched], matched_lead_ids={"matched"}, company=_company(),
            fetcher=fetcher, budget=CreditBudget(), pdl_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()

    assert calls["n"] == 1  # only the matched lead triggered a PDL call
    result_by_id = {lead.lead_id: lead for lead in result}
    assert result_by_id["unmatched"].enrichment_status == EnrichmentStatus.NOT_ATTEMPTED
    assert result_by_id["matched"].enrichment_status == EnrichmentStatus.ENRICHED


async def test_run_stage8_skips_already_complete_leads(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _pdl_client(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"data": {}}))

    complete_lead = _lead(
        lead_id="complete", email="jane@acme.com", email_status=EmailStatus.VERIFIED, phone="+1-555-0100"
    )
    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        result = await run_stage8(
            leads=[complete_lead], matched_lead_ids={"complete"}, company=_company(),
            fetcher=fetcher, budget=CreditBudget(), pdl_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()
    assert calls["n"] == 0
    assert result[0].enrichment_status == EnrichmentStatus.NOT_ATTEMPTED


async def test_run_stage8_weak_identity_match_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _pdl_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        # A name+domain-only lookup returns a record with no corroborating field.
        return httpx.Response(200, text=json.dumps({"data": {"job_title": "Totally Different Role", "location_name": "Nowhere"}}))

    weak_lead = _lead(
        lead_id="weak", email=None, email_status=None, phone=None, linkedin_url=None,
        title_raw="VP Engineering", location_raw="San Francisco",
    )
    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        result = await run_stage8(
            leads=[weak_lead], matched_lead_ids={"weak"}, company=_company(),
            fetcher=fetcher, budget=CreditBudget(), pdl_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()
    assert result[0].enrichment_status == EnrichmentStatus.ENRICHMENT_IDENTITY_WEAK
    assert result[0].email is None  # PDL data discarded, not merged


async def test_run_stage8_budget_exhausted_marks_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _pdl_client(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"data": {}}))

    lead = _lead(lead_id="l1", email=None, email_status=None, phone=None)
    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    budget = CreditBudget(ceilings={BudgetMeter.PDL_CREDITS: 0})
    try:
        result = await run_stage8(
            leads=[lead], matched_lead_ids={"l1"}, company=_company(),
            fetcher=fetcher, budget=budget, pdl_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()
    assert calls["n"] == 0
    assert result[0].enrichment_status == EnrichmentStatus.ENRICHMENT_SKIPPED


async def test_run_stage8_pdl_miss_marks_skipped_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _pdl_client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    lead = _lead(lead_id="l1", email=None, email_status=None, phone=None, linkedin_url="https://linkedin.com/in/x")
    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        result = await run_stage8(
            leads=[lead], matched_lead_ids={"l1"}, company=_company(),
            fetcher=fetcher, budget=CreditBudget(), pdl_client=client, now=NOW,
        )
    finally:
        await fetcher.aclose()
    assert len(result) == 1  # never dropped
    assert result[0].enrichment_status == EnrichmentStatus.ENRICHMENT_SKIPPED
