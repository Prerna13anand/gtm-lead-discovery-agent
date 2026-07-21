"""Stage 11 — Publication & Output Contract (spec §14).

**Goal:** deliver the ranked result and the events that drive incremental
work. Assembly only — no I/O; `core.publication_store` persists what this
module builds, and `to_csv_rows`/`write_csv` below are the spec §14.4
"queryable table plus CSV export" delivery format, not a network call.
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime
from pathlib import Path

from gtm_agent.models.company import Company
from gtm_agent.models.company_context import CompanyContext
from gtm_agent.models.matching import LeadJobMatch, UnmatchedJob
from gtm_agent.models.publication import (
    CompanySummary,
    GtmLead,
    JobSummary,
    LeadSummary,
    PublicationEvent,
    PublicationEventType,
)
from gtm_agent.models.scoring import ScoredLead
from gtm_agent.scoring.ranking import RankedEntry, contactability_label


def assemble_gtm_lead(
    entry: RankedEntry, *, company: Company, match: LeadJobMatch, context: CompanyContext | None, rank: int
) -> GtmLead:
    """Spec §14.1's `GtmLead`, field for field."""
    scored, job, lead = entry.scored, entry.job, entry.lead
    return GtmLead(
        company=CompanySummary(
            name=company.name, domain=company.domain, stage=company.funding_stage, headcount=company.headcount
        ),
        job=JobSummary(
            title=job.title_canonical,
            function=job.function.value if job.function else None,
            seniority=job.seniority.value if job.seniority else None,
            location=job.location_raw,
            posting_url=job.posting_url,
        ),
        lead=LeadSummary(
            name=lead.full_name,
            title=lead.title_raw,
            linkedin_url=lead.linkedin_url,
            email=lead.email,
            phone=lead.phone,
            contactability=contactability_label(lead),
        ),
        relevance_score=scored.relevance_score,
        confidence_score=scored.confidence_score,
        rationale=scored.rationale,
        match_signals=match.signals,
        company_context=context.summary if context else None,
        rank=rank,
        generated_at=scored.scored_at,
        data_provenance={
            "lead": lead.source.value,
            "job": job.source_platform,
            "score": "llm" if scored.rationale else "rules_fallback",
        },
    )


def publish(
    entries: list[RankedEntry],
    *,
    matches_by_id: dict[str, LeadJobMatch],
    company_by_id: dict[str, Company],
    company_id_by_job_id: dict[str, str],
    context_by_company: dict[str, CompanyContext | None],
) -> tuple[list[GtmLead], list[PublicationEvent]]:
    """Spec §13.6: "Ranked within company, then across companies." `entries`
    must already be in `scoring.ranking.rank`'s output order — this function
    only numbers each company's entries 1..N (resetting per company, per
    §14.1's `rank` field) and emits one `lead_ready` event per lead. It does
    not re-sort; sorting is `ranking.rank`'s job, kept separate so this
    module stays pure assembly.
    """
    gtm_leads: list[GtmLead] = []
    events: list[PublicationEvent] = []
    rank_counters: dict[str, int] = {}

    for entry in entries:
        company_id = company_id_by_job_id[entry.job.job_id]
        rank_counters[company_id] = rank_counters.get(company_id, 0) + 1
        company = company_by_id[company_id]
        match = matches_by_id[entry.scored.match_id]
        context = context_by_company.get(company_id)

        gtm_lead = assemble_gtm_lead(entry, company=company, match=match, context=context, rank=rank_counters[company_id])
        gtm_leads.append(gtm_lead)
        events.append(
            PublicationEvent(
                id=str(uuid.uuid4()),
                event_type=PublicationEventType.LEAD_READY,
                job_id=entry.job.job_id,
                lead_id=entry.lead.lead_id,
                occurred_at=entry.scored.scored_at,
            )
        )

    return gtm_leads, events


