#!/usr/bin/env python
"""CLI entry point — runs Stages 1-4 for a single company (spec §3.2, Part I).

This is a development/demo harness, not the sweep orchestrator described in
spec §16 (which is later-phase work — no scheduling, no concurrency across
companies). It exists to prove the Part I pipeline is wired correctly end to
end: source resolution -> ATS fingerprinting -> extraction -> normalisation
-> scrape_run ledger.

Every invocation records exactly one `ScrapeRun` (spec §15.1) regardless of
where it terminates — a Stage 1 failure is still "one company per attempt",
not a silent non-event (spec §2.3). See core/run_ledger.py.
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

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

from gtm_agent.core.fetch import Fetcher  # noqa: E402
from gtm_agent.core.logging import configure_logging, get_logger  # noqa: E402
from gtm_agent.core.metrics import CoverageMetrics, compute_coverage_metrics  # noqa: E402
from gtm_agent.core.run_ledger import ScrapeRunLedger, archive_raw_payloads  # noqa: E402
from gtm_agent.discovery.ats_detection import identify_ats, route_extraction  # noqa: E402
from gtm_agent.discovery.extraction import get_adapter  # noqa: E402
from gtm_agent.discovery.normalization import normalize_batch  # noqa: E402
from gtm_agent.discovery.source_resolution import resolve_source  # noqa: E402
from gtm_agent.models.ats import AtsPlatform  # noqa: E402
from gtm_agent.models.company import Company  # noqa: E402
from gtm_agent.models.results import (  # noqa: E402
    AtsFingerprintStatus,
    ExtractionStatus,
    SourceResolutionStatus,
)
from gtm_agent.models.scrape_run import ScrapeRun, ScrapeRunStatus  # noqa: E402

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


@click.group()
def cli() -> None:
    """GTM Lead Discovery Agent — Phase 1 CLI."""
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
            page = await fetcher.get(source.careers_url)
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
        if extraction_result.status != ExtractionStatus.SUCCESS or extraction_result.value is None:
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

        click.echo("\n== Stage 4: Normalisation ==")
        job_postings = normalize_batch(raw_postings) if raw_postings else []
        if not raw_postings:
            click.echo("  (no postings to normalise — a validated, empty board is a real result, per spec §2.3)")
        for job in job_postings:
            click.echo(f"  - {job.title_canonical or '(untitled)'}  [{job.function}/{job.seniority}]")
            click.echo(f"      location: {job.location_raw}  workplace: {job.workplace_type}")
            click.echo(f"      posted_at: {job.posted_at} (inferred={job.posted_at_is_inferred})")
            click.echo(f"      degraded: {job.is_degraded}  confidence: {job.extraction_confidence:.2f}")

        raw_payload_ref = None
        if raw_postings:
            raw_payload_ref = archive_raw_payloads(
                company.id, run.id, [p.raw_payload for p in raw_postings]
            )

        requests_made, bytes_made = _counts()
        run = ledger.close_run(
            run,
            status=ScrapeRunStatus.SUCCESS,
            jobs_found=len(job_postings),
            adapter_used=routed_platform.value,
            http_requests_made=requests_made,
            bytes_fetched=bytes_made,
            used_rendering=routed_platform == AtsPlatform.RENDERED_DOM,  # never true in Phase 1
            raw_payload_ref=raw_payload_ref,
        )
        _print_run_summary(run)


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


if __name__ == "__main__":
    cli()
