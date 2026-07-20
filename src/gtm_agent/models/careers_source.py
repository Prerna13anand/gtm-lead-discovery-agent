"""Stage 1 output — spec §4 and §15.1.

`CareersSource` is long-lived: expensive to compute, cheap to reuse (spec §3.2).
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ResolutionStrategy(StrEnum):
    """The resolution ladder, in decreasing order of reliability (spec §4.1)."""

    HOMEPAGE_LINK = "homepage_link"
    PATH_PROBE = "path_probe"
    SITEMAP = "sitemap"  # Strategy C
    TAVILY_SEARCH = "tavily_search"  # Strategy D
    MANUAL_OVERRIDE = "manual_override"


class CareersSource(BaseModel):
    id: str | None = None
    company_id: str

    careers_url: str
    resolution_strategy: ResolutionStrategy
    resolution_confidence: float = Field(ge=0.0, le=1.0)

    is_manual_override: bool = False
    needs_review: bool = False

    last_verified_at: datetime | None = None
    created_at: datetime

    @property
    def below_confidence_floor(self) -> bool:
        """Spec §4.3: below 0.50, a source is stored but excluded from automated runs."""
        return self.resolution_confidence < 0.50
