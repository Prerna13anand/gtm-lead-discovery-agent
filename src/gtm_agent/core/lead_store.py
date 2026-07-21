"""Part II persistence — spec §15.2's `lead`, `lead_discovery_run`,
`lead_job_match`, `unmatched_job`, and `company_context` tables.

Same local-file stand-in as `core.run_ledger` and `core.lifecycle_store`: no
database exists yet in this codebase, so each table is an append-only JSONL
file. See `run_ledger.py`'s module docstring for the full rationale; this
module repeats that same small, explicit pattern rather than factoring out a
shared base class, matching this codebase's existing one-class-per-table style.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from gtm_agent.config import get_settings
from gtm_agent.core.logging import get_logger
from gtm_agent.models.company_context import CompanyContext
from gtm_agent.models.feedback import LeadFeedback
from gtm_agent.models.lead import LeadDiscoveryRun, LeadDiscoveryStatus, LeadRecord
from gtm_agent.models.matching import LeadJobMatch, UnmatchedJob

logger = get_logger(__name__)


class LeadStore:
    """`lead` — spec §15.2: "Cached, not per-run — this is the §2.7 saving
    made concrete." "Unique on `(company_id, source_person_id)`" — this
    codebase keys by `(company_id, lead_id)` instead: `lead_id` is already
    set from `source_person_id` when Apollo supplies one (see
    `leads.discovery.person_to_lead`) and falls back to a generated UUID
    only when it doesn't, so the two keys coincide whenever the spec's own
    key is even defined.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.lead_store_path)

    def save(self, leads: list[LeadRecord]) -> None:
        if not leads:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for lead in leads:
                f.write(lead.model_dump_json() + "\n")

    def current_leads(self, company_id: str) -> dict[str, LeadRecord]:
        """This company's current `lead` rows, keyed by `lead_id` — the last
        occurrence in file order wins, same convention as
        `core.lifecycle_store.JobPostingStore.current_records`.
        """
        latest: dict[str, LeadRecord] = {}
        for lead in self._read_all():
            if lead.company_id != company_id:
                continue
            latest[lead.lead_id] = lead
        return latest

    def _read_all(self) -> list[LeadRecord]:
        if not self._path.exists():
            return []
        leads: list[LeadRecord] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    leads.append(LeadRecord.model_validate_json(line))
        return leads


class LeadDiscoveryRunLedger:
    """`lead_discovery_run` — spec §15.2: "the lead-side analogue of
    `scrape_run`." Same begin/close/list shape as `core.run_ledger.ScrapeRunLedger`.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.lead_discovery_run_path)

    def begin_run(self, company_id: str, *, started_at: datetime) -> LeadDiscoveryRun:
        return LeadDiscoveryRun(id=str(uuid4()), company_id=company_id, started_at=started_at)

    def close_run(
        self,
        run: LeadDiscoveryRun,
        *,
        status: LeadDiscoveryStatus,
        finished_at: datetime,
        personas_requested: list[str] | None = None,
        leads_returned: int = 0,
        apollo_credits_used: int = 0,
        cache_hit: bool = False,
    ) -> LeadDiscoveryRun:
        run.status = status
        run.finished_at = finished_at
        run.personas_requested = personas_requested or []
        run.leads_returned = leads_returned
        run.apollo_credits_used = apollo_credits_used
        run.cache_hit = cache_hit

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(run.model_dump_json() + "\n")
        logger.info(
            "lead_discovery_run_recorded",
            run_id=run.id,
            company_id=run.company_id,
            status=status.value,
            leads_returned=leads_returned,
            cache_hit=cache_hit,
        )
        return run

    def list_runs(self, *, company_id: str | None = None) -> list[LeadDiscoveryRun]:
        if not self._path.exists():
            return []
        runs: list[LeadDiscoveryRun] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    runs.append(LeadDiscoveryRun.model_validate_json(line))
        if company_id is not None:
            runs = [r for r in runs if r.company_id == company_id]
        return runs


class LeadJobMatchStore:
    """`lead_job_match` — spec §15.2: "the inspectable record of *why*."
    Append-only: a re-run recomputes and appends fresh matches rather than
    mutating old ones, so `job_posting_version`-style history is implicitly
    preserved via `rules_version` + `computed_at` on each row.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.lead_job_match_path)

    def append(self, matches: list[LeadJobMatch]) -> None:
        if not matches:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for match in matches:
                f.write(match.model_dump_json() + "\n")

    def list_matches(self, *, job_id: str | None = None) -> list[LeadJobMatch]:
        if not self._path.exists():
            return []
        matches: list[LeadJobMatch] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    matches.append(LeadJobMatch.model_validate_json(line))
        if job_id is not None:
            matches = [m for m in matches if m.job_id == job_id]
        return matches


class UnmatchedJobStore:
    """`unmatched_job` — spec §15.2: "a work queue for persona-coverage bugs,
    not just an output state."
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.unmatched_job_path)

    def append(self, unmatched: list[UnmatchedJob]) -> None:
        if not unmatched:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for entry in unmatched:
                f.write(entry.model_dump_json() + "\n")

    def list_unmatched(self, *, job_id: str | None = None) -> list[UnmatchedJob]:
        if not self._path.exists():
            return []
        entries: list[UnmatchedJob] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(UnmatchedJob.model_validate_json(line))
        if job_id is not None:
            entries = [e for e in entries if e.job_id == job_id]
        return entries


class CompanyContextStore:
    """`company_context` — spec §15.2: "one row per company with TTL." Last
    occurrence in file order wins, same "current state via append-only log"
    convention as `LeadStore`/`JobPostingStore`.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.company_context_path)

    def save(self, context: CompanyContext) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(context.model_dump_json() + "\n")

    def get(self, company_id: str) -> CompanyContext | None:
        latest: CompanyContext | None = None
        if not self._path.exists():
            return None
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                context = CompanyContext.model_validate_json(line)
                if context.company_id == company_id:
                    latest = context
        return latest


class LeadFeedbackStore:
    """`lead_feedback` (spec §19.5) — append-only capture, no consumer yet
    (Phase 5 scope, per the spec quote in `leads.feedback`'s module docstring).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.lead_feedback_path)

    def append(self, feedback: list[LeadFeedback]) -> None:
        if not feedback:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for entry in feedback:
                f.write(entry.model_dump_json() + "\n")

    def list_feedback(self, *, job_id: str | None = None, lead_id: str | None = None) -> list[LeadFeedback]:
        if not self._path.exists():
            return []
        entries: list[LeadFeedback] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(LeadFeedback.model_validate_json(line))
        if job_id is not None:
            entries = [e for e in entries if e.job_id == job_id]
        if lead_id is not None:
            entries = [e for e in entries if e.lead_id == lead_id]
        return entries
