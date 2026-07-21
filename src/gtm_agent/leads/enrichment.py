"""Stage 8 — Enrichment (spec §11).

**Goal:** fill gaps in matched leads' profiles using PDL, and validate what
Apollo returned. Same pure/I/O split as Stage 6: decision logic
(`needs_enrichment`, `is_enrichment_stale`, `has_corroboration`,
`apply_enrichment`) is synchronous and network-free; `run_stage8` is the
thin async orchestrator that calls `PDLClient` and spends the shared
`CreditBudget`.

PDL's exact response field names (`work_email`, `job_title`,
`job_company_website`, `mobile_phone`, `location_name`, ...) are this
codebase's best-effort guess at PDL's public Person Enrichment schema,
following the same "not exercised against the live API" caveat as
`services.pdl` itself — verify against current PDL docs before production use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gtm_agent.core.fetch import Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.leads.budget import BudgetMeter, CreditBudget
from gtm_agent.models.company import Company
from gtm_agent.models.lead import EmailStatus, EnrichmentStatus, LeadRecord
from gtm_agent.services.pdl import PDLClient, PDLEnrichError, PDLNotConfiguredError

logger = get_logger(__name__)

# Spec §11.4: "cached per lead with a 90-day TTL."
_ENRICHMENT_STALENESS_DAYS = 90


def needs_enrichment(lead: LeadRecord) -> bool:
    """Spec §11.1: "skip enrichment when Apollo's record is already
    complete — a verified email, phone, and current title need no
    supplementation."
    """
    complete = lead.email_status == EmailStatus.VERIFIED and bool(lead.phone) and bool(lead.title_raw)
    return not complete


def is_enrichment_stale(enriched_at: datetime | None, *, now: datetime) -> bool:
    """Spec §11.4. Never-enriched (`None`) counts as stale — there's nothing
    to be stale relative to, but it's equally not "already fresh."
    """
    if enriched_at is None:
        return True
    return (now - enriched_at).days >= _ENRICHMENT_STALENESS_DAYS


def should_attempt_enrichment(lead: LeadRecord, *, now: datetime) -> bool:
    """Combines §11.1 (skip if complete) and §11.4 (re-enrich once stale).

    A record that is complete *and has never been PDL-enriched*
    (`enriched_at is None`) is skipped outright — spec §11.1's completeness
    check is meant to avoid ever calling PDL for such a lead in the first
    place, not just to avoid a *second* call. Staleness (§11.4) only forces
    a re-check once a lead has actually been enriched before; it never
    manufactures a first PDL call for an already-complete Apollo record.
    """
    if needs_enrichment(lead):
        return True
    return lead.enriched_at is not None and is_enrichment_stale(lead.enriched_at, now=now)


_STOPWORDS = frozenset({"of", "the", "and", "a", "an"})

# Generic seniority/role words excluded specifically from the *title*
# corroboration check — "VP Engineering" and "VP Sales" sharing "vp" is not
# corroboration, it's two different roles that happen to be leadership
# titles. Not excluded from location matching, where no such collision risk
# exists.
_GENERIC_TITLE_WORDS = frozenset(
    {"vp", "head", "chief", "director", "manager", "lead", "president", "officer", "of"}
)


def _significant_tokens(text: str, *, exclude: frozenset[str] = frozenset()) -> set[str]:
    words = {word for word in re.findall(r"[a-z]+", text.lower()) if len(word) > 1}
    return words - _STOPWORDS - exclude


def has_corroboration(lead: LeadRecord, pdl_record: dict[str, Any]) -> bool:
    """Spec §11.3: "require a corroborating field (title or location) before
    accepting" a match that rested on name + company domain alone — PDL's
    weakest key. Compares by significant-word overlap rather than raw
    substring containment, since real titles/locations rarely match
    verbatim ("VP Engineering" vs. "VP of Engineering", "San Francisco" vs.
    "San Francisco, CA") but do share their meaningful words.
    """
    pdl_title = pdl_record.get("job_title")
    if isinstance(pdl_title, str) and pdl_title:
        title_overlap = _significant_tokens(pdl_title, exclude=_GENERIC_TITLE_WORDS) & _significant_tokens(
            lead.title_raw, exclude=_GENERIC_TITLE_WORDS
        )
        if title_overlap:
            return True

    pdl_location = pdl_record.get("location_name")
    if isinstance(pdl_location, str) and pdl_location and lead.location_raw:
        if _significant_tokens(pdl_location) & _significant_tokens(lead.location_raw):
            return True

    return False


@dataclass
class EnrichmentOutcome:
    lead: LeadRecord
    conflicts: dict[str, dict[str, str]] = field(default_factory=dict)
    job_change_signal: bool = False
    """Spec §11.2: "Where Apollo and PDL disagree on current employer or
    title, that is a job-change signal... it may be the most actionable
    thing enrichment surfaces." Recorded, not resolved — a caller (later
    phase) decides what to do with a positive signal; this module's job
    stops at detecting and preserving it in `conflicts`.
    """


# Best-effort field-name mapping onto this codebase's `Lead` schema.
# `email_status` is inferred from whether PDL reports the email as its own
# verified field; PDL's exact status vocabulary is unverified (see module
# docstring), so anything not explicitly "verified" is conservatively
# treated as `GUESSED`, never silently upgraded to `VERIFIED`.
def apply_enrichment(lead: LeadRecord, pdl_record: dict[str, Any], *, now: datetime) -> EnrichmentOutcome:
    """Spec §11.2's field-level trust waterfall, applied field by field."""
    updates: dict[str, Any] = {}
    conflicts: dict[str, dict[str, str]] = {}

    if not (lead.email_status == EmailStatus.VERIFIED and lead.email):
        pdl_email = pdl_record.get("work_email")
        if isinstance(pdl_email, str) and pdl_email:
            updates["email"] = pdl_email
            updates["email_status"] = (
                EmailStatus.VERIFIED if pdl_record.get("work_email_status") == "verified" else EmailStatus.GUESSED
            )
        elif lead.email:
            updates.setdefault("email_status", lead.email_status or EmailStatus.GUESSED)

    if not lead.phone:
        pdl_phone = pdl_record.get("mobile_phone")
        if isinstance(pdl_phone, str) and pdl_phone:
            updates["phone"] = pdl_phone

    if not lead.linkedin_url:
        pdl_linkedin = pdl_record.get("linkedin_url")
        if isinstance(pdl_linkedin, str) and pdl_linkedin:
            updates["linkedin_url"] = pdl_linkedin

    if not lead.location_raw:
        pdl_location = pdl_record.get("location_name")
        if isinstance(pdl_location, str) and pdl_location:
            updates["location_raw"] = pdl_location

    job_change_signal = False
    pdl_title = pdl_record.get("job_title")
    if isinstance(pdl_title, str) and pdl_title and pdl_title.lower() != lead.title_raw.lower():
        conflicts["title"] = {"apollo": lead.title_raw, "pdl": pdl_title}
        job_change_signal = True

    updates["enrichment_status"] = EnrichmentStatus.ENRICHED
    updates["enriched_at"] = now

    return EnrichmentOutcome(
        lead=lead.model_copy(update=updates), conflicts=conflicts, job_change_signal=job_change_signal
    )


