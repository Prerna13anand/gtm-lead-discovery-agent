"""Stage 5 persistence — spec §15.1's `job_posting`, `job_posting_version`,
and `scrape_event` tables.

Same local-file stand-in as `core.run_ledger.ScrapeRunLedger`: no database
exists yet in this codebase, so each table is an append-only JSONL file —
durable across CLI invocations, trivially replaced by a real table later
without changing any call site. See `run_ledger.py`'s module docstring for
the full rationale; this module intentionally repeats the same small,
explicit pattern rather than factoring out a shared base class, matching
this codebase's existing style of one focused class per persisted concept.

`JobPostingStore` reduces "current state" (`job_posting`, one row per
identity) by keeping the *last* row written per `(company_id, job_id)` —
safe because rows are only ever appended in run order, never reordered or
rewritten in place, so the last occurrence in file order is always the most
recent observation. `JobPostingVersionLedger` and `ScrapeEventLog` are
simpler still: pure append-only logs, read back in full.
"""

from __future__ import annotations

from pathlib import Path

from gtm_agent.config import get_settings
from gtm_agent.models.lifecycle import JobPostingRecord, JobPostingVersion, ScrapeEvent


class JobPostingStore:
    """`job_posting` — current lifecycle state, one logical row per
    `(company_id, job_id)` (spec §15.1: "current state, one row per distinct
    job identity"; "history lives in `job_posting_version`").
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.job_posting_store_path)

    def save(self, records: list[JobPostingRecord]) -> None:
        """Append this run's full set of current-state rows. Only the caller
        (`discovery.lifecycle.apply_lifecycle`) decides which records actually
        changed — this just persists whatever it hands back.
        """
        if not records:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for record in records:
                f.write(record.model_dump_json() + "\n")

    def current_records(self, company_id: str) -> dict[str, JobPostingRecord]:
        """This company's current `job_posting` rows, keyed by `job_id`.
        Empty dict if the company has no history yet.
        """
        latest: dict[str, JobPostingRecord] = {}
        for record in self._read_all():
            if record.company_id != company_id:
                continue
            latest[record.job_id] = record
        return latest

    def _read_all(self) -> list[JobPostingRecord]:
        if not self._path.exists():
            return []
        records: list[JobPostingRecord] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(JobPostingRecord.model_validate_json(line))
        return records


class JobPostingVersionLedger:
    """`job_posting_version` — append-only change history, written only for
    material changes (spec §8.5).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.job_posting_version_path)

    def append(self, versions: list[JobPostingVersion]) -> None:
        if not versions:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for version in versions:
                f.write(version.model_dump_json() + "\n")

    def list_versions(self, job_id: str) -> list[JobPostingVersion]:
        if not self._path.exists():
            return []
        versions: list[JobPostingVersion] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    version = JobPostingVersion.model_validate_json(line)
                    if version.job_id == job_id:
                        versions.append(version)
        return versions


class ScrapeEventLog:
    """`scrape_event` — the downstream-facing event stream (spec §15.1)."""

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.scrape_event_log_path)

    def append(self, events: list[ScrapeEvent]) -> None:
        if not events:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for event in events:
                f.write(event.model_dump_json() + "\n")

    def list_events(self, company_id: str | None = None) -> list[ScrapeEvent]:
        if not self._path.exists():
            return []
        events: list[ScrapeEvent] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(ScrapeEvent.model_validate_json(line))
        if company_id is not None:
            events = [e for e in events if e.company_id == company_id]
        return events
