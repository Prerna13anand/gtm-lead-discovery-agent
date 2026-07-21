"""Stage 6 — Lead Discovery (spec §9).

**Goal:** for one company, retrieve the set of people who could plausibly
own hiring for any of its open roles — in a single Apollo sweep, cached for
reuse (spec §2.7: once per company, not once per job).

Same split as `discovery.lifecycle`: pure decision logic (`needs_refresh`,
`personas_uncovered`, `is_stale`, mapping Apollo's raw person dicts to
`Lead`) lives here and is unit-testable without any network; the one I/O
call (`run_stage6`) is a thin async wrapper around `ApolloClient` and the
shared `CreditBudget`. Persistence (`LeadStore`, `LeadDiscoveryRunLedger`)
is the caller's job, same as `core.lifecycle_store` is for Stage 5.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gtm_agent.core.compliance_store import PersonSuppressionStore
from gtm_agent.core.fetch import Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.discovery.normalization import classify_function, classify_seniority
from gtm_agent.leads.budget import BudgetMeter, CreditBudget
from gtm_agent.leads.compliance import filter_suppressed
from gtm_agent.leads.personas import FOUNDER_TITLES, RECRUITING_TITLES, personas_for
from gtm_agent.models.common import Provenance
from gtm_agent.models.company import Company
from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import EmailStatus, EnrichmentStatus, Lead, LeadDiscoveryStatus, LeadRecord, LeadSource
from gtm_agent.models.results import StageResult
from gtm_agent.services.apollo import ApolloClient, ApolloNotConfiguredError, ApolloSearchError

logger = get_logger(__name__)

# Spec §9.4: "Cap retrieval at ~50 people per company."
RETRIEVAL_CAP = 50

# Spec §9.6: "30 days elapse."
_STALENESS_DAYS = 30


# --- Pure decision logic (spec §9.6) ---------------------------------------


def personas_uncovered(cached_leads: list[LeadRecord], open_jobs: list[JobPosting]) -> bool:
    """Spec §9.6: recompute when "a new job appears whose `function` has no
    covered persona in the cached set (e.g. first design hire at an
    eng-only company)."

    A function is "covered" if at least one cached lead was classified with
    that same function — i.e. a function-specific persona was actually
    retrieved for it before. Jobs with no classified `function` (spec §7.3
    rules-classifier residue) can't be checked against Appendix C anyway and
    never trigger this.
    """
    covered_functions = {lead.function for lead in cached_leads if lead.function is not None}
    open_functions = {job.function for job in open_jobs if job.function is not None}
    return bool(open_functions - covered_functions)


def is_stale(cache_retrieved_at: datetime | None, *, now: datetime) -> bool:
    """Spec §9.6: "30 days elapse." `None` (never retrieved) is stale by definition."""
    if cache_retrieved_at is None:
        return True
    return (now - cache_retrieved_at).days >= _STALENESS_DAYS


def needs_refresh(
    *,
    cached_leads: list[LeadRecord] | None,
    cache_retrieved_at: datetime | None,
    open_jobs: list[JobPosting],
    now: datetime,
    force_refresh: bool = False,
) -> bool:
    """Spec §9.6's full recompute condition, minus the "headcount-change or
    funding signal" trigger — this codebase has no such signal source yet
    (Stage 9 company context, built later in this same phase, could
    eventually supply one; wiring that is future work, not invented here).
    `force_refresh` stands in for "manual invalidation."
    """
    if force_refresh or cached_leads is None:
        return True
    return personas_uncovered(cached_leads, open_jobs) or is_stale(cache_retrieved_at, now=now)


# --- Mapping Apollo's raw person dicts to Lead (spec §9.5) -----------------


def _is_founder(title_canonical: str) -> bool:
    haystack = title_canonical.lower()
    return any(founder_title.lower() in haystack for founder_title in FOUNDER_TITLES)


def _is_recruiter(title_canonical: str) -> bool:
    haystack = title_canonical.lower()
    return any(recruiting_title.lower() in haystack for recruiting_title in RECRUITING_TITLES)


def _extract_phone(person: dict[str, Any]) -> str | None:
    numbers = person.get("phone_numbers")
    if isinstance(numbers, list) and numbers:
        first = numbers[0]
        if isinstance(first, dict):
            raw = first.get("raw_number") or first.get("sanitized_number")
            return raw if isinstance(raw, str) else None
        if isinstance(first, str):
            return first
    return None


def _extract_tenure_months(person: dict[str, Any], *, now: datetime) -> int | None:
    """Best-effort — Apollo's exact employment-history shape is unverified
    against the live API (see `services.apollo` module docstring). Looks for
    a `start_date` on the entry marked current, or on `organization`'s own
    start-date field if present; `None` if neither is found rather than
    guessing (spec §2.9).
    """
    start_raw = person.get("organization_start_date") or person.get("current_employment_start_date")
    if not isinstance(start_raw, str):
        return None
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    months = (now.year - start.year) * 12 + (now.month - start.month)
    return max(0, months)


def person_to_lead(
    person: dict[str, Any],
    *,
    company_id: str,
    retrieved_at: datetime,
) -> Lead | None:
    """Map one raw Apollo person dict to a `Lead` (spec §9.5). Returns `None`
    for a record with no usable name or title — not enough to act on.
    """
    full_name = person.get("name")
    title_raw = person.get("title")
    if not isinstance(full_name, str) or not full_name.strip():
        return None
    if not isinstance(title_raw, str) or not title_raw.strip():
        return None

    title_canonical = title_raw.strip()
    function, function_provenance = classify_function(title_canonical, None)
    seniority, seniority_provenance = classify_seniority(title_canonical)

    person_id = person.get("id")
    linkedin_url = person.get("linkedin_url")
    email = person.get("email")
    email_status_raw = person.get("email_status")

    email_status = None
    if isinstance(email_status_raw, str):
        try:
            email_status = EmailStatus(email_status_raw.lower())
        except ValueError:
            email_status = None

    location_parts = [person.get("city"), person.get("state"), person.get("country")]
    location_raw = ", ".join(p for p in location_parts if isinstance(p, str) and p) or None

    confidence = 1.0
    provenance: dict[str, Provenance] = {
        "function": function_provenance,
        "seniority": seniority_provenance,
    }
    if function is None or seniority is None:
        # Spec §10.5: confidence is lowered when function/seniority were
        # derived rather than observed. Neither ever comes from Apollo
        # directly — they're always our own classification of the title —
        # but an unresolved classification (rules-classifier residue) is a
        # stronger signal of thin evidence than a resolved one.
        confidence = 0.7

    return Lead(
        lead_id=str(person_id) if person_id else str(uuid.uuid4()),
        company_id=company_id,
        source=LeadSource.APOLLO,
        source_person_id=str(person_id) if person_id else None,
        full_name=full_name.strip(),
        title_raw=title_raw.strip(),
        title_canonical=title_canonical,
        function=function,
        seniority=seniority,
        is_founder=_is_founder(title_canonical),
        is_recruiter=_is_recruiter(title_canonical),
        linkedin_url=linkedin_url if isinstance(linkedin_url, str) else None,
        email=email if isinstance(email, str) else None,
        email_status=email_status,
        phone=_extract_phone(person),
        location_raw=location_raw,
        tenure_months=_extract_tenure_months(person, now=retrieved_at),
        field_provenance=provenance,
        confidence=confidence,
        retrieved_at=retrieved_at,
    )


# --- Orchestration (I/O) ----------------------------------------------------


@dataclass
class Stage6Outcome:
    status: LeadDiscoveryStatus
    leads: list[LeadRecord]
    personas_requested: list[str]
    detail: str | None = None


async def run_stage6(
    *,
    company: Company,
    open_jobs: list[JobPosting],
    fetcher: Fetcher,
    budget: CreditBudget,
    apollo_client: ApolloClient | None = None,
    now: datetime | None = None,
    suppression_store: PersonSuppressionStore | None = None,
) -> Stage6Outcome:
    """Perform the Apollo sweep itself. Callers should only invoke this once
    `needs_refresh` says a fresh sweep is warranted — a cache hit never
    reaches this function at all (spec §2.7, §3.3).

    `suppression_store`, when given, is spec §21.6's Stage 6 check: a
    person who requested erasure is filtered out of the freshly-retrieved
    Apollo results before they're ever returned or cached, "so the next
    Apollo sweep doesn't silently re-add them." `None` (the default) skips
    the check entirely — existing call sites that don't pass one keep
    their prior behaviour unchanged.
    """
    now = now or datetime.now(UTC)
    client = apollo_client or ApolloClient()
    personas = personas_for(open_jobs)

    if not budget.try_consume(BudgetMeter.APOLLO_CREDITS, 1):
        return Stage6Outcome(
            status=LeadDiscoveryStatus.BUDGET_EXHAUSTED,
            leads=[],
            personas_requested=personas,
            detail="Apollo credit budget exhausted for this sweep (spec §18.3)",
        )

    try:
        result = await client.search_people(
            company_domain=company.domain, titles=personas, fetcher=fetcher, limit=RETRIEVAL_CAP
        )
    except (ApolloNotConfiguredError, ApolloSearchError) as exc:
        logger.warning("lead_discovery_failed", company_id=company.id, error=str(exc))
        return Stage6Outcome(
            status=LeadDiscoveryStatus.LEAD_DISCOVERY_FAILED,
            leads=[],
            personas_requested=personas,
            detail=str(exc),
        )

    # Spec §9.4: prefer Apollo's own reported total when available (no
    # over-fetching needed to detect overage); fall back to a hitting-the-cap
    # heuristic when it isn't.
    exceeded_cap = (
        result.total_entries is not None and result.total_entries > RETRIEVAL_CAP
    ) or (result.total_entries is None and len(result.people) >= RETRIEVAL_CAP)
    if exceeded_cap:
        return Stage6Outcome(
            status=LeadDiscoveryStatus.COMPANY_IDENTITY_SUSPECT,
            leads=[],
            personas_requested=personas,
            detail=(
                f"Apollo returned {result.total_entries or len(result.people)} people, "
                f"exceeding the {RETRIEVAL_CAP}-person retrieval cap — domain likely "
                "resolved to the wrong organisation (spec §9.4)"
            ),
        )

    if not result.people:
        return Stage6Outcome(
            status=LeadDiscoveryStatus.NO_LEADS_FOUND, leads=[], personas_requested=personas
        )

    leads = [
        LeadRecord(**lead.model_dump(), enrichment_status=EnrichmentStatus.NOT_ATTEMPTED)
        for person in result.people
        if (lead := person_to_lead(person, company_id=company.id, retrieved_at=now)) is not None
    ]

    if suppression_store is not None:
        leads = filter_suppressed(leads, company_domain=company.domain, suppression_store=suppression_store)

    if not leads:
        return Stage6Outcome(
            status=LeadDiscoveryStatus.NO_LEADS_FOUND, leads=[], personas_requested=personas
        )

    return Stage6Outcome(status=LeadDiscoveryStatus.LEADS_OK, leads=leads, personas_requested=personas)
