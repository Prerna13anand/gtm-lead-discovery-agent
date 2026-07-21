"""Cache for Stage 4's LLM title-residue classification — spec §7.3.

"Only titles the rules fail to classify go to an LLM call, and results are
cached by canonical title so each distinct title is classified once ever."
Same local-JSONL stand-in convention as every other store in this codebase;
keyed by `title_canonical` specifically (not job/company), since the whole
point is that identical titles across different companies never pay for a
second LLM call.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from gtm_agent.config import get_settings
from gtm_agent.models.common import JobFunction, Seniority


class TitleClassificationEntry(BaseModel):
    title_canonical: str
    function: JobFunction
    seniority: Seniority


class TitleClassificationCache:
    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path) if path is not None else Path(settings.title_classification_cache_path)

    def get(self, title_canonical: str) -> TitleClassificationEntry | None:
        if not self._path.exists():
            return None
        match: TitleClassificationEntry | None = None
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = TitleClassificationEntry.model_validate_json(line)
                if entry.title_canonical == title_canonical:
                    match = entry  # last write wins, same convention as every other store here
        return match

    def save(self, entry: TitleClassificationEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
