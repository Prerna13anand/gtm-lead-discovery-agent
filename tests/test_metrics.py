"""core.metrics tests — spec §19.1. Pure computation, no ledger/file I/O."""

from datetime import UTC, datetime, timedelta

from gtm_agent.core.metrics import compute_ats_unknown_frequency, compute_coverage_metrics
from gtm_agent.models.scrape_run import ScrapeRun, ScrapeRunStatus

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _run(
    company_id: str,
    status: ScrapeRunStatus | None,
    *,
    adapter_used: str | None = None,
    started_at: datetime = _T0,
    run_id: str | None = None,
) -> ScrapeRun:
    return ScrapeRun(
        id=run_id or f"{company_id}-{started_at.isoformat()}",
        company_id=company_id,
        started_at=started_at,
        finished_at=started_at if status is not None else None,
        status=status,
        adapter_used=adapter_used,
    )


def test_empty_ledger_yields_none_rates_and_zero_counts():
    result = compute_coverage_metrics([])

    assert result.attempted_runs == 0
    assert result.scrape_success_rate is None
    assert result.total_companies == 0
    assert result.source_resolution_rate is None
    assert result.ats_coverage is None
    assert result.degraded_extraction_rate is None
    assert result.unscraped_count == 0


def test_open_runs_are_excluded_from_attempted_count():
    # status=None means the run never terminated — spec §17: every run
    # terminates in exactly one typed status; an open one isn't "attempted" yet.
    runs = [_run("acme", None)]
    result = compute_coverage_metrics(runs)
    assert result.attempted_runs == 0
    assert result.total_companies == 0


def test_scrape_success_rate_is_success_runs_over_attempted_runs():
    runs = [
        _run("a", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse"),
        _run("b", ScrapeRunStatus.SUCCESS, adapter_used="lever"),
        _run("c", ScrapeRunStatus.NO_CAREERS_PAGE),
        _run("d", ScrapeRunStatus.DOMAIN_UNREACHABLE),
    ]
    result = compute_coverage_metrics(runs)
    assert result.attempted_runs == 4
    assert result.successful_runs == 2
    assert result.scrape_success_rate == 0.5


def test_scrape_success_rate_counts_every_run_not_deduplicated_by_company():
    # Spec phrases this metric in terms of *runs*, not companies — two
    # attempts at the same company contribute two data points.
    runs = [
        _run("acme", ScrapeRunStatus.NO_CAREERS_PAGE, started_at=_T0),
        _run("acme", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse", started_at=_T0 + timedelta(days=1)),
    ]
    result = compute_coverage_metrics(runs)
    assert result.attempted_runs == 2
    assert result.successful_runs == 1
    assert result.scrape_success_rate == 0.5


def test_source_resolution_rate_excludes_unresolved_statuses():
    runs = [
        _run("a", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse"),
        _run("b", ScrapeRunStatus.ATS_UNKNOWN),  # source resolved; Stage 2/3 failed after
        _run("c", ScrapeRunStatus.NO_CAREERS_PAGE),
        _run("d", ScrapeRunStatus.RESOLUTION_UNVALIDATED),
        _run("e", ScrapeRunStatus.DOMAIN_UNREACHABLE),
        _run("f", ScrapeRunStatus.NEEDS_REVIEW),
    ]
    result = compute_coverage_metrics(runs)
    assert result.total_companies == 6
    assert result.resolved_companies == 2  # only a and b resolved a source
    assert result.source_resolution_rate == 2 / 6


def test_ats_coverage_denominator_is_resolved_companies_not_total():
    runs = [
        _run("a", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse"),
        _run("b", ScrapeRunStatus.SUCCESS, adapter_used="jsonld"),  # resolved, but not an ATS adapter
        _run("c", ScrapeRunStatus.NO_CAREERS_PAGE),  # never resolved — excluded from this denominator
    ]
    result = compute_coverage_metrics(runs)
    assert result.resolved_companies == 2  # a, b (c never resolved)
    assert result.ats_matched_companies == 1  # only a
    assert result.ats_coverage == 1 / 2


def test_ats_coverage_recognises_all_three_real_adapters():
    runs = [
        _run("a", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse"),
        _run("b", ScrapeRunStatus.SUCCESS, adapter_used="lever"),
        _run("c", ScrapeRunStatus.SUCCESS, adapter_used="ashby"),
        _run("d", ScrapeRunStatus.SUCCESS, adapter_used="generic_html"),
    ]
    result = compute_coverage_metrics(runs)
    assert result.ats_matched_companies == 3
    assert result.ats_coverage == 3 / 4


def test_degraded_extraction_rate_is_degraded_over_successful_runs():
    runs = [
        _run("a", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse"),
        _run("b", ScrapeRunStatus.SUCCESS, adapter_used="lever"),
        _run("c", ScrapeRunStatus.PARSE_DEGRADED, adapter_used="generic_html"),
    ]
    result = compute_coverage_metrics(runs)
    assert result.degraded_runs == 1
    assert result.successful_runs == 2
    assert result.degraded_extraction_rate == 0.5


def test_unscraped_count_is_absolute_and_covers_every_non_success_status():
    runs = [
        _run("a", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse"),
        _run("b", ScrapeRunStatus.NO_CAREERS_PAGE),
        _run("c", ScrapeRunStatus.ATS_UNKNOWN),
        _run("d", ScrapeRunStatus.PARSE_DEGRADED, adapter_used="generic_html"),
    ]
    result = compute_coverage_metrics(runs)
    # literal spec wording: "Companies in a non-success terminal state"
    assert result.unscraped_count == 3


def test_unscraped_count_uses_latest_run_per_company_not_every_attempt():
    runs = [
        _run("acme", ScrapeRunStatus.NO_CAREERS_PAGE, started_at=_T0),
        _run("acme", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse", started_at=_T0 + timedelta(days=1)),
    ]
    result = compute_coverage_metrics(runs)
    # acme's *current* state is success, even though its first attempt failed
    assert result.unscraped_count == 0
    assert result.total_companies == 1


def test_latest_run_wins_regardless_of_input_order():
    older_success = _run("acme", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse", started_at=_T0)
    newer_failure = _run("acme", ScrapeRunStatus.DOMAIN_UNREACHABLE, started_at=_T0 + timedelta(days=1))

    result = compute_coverage_metrics([newer_failure, older_success])

    assert result.total_companies == 1
    assert result.unscraped_count == 1  # the newer, failing run reflects current state


# --- compute_ats_unknown_frequency (spec §22 Phase 5, §19.6) ---------------


def test_ats_unknown_frequency_counts_by_adapter_used():
    runs = [
        _run("a", ScrapeRunStatus.ATS_UNKNOWN, adapter_used="wellfound"),
        _run("b", ScrapeRunStatus.ATS_UNKNOWN, adapter_used="wellfound"),
        _run("c", ScrapeRunStatus.ATS_UNKNOWN, adapter_used="teamtailor"),
        _run("d", ScrapeRunStatus.SUCCESS, adapter_used="greenhouse"),
    ]
    assert compute_ats_unknown_frequency(runs) == {"wellfound": 2, "teamtailor": 1}


def test_ats_unknown_frequency_ignores_runs_with_no_adapter_identified():
    runs = [_run("a", ScrapeRunStatus.ATS_UNKNOWN, adapter_used=None)]
    assert compute_ats_unknown_frequency(runs) == {}


def test_ats_unknown_frequency_empty_for_no_runs():
    assert compute_ats_unknown_frequency([]) == {}
