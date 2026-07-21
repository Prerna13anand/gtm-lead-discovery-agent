"""Canary Suite tests (spec §20.3) — deterministic and offline (spec §20.1).
`check_target`/`run_canary_suite` reuse real Stage 2/3 logic (`identify_ats`,
`route_extraction`, the registered adapters), so these tests exercise that
real integration against a `FakeFetcher` serving canned responses — same
test-double pattern as the adapter test suites — rather than mocking
`check_target` itself.
"""

from datetime import UTC, datetime

import httpx
import pytest

from gtm_agent.core.fetch import FetchResult
from gtm_agent.discovery.canary import check_target, detect_drift, run_canary_suite
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.canary import CanaryRunResult, CanaryTarget
from gtm_agent.models.results import ExtractionStatus


def _result(url: str, status_code: int, text: str) -> FetchResult:
    return FetchResult(url=url, status_code=status_code, text=text, headers=httpx.Headers({}))


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResult] | None = None) -> None:
        self.responses = responses or {}
        self.requested_urls: list[str] = []

    async def get(self, url: str, **kwargs: object) -> FetchResult:
        self.requested_urls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected URL requested: {url}")
        return self.responses[url]


def _target(
    company_id: str = "acme",
    *,
    platform: AtsPlatform = AtsPlatform.GREENHOUSE,
    careers_url: str = "https://boards.greenhouse.io/acme",
) -> CanaryTarget:
    return CanaryTarget(
        company_id=company_id,
        company_name="Acme",
        domain="acme.com",
        careers_url=careers_url,
        expected_platform=platform,
    )


_GREENHOUSE_JOBS_JSON = '{"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"}], "meta": {"total": 1}}'
_GREENHOUSE_EMPTY_JSON = '{"jobs": [], "meta": {"total": 0}}'


def _greenhouse_fetcher(jobs_json: str = _GREENHOUSE_JOBS_JSON, status: int = 200) -> FakeFetcher:
    return FakeFetcher(
        {
            "https://boards.greenhouse.io/acme": _result("https://boards.greenhouse.io/acme", 200, "<html></html>"),
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": _result(
                "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", status, jobs_json
            ),
        }
    )


# --- check_target ---


async def test_check_target_identifies_platform_and_job_count():
    result = await check_target(_target(), _greenhouse_fetcher())
    assert result.detected_platform == AtsPlatform.GREENHOUSE
    assert result.extraction_status == ExtractionStatus.SUCCESS.value
    assert result.job_count == 1
    assert result.company_id == "acme"


async def test_check_target_zero_jobs():
    result = await check_target(_target(), _greenhouse_fetcher(_GREENHOUSE_EMPTY_JSON))
    assert result.job_count == 0
    assert result.extraction_status == ExtractionStatus.SUCCESS.value


async def test_check_target_records_a_failure_status():
    result = await check_target(_target(), _greenhouse_fetcher(status=403))
    assert result.extraction_status == ExtractionStatus.BLOCKED_403.value
    assert result.job_count == 0


# --- detect_drift ---


def _canary_result(*, platform=AtsPlatform.GREENHOUSE, status=ExtractionStatus.SUCCESS, jobs=5) -> CanaryRunResult:
    return CanaryRunResult(
        id="r1", company_id="acme", run_at=datetime.now(UTC), detected_platform=platform,
        extraction_status=status.value, job_count=jobs, adapter_used=platform.value,
    )


def test_no_drift_when_platform_matches_and_nothing_previous():
    current = _canary_result()
    assert detect_drift(_target(), None, current) == []


def test_drift_when_detected_platform_differs_from_expected():
    current = _canary_result(platform=AtsPlatform.LEVER)
    reasons = detect_drift(_target(platform=AtsPlatform.GREENHOUSE), None, current)
    assert any("expected greenhouse" in r for r in reasons)


def test_no_drift_when_nothing_changed_since_previous():
    previous = _canary_result(jobs=5)
    current = _canary_result(jobs=6)  # job count moving is not drift by itself
    assert detect_drift(_target(), previous, current) == []


