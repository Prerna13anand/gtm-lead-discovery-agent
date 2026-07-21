"""Canary Suite (spec §20.3) — models.

"~20 real companies, one per ATS plus several generic-path, scraped nightly
against the live web. This is the only test that catches real-world drift —
an ATS quietly changing a field, a company migrating platforms, a careers
page being redesigned. Fixtures by definition cannot catch any of these,
because fixtures are frozen."
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from gtm_agent.models.ats import AtsPlatform


class CanaryTarget(BaseModel):
    """One entry in the curated canary company list. `careers_url` is a
    manual override — canary checks re-verify fingerprinting and extraction
    (spec §20.3's actual point), not Stage 1 source resolution, so there's no
    need to re-discover a URL that's already known and stable.
    """

    company_id: str
    company_name: str
    domain: str
    careers_url: str
    expected_platform: AtsPlatform
    """The platform this target is known to use as of when it was added —
    the baseline `detect_drift` compares live behaviour against, independent
    of whatever the *previous canary run* happened to observe.
    """
    notes: str | None = None


class CanaryRunResult(BaseModel):
    """One canary check's outcome for one target — persisted so the *next*
    run has something to diff against (spec §20.3's whole mechanism).
    """

    id: str
    company_id: str
    run_at: datetime
    detected_platform: AtsPlatform
    extraction_status: str
    """`ExtractionStatus` value as a plain string — kept loose the same way
    `RawPosting.source_platform` is (models/job.py), so an unrecognised
    status from a future addition doesn't break parsing old canary history.
    """
    job_count: int
    adapter_used: str


class CanaryFinding(BaseModel):
    """Spec §20.3: "Failures here are informational rather than
    build-blocking, but they open a ticket automatically." This codebase has
    no real ticketing system integration — this is the local, honest stand-in
    (see `core.canary_store`'s module docstring), same spirit as
    `ScrapeRunLedger` standing in for a database.
    """

    id: str
    company_id: str
    company_name: str
    detected_at: datetime
    reasons: list[str] = Field(default_factory=list)
    previous: CanaryRunResult | None = None
    current: CanaryRunResult
