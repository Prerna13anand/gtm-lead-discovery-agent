"""Stage 3 and 4 outputs — `RawPosting` (spec §6) and `JobPosting` (spec §7.1)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from gtm_agent.models.common import (
    Compensation,
    EmploymentType,
    JobFunction,
    Location,
    Provenance,
    Seniority,
    WorkplaceType,
)


class RawPosting(BaseModel):
    """Platform-shaped extraction output, before normalisation.

    `raw_payload` is the untouched payload (a parsed JSON dict for ATS-API and
    JSON-LD paths, or raw HTML for the generic-HTML path) — this is what gets
    archived per spec §6.4, independent of whatever normalisation later does
    with it.
    """

    company_id: str
    source_platform: str  # AtsPlatform value; kept as str so unknown platforms don't break parsing
    source_job_id: str | None = None
    posting_url: str | None = None

    raw_payload: dict[str, Any] | str

    fetched_at: datetime
    is_hydrated: bool = False
    """False when produced by `discover()` on a two-phase platform and `hydrate()`
    has not yet filled in the full description (spec §6.1)."""


class JobPosting(BaseModel):
    """Canonical, cross-platform job record. See spec §7.1 for the full rationale
    behind each field, in particular:

    - `title_raw` is never overwritten — every downstream consumer, especially
      an LLM doing lead scoring, needs to see exactly what the company wrote.
    - `field_provenance` + `extraction_confidence` + `is_degraded` let a
      consumer distinguish an authoritative field from an inferred one.
    """

    # Identity
    job_id: str
    company_id: str
    source_job_id: str | None = None
    source_platform: str
    posting_url: str

    # Core content
    title_raw: str
    title_canonical: str
    description_text: str
    description_markdown: str
    department_raw: str | None = None
    function: JobFunction | None = None
    seniority: Seniority | None = None

    # Location
    location_raw: str | None = None
    locations: list[Location] = Field(default_factory=list)
    workplace_type: WorkplaceType | None = None
    remote_scope: str | None = None

    # Terms
    employment_type: EmploymentType | None = None
    compensation: Compensation | None = None

    # Timing
    posted_at: datetime | None = None
    posted_at_is_inferred: bool = False
    first_seen_at: datetime
    last_seen_at: datetime
    closed_at: datetime | None = None

    # Quality
    field_provenance: dict[str, Provenance] = Field(default_factory=dict)
    extraction_confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    is_degraded: bool = False
