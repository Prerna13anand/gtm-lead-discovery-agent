"""The scrape_run ledger — spec §15.1, §16.1.

"`scrape_run` — one row per company per attempt. **The ledger that makes
§2.3 enforceable.**" Recording a run — success or failure — is what lets a
company that couldn't be scraped stay *visible* instead of silently
disappearing (spec §2.3), and it's the source of truth every coverage metric
in §19 will eventually derive from (not implemented here — that's a
separate, not-yet-built consumer of this data).

Scope: this module is the ledger itself — `begin_run`/`close_run`/`list_runs`
— not the sweep orchestrator (spec §16.1's `sweep()`, which schedules and
runs many companies; that's later-phase work) and not the §19 metrics
computation. No database exists yet in this codebase, so persistence is a
simple append-only JSONL file: durable across CLI invocations (unlike an
in-memory-only list, which would defeat the ledger's whole purpose of
keeping unscraped companies visible over time), and trivially replaced by a
real table later without changing `ScrapeRun` or any call site.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from gtm_agent.config import get_settings
from gtm_agent.core.logging import get_logger
from gtm_agent.models.scrape_run import ScrapeRun, ScrapeRunStatus

logger = get_logger(__name__)


class ScrapeRunLedger:
    """Append-only JSONL-backed store for `ScrapeRun` rows."""

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.scrape_run_ledger_path)

    def begin_run(self, company_id: str) -> ScrapeRun:
        """Open a new run. `source_id` isn't known yet at this point in the
        pipeline (Stage 1 hasn't resolved anything) — set it on the returned
        instance directly once it is, `ScrapeRun` is a plain mutable model.
        """
        return ScrapeRun(id=str(uuid4()), company_id=company_id, started_at=datetime.now(UTC))

    def close_run(
        self,
        run: ScrapeRun,
        *,
        status: ScrapeRunStatus,
        failure_detail: str | None = None,
        jobs_found: int = 0,
        http_requests_made: int = 0,
        bytes_fetched: int = 0,
        used_rendering: bool = False,
        raw_payload_ref: str | None = None,
        adapter_used: str | None = None,
    ) -> ScrapeRun:
        """Finalise and persist a run. Spec §17: "Every run terminates in
        exactly one typed status" — this is the one place that assigns it.
        """
        run.finished_at = datetime.now(UTC)
        run.status = status
        run.failure_detail = failure_detail
        run.jobs_found = jobs_found
        run.http_requests_made = http_requests_made
        run.bytes_fetched = bytes_fetched
        run.used_rendering = used_rendering
        run.raw_payload_ref = raw_payload_ref
        run.adapter_used = adapter_used

        self._append(run)
        return run

    def _append(self, run: ScrapeRun) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(run.model_dump_json() + "\n")
        logger.info(
            "scrape_run_recorded",
            run_id=run.id,
            company_id=run.company_id,
            status=run.status.value if run.status else None,
            jobs_found=run.jobs_found,
        )

    def list_runs(self, *, company_id: str | None = None) -> list[ScrapeRun]:
        """Read back recorded runs — a hook for future coverage metrics
        (spec §19), not implemented here. Empty list if nothing's been
        recorded yet (no ledger file).
        """
        if not self._path.exists():
            return []

        runs: list[ScrapeRun] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    runs.append(ScrapeRun.model_validate_json(line))

        if company_id is not None:
            runs = [r for r in runs if r.company_id == company_id]
        return runs


def archive_raw_payloads(
    company_id: str,
    run_id: str,
    payloads: list[Any],
    *,
    base_dir: str | Path | None = None,
) -> str:
    """Spec §6.4: "Every successful extraction archives its raw payload...
    keyed by `(company_id, run_id)`." This is a minimal local-file stand-in
    for the object storage the spec describes — same key shape, no
    S3/cloud integration (that's later-phase infrastructure work, not part
    of the ledger itself). Returns the reference string to store in
    `ScrapeRun.raw_payload_ref`.
    """
    settings = get_settings()
    directory = Path(base_dir) if base_dir is not None else Path(settings.raw_payload_archive_dir)
    directory = directory / company_id
    directory.mkdir(parents=True, exist_ok=True)

    file_path = directory / f"{run_id}.json"
    file_path.write_text(json.dumps(payloads, default=str, indent=2), encoding="utf-8")

    return f"{company_id}/{run_id}"
