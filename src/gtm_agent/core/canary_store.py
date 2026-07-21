"""Canary Suite persistence (spec §20.3) — same local-JSONL pattern as
`core.run_ledger` and `core.lifecycle_store`; see either's module docstring
for the full rationale (no database yet, trivially replaced later).

`CanaryFindingLog` is the local stand-in for "opens a ticket automatically"
(spec §20.3) — this codebase has no real ticketing system integration, and
building one would be inventing infrastructure well beyond this milestone's
scope. A `CanaryFinding` row is the same kind of typed, durable, queryable
record a real ticket would be; wiring it to an actual ticketing API later
doesn't change anything upstream of this file.
"""

from __future__ import annotations

from pathlib import Path

from gtm_agent.config import get_settings
from gtm_agent.models.canary import CanaryFinding, CanaryRunResult


class CanaryResultLog:
    """Every canary run result ever recorded, append-only. `latest_per_target`
    reduces to "what did we last see for this company" — the baseline
    `discovery.canary.detect_drift` compares the newest run against.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.canary_result_log_path)

    def append(self, results: list[CanaryRunResult]) -> None:
        if not results:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for result in results:
                f.write(result.model_dump_json() + "\n")

    def latest_per_target(self) -> dict[str, CanaryRunResult]:
        if not self._path.exists():
            return {}
        latest: dict[str, CanaryRunResult] = {}
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    result = CanaryRunResult.model_validate_json(line)
                    latest[result.company_id] = result
        return latest


class CanaryFindingLog:
    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.canary_finding_log_path)

    def append(self, findings: list[CanaryFinding]) -> None:
        if not findings:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for finding in findings:
                f.write(finding.model_dump_json() + "\n")

    def list_findings(self) -> list[CanaryFinding]:
        if not self._path.exists():
            return []
        findings: list[CanaryFinding] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    findings.append(CanaryFinding.model_validate_json(line))
        return findings
