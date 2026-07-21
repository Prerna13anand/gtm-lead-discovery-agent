"""Canary Suite persistence tests — spec §20.3. Uses tmp_path so nothing is
ever written to the real `.data/` directory during tests.
"""

from datetime import UTC, datetime
from pathlib import Path

from gtm_agent.core.canary_store import CanaryFindingLog, CanaryResultLog
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.canary import CanaryFinding, CanaryRunResult


def _result(company_id: str, *, jobs: int = 5) -> CanaryRunResult:
    return CanaryRunResult(
        id="r1", company_id=company_id, run_at=datetime.now(UTC), detected_platform=AtsPlatform.GREENHOUSE,
        extraction_status="success", job_count=jobs, adapter_used="greenhouse",
    )


# --- CanaryResultLog ---


def test_latest_per_target_on_fresh_log_is_empty(tmp_path: Path):
    log = CanaryResultLog(tmp_path / "canary_results.jsonl")
    assert log.latest_per_target() == {}


def test_append_and_latest_per_target_round_trips(tmp_path: Path):
    log = CanaryResultLog(tmp_path / "canary_results.jsonl")
    log.append([_result("acme", jobs=5)])

    latest = log.latest_per_target()
    assert set(latest) == {"acme"}
    assert latest["acme"].job_count == 5


def test_later_append_wins_as_latest(tmp_path: Path):
    log = CanaryResultLog(tmp_path / "canary_results.jsonl")
    log.append([_result("acme", jobs=5)])
    log.append([_result("acme", jobs=0)])

    assert log.latest_per_target()["acme"].job_count == 0


def test_empty_append_is_a_noop(tmp_path: Path):
    path = tmp_path / "canary_results.jsonl"
    log = CanaryResultLog(path)
    log.append([])
    assert not path.exists()


# --- CanaryFindingLog ---


def test_finding_log_fresh_is_empty(tmp_path: Path):
    log = CanaryFindingLog(tmp_path / "canary_findings.jsonl")
    assert log.list_findings() == []


def test_finding_log_round_trips(tmp_path: Path):
    finding = CanaryFinding(
        id="f1", company_id="acme", company_name="Acme", detected_at=datetime.now(UTC),
        reasons=["platform drift: expected lever, detected greenhouse"], previous=None, current=_result("acme"),
    )
    log = CanaryFindingLog(tmp_path / "findings.jsonl")
    log.append([finding])

    findings = log.list_findings()
    assert len(findings) == 1
    assert findings[0].company_id == "acme"
    assert findings[0].reasons == ["platform drift: expected lever, detected greenhouse"]
