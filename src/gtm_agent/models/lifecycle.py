"""Stage 5 — Change Detection & Identity (spec §8) — persisted models.

`JobPosting` (spec §7.1, `models/job.py`) is Stage 4's *output*: a fresh,
history-less snapshot recomputed from scratch every run. Stage 5 layers
lifecycle state on top of it for persistence — spec §15.1's `job_posting`
table: "All `JobPosting` fields from §7.1, plus `identity_strategy`, `status`
(open/missing/closed), `consecutive_absences`." `JobPostingRecord` below is
exactly that.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from gtm_agent.models.job import JobPosting


class IdentityStrategy(StrEnum):
    """Spec §8.1's identity ladder, in the same decreasing-reliability order.

    "Identity strategy is recorded per job, because it determines how much
    to trust the change signal."
    """

    ATS_NATIVE_ID = "ats_native_id"
    CANONICAL_URL = "canonical_url"
    CONTENT_HASH = "content_hash"


class JobLifecycleStatus(StrEnum):
    """Spec §8.2's three states."""

    OPEN = "open"
    MISSING = "missing"
    CLOSED = "closed"


class JobPostingRecord(JobPosting):
    """The persisted `job_posting` row (spec §15.1) — current lifecycle state
    for one job identity, one row per `(company_id, job_id)`.
    """

    identity_strategy: IdentityStrategy
    status: JobLifecycleStatus
    consecutive_absences: int = 0

    missing_since: datetime | None = None
    """Set when this job transitions OPEN -> MISSING; cleared on any return
    to OPEN. Not one of the spec's three explicitly-named additions, but
    required to evaluate the "...or 7 days, whichever is longer" half of the
    §8.3 grace window — `consecutive_absences` alone can't answer that, since
    scrape cadence varies by tier (spec §16.2: daily / 3-day / weekly /
    monthly), so a fixed absence count isn't a fixed number of days.
    """


class JobPostingVersion(BaseModel):
    """Spec §15.1's `job_posting_version` — append-only change history.
    Written only for material changes (spec §8.5), not every observation;
    an unchanged re-observation just updates the `job_posting` row in place.
    """

    id: str
    job_id: str
    company_id: str
    observed_at: datetime
    run_id: str
    changed_fields: dict[str, Any] = Field(default_factory=dict)
    """`{field_name: {"old": ..., "new": ...}}` for each materially-changed field."""
    snapshot: dict[str, Any] = Field(default_factory=dict)
    """The full `JobPostingRecord` as of this version, for point-in-time reconstruction."""


class ScrapeEventType(StrEnum):
    """Spec §8.4."""

    JOB_OPENED = "job_opened"
    JOB_CLOSED = "job_closed"
    JOB_REOPENED = "job_reopened"
    JOB_UPDATED = "job_updated"
    BOARD_EMPTIED = "board_emptied"
    BOARD_FIRST_SEEN = "board_first_seen"


class ScrapeEvent(BaseModel):
    """Spec §15.1's `scrape_event` — the downstream-facing event stream.

    `job_id` is `None` for the two company-level events (`board_emptied`,
    `board_first_seen`); every other event type carries one.
    """

    id: str
    company_id: str
    job_id: str | None = None
    event_type: ScrapeEventType
    occurred_at: datetime
    run_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    published_at: datetime | None = None
    identity_strategy: IdentityStrategy | None = None
