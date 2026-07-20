"""Regression tests for the Stage 3 -> Stage 4 handoff logic in `main.py`.

Covers the bug found in code review: the CLI harness used to treat any
`ExtractionStatus` other than `SUCCESS` as a hard failure and return before
ever calling `normalize_batch()` — which silently discarded every posting a
`PARSE_DEGRADED` extraction (Generic-HTML, or Rendered-DOM's DOM-link
fallback) actually found, contradicting spec §17's "Published with low
confidence + flag." `main.py` isn't otherwise unit-tested (it's a Click I/O
harness over `ScrapeRunLedger`'s real JSONL file), so this targets the two
small, pure functions the fix was written as, rather than the full CLI.

`main` is importable directly because `tests/__init__.py` makes `tests` a
package, which puts the project root (where `main.py` lives) on `sys.path`
under pytest's default import mode.
"""

from datetime import UTC, datetime

from main import _extraction_reached_stage4, _final_run_status
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult
from gtm_agent.models.scrape_run import ScrapeRunStatus


def _posting() -> RawPosting:
    return RawPosting(
        company_id="acme",
        source_platform="generic_html",
        posting_url="https://acme.com/careers/engineer",
        raw_payload={"title": "Engineer"},
        fetched_at=datetime.now(UTC),
        is_hydrated=False,
    )


# --- _extraction_reached_stage4 ---


def test_success_with_postings_reaches_stage4():
    result = StageResult(status=ExtractionStatus.SUCCESS, value=[_posting()])
    assert _extraction_reached_stage4(result) is True


def test_success_with_empty_list_reaches_stage4():
    # A validated, empty board is a real result (spec §2.3) -- still proceeds.
    result = StageResult(status=ExtractionStatus.SUCCESS, value=[])
    assert _extraction_reached_stage4(result) is True


def test_parse_degraded_with_postings_reaches_stage4():
    # This is the exact bug: PARSE_DEGRADED carries real postings and must
    # not be treated as a hard failure.
    result = StageResult(status=ExtractionStatus.PARSE_DEGRADED, value=[_posting()])
    assert _extraction_reached_stage4(result) is True


def test_parse_degraded_with_empty_list_reaches_stage4():
    result = StageResult(status=ExtractionStatus.PARSE_DEGRADED, value=[])
    assert _extraction_reached_stage4(result) is True


def test_blocked_403_does_not_reach_stage4():
    result = StageResult(status=ExtractionStatus.BLOCKED_403, value=None)
    assert _extraction_reached_stage4(result) is False


def test_schema_violation_does_not_reach_stage4():
    result = StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, value=None)
    assert _extraction_reached_stage4(result) is False


def test_rate_limited_does_not_reach_stage4():
    result = StageResult(status=ExtractionStatus.RATE_LIMITED, value=None)
    assert _extraction_reached_stage4(result) is False


def test_render_timeout_does_not_reach_stage4():
    result = StageResult(status=ExtractionStatus.RENDER_TIMEOUT, value=None)
    assert _extraction_reached_stage4(result) is False


def test_board_not_found_does_not_reach_stage4():
    result = StageResult(status=ExtractionStatus.BOARD_NOT_FOUND, value=None)
    assert _extraction_reached_stage4(result) is False


# --- _final_run_status ---


def test_final_run_status_success_stays_success():
    assert _final_run_status(ExtractionStatus.SUCCESS) == ScrapeRunStatus.SUCCESS


def test_final_run_status_parse_degraded_stays_degraded_not_success():
    # The ledger must be able to tell a degraded run apart from a clean one
    # (spec §17) -- it must never be reported as an indistinguishable success.
    assert _final_run_status(ExtractionStatus.PARSE_DEGRADED) == ScrapeRunStatus.PARSE_DEGRADED
