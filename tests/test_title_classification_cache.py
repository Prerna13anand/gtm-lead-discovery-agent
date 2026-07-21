"""Title classification cache tests — spec §7.3."""

from pathlib import Path

from gtm_agent.core.title_classification_cache import TitleClassificationCache, TitleClassificationEntry
from gtm_agent.models.common import JobFunction, Seniority


def test_get_on_fresh_cache_is_none(tmp_path: Path):
    cache = TitleClassificationCache(tmp_path / "cache.jsonl")
    assert cache.get("Growth Ninja") is None


def test_save_and_get_round_trips(tmp_path: Path):
    cache = TitleClassificationCache(tmp_path / "cache.jsonl")
    cache.save(TitleClassificationEntry(title_canonical="Growth Ninja", function=JobFunction.MARKETING, seniority=Seniority.MID))
    entry = cache.get("Growth Ninja")
    assert entry is not None
    assert entry.function == JobFunction.MARKETING
    assert entry.seniority == Seniority.MID


def test_last_write_wins_for_same_title(tmp_path: Path):
    cache = TitleClassificationCache(tmp_path / "cache.jsonl")
    cache.save(TitleClassificationEntry(title_canonical="X", function=JobFunction.SALES, seniority=Seniority.ENTRY))
    cache.save(TitleClassificationEntry(title_canonical="X", function=JobFunction.MARKETING, seniority=Seniority.SENIOR))
    entry = cache.get("X")
    assert entry.function == JobFunction.MARKETING
