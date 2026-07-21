"""Stage 6 (spec §9) outputs and the persisted `lead` / `lead_discovery_run` rows (spec §15.2).

`Lead.function` / `Lead.seniority` deliberately reuse `JobPosting`'s taxonomy
(`JobFunction`, `Seniority` — see `models/common.py`), not a parallel one.
Spec §9.5: "This is not incidental — it is what makes matching a comparison
of like with like rather than a fuzzy string problem. Reusing one classifier
for both sides is the single change that most simplifies §10." — see
`discovery.normalization.classify_function` / `.classify_seniority`, which
Stage 6 calls directly for exactly that reason.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from gtm_agent.models.common import JobFunction, Provenance, Seniority


class LeadSource(StrEnum):
    APOLLO = "apollo"
    PDL = "pdl"
    MERGED = "merged"


class EmailStatus(StrEnum):
    VERIFIED = "verified"
    GUESSED = "guessed"
    NONE = "none"


class Lead(BaseModel):
    """Spec §9.5's `Lead` schema, field for field."""

    lead_id: str
    company_id: str
    source: LeadSource
    source_person_id: str | None = None

    full_name: str
    title_raw: str
    title_canonical: str
    function: JobFunction | None = None
    seniority: Seniority | None = None
    is_founder: bool = False
    is_recruiter: bool = False

    linkedin_url: str | None = None
    email: str | None = None
    email_status: EmailStatus | None = None
    phone: str | None = None
    location_raw: str | None = None

    tenure_months: int | None = None
    field_provenance: dict[str, Provenance] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    retrieved_at: datetime


class EnrichmentStatus(StrEnum):
    """Spec §17.2's Stage 8 terminal states, as they apply to one lead."""

    NOT_ATTEMPTED = "not_attempted"
    """Never enriched — either not yet matched to anything, or Apollo's
    record was already complete (spec §11.1)."""

    ENRICHED = "enriched"
    ENRICHMENT_SKIPPED = "enrichment_skipped"
    """PDL unavailable or budget-capped (spec §11.5) — published with Apollo
    data only, flagged, not dropped."""

    ENRICHMENT_IDENTITY_WEAK = "enrichment_identity_weak"
    """PDL matched on name alone with no corroborating field (spec §11.3) —
    discarded; "better no data than wrong data"."""


class LeadRecord(Lead):
    """The persisted `lead` row (spec §15.2): "All `Lead` fields from §9.5,
    plus `retrieved_at`, `enriched_at`, `enrichment_status`, `is_stale`."
    `retrieved_at` is already on `Lead` itself; the three genuinely new
    fields are added here, following the same `XRecord(X)` extension pattern
    as `JobPostingRecord(JobPosting)` (models/lifecycle.py).
    """

    enriched_at: datetime | None = None
    enrichment_status: EnrichmentStatus = EnrichmentStatus.NOT_ATTEMPTED
    is_stale: bool = False


class LeadDiscoveryStatus(StrEnum):
    """Spec §9.7 / §17.2's Stage 6 terminal states."""

    LEADS_OK = "leads_ok"
    """Lead set retrieved, or served from cache (spec §17.2)."""

    NO_LEADS_FOUND = "no_leads_found"
    """Apollo returned nobody. Not "this company has no hiring leads" —
    just that we didn't retrieve them (spec §17.2)."""

    LEAD_DISCOVERY_FAILED = "lead_discovery_failed"
    """Apollo error or timeout. Unknown, not "nobody" (spec §2.3, §9.7)."""

    COMPANY_IDENTITY_SUSPECT = "company_identity_suspect"
    """Result count exceeded the §9.4 retrieval cap — the domain probably
    resolved to the wrong (larger) organisation. Held for review."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """Apollo credit ceiling hit mid-sweep (spec §18.3). Never a silent
    truncation."""


class LeadDiscoveryRun(BaseModel):
    """Spec §15.2's `lead_discovery_run` — "the lead-side analogue of
    `scrape_run`, and the ledger that keeps §2.3 honest on this side of the
    pipeline."
    """

    id: str
    company_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: LeadDiscoveryStatus | None = None
    personas_requested: list[str] = Field(default_factory=list)
    leads_returned: int = 0
    apollo_credits_used: int = 0
    cache_hit: bool = False