async def run_stage8(
    *,
    leads: list[LeadRecord],
    matched_lead_ids: set[str],
    company: Company,
    fetcher: Fetcher,
    budget: CreditBudget,
    pdl_client: PDLClient | None = None,
    now: datetime | None = None,
) -> list[LeadRecord]:
    """Enrich only leads that matched at least one job above the floor
    (spec §11.1) and actually need it (§11.1 completeness check, §11.4
    staleness). Every input lead is returned — enrichment never drops a
    lead, only annotates it (spec §11.5: "degraded, not dropped").
    """
    now = now or datetime.now(UTC)
    client = pdl_client or PDLClient()
    result: list[LeadRecord] = []

    for lead in leads:
        if lead.lead_id not in matched_lead_ids:
            result.append(lead)  # spec §11.1: enrich late — matched leads only
            continue

        if not should_attempt_enrichment(lead, now=now):
            result.append(lead)
            continue

        if not budget.try_consume(BudgetMeter.PDL_CREDITS, 1):
            result.append(lead.model_copy(update={"enrichment_status": EnrichmentStatus.ENRICHMENT_SKIPPED}))
            continue

        matched_via_name_only = not (lead.linkedin_url or lead.email)
        try:
            pdl_record = await client.enrich_person(
                fetcher=fetcher,
                linkedin_url=lead.linkedin_url,
                work_email=lead.email,
                full_name=lead.full_name if matched_via_name_only else None,
                company_domain=company.domain if matched_via_name_only else None,
            )
        except (PDLNotConfiguredError, PDLEnrichError) as exc:
            logger.warning("enrichment_skipped", lead_id=lead.lead_id, error=str(exc))
            result.append(lead.model_copy(update={"enrichment_status": EnrichmentStatus.ENRICHMENT_SKIPPED}))
            continue

        if pdl_record is None:
            result.append(
                lead.model_copy(update={"enrichment_status": EnrichmentStatus.ENRICHMENT_SKIPPED, "enriched_at": now})
            )
            continue

        if matched_via_name_only and not has_corroboration(lead, pdl_record):
            # Spec §11.3: "A wrong-person enrichment is worse than no
            # enrichment" — discard the PDL data entirely, don't merge it.
            result.append(
                lead.model_copy(
                    update={"enrichment_status": EnrichmentStatus.ENRICHMENT_IDENTITY_WEAK, "enriched_at": now}
                )
            )
            continue

        outcome = apply_enrichment(lead, pdl_record, now=now)
        if outcome.job_change_signal:
            logger.info("lead_job_change_signal", lead_id=lead.lead_id, conflicts=outcome.conflicts)
        result.append(outcome.lead)

    return result
