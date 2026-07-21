"""Suppression key derivation and filtering — spec §21.6 (Phase 5).

Pure logic, no I/O — `core.compliance_store.PersonSuppressionStore` is the
persisted list this checks against; this module only computes keys and
filters, same "pure decision logic, separate from the store" convention as
`leads.discovery`/`leads.matching`.
"""

from __future__ import annotations

from gtm_agent.core.compliance_store import PersonSuppressionStore
from gtm_agent.models.compliance import PersonSuppressionEntry
from gtm_agent.models.lead import Lead


def suppression_key(*, email: str | None = None, full_name: str | None = None, company_domain: str | None = None) -> str:
    """A verified email is the durable identity to key on when available —
    stable across a lead being re-discovered under a different Apollo
    `source_person_id` later. Falling back to name+domain when there's no
    email is weaker (spec §11.3 makes the same trade-off for PDL matching)
    but is what's available for a lead with no email on file.
    """
    if email:
        return f"email:{email.strip().lower()}"
    return f"name:{(full_name or '').strip().lower()}@{(company_domain or '').strip().lower()}"


def lead_suppression_key(lead: Lead, *, company_domain: str) -> str:
    return suppression_key(email=lead.email, full_name=lead.full_name, company_domain=company_domain)


def filter_suppressed(
    leads: list[Lead], *, company_domain: str, suppression_store: PersonSuppressionStore
) -> list[Lead]:
    """Spec §21.6: "checked at stage 6, so the next Apollo sweep doesn't
    silently re-add them." Applied both to freshly-retrieved Apollo results
    (Stage 6) and to the cached lead set on read, so a suppression request
    made after a lead was already cached still takes effect immediately —
    without needing to physically rewrite the JSONL `lead` store.
    """
    return [
        lead
        for lead in leads
        if not suppression_store.is_suppressed(lead_suppression_key(lead, company_domain=company_domain))
    ]


def erase_lead(
    lead: Lead, *, company_domain: str, suppression_store: PersonSuppressionStore, reason: str | None = None
) -> PersonSuppressionEntry:
    """Spec §21.6: "Deletion without suppression is not erasure." Adds the
    lead's identity key to the suppression list; combined with
    `filter_suppressed` being applied on every read, the erased lead is
    never surfaced or re-added again, which is the compliance-relevant
    guarantee — even though the original JSONL row isn't physically
    scrubbed (same "no real database yet" limitation as every other store
    in this codebase; a real table would additionally hard-delete or
    anonymise the row itself).
    """
    key = lead_suppression_key(lead, company_domain=company_domain)
    return suppression_store.add(key, reason=reason)
