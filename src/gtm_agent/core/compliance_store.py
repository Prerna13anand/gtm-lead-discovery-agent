"""Compliance persistence — spec §21.6. Same local-JSONL stand-in
convention as every other store in this codebase.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gtm_agent.config import get_settings
from gtm_agent.models.compliance import CompanyDenylistEntry, PersonSuppressionEntry


class CompanyDenylistStore:
    """"A documented path for a company to request exclusion from
    scraping. A domain denylist checked at stage 1, honoured immediately,
    never re-resolved." (spec §21.6)
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.company_denylist_path)

    def add(self, domain: str, *, reason: str | None = None, now: datetime | None = None) -> CompanyDenylistEntry:
        entry = CompanyDenylistEntry(domain=domain.strip().lower(), reason=reason, added_at=now or datetime.now(UTC))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        return entry

    def is_denied(self, domain: str) -> bool:
        target = domain.strip().lower()
        return any(entry.domain == target for entry in self.list_all())

    def list_all(self) -> list[CompanyDenylistEntry]:
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as f:
            return [CompanyDenylistEntry.model_validate_json(line) for line in f if line.strip()]


class PersonSuppressionStore:
    """"A lead who requests erasure is deleted and added to a suppression
    list checked at stage 6, so the next Apollo sweep doesn't silently
    re-add them." (spec §21.6)
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.person_suppression_path)

    def add(self, key: str, *, reason: str | None = None, now: datetime | None = None) -> PersonSuppressionEntry:
        entry = PersonSuppressionEntry(key=key, reason=reason, added_at=now or datetime.now(UTC))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        return entry

    def is_suppressed(self, key: str) -> bool:
        return any(entry.key == key for entry in self.list_all())

    def list_all(self) -> list[PersonSuppressionEntry]:
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as f:
            return [PersonSuppressionEntry.model_validate_json(line) for line in f if line.strip()]
