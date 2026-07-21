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
    Both choices were made before the generic-HTML and rendered-DOM adapters
    existed to actually produce a `parse_degraded` run; now that they do,
    the distinction is live, not just principled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gtm_agent.models.job import JobPosting
from gtm_agent.models.lead import LeadRecord
from gtm_agent.models.matching import LeadJobMatch, UnmatchedJob, UnmatchedReason
from gtm_agent.models.scoring import ScoredLead
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
# Phase 2 adds workable, smartrecruiters, recruitee, rippling). JSON-LD and
# generic-HTML are fallback extraction paths, not "an ATS adapter".
_REAL_ATS_ADAPTERS = frozenset(
    {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "rippling"}
)


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


# --- Adapter-expansion prioritisation — spec §22 Phase 5, §19.6 ------------


def compute_ats_unknown_frequency(runs: list[ScrapeRun]) -> dict[str, int]:
    """Spec §22 Phase 5: "adapter expansion driven by `ats_unknown`
    frequency." Spec §19.6's alert: "New `ats_unknown` platform seen >= 5
    times -> Ticket — build the adapter." `adapter_used` is set even on an
    `ATS_UNKNOWN` run (`main.py` sets it to the routed-but-unregistered
    platform, or `None` if fingerprinting itself never identified one) —
    this counts by that value, so a *specific* unrecognised platform can be
    told apart from a genuinely unidentifiable board, rather than lumping
    every `ats_unknown` run into one undifferentiated count.

    This is a *prioritisation* input, not itself an answer to spec open
    question #1 ("what is the actual ATS distribution across the target
    list?") — that question needs a real company list to fingerprint,
    which this codebase doesn't have (see project report).
    """
    counts: dict[str, int] = {}
    for run in runs:
        if run.status == ScrapeRunStatus.ATS_UNKNOWN and run.adapter_used:
            counts[run.adapter_used] = counts.get(run.adapter_used, 0) + 1
    return counts


# --- Part II/III metrics — spec §19.3 (Lead discovery & matching) ----------


@dataclass(frozen=True)
class MatchingMetrics:
    """A subset of spec §19.3's table — the metrics directly computable
    from this codebase's own persisted records without a human-labelled
    input (golden-set accuracy, e.g., needs `tests/fixtures/matching_golden_set.json`
    or a real equivalent, and is exercised as a test, not a runtime metric).
    """

    jobs_with_lead_count: int
    jobs_without_lead_count: int
    jobs_with_lead_rate: float | None
    """Spec §19.3: "SDD §13.3's headline metric. The end-to-end yield of Part II." """

    no_plausible_owner_by_function: dict[str, int] = field(default_factory=dict)
    """Spec §19.3: "Per-function breakdown is what exposes `persona_gap`
    (§17.2); the aggregate hides it." Keyed by `JobFunction` value; a
    function with a repeatedly-high count here is `persona_gap` (§10.6,
    §17.2), not ordinary noise — see `leads.persona_gap` (Phase 5).
    """

    founder_match_share: float | None = None
    """Spec §19.3: "matches where the top lead is a founder... Should be
    high at low headcount and fall as headcount rises — if it doesn't, the
    modulation is miscalibrated" (§10.4 sanity check).
    """


def compute_matching_metrics(
    *,
    matches: list[LeadJobMatch],
    unmatched: list[UnmatchedJob],
    jobs_by_id: dict[str, JobPosting],
    leads_by_id: dict[str, LeadRecord],
) -> MatchingMetrics:
    """Pure function over already-persisted Stage 7 output — the caller
    reads `LeadJobMatchStore`/`UnmatchedJobStore` and passes the results in,
    same convention as `compute_coverage_metrics` over `ScrapeRunLedger`.
    """
    top_matches = [m for m in matches if m.rank_within_job == 1]
    jobs_with_lead = {m.job_id for m in matches}
    jobs_without_lead = {u.job_id for u in unmatched} - jobs_with_lead

    jobs_with_lead_count = len(jobs_with_lead)
    jobs_without_lead_count = len(jobs_without_lead)
    total_jobs = jobs_with_lead_count + jobs_without_lead_count
    jobs_with_lead_rate = _rate(jobs_with_lead_count, total_jobs)

    no_plausible_owner_by_function: dict[str, int] = {}
    for entry in unmatched:
        if entry.reason != UnmatchedReason.NO_PLAUSIBLE_OWNER:
            continue
        job = jobs_by_id.get(entry.job_id)
        function_label = job.function.value if job and job.function else "unknown"
        no_plausible_owner_by_function[function_label] = no_plausible_owner_by_function.get(function_label, 0) + 1

    founder_top_matches = sum(
        1 for m in top_matches if (lead := leads_by_id.get(m.lead_id)) is not None and lead.is_founder
    )
    founder_match_share = _rate(founder_top_matches, len(top_matches))

    return MatchingMetrics(
        jobs_with_lead_count=jobs_with_lead_count,
        jobs_without_lead_count=jobs_without_lead_count,
        jobs_with_lead_rate=jobs_with_lead_rate,
        no_plausible_owner_by_function=no_plausible_owner_by_function,
        founder_match_share=founder_match_share,
    )


def compute_disagreement_rate(scored_leads: list[ScoredLead]) -> float | None:
    """Spec §13.4 / §19.3: "`disagrees_with_rules` rate | LLM departures from
    rules score | Rising or directional = rules drifting from reality."
    """
    if not scored_leads:
        return None
    disagreements = sum(1 for s in scored_leads if s.disagrees_with_rules)
    return _rate(disagreements, len(scored_leads))