def test_drift_when_platform_changes_since_previous_run():
    previous = _canary_result(platform=AtsPlatform.GREENHOUSE)
    current = _canary_result(platform=AtsPlatform.GREENHOUSE)
    # Target itself migrated ATS platform between canary runs.
    current2 = current.model_copy(update={"detected_platform": AtsPlatform.ASHBY})
    reasons = detect_drift(_target(), previous, current2)
    assert any("platform changed since last canary run" in r for r in reasons)


def test_drift_when_extraction_status_regresses():
    previous = _canary_result(status=ExtractionStatus.SUCCESS)
    current = _canary_result(status=ExtractionStatus.SCHEMA_VIOLATION)
    reasons = detect_drift(_target(), previous, current)
    assert any("extraction status regressed" in r for r in reasons)


def test_drift_when_job_count_drops_to_zero_after_being_nonzero():
    previous = _canary_result(jobs=12, status=ExtractionStatus.SUCCESS)
    current = _canary_result(jobs=0, status=ExtractionStatus.SUCCESS)
    reasons = detect_drift(_target(), previous, current)
    assert any("job count dropped to zero (was 12)" in r for r in reasons)


def test_no_duplicate_job_count_reason_when_status_also_regressed():
    # A blocked/failed run naturally has 0 jobs -- that's already covered by
    # the status-regression reason, not a separate "job count" complaint.
    previous = _canary_result(jobs=12, status=ExtractionStatus.SUCCESS)
    current = _canary_result(jobs=0, status=ExtractionStatus.BLOCKED_403)
    reasons = detect_drift(_target(), previous, current)
    assert any("extraction status regressed" in r for r in reasons)
    assert not any("job count dropped" in r for r in reasons)


def test_parse_degraded_counts_as_healthy_not_drift():
    previous = _canary_result(status=ExtractionStatus.SUCCESS, jobs=5)
    current = _canary_result(status=ExtractionStatus.PARSE_DEGRADED, jobs=5)
    assert detect_drift(_target(), previous, current) == []


# --- run_canary_suite ---


async def test_run_canary_suite_records_result_and_finding_for_drifted_target():
    target = _target(platform=AtsPlatform.LEVER)  # expects Lever, board is actually Greenhouse
    fetcher = _greenhouse_fetcher()
    report = await run_canary_suite([target], fetcher=fetcher, previous_results={})

    assert len(report.results) == 1
    assert report.results[0].detected_platform == AtsPlatform.GREENHOUSE
    assert len(report.findings) == 1
    assert report.findings[0].company_id == "acme"
    assert any("expected lever" in r for r in report.findings[0].reasons)


async def test_run_canary_suite_no_findings_when_healthy_and_matching():
    target = _target(platform=AtsPlatform.GREENHOUSE)
    fetcher = _greenhouse_fetcher()
    report = await run_canary_suite([target], fetcher=fetcher, previous_results={})

    assert len(report.results) == 1
    assert report.findings == []


async def test_run_canary_suite_checks_every_target_independently():
    targets = [
        _target("acme", platform=AtsPlatform.GREENHOUSE, careers_url="https://boards.greenhouse.io/acme"),
        _target("beta", platform=AtsPlatform.GREENHOUSE, careers_url="https://boards.greenhouse.io/beta"),
    ]
    fetcher = FakeFetcher(
        {
            "https://boards.greenhouse.io/acme": _result("https://boards.greenhouse.io/acme", 200, "<html></html>"),
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": _result(
                "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", 200, _GREENHOUSE_JOBS_JSON
            ),
            "https://boards.greenhouse.io/beta": _result("https://boards.greenhouse.io/beta", 200, "<html></html>"),
            "https://boards-api.greenhouse.io/v1/boards/beta/jobs?content=true": _result(
                "https://boards-api.greenhouse.io/v1/boards/beta/jobs?content=true", 200, _GREENHOUSE_EMPTY_JSON
            ),
        }
    )
    report = await run_canary_suite(targets, fetcher=fetcher, previous_results={})
    assert {r.company_id for r in report.results} == {"acme", "beta"}
    by_company = {r.company_id: r for r in report.results}
    assert by_company["acme"].job_count == 1
    assert by_company["beta"].job_count == 0