def publish_unmatched(unmatched: list[UnmatchedJob], *, now: datetime) -> list[PublicationEvent]:
    """Spec §14.2: "A job with `no_plausible_owner` or `lead_discovery_failed`
    is published with an empty lead set and its reason." The `GtmLead` table
    has no representation for a leadless job (every `GtmLead` row has a
    lead) — this event stream is how such a job still surfaces, per §14.3's
    `job_unmatched` event and §10.6's `unmatched_job` work-queue table.
    """
    return [
        PublicationEvent(
            id=str(uuid.uuid4()),
            event_type=PublicationEventType.JOB_UNMATCHED,
            job_id=entry.job_id,
            occurred_at=now,
            payload={"reason": entry.reason.value},
        )
        for entry in unmatched
    ]


def publish_job_closed(job_ids: list[str], *, now: datetime) -> list[PublicationEvent]:
    """Spec §14.3: "prevents outreach about a role that no longer exists."""
    return [
        PublicationEvent(id=str(uuid.uuid4()), event_type=PublicationEventType.JOB_CLOSED, job_id=job_id, occurred_at=now)
        for job_id in job_ids
    ]


def detect_superseded(
    *, previous_top_lead_by_job: dict[str, str], new_gtm_leads: list[GtmLead], now: datetime
) -> list[PublicationEvent]:
    """Spec §14.3's `lead_superseded`: "Better lead found for a job
    previously published." Compares each job's current rank-1 lead against
    the rank-1 lead from the last publish; a rank-1 lead is never dropped
    silently, it becomes an explicit event when it changes.
    """
    events = []
    for gtm_lead in new_gtm_leads:
        if gtm_lead.rank != 1:
            continue
        job_id = gtm_lead.job.posting_url  # jobs aren't keyed by URL elsewhere, but GtmLead carries no job_id
        previous = previous_top_lead_by_job.get(job_id)
        current_lead_key = gtm_lead.lead.email or gtm_lead.lead.name
        if previous is not None and previous != current_lead_key:
            events.append(
                PublicationEvent(
                    id=str(uuid.uuid4()),
                    event_type=PublicationEventType.LEAD_SUPERSEDED,
                    job_id=job_id,
                    lead_id=current_lead_key,
                    occurred_at=now,
                    payload={"previous_lead": previous},
                )
            )
    return events


# --- CSV / queryable-table delivery (spec §14.4) ----------------------------

_CSV_FIELDS = [
    "company_name", "company_domain", "company_stage", "company_headcount",
    "job_title", "job_function", "job_seniority", "job_location", "job_posting_url",
    "lead_name", "lead_title", "lead_linkedin_url", "lead_email", "lead_phone", "lead_contactability",
    "relevance_score", "confidence_score", "rationale", "match_signals", "company_context",
    "rank", "generated_at",
]


def to_csv_rows(gtm_leads: list[GtmLead]) -> list[dict[str, str]]:
    rows = []
    for lead in gtm_leads:
        rows.append(
            {
                "company_name": lead.company.name,
                "company_domain": lead.company.domain,
                "company_stage": lead.company.stage or "",
                "company_headcount": str(lead.company.headcount) if lead.company.headcount is not None else "",
                "job_title": lead.job.title,
                "job_function": lead.job.function or "",
                "job_seniority": lead.job.seniority or "",
                "job_location": lead.job.location or "",
                "job_posting_url": lead.job.posting_url,
                "lead_name": lead.lead.name,
                "lead_title": lead.lead.title,
                "lead_linkedin_url": lead.lead.linkedin_url or "",
                "lead_email": lead.lead.email or "",
                "lead_phone": lead.lead.phone or "",
                "lead_contactability": lead.lead.contactability,
                "relevance_score": f"{lead.relevance_score:.3f}",
                "confidence_score": f"{lead.confidence_score:.3f}",
                "rationale": lead.rationale,
                "match_signals": json.dumps(lead.match_signals),
                "company_context": lead.company_context or "",
                "rank": str(lead.rank),
                "generated_at": lead.generated_at.isoformat(),
            }
        )
    return rows


def write_csv(path: str | Path, gtm_leads: list[GtmLead]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(to_csv_rows(gtm_leads))
