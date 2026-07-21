"""Part III persistence — spec §15.2's `scored_lead` row, plus the
`publication_event` stream (spec §14.3) and the `GtmLead` "queryable table"
(spec §14.4) this codebase renders as a JSONL append log — same local-file
stand-in convention as every other store in this codebase (see
`core.run_ledger`'s module docstring for the full rationale).
"""

from __future__ import annotations

from pathlib import Path

from gtm_agent.config import get_settings
from gtm_agent.models.publication import GtmLead, PublicationEvent
from gtm_agent.models.scoring import ScoredLead


class ScoredLeadStore:
    """`scored_lead` — spec §15.2: "cached against re-scoring." "Unique on
    `(match_id, prompt_version, job_version, lead_version)`" (spec §15.2) —
    `get_cached` implements that lookup; a re-run with an unchanged job,
    lead, and prompt finds its prior score and never re-spends LLM tokens
    (spec §13.5).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.scored_lead_path)

    def save(self, scored: ScoredLead) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(scored.model_dump_json() + "\n")

    def get_cached(
        self, *, match_id: str, prompt_version: str, job_version: str, lead_version: str
    ) -> ScoredLead | None:
        latest: ScoredLead | None = None
        if not self._path.exists():
            return None
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                scored = ScoredLead.model_validate_json(line)
                if (
                    scored.match_id == match_id
                    and scored.prompt_version == prompt_version
                    and scored.job_version == job_version
                    and scored.lead_version == lead_version
                ):
                    latest = scored
        return latest

    def list_all(self) -> list[ScoredLead]:
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as f:
            return [ScoredLead.model_validate_json(line) for line in f if line.strip()]


class PublicationEventStore:
    """`publication_event` — spec §14.3's event stream, the Part III
    analogue of Stage 5's `scrape_event` (spec §15.1).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.publication_event_path)

    def append(self, events: list[PublicationEvent]) -> None:
        if not events:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for event in events:
                f.write(event.model_dump_json() + "\n")

    def list_events(self, *, job_id: str | None = None) -> list[PublicationEvent]:
        if not self._path.exists():
            return []
        events = [PublicationEvent.model_validate_json(line) for line in self._path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if job_id is not None:
            events = [e for e in events if e.job_id == job_id]
        return events


class GtmLeadStore:
    """The spec §14.4 "queryable table" — a JSONL append log here, same
    stand-in convention as every other table in this codebase. Each publish
    appends a fresh snapshot; `latest()` collapses to the most recent
    publish per `(company.domain, job.posting_url, lead.email or lead.name)`,
    since `GtmLead` carries no internal IDs to key on (spec §14.4:
    "consumers see `GtmLead` and nothing about how it was assembled").
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.gtm_lead_table_path)

    def append(self, gtm_leads: list[GtmLead]) -> None:
        if not gtm_leads:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            for lead in gtm_leads:
                f.write(lead.model_dump_json() + "\n")

    def list_all(self) -> list[GtmLead]:
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as f:
            return [GtmLead.model_validate_json(line) for line in f if line.strip()]

    def latest(self) -> list[GtmLead]:
        latest_by_key: dict[tuple[str, str, str], GtmLead] = {}
        for lead in self.list_all():
            key = (lead.company.domain, lead.job.posting_url, lead.lead.email or lead.lead.name)
            latest_by_key[key] = lead
        return list(latest_by_key.values())
