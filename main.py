#!/usr/bin/env python
"""CLI entry point — runs Stages 1-9 for a single company (spec §3.2, Parts I-II).

This is a development/demo harness, not the sweep orchestrator described in
spec §16 (which is later-phase work — no scheduling, no concurrency across
companies, no cadence tiering). It exists to prove the pipeline is wired
correctly end to end: source resolution -> ATS fingerprinting -> extraction
-> normalisation -> change detection & lifecycle (Part I) -> lead discovery
-> matching -> enrichment -> company context (Part II, Phase 3).

Every invocation records exactly one `ScrapeRun` (spec §15.1) regardless of
where it terminates — a Stage 1 failure is still "one company per attempt",
not a silent non-event (spec §2.3). See core/run_ledger.py.

Stage 5 (spec §8) persists lifecycle state across invocations via
`core.lifecycle_store` — unlike `ScrapeRunLedger`, which is a pure append log
never read back mid-flow, `JobPostingStore.current_records` is read at the
*start* of Stage 5 specifically so this run can diff against the last one.
Because this harness has no scheduler, "the last run" means the last time
this exact CLI command was invoked for this company — a real sweep (spec
§16) would call the same Stage 5 functions with the same semantics, just on
a schedule instead of on demand. Part II (`_process_part2`) follows the
identical convention: `LeadStore`/`CompanyContextStore` are read at the start
of Stages 6/9 for the same reason.

Part II only runs when Part I found at least one currently-open job (spec
§16.1: "A Part I failure returns early. No jobs means nothing to match").
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# Windows consoles default stdout/stderr to a legacy codepage (e.g. cp1252),
# which can't encode plenty of real, legitimate characters that show up in
# job postings (e.g. U+2011 NON-BREAKING HYPHEN in "Front‑End Engineer").
# The data itself is correct UTF-8 all the way through the pipeline; this
# only affects how the CLI writes to the terminal. `errors="replace"` is a
# last-resort safety net for any codepoint even UTF-8-aware terminals can't
# render, so a rare glyph degrades to `?` instead of crashing the process.
#
# Guarded on all platforms, not just Windows: `sys.stdout`/`sys.stderr` can be
# `None` (e.g. a windowed/frozen app with no console) or lack `.reconfigure()`
# entirely (e.g. redirected to a plain object such as `io.StringIO` by a test
# harness) — on Linux/macOS this block is normally a no-op anyway, since
# those typically report `utf-8` already, but the guards apply everywhere,
# not just the Windows case. The `try/except` is a last-resort net for any
# other platform-specific `reconfigure()` failure — this is startup plumbing
# and must never be what crashes the CLI.
for _stream in (sys.stdout, sys.stderr):
    if (
        _stream is not None
        and hasattr(_stream, "reconfigure")
        and getattr(_stream, "encoding", None)
        and _stream.encoding.lower() != "utf-8"
    ):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # best-effort — never let console setup crash the CLI
del _stream

sys.path.insert(0, str(Path(__file__).parent / "src"))

import click  # noqa: E402

from gtm_agent.core.canary_store import CanaryFindingLog, CanaryResultLog  # noqa: E402
from gtm_agent.core.fetch import Fetcher  # noqa: E402
from gtm_agent.config import get_settings  # noqa: E402
from gtm_agent.core.compliance_store import CompanyDenylistStore, PersonSuppressionStore  # noqa: E402
from gtm_agent.core.lead_store import (  # noqa: E402
    CompanyContextStore,
    LeadDiscoveryRunLedger,
    LeadJobMatchStore,
    LeadStore,
    UnmatchedJobStore,
)
from gtm_agent.core.lifecycle_store import JobPostingStore, JobPostingVersionLedger, ScrapeEventLog  # noqa: E402
from gtm_agent.core.logging import configure_logging, get_logger  # noqa: E402
from gtm_agent.core.metrics import (  # noqa: E402
    CoverageMetrics,
    compute_coverage_metrics,
    compute_disagreement_rate,
)
from gtm_agent.core.run_ledger import ScrapeRunLedger, archive_raw_payloads  # noqa: E402
from gtm_agent.core.scoring_store import GtmLeadStore, PublicationEventStore, ScoredLeadStore  # noqa: E402
from gtm_agent.discovery.ats_detection import identify_ats, route_extraction  # noqa: E402
from gtm_agent.discovery.canary import run_canary_suite  # noqa: E402
from gtm_agent.discovery.canary_targets import CANARY_TARGETS  # noqa: E402
from gtm_agent.discovery.extraction import get_adapter  # noqa: E402
from gtm_agent.discovery.lifecycle import ZeroJobsDecision, previously_open_count, run_stage5  # noqa: E402
from gtm_agent.discovery.llm_residue import resolve_unclassified  # noqa: E402
from gtm_agent.discovery.normalization import normalize_batch  # noqa: E402
from gtm_agent.discovery.source_resolution import resolve_source  # noqa: E402
from gtm_agent.leads.budget import CreditBudget  # noqa: E402
from gtm_agent.leads.company_context import is_context_stale, run_stage9  # noqa: E402
from gtm_agent.leads.compliance import filter_suppressed, suppression_key  # noqa: E402
from gtm_agent.leads.discovery import needs_refresh, run_stage6  # noqa: E402
from gtm_agent.leads.enrichment import run_stage8  # noqa: E402
from gtm_agent.leads.matching import match as run_stage7  # noqa: E402
from gtm_agent.leads.tie_break import resolve_tie_breaks  # noqa: E402
from gtm_agent.models.ats import AtsPlatform  # noqa: E402
from gtm_agent.models.company import Company  # noqa: E402
from gtm_agent.models.job import RawPosting  # noqa: E402
from gtm_agent.models.lead import LeadDiscoveryStatus, LeadRecord  # noqa: E402
from gtm_agent.models.lifecycle import JobLifecycleStatus, JobPostingRecord  # noqa: E402
from gtm_agent.models.matching import UnmatchedReason  # noqa: E402
from gtm_agent.models.results import (  # noqa: E402
    AtsFingerprintStatus,
    ExtractionStatus,
    SourceResolutionStatus,
    StageResult,
)
from gtm_agent.models.scoring import ScoredLead, ScoringStatus  # noqa: E402
from gtm_agent.models.scrape_run import ScrapeRun, ScrapeRunStatus  # noqa: E402
from gtm_agent.scoring.publication import publish, publish_unmatched, write_csv  # noqa: E402
from gtm_agent.scoring.ranking import rank  # noqa: E402
from gtm_agent.scoring.rationale import PROMPT_VERSION, fallback_scored_lead, score_pair  # noqa: E402
from gtm_agent.services.azure_openai import AzureOpenAIService  # noqa: E402

logger = get_logger(__name__)

# Stage 1 -> scrape_run status. Direct 1:1 mapping — every SourceResolutionStatus
# value that isn't RESOLVED corresponds exactly to a §17/§16.1 scrape_run status.
_SOURCE_STATUS_TO_RUN_STATUS: dict[SourceResolutionStatus, ScrapeRunStatus] = {
    SourceResolutionStatus.NO_CAREERS_PAGE: ScrapeRunStatus.NO_CAREERS_PAGE,
    SourceResolutionStatus.RESOLUTION_UNVALIDATED: ScrapeRunStatus.RESOLUTION_UNVALIDATED,
    SourceResolutionStatus.DOMAIN_UNREACHABLE: ScrapeRunStatus.DOMAIN_UNREACHABLE,
    SourceResolutionStatus.NEEDS_REVIEW: ScrapeRunStatus.NEEDS_REVIEW,
}

# Stage 3 -> scrape_run status. `BOARD_NOT_FOUND` and `NOT_IMPLEMENTED` aren't
# in spec §17 at all: `BOARD_NOT_FOUND` is this codebase's own addition from
# Phases 2A-2C (a stale/wrong board token on an otherwise-identified ATS
# platform), and `NOT_IMPLEMENTED` only fires today via the generic-HTML
# placeholder. Both map onto `ats_unknown`, whose §17 definition — "No adapter
# matched; generic fallback also failed" — is the closest existing category:
# from the ledger's perspective, both mean "no working extraction path exists
# for this company," which is exactly what `ats_unknown` records.
_EXTRACTION_STATUS_TO_RUN_STATUS: dict[ExtractionStatus, ScrapeRunStatus] = {
    ExtractionStatus.BOARD_NOT_FOUND: ScrapeRunStatus.ATS_UNKNOWN,
    ExtractionStatus.NOT_IMPLEMENTED: ScrapeRunStatus.ATS_UNKNOWN,
    ExtractionStatus.BLOCKED_403: ScrapeRunStatus.BLOCKED_403,
    ExtractionStatus.SCHEMA_VIOLATION: ScrapeRunStatus.SCHEMA_VIOLATION,
    ExtractionStatus.RATE_LIMITED: ScrapeRunStatus.RATE_LIMITED,
    ExtractionStatus.PARSE_DEGRADED: ScrapeRunStatus.PARSE_DEGRADED,
}


def _extraction_reached_stage4(extraction_result: StageResult[list[RawPosting], ExtractionStatus]) -> bool:
    """Should Stage 3's output proceed into Stage 4 normalisation?

    Only a genuine failure — no usable postings at all — should stop the
    pipeline here. `PARSE_DEGRADED` still carries a real (if heuristically
    extracted) posting list and must be published per spec §17: "Published
    with low confidence + flag" — not silently discarded like a hard failure
    (`BLOCKED_403`, `SCHEMA_VIOLATION`, ...), none of which ever set `.value`.
    Checking `.value is not None` is therefore equivalent to, but more
    direct than, enumerating every non-failure `ExtractionStatus` by name.
    """
    return extraction_result.value is not None


def _final_run_status(extraction_status: ExtractionStatus) -> ScrapeRunStatus:
    """The `scrape_run` status once Stage 4 has actually run. A degraded
    extraction stays visibly degraded in the ledger (spec §17) rather than
    being reported as an indistinguishable `success`.
    """
    if extraction_status == ExtractionStatus.PARSE_DEGRADED:
        return ScrapeRunStatus.PARSE_DEGRADED
    return ScrapeRunStatus.SUCCESS


@click.group()
def cli() -> None:
    """GTM Lead Discovery Agent CLI (Phases 1-5)."""
    configure_logging()


@cli.command()
@click.option("--domain", required=True, help="Company domain, e.g. example.com")
@click.option("--name", "company_name", required=True, help="Company display name")
@click.option(
    "--manual-careers-url",
    default=None,
    help="Skip resolution and use this URL directly (spec §4.1 Strategy E)",
)
def discover(domain: str, company_name: str, manual_careers_url: str | None) -> None:
    """Run Stages 1-4 for one company and print the resulting job postings."""
    asyncio.run(_discover(domain, company_name, manual_careers_url))


def _print_run_summary(run: ScrapeRun) -> None:
    click.echo("\n== scrape_run ledger ==")
    click.echo(f"  run_id: {run.id}")
    click.echo(f"  status: {run.status.value if run.status else None}")
    if run.failure_detail:
        click.echo(f"  failure_detail: {run.failure_detail}")
    click.echo(f"  adapter_used: {run.adapter_used}")
    click.echo(f"  jobs_found: {run.jobs_found}")
    click.echo(f"  http_requests_made: {run.http_requests_made}  bytes_fetched: {run.bytes_fetched}")
    click.echo(f"  used_rendering: {run.used_rendering}")
    click.echo(f"  raw_payload_ref: {run.raw_payload_ref}")
    duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
    click.echo(f"  started_at: {run.started_at}  finished_at: {run.finished_at}  ({duration:.3f}s)"
               if duration is not None else f"  started_at: {run.started_at}")


async def _discover(domain: str, company_name: str, manual_careers_url: str | None) -> None:
    company = Company(id=domain, name=company_name, domain=domain, added_at=datetime.now(UTC))

    # Spec §21.6: "A domain denylist checked at stage 1, honoured
    # immediately, never re-resolved." Checked before Stage 1 even begins —
    # no `scrape_run` row is recorded for a denylisted company; it isn't an
    # attempt that failed, it's a company excluded from scraping entirely
    # by an operator decision, a different kind of absence than anything
    # §17's failure taxonomy models.
    if CompanyDenylistStore().is_denied(domain):
        click.echo(f"'{domain}' is on the company denylist (spec §21.6) — skipping, not scraping.")
        return

    ledger = ScrapeRunLedger()

    async with Fetcher() as fetcher:
        # One scrape_run per company per execution attempt, opened before Stage
        # 1 is even tried — a Stage 1 failure is still an attempt, and must stay
        # visible in the ledger (spec §2.3), not vanish because nothing was ever
        # recorded for it.
        run = ledger.begin_run(company.id)
        request_baseline = fetcher.request_count
        bytes_baseline = fetcher.bytes_fetched

        def _counts() -> tuple[int, int]:
            return fetcher.request_count - request_baseline, fetcher.bytes_fetched - bytes_baseline

        click.echo(f"== Stage 1: Source Resolution ({domain}) ==")
        source_result = await resolve_source(company, fetcher, manual_override_url=manual_careers_url)

        if source_result.value is not None:
            run.source_id = source_result.value.careers_url

        if source_result.status != SourceResolutionStatus.RESOLVED or source_result.value is None:
            click.echo(f"  status: {source_result.status.value}")
            if source_result.detail:
                click.echo(f"  detail: {source_result.detail}")
            click.echo(
                "  No careers source resolved — stopping (spec §2.3: this is "
                "'unscraped', never 'no jobs')."
            )
            requests_made, bytes_made = _counts()
            run = ledger.close_run(
                run,
                status=_SOURCE_STATUS_TO_RUN_STATUS[source_result.status],
                failure_detail=source_result.detail,
                http_requests_made=requests_made,
                bytes_fetched=bytes_made,
            )
            _print_run_summary(run)
            return

        source = source_result.value
        click.echo(f"  resolved: {source.careers_url}")
        click.echo(
            f"  strategy: {source.resolution_strategy.value}  confidence: {source.resolution_confidence:.2f}"
        )
        if source.needs_review:
            click.echo("  NOTE: flagged needs_review — would be excluded from automated runs")

        click.echo("\n== Stage 2: ATS Fingerprinting ==")
        ats_result = await identify_ats(source, fetcher)
        if ats_result.status != AtsFingerprintStatus.IDENTIFIED or ats_result.value is None:
            click.echo(f"  status: {ats_result.status.value}")
            if ats_result.detail:
                click.echo(f"  detail: {ats_result.detail}")
        else:
            identification = ats_result.value
            click.echo(f"  platform: {identification.platform.value}  token: {identification.board_token}")
            click.echo(
                f"  signal: {identification.detection_signal.value}  confidence: {identification.confidence:.2f}"
            )

        click.echo("\n== Stage 3: Extraction ==")
        try:
            # `use_cache=False`: this routing peek and Stage 2's own
            # fingerprinting fetch both read `source.careers_url` moments
            # before the chosen adapter reads it again for real — without
            # opting out of conditional caching here, that third read can
            # see a spurious 304 (a live-verified bug; see
            # `core.fetch.Fetcher._request`'s docstring).
            page = await fetcher.get(source.careers_url, use_cache=False)
            page_html: str | None = page.text
        except Exception:  # noqa: BLE001 — best-effort fetch for routing only
            page_html = None

        routed_platform = route_extraction(ats_result.value, page_html)
        click.echo(f"  routed to adapter: {routed_platform.value}")

        adapter = get_adapter(routed_platform)
        if adapter is None:
            click.echo("  No adapter registered for this platform.")
            requests_made, bytes_made = _counts()
            run = ledger.close_run(
                run,
                status=ScrapeRunStatus.ATS_UNKNOWN,
                failure_detail=f"no adapter registered for platform {routed_platform.value}",
                adapter_used=routed_platform.value,
                http_requests_made=requests_made,
                bytes_fetched=bytes_made,
            )
            _print_run_summary(run)
            return

        extraction_result = await adapter.discover(source, fetcher)
        if not _extraction_reached_stage4(extraction_result):
            click.echo(f"  status: {extraction_result.status.value}")
            if extraction_result.detail:
                click.echo(f"  detail: {extraction_result.detail}")
            requests_made, bytes_made = _counts()
            run = ledger.close_run(
                run,
                status=_EXTRACTION_STATUS_TO_RUN_STATUS.get(extraction_result.status, ScrapeRunStatus.ATS_UNKNOWN),
                failure_detail=extraction_result.detail,
                adapter_used=routed_platform.value,
                http_requests_made=requests_made,
                bytes_fetched=bytes_made,
            )
            _print_run_summary(run)
            return

        raw_postings = extraction_result.value
        click.echo(f"  discovered {len(raw_postings)} raw posting(s)")
        if extraction_result.status == ExtractionStatus.PARSE_DEGRADED:
            click.echo("  NOTE: parse_degraded — heuristic extraction, publishing with reduced confidence")

        click.echo("\n== Stage 4: Normalisation ==")
        job_postings = normalize_batch(raw_postings) if raw_postings else []
        if not raw_postings:
            click.echo("  (no postings to normalise — a validated, empty board is a real result, per spec §2.3)")

        # Spec §7.3: "Only titles the rules fail to classify go to an LLM
        # call, and results are cached by canonical title." Optional — a
        # missing/unconfigured Azure OpenAI client degrades to Phase 1-3's
        # original behaviour (unclassified titles stay `None`), never a
        # hard failure of Stage 4 itself.
        unclassified_count = sum(1 for j in job_postings if j.function is None or j.seniority is None)
        if unclassified_count and AzureOpenAIService().is_configured:
            job_postings = await resolve_unclassified(job_postings)
            click.echo(f"  LLM residue classification: resolved up to {unclassified_count} unclassified title(s)")

        for job in job_postings:
            click.echo(f"  - {job.title_canonical or '(untitled)'}  [{job.function}/{job.seniority}]")
            click.echo(f"      location: {job.location_raw}  workplace: {job.workplace_type}")
            click.echo(f"      posted_at: {job.posted_at} (inferred={job.posted_at_is_inferred})")
            click.echo(f"      degraded: {job.is_degraded}  confidence: {job.extraction_confidence:.2f}")

        # ---- Stage 5: Change Detection & Identity (spec §8) ---------------
        click.echo("\n== Stage 5: Change Detection & Identity ==")
        job_posting_store = JobPostingStore()
        version_ledger = JobPostingVersionLedger()
        event_log = ScrapeEventLog()

        previous_records = job_posting_store.current_records(company.id)
        prior_runs = ledger.list_runs(company_id=company.id)  # this run isn't closed/appended yet

        outcome = run_stage5(
            company_id=company.id,
            run_id=run.id,
            observed_at=datetime.now(UTC),
            raw_postings=raw_postings,
            job_postings=job_postings,
            previous_records=previous_records,
            prior_runs=prior_runs,
        )

        if outcome.decision == ZeroJobsDecision.HOLD_FOR_REVIEW:
            click.echo(
                "  zero_jobs_suspicious: 0 jobs found where jobs were previously open — "
                "holding for review, not publishing (spec §17.1)"
            )
            requests_made, bytes_made = _counts()
            run = ledger.close_run(
                run,
                status=ScrapeRunStatus.ZERO_JOBS_SUSPICIOUS,
                failure_detail=(
                    f"0 jobs found; {previously_open_count(previous_records)} were open as of the "
                    "last successful scrape — held for review pending re-verification (spec §17.1)"
                ),
                adapter_used=routed_platform.value,
                http_requests_made=requests_made,
                bytes_fetched=bytes_made,
            )
            _print_run_summary(run)
            return

        if outcome.decision == ZeroJobsDecision.CONFIRMED_BOARD_EMPTIED:
            click.echo("  board_emptied: zero confirmed across two verified sweeps (spec §17.1 step 5)")

        lifecycle_result = outcome.lifecycle
        assert lifecycle_result is not None  # only None on HOLD_FOR_REVIEW, handled above
        job_posting_store.save(lifecycle_result.records)
        version_ledger.append(lifecycle_result.versions)
        event_log.append(lifecycle_result.events)

        if lifecycle_result.events:
            for event in lifecycle_result.events:
                click.echo(f"  - {event.event_type.value}" + (f"  job_id={event.job_id}" if event.job_id else ""))
        else:
            click.echo("  (no lifecycle events this run)")

        raw_payload_ref = None
        if raw_postings:
            raw_payload_ref = archive_raw_payloads(
                company.id, run.id, [p.raw_payload for p in raw_postings]
            )

        requests_made, bytes_made = _counts()
        run = ledger.close_run(
            run,
            status=_final_run_status(extraction_result.status),
            jobs_found=len(job_postings),
            adapter_used=routed_platform.value,
            http_requests_made=requests_made,
            bytes_fetched=bytes_made,
            used_rendering=routed_platform == AtsPlatform.RENDERED_DOM,  # never true in Phase 1
            raw_payload_ref=raw_payload_ref,
        )
        _print_run_summary(run)

        # ---- Part II: leads, matching, enrichment, context (spec §§9-12) --
        open_jobs = [
            record for record in lifecycle_result.records if record.status == JobLifecycleStatus.OPEN
        ]
        if not open_jobs:
            click.echo(
                "\n(no open jobs — Part II skipped; spec §16.1: 'no jobs -> nothing to match')"
            )
            return

        await _process_part2(company, open_jobs, fetcher)


async def _process_part2(company: Company, open_jobs: list[JobPostingRecord], fetcher: Fetcher) -> None:
    """Stages 6-9 (spec §§9-12) for one company's currently-open jobs.

    Same "demo harness, not a real sweep" scope as the rest of this module
    (see module docstring): one `CreditBudget` per invocation, not per
    sweep across many companies (spec §18.3) — there is no multi-company
    sweep for it to be shared across yet. `open_jobs` are `JobPostingRecord`
    (Stage 5 output), which is itself a `JobPosting`, so every Stage 7-9
    function that expects a `JobPosting` accepts them unchanged.
    """
    budget = CreditBudget.from_settings()
    lead_store = LeadStore()
    lead_run_ledger = LeadDiscoveryRunLedger()
    match_store = LeadJobMatchStore()
    unmatched_store = UnmatchedJobStore()
    context_store = CompanyContextStore()
    suppression_store = PersonSuppressionStore()

    now = datetime.now(UTC)

    # --- Stage 6: Lead Discovery (spec §9) ---------------------------------
    click.echo("\n== Stage 6: Lead Discovery ==")
    cached = lead_store.current_leads(company.id)
    cached_leads: list[LeadRecord] = filter_suppressed(
        list(cached.values()), company_domain=company.domain, suppression_store=suppression_store
    )
    cache_retrieved_at = min((lead.retrieved_at for lead in cached_leads), default=None)

    if cached_leads and not needs_refresh(
        cached_leads=cached_leads, cache_retrieved_at=cache_retrieved_at, open_jobs=open_jobs, now=now
    ):
        click.echo(f"  cache hit — {len(cached_leads)} lead(s), no Apollo sweep needed (spec §2.7, §3.3)")
        leads = cached_leads
        lead_status = LeadDiscoveryStatus.LEADS_OK
    else:
        run = lead_run_ledger.begin_run(company.id, started_at=now)
        outcome = await run_stage6(
            company=company, open_jobs=open_jobs, fetcher=fetcher, budget=budget, suppression_store=suppression_store
        )
        lead_run_ledger.close_run(
            run,
            status=outcome.status,
            finished_at=datetime.now(UTC),
            personas_requested=outcome.personas_requested,
            leads_returned=len(outcome.leads),
            apollo_credits_used=len(outcome.leads),
            cache_hit=False,
        )
        click.echo(f"  status: {outcome.status.value}  leads_returned: {len(outcome.leads)}")
        if outcome.detail:
            click.echo(f"  detail: {outcome.detail}")
        if outcome.leads:
            lead_store.save(outcome.leads)
        leads = outcome.leads if outcome.leads else cached_leads
        lead_status = outcome.status

    empty_leads_reason = {
        LeadDiscoveryStatus.NO_LEADS_FOUND: UnmatchedReason.NO_LEADS_RETRIEVED,
        LeadDiscoveryStatus.LEAD_DISCOVERY_FAILED: UnmatchedReason.LEAD_DISCOVERY_FAILED,
        LeadDiscoveryStatus.BUDGET_EXHAUSTED: UnmatchedReason.LEAD_DISCOVERY_FAILED,
        LeadDiscoveryStatus.COMPANY_IDENTITY_SUSPECT: UnmatchedReason.LEAD_DISCOVERY_FAILED,
    }.get(lead_status, UnmatchedReason.NO_LEADS_RETRIEVED)

    # --- Stage 7: Lead-Job Matching (spec §10) -----------------------------
    click.echo("\n== Stage 7: Lead-Job Matching ==")
    result = run_stage7(
        company=company, leads=leads, jobs=open_jobs, run_id=str(uuid4()), computed_at=now,
        empty_leads_reason=empty_leads_reason,
    )

    # Spec §10.7: optional LLM tie-break — only when the top two candidates
    # for a job are within the narrow band `leads.matching.TIE_BREAK_BAND`.
    # Degrades to the rules-only ranking when Azure OpenAI isn't configured,
    # same optional-second-pass convention as Stage 4's LLM residue step.
    if result.matches and AzureOpenAIService().is_configured:
        leads_by_id_for_tie_break = {lead.lead_id: lead for lead in leads}
        jobs_by_id_for_tie_break = {job.job_id: job for job in open_jobs}
        tie_outcome = await resolve_tie_breaks(
            result.matches, jobs_by_id=jobs_by_id_for_tie_break, leads_by_id=leads_by_id_for_tie_break, company=company
        )
        result.matches = tie_outcome.matches
        if tie_outcome.ties_detected:
            click.echo(
                f"  LLM tie-break: {tie_outcome.ties_resolved}/{tie_outcome.ties_detected} tie(s) resolved (spec §10.7)"
            )

    match_store.append(result.matches)
    unmatched_store.append(result.unmatched)
    if result.matches:
        for m in sorted(result.matches, key=lambda m: (m.job_id, m.rank_within_job)):
            click.echo(f"  job={m.job_id}  rank={m.rank_within_job}  lead={m.lead_id}  score={m.match_score:.2f}")
    if result.unmatched:
        for u in result.unmatched:
            click.echo(f"  job={u.job_id}  UNMATCHED  reason={u.reason.value}")

    # --- Stage 8: Enrichment (spec §11) ------------------------------------
    click.echo("\n== Stage 8: Enrichment ==")
    matched_lead_ids = {m.lead_id for m in result.matches}
    if matched_lead_ids:
        enriched = await run_stage8(
            leads=leads, matched_lead_ids=matched_lead_ids, company=company, fetcher=fetcher, budget=budget
        )
        lead_store.save(enriched)
        for lead in enriched:
            if lead.lead_id in matched_lead_ids:
                click.echo(f"  {lead.lead_id}: {lead.enrichment_status.value}")
    else:
        click.echo("  (no matched leads — nothing to enrich, spec §11.1)")

    # --- Stage 9: Company Context (spec §12) -------------------------------
    click.echo("\n== Stage 9: Company Context ==")
    existing_context = context_store.get(company.id)
    if existing_context and not is_context_stale(existing_context.fetched_at, now=now):
        click.echo(f"  cache hit (fetched_at={existing_context.fetched_at})")
        company_context = existing_context
    else:
        status, company_context = await run_stage9(
            company_id=company.id, company_domain=company.domain, company_name=company.name,
            fetcher=fetcher, budget=budget,
        )
        click.echo(f"  status: {status.value}")
        if company_context:
            context_store.save(company_context)
            click.echo(f"  summary: {company_context.summary[:120]}")

    # --- Stage 10: Scoring & Rationale (spec §13) --------------------------
    click.echo("\n== Stage 10: Scoring & Rationale ==")
    scored_store = ScoredLeadStore()
    leads_by_id = {lead.lead_id: lead for lead in leads}
    jobs_by_id = {job.job_id: job for job in open_jobs}
    scored_leads: list[ScoredLead] = []
    if not result.matches:
        click.echo("  (no matches above the floor — nothing to score)")
    for m in result.matches:
        job = jobs_by_id[m.job_id]
        lead = leads_by_id[m.lead_id]
        job_version = job.last_seen_at.isoformat()
        lead_version = (lead.enriched_at or lead.retrieved_at).isoformat()
        cached_score = scored_store.get_cached(
            match_id=m.id, prompt_version=PROMPT_VERSION, job_version=job_version, lead_version=lead_version
        )
        if cached_score is not None:
            scored_leads.append(cached_score)
            click.echo(f"  {m.job_id}/{m.lead_id}: cache hit (spec §13.5)")
            continue

        outcome = await score_pair(job=job, lead=lead, company=company, match=m, context=company_context)
        if outcome.status == ScoringStatus.SCORED and outcome.scored_lead is not None:
            scored = outcome.scored_lead
        else:
            scored = fallback_scored_lead(m, job, lead, now=now)
            click.echo(f"  {m.job_id}/{m.lead_id}: scoring_failed ({outcome.detail}) — rules-score fallback")
        scored_store.save(scored)
        scored_leads.append(scored)
        click.echo(
            f"  {m.job_id}/{m.lead_id}: relevance={scored.relevance_score:.2f} "
            f"confidence={scored.confidence_score:.2f} disagrees={scored.disagrees_with_rules}"
        )

    disagreement_rate = compute_disagreement_rate(scored_leads)
    if disagreement_rate is not None:
        click.echo(f"  disagrees_with_rules rate: {disagreement_rate:.1%} (spec §13.4, §19.3)")

    # --- Stage 11: Ranking & Publication (spec §13.6, §14) -----------------
    click.echo("\n== Stage 11: Ranking & Publication ==")
    scored_by_match_id = {s.match_id: s for s in scored_leads}
    entries = [
        (scored_by_match_id[m.id], jobs_by_id[m.job_id], leads_by_id[m.lead_id])
        for m in result.matches
        if m.id in scored_by_match_id
    ]
    company_id_by_job_id = {job.job_id: company.id for job in open_jobs}
    ranked = rank(
        entries,
        context_by_company={company.id: company_context},
        company_id_by_job_id=company_id_by_job_id,
        now=now,
    )
    matches_by_id = {m.id: m for m in result.matches}
    gtm_leads, publication_events = publish(
        ranked,
        matches_by_id=matches_by_id,
        company_by_id={company.id: company},
        company_id_by_job_id=company_id_by_job_id,
        context_by_company={company.id: company_context},
    )
    publication_events += publish_unmatched(result.unmatched, now=now)

    gtm_lead_store = GtmLeadStore()
    gtm_lead_store.append(gtm_leads)
    event_store = PublicationEventStore()
    event_store.append(publication_events)

    for gtm_lead in gtm_leads:
        click.echo(
            f"  #{gtm_lead.rank} {gtm_lead.job.title} -> {gtm_lead.lead.name} "
            f"({gtm_lead.lead.contactability})  relevance={gtm_lead.relevance_score:.2f}"
        )
        click.echo(f"      {gtm_lead.rationale}")

    if gtm_leads:
        settings = get_settings()
        write_csv(settings.gtm_lead_csv_path, gtm_lead_store.latest())
        click.echo(f"\n  {len(gtm_leads)} lead(s) published; CSV export -> {settings.gtm_lead_csv_path}")


def _fmt_rate(rate: float | None) -> str:
    return "n/a (no data)" if rate is None else f"{rate:.1%}"


@cli.command()
def metrics() -> None:
    """Print Phase 1 coverage metrics computed from the scrape_run ledger (spec §19.1)."""
    ledger = ScrapeRunLedger()
    runs = ledger.list_runs()
    result: CoverageMetrics = compute_coverage_metrics(runs)

    click.echo("== Coverage metrics (spec §19.1) ==")
    click.echo(
        f"  scrape success rate:      {_fmt_rate(result.scrape_success_rate)}"
        f"  ({result.successful_runs}/{result.attempted_runs} runs)"
    )
    click.echo(
        f"  source resolution rate:   {_fmt_rate(result.source_resolution_rate)}"
        f"  ({result.resolved_companies}/{result.total_companies} companies)"
    )
    click.echo(
        f"  ATS coverage:             {_fmt_rate(result.ats_coverage)}"
        f"  ({result.ats_matched_companies}/{result.resolved_companies} resolved companies)"
    )
    click.echo(
        f"  degraded extraction rate: {_fmt_rate(result.degraded_extraction_rate)}"
        f"  ({result.degraded_runs}/{result.successful_runs} successful runs)"
    )
    click.echo(
        f"  unscraped count:          {result.unscraped_count}"
        "  (absolute, never a percentage — spec §19.1)"
    )


@cli.command()
def canary() -> None:
    """Run the canary suite (spec §20.3) against the curated live target list.

    Not scheduled here — this codebase has no scheduler (see this module's
    docstring). Run this on a nightly cron/CI job for the "scraped nightly
    against the live web" cadence spec §20.3 asks for; each invocation is
    one complete, self-contained run.
    """
    asyncio.run(_canary())


async def _canary() -> None:
    result_log = CanaryResultLog()
    finding_log = CanaryFindingLog()
    previous_results = result_log.latest_per_target()

    click.echo(f"== Canary Suite (spec §20.3): {len(CANARY_TARGETS)} targets ==")
    async with Fetcher() as fetcher:
        report = await run_canary_suite(CANARY_TARGETS, fetcher=fetcher, previous_results=previous_results)

    result_log.append(report.results)
    finding_log.append(report.findings)

    findings_by_company = {f.company_id: f for f in report.findings}
    for target, result in zip(CANARY_TARGETS, report.results, strict=True):
        flag = "DRIFT" if target.company_id in findings_by_company else "ok"
        click.echo(
            f"  [{flag:5s}] {target.company_name:20s} platform={result.detected_platform.value:14s} "
            f"status={result.extraction_status:16s} jobs={result.job_count}"
        )

    if report.findings:
        click.echo(f"\n{len(report.findings)} finding(s) recorded (spec §20.3: informational, not build-blocking):")
        for finding in report.findings:
            click.echo(f"  - {finding.company_name} ({finding.company_id}):")
            for reason in finding.reasons:
                click.echo(f"      {reason}")
    else:
        click.echo("\nNo drift detected.")


@cli.command(name="golden-set")
def golden_set_cmd() -> None:
    """Spec §22 Phase 5's "golden-set automation": run the matching
    golden-set accuracy check (spec §19.4) on demand, printing per-case
    mismatches, not just a pass/fail. The same evaluator backs
    `tests/test_matching_golden_set.py`, which runs it on every change;
    this command is the operator-triggered equivalent.
    """
    from gtm_agent.leads.golden_set import evaluate, load_cases

    cases = load_cases()
    report = evaluate(cases, now=datetime.now(UTC))
    click.echo(f"== Matching golden set (spec §19.4): {report.correct}/{report.total} correct ==")
    click.echo(f"  accuracy: {report.accuracy:.1%}")
    if report.mismatches:
        click.echo("  mismatches:")
        for m in report.mismatches:
            click.echo(f"    {m.case_id}: expected={m.expected_label} predicted={m.predicted_label} score={m.score:.3f}")
    else:
        click.echo("  no mismatches")


@cli.command(name="denylist-add")
@click.option("--domain", required=True, help="Company domain to exclude from all future scraping (spec §21.6)")
@click.option("--reason", default=None, help="Why this company is being excluded")
def denylist_add(domain: str, reason: str | None) -> None:
    """Add a company to the scraping denylist (spec §21.6). Honoured
    immediately by every future `discover` invocation for this domain.
    """
    entry = CompanyDenylistStore().add(domain, reason=reason)
    click.echo(f"Added '{entry.domain}' to the company denylist.")


@cli.command(name="suppress-lead")
@click.option("--email", default=None, help="The lead's email — the strongest identity key (spec §21.6)")
@click.option("--full-name", default=None, help="The lead's full name, if no email is on file")
@click.option("--company-domain", default=None, help="Required when suppressing by name instead of email")
@click.option("--reason", default=None)
def suppress_lead(email: str | None, full_name: str | None, company_domain: str | None, reason: str | None) -> None:
    """Erase a lead and suppress them from every future Apollo sweep (spec
    §21.6): "Deletion without suppression is not erasure." Takes identity
    fields directly rather than a `lead_id`, since a suppression request
    must outlive any specific Apollo record for the same person.
    """
    if not email and not (full_name and company_domain):
        raise click.UsageError("Provide --email, or both --full-name and --company-domain.")
    key = suppression_key(email=email, full_name=full_name, company_domain=company_domain)
    PersonSuppressionStore().add(key, reason=reason)
    click.echo(f"Suppressed '{key}'. Future Apollo sweeps will never re-add this person.")


if __name__ == "__main__":
    cli()
