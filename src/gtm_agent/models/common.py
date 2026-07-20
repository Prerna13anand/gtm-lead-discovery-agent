"""Shared value types used across domain models.

`JobFunction` and `Seniority` (spec §7.3) are deliberately the single shared
taxonomy used for both `JobPosting` and, in a later phase, `Lead` — see
JOB_SCRAPING_AGENT.md §9.5: "Reusing one classifier for both sides is the
single change that most simplifies §10."
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class JobFunction(StrEnum):
    ENGINEERING = "engineering"
    PRODUCT = "product"
    DESIGN = "design"
    DATA = "data"
    SALES = "sales"
    MARKETING = "marketing"
    CUSTOMER_SUCCESS = "customer_success"
    OPERATIONS = "operations"
    FINANCE = "finance"
    PEOPLE = "people"
    LEGAL = "legal"
    OTHER = "other"


class Seniority(StrEnum):
    """Ordered by hiring authority, not IC scope — see spec Appendix C.3.

    `founding` has no natural rank; normalisation treats it as `senior` for
    any numeric comparison and leaves the founder-bonus logic (Phase 3) to
    do the real work.
    """

    INTERN = "intern"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    VP = "vp"
    EXECUTIVE = "executive"
    FOUNDING = "founding"


class WorkplaceType(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"


class Provenance(BaseModel):
    """Where a normalised field's value came from, and how confident we are in it.

    Spec §2.6: "Downstream LLM scoring should be able to see the difference[s],
    and debugging is impossible without it."
    """

    source: str  # e.g. "greenhouse_api", "jsonld", "regex:title_location", "inferred"
    confidence: float = Field(ge=0.0, le=1.0)
    derived_at: datetime
    notes: str | None = None


class Location(BaseModel):
    city: str | None = None
    region: str | None = None
    country: str | None = None
    is_remote: bool = False
    remote_scope: str | None = None  # e.g. "US", "EMEA", "global"
    raw: str | None = None


class Compensation(BaseModel):
    """Extracted only when explicitly posted; never inferred (spec §7.5)."""

    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None
    period: str | None = None  # annual | hourly
    equity_mentioned: bool = False
