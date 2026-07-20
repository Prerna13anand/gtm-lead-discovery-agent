"""Phase 1 coverage metrics — spec §19.1.

Computes the five named coverage metrics directly from the `scrape_run`
ledger (core/run_ledger.py):

| Metric | Definition | Target |
| --- | --- | --- |
| Scrape success rate | `success` runs / attempted runs | > 90% |
| Source resolution rate | Companies with a validated careers source / total | > 95% |
| ATS coverage | Companies matched to an ATS adapter / resolved | > 75% |
| Degraded extraction rate | `parse_degraded` / successful | < 10% |
| Unscraped count | Companies in a non-success terminal state | absolute, never a percentage |

Scope: this is metrics *computation* only. No dashboards, no alerting
(spec §19.6), no scheduling, no Stage 5 change-detection/lifecycle logic —
all explicitly later-phase concerns, same scoping as core/run_ledger.py.

Aggregation level, read directly from the spec's own wording — this matters
and isn't arbitrary:
    - "Scrape success rate" and "Degraded extraction rate" are phrased in
      terms of *runs* ("success runs / attempted runs", "parse_degraded /
      successful") — computed over every closed run, not deduplicated by
      company. Re-attempting a company multiple times contributes multiple
      data points to these two.
    - "Source resolution rate", "ATS coverage", and "Unscraped count" are
      explicitly phrased in terms of *companies* ("Companies with a
      validated careers source / total", etc.) — computed over each
      company's most recent run, since these describe a company's *current*
      state, not its historical churn across retries.

Two definitions were read literally rather than "improved" with inferred
nuance, on the principle that the spec's own wording is the contract:
    - "Unscraped count" is defined as "Companies in a **non-success**
      terminal state" — literally `status != SUCCESS`. A more elaborate
      reading (e.g. excluding `parse_degraded`/`partial` because §17's
      *downstream* column calls those "published") would import a
      distinction the metric's own definition doesn't draw; the metric
      table's wording wins.
    - "Degraded extraction rate" denominator is "successful" — read as
      `status == SUCCESS` specifically (the same "success" §17/§19.1 use
      everywhere else), not "success-or-degraded". `parse_degraded` is its
      own distinct terminal status in this ledger (see `ScrapeRunStatus`),
      never a variant of `success`.
    Neither choice currently changes any real output: `parse_degraded` isn't
    reachable yet (the generic-HTML adapter that would produce it is a
    Phase 2 placeholder), so the degraded-rate numerator is always 0 today.
"""

from __future__ import annotations

from dataclasses import dataclass

from gtm_agent.models.scrape_run import ScrapeRun, ScrapeRunStatus

# Stage 1 outcomes that never reached a validated careers source (spec
# §4.2/§4.3: below-floor or failed-validation sources are excluded from
# automated runs — "stored but flagged", not validated).
_UNRESOLVED_SOURCE_STATUSES = frozenset(
    {
        ScrapeRunStatus.NO_CAREERS_PAGE,
        ScrapeRunStatus.RESOLUTION_UNVALIDATED,
        ScrapeRunStatus.DOMAIN_UNREACHABLE,
        ScrapeRunStatus.NEEDS_REVIEW,
    }
)

# The real ATS-API adapters (spec §22 Phase 1: greenhouse/lever/ashby;
# Phase 2 adds workable). JSON-LD and generic-HTML are fallback extraction
# paths, not "an ATS adapter".
_REAL_ATS_ADAPTERS = frozenset({"greenhouse", "lever", "ashby", "workable"})


@dataclass(frozen=True)
class CoverageMetrics:
    """The five spec §19.1 metrics, alongside the raw counts each is derived
    from — so a caller can see the denominator, not just a bare percentage.
    Rates are `None` (not `0.0`) when there's no data yet, so "no runs
    recorded" is never displayed as a misleading 0% failure rate.
    """

    attempted_runs: int
    successful_runs: int
    scrape_success_rate: float | None

    total_companies: int
    resolved_companies: int
    source_resolution_rate: float | None

    ats_matched_companies: int
    ats_coverage: float | None

    degraded_runs: int
    degraded_extraction_rate: float | None

    unscraped_count: int  # always absolute — spec §19.1


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _latest_run_per_company(runs: list[ScrapeRun]) -> dict[str, ScrapeRun]:
    """A company's *current* state is its most recent attempt, not every
    historical retry (see module docstring).
    """
    latest: dict[str, ScrapeRun] = {}
    for run in runs:
        current = latest.get(run.company_id)
        if current is None or run.started_at > current.started_at:
            latest[run.company_id] = run
    return latest


def compute_coverage_metrics(runs: list[ScrapeRun]) -> CoverageMetrics:
    """spec §19.1. Takes a list of `ScrapeRun` directly — normally
    `ScrapeRunLedger.list_runs()`'s output — rather than the ledger itself,
    so this stays a pure function, trivially testable without file I/O.
    """
    # Only closed (terminated) runs count as "attempted" — an open run
    # (status is None) hasn't reached an outcome yet, and including it would
    # misrepresent every ratio below (spec §17: "every run terminates in
    # exactly one typed status").
    closed_runs = [r for r in runs if r.status is not None]

    attempted_runs = len(closed_runs)
    successful_runs = sum(1 for r in closed_runs if r.status == ScrapeRunStatus.SUCCESS)
    scrape_success_rate = _rate(successful_runs, attempted_runs)

    degraded_runs = sum(1 for r in closed_runs if r.status == ScrapeRunStatus.PARSE_DEGRADED)
    degraded_extraction_rate = _rate(degraded_runs, successful_runs)

    latest_by_company = _latest_run_per_company(closed_runs)
    total_companies = len(latest_by_company)

    resolved_companies = sum(
        1 for r in latest_by_company.values() if r.status not in _UNRESOLVED_SOURCE_STATUSES
    )
    source_resolution_rate = _rate(resolved_companies, total_companies)

    ats_matched_companies = sum(
        1
        for r in latest_by_company.values()
        if r.status not in _UNRESOLVED_SOURCE_STATUSES and r.adapter_used in _REAL_ATS_ADAPTERS
    )
    ats_coverage = _rate(ats_matched_companies, resolved_companies)

    unscraped_count = sum(1 for r in latest_by_company.values() if r.status != ScrapeRunStatus.SUCCESS)

    return CoverageMetrics(
        attempted_runs=attempted_runs,
        successful_runs=successful_runs,
        scrape_success_rate=scrape_success_rate,
        total_companies=total_companies,
        resolved_companies=resolved_companies,
        source_resolution_rate=source_resolution_rate,
        ats_matched_companies=ats_matched_companies,
        ats_coverage=ats_coverage,
        degraded_runs=degraded_runs,
        degraded_extraction_rate=degraded_extraction_rate,
        unscraped_count=unscraped_count,
    )
