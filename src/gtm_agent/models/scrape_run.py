"""`scrape_run` — spec §15.1, §16.1, §17.

"`scrape_run` — one row per company per attempt. **The ledger that makes
§2.3 enforceable.**" (§15.1)

This is the record that lets a caller distinguish "we tried and it failed",
"we never tried", and "we tried and there's genuinely nothing there" —
three states that must never collapse into each other (spec §2.3).
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ScrapeRunStatus(StrEnum):
    """"Every run terminates in exactly one typed status. This table is the
    operational contract." (§17)

    The 13 values below are exactly the §17 failure-taxonomy table's rows.
    `NEEDS_REVIEW` is not one of that table's rows, but is named explicitly
    in the §16.1 orchestration pseudocode ("if source.needs_review: -> skip,
    status=needs_review") — included for that reason and called out
    separately so it's traceable to its actual source, not silently folded
    into the §17 table as if it belonged there.

    Several of these values are not reachable by this codebase's current
    Phase 1 logic — `robots_disallowed` and `render_timeout` need fetch-layer
    and rendered-DOM capabilities that are explicit later-phase TODOs;
    `zero_jobs_suspicious` needs Stage 5 change detection to compare against
    a prior run, which is explicitly Phase 2 (spec: "Change detection is
    Phase 2, not Phase 1"); `partial` needs a two-phase/selective-hydration
    path none of the three real adapters take (all three are inline). They
    are still declared here for schema completeness, matching how
    `ExtractionStatus`/`SourceResolutionStatus` already include not-yet-
    reachable values (see models/results.py) — declaring the full enum is
    not the same as implementing the detection logic behind each value.
    """

    SUCCESS = "success"
    NO_CAREERS_PAGE = "no_careers_page"
    RESOLUTION_UNVALIDATED = "resolution_unvalidated"
    DOMAIN_UNREACHABLE = "domain_unreachable"
    ATS_UNKNOWN = "ats_unknown"
    BLOCKED_403 = "blocked_403"
    ROBOTS_DISALLOWED = "robots_disallowed"
    RATE_LIMITED = "rate_limited"
    RENDER_TIMEOUT = "render_timeout"
    PARSE_DEGRADED = "parse_degraded"
    SCHEMA_VIOLATION = "schema_violation"
    ZERO_JOBS_SUSPICIOUS = "zero_jobs_suspicious"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"  # §16.1 pseudocode; not a row in the §17 table


class ScrapeRun(BaseModel):
    """Field list is exactly spec §15.1's `scrape_run` row:

    `id` · `company_id` → company · `source_id` → careers_source ·
    `started_at` · `finished_at` · `status` (enum, §17) · `failure_detail` ·
    `jobs_found` · `http_requests_made` · `bytes_fetched` · `used_rendering` ·
    `raw_payload_ref` · `adapter_used`

    `status` is `None` while a run is still open (spec: "every run
    terminates in exactly one typed status" — before termination, none is
    assigned yet). Everything else defaults to its natural empty/zero value
    so a freshly `begin_run`'d instance is always valid, without implying
    anything about an outcome that hasn't happened yet.
    """

    id: str
    company_id: str
    source_id: str | None = None

    started_at: datetime
    finished_at: datetime | None = None
    status: ScrapeRunStatus | None = None
    failure_detail: str | None = None

    jobs_found: int = 0
    http_requests_made: int = 0
    bytes_fetched: int = 0
    used_rendering: bool = False
    raw_payload_ref: str | None = None
    adapter_used: str | None = None
