"""Apollo reveal — a real API behavior discovered post-implementation, not
described by the spec as its own stage.

Spec §9.3 says Apollo search should "prefer verified email/phone, but do
not require" — implicitly assuming the search response itself carries
contact details. Apollo's real, current `mixed_people/api_search` response
does not: it returns an obfuscated last name and boolean `has_email` /
`has_direct_phone` flags only (see `services.apollo`'s module docstring for
the live-verification trail). Getting the real name/email/LinkedIn/location
needs a second, separately-credited call per person — Apollo's
`people/match` "reveal" endpoint.

This module applies spec §11.1's "enrich late" principle — the one this
codebase already uses for PDL enrichment — to that reveal call: only leads
that matched at least one job above the floor are worth spending an
additional Apollo credit on. It runs *before* Stage 8's PDL waterfall
(`leads.enrichment`) in `main.py`'s pipeline, since Apollo is the primary
source (spec §11.2) and PDL's job is to fill whatever gaps remain after
Apollo has had its real chance — including, now, its reveal step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gtm_agent.core.fetch import Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.leads.budget import BudgetMeter, CreditBudget
from gtm_agent.models.lead import EmailStatus, LeadRecord, LeadSource
from gtm_agent.services.apollo import ApolloClient, ApolloNotConfiguredError, ApolloSearchError

logger = get_logger(__name__)


def _email_status_from(value: Any) -> EmailStatus | None:
    if isinstance(value, str):
        try:
            return EmailStatus(value.lower())
        except ValueError:
            return None
    return None


def apply_reveal(lead: LeadRecord, person: dict[str, Any], *, now: datetime) -> LeadRecord:
    """Merge a revealed Apollo person into a lead. `full_name` is always
    replaced — the pre-reveal value is always the obfuscated search-step
    name, never "good data" worth protecting (spec §11.2's don't-clobber
    rule governs *contact* fields, not this one). Contact fields only fill
    gaps, same waterfall convention as PDL enrichment.
    """
    updates: dict[str, Any] = {}

    full_name = person.get("name")
    if isinstance(full_name, str) and full_name.strip():
        updates["full_name"] = full_name.strip()

    if not lead.email:
        email = person.get("email")
        if isinstance(email, str) and email.strip():
            updates["email"] = email.strip()
            updates["email_status"] = _email_status_from(person.get("email_status")) or EmailStatus.GUESSED

    if not lead.linkedin_url:
        linkedin_url = person.get("linkedin_url")
        if isinstance(linkedin_url, str) and linkedin_url.strip():
            updates["linkedin_url"] = linkedin_url.strip()

    if not lead.location_raw:
        location_parts = [person.get("city"), person.get("state"), person.get("country")]
        location_raw = ", ".join(p for p in location_parts if isinstance(p, str) and p)
        if location_raw:
            updates["location_raw"] = location_raw

    if not updates:
        return lead
    return lead.model_copy(update=updates)


async def reveal_lead(
    lead: LeadRecord,
    *,
    fetcher: Fetcher,
    apollo_client: ApolloClient,
    budget: CreditBudget,
    now: datetime | None = None,
) -> LeadRecord:
    """Reveal one lead, if it's eligible and budget allows. Never raises —
    any failure (not configured, HTTP error, no match) returns the lead
    unchanged, same "degrade, don't crash" convention as Stage 8.
    """
    if lead.source != LeadSource.APOLLO or not lead.source_person_id:
        return lead  # nothing to reveal — not an Apollo-sourced lead with an id

    if not budget.try_consume(BudgetMeter.APOLLO_CREDITS, 1):
        return lead

    try:
        person = await apollo_client.reveal_person(person_id=lead.source_person_id, fetcher=fetcher)
    except (ApolloNotConfiguredError, ApolloSearchError) as exc:
        logger.warning("apollo_reveal_failed", lead_id=lead.lead_id, error=str(exc))
        return lead

    if person is None:
        return lead

    return apply_reveal(lead, person, now=now or datetime.now(UTC))


async def run_apollo_reveal(
    *,
    leads: list[LeadRecord],
    matched_lead_ids: set[str],
    fetcher: Fetcher,
    budget: CreditBudget,
    apollo_client: ApolloClient | None = None,
    now: datetime | None = None,
) -> list[LeadRecord]:
    """Reveal only leads that matched at least one job above the floor
    (spec §11.1, applied to Apollo's own reveal). Every input lead is
    returned — unmatched leads pass through untouched, matched ones are
    revealed where possible. Mirrors `leads.enrichment.run_stage8`'s shape.
    """
    client = apollo_client or ApolloClient()
    now = now or datetime.now(UTC)
    result: list[LeadRecord] = []

    for lead in leads:
        if lead.lead_id not in matched_lead_ids:
            result.append(lead)
            continue
        result.append(await reveal_lead(lead, fetcher=fetcher, apollo_client=client, budget=budget, now=now))

    return result
