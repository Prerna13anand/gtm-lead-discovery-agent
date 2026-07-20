"""Typed terminal states for Part I stages (spec §17) and a small result wrapper.

Spec §2.3 is the reason this file exists: "scrape failed" and "no open jobs"
are semantically opposite and must never be collapsed into each other. Every
stage below returns one of these explicit statuses rather than an empty list
or a bare exception, so a caller can never mistake "we don't know" for "we
checked and there's nothing there."

Only the subset of the §17 failure taxonomy relevant to Part I (stages 1-4)
is modelled here. Part II/III statuses (`no_leads_found`, `budget_exhausted`,
etc.) belong to later phases.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar


class SourceResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    RESOLUTION_UNVALIDATED = "resolution_unvalidated"
    NO_CAREERS_PAGE = "no_careers_page"
    DOMAIN_UNREACHABLE = "domain_unreachable"
    NEEDS_REVIEW = "needs_review"  # below confidence floor, spec §4.3


class AtsFingerprintStatus(StrEnum):
    IDENTIFIED = "identified"
    ATS_UNKNOWN = "ats_unknown"


class ExtractionStatus(StrEnum):
    SUCCESS = "success"
    PARSE_DEGRADED = "parse_degraded"
    SCHEMA_VIOLATION = "schema_violation"
    RATE_LIMITED = "rate_limited"
    BLOCKED_403 = "blocked_403"
    ROBOTS_DISALLOWED = "robots_disallowed"
    RENDER_TIMEOUT = "render_timeout"
    PARTIAL = "partial"
    NOT_IMPLEMENTED = "not_implemented"  # Phase 1 placeholder adapters — see discovery/extraction
    BOARD_NOT_FOUND = "board_not_found"
    """The ATS API rejected the board token (404), or an adapter couldn't
    resolve a board token for the source at all. Distinct from
    `SCHEMA_VIOLATION` (a board we *could* reach returned an unexpected
    shape) and from `BLOCKED_403` (a board we know exists but were denied) —
    this is "there is no board here," which is closest to `no_careers_page`
    in spirit but discovered one stage later, at extraction time (spec §2.3:
    never collapse "we don't know" into "no jobs")."""


T = TypeVar("T")
S = TypeVar("S", bound=StrEnum)


@dataclass(frozen=True)
class StageResult(Generic[T, S]):
    """The outcome of running one stage: either a value, or a typed non-success status.

    `status` is always set. `value` is only meaningful when `status` indicates
    success — check `status` first, don't infer success from `value` being
    non-None (an empty-but-successful result, e.g. a validated board with zero
    jobs, is a legitimate value).
    """

    status: S
    value: T | None = None
    detail: str | None = None
