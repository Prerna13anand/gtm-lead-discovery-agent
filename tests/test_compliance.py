"""Compliance & suppression tests — spec §21.6."""

from datetime import UTC, datetime
from pathlib import Path

from gtm_agent.core.compliance_store import CompanyDenylistStore, PersonSuppressionStore
from gtm_agent.leads.compliance import erase_lead, filter_suppressed, lead_suppression_key, suppression_key
from gtm_agent.models.lead import EnrichmentStatus, LeadRecord, LeadSource

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _lead(**overrides) -> LeadRecord:
    base = dict(
        lead_id="l1", company_id="acme", source=LeadSource.APOLLO, full_name="Jane Doe",
        title_raw="VP Engineering", title_canonical="VP Engineering", retrieved_at=NOW,
        enrichment_status=EnrichmentStatus.NOT_ATTEMPTED,
    )
    base.update(overrides)
    return LeadRecord(**base)


# --- CompanyDenylistStore --------------------------------------------------


def test_company_not_denied_by_default(tmp_path: Path):
    store = CompanyDenylistStore(tmp_path / "denylist.jsonl")
    assert store.is_denied("acme.com") is False


def test_company_denylist_add_and_check(tmp_path: Path):
    store = CompanyDenylistStore(tmp_path / "denylist.jsonl")
    store.add("Acme.com", reason="requested exclusion", now=NOW)
    assert store.is_denied("acme.com") is True  # normalised, case-insensitive
    assert store.is_denied("ACME.COM") is True
    assert store.is_denied("other.com") is False


# --- suppression_key / lead_suppression_key --------------------------------


def test_suppression_key_prefers_email():
    assert suppression_key(email="Jane@Acme.com", full_name="Jane Doe") == "email:jane@acme.com"


def test_suppression_key_falls_back_to_name_and_domain():
    key = suppression_key(email=None, full_name="Jane Doe", company_domain="Acme.com")
    assert key == "name:jane doe@acme.com"


def test_lead_suppression_key_uses_lead_fields():
    lead = _lead(email="jane@acme.com")
    assert lead_suppression_key(lead, company_domain="acme.com") == "email:jane@acme.com"


# --- PersonSuppressionStore / filter_suppressed / erase_lead ---------------


def test_person_not_suppressed_by_default(tmp_path: Path):
    store = PersonSuppressionStore(tmp_path / "suppression.jsonl")
    assert store.is_suppressed("email:jane@acme.com") is False


def test_erase_lead_adds_to_suppression_list(tmp_path: Path):
    store = PersonSuppressionStore(tmp_path / "suppression.jsonl")
    lead = _lead(email="jane@acme.com")
    erase_lead(lead, company_domain="acme.com", suppression_store=store, reason="GDPR erasure request")
    assert store.is_suppressed("email:jane@acme.com") is True


def test_filter_suppressed_removes_erased_lead_from_a_batch(tmp_path: Path):
    store = PersonSuppressionStore(tmp_path / "suppression.jsonl")
    erased = _lead(lead_id="l1", email="jane@acme.com")
    kept = _lead(lead_id="l2", email="bob@acme.com", full_name="Bob Smith")
    erase_lead(erased, company_domain="acme.com", suppression_store=store)

    remaining = filter_suppressed([erased, kept], company_domain="acme.com", suppression_store=store)
    assert [lead.lead_id for lead in remaining] == ["l2"]


def test_filter_suppressed_prevents_re_adding_after_erasure_even_without_email(tmp_path: Path):
    store = PersonSuppressionStore(tmp_path / "suppression.jsonl")
    lead_no_email = _lead(lead_id="l1", email=None, full_name="Jane Doe")
    erase_lead(lead_no_email, company_domain="acme.com", suppression_store=store)

    # A "re-discovered" version of the same person (e.g. a fresh Apollo
    # sweep with a new source_person_id) with the same name+domain must
    # still be filtered.
    rediscovered = _lead(lead_id="l1-new", email=None, full_name="Jane Doe")
    remaining = filter_suppressed([rediscovered], company_domain="acme.com", suppression_store=store)
    assert remaining == []
