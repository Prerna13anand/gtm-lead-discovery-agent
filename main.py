#!/usr/bin/env python
"""CLI entry point — runs Stages 1-4 for a single company (spec §3.2, Part I).

This is a development/demo harness, not the sweep orchestrator described in
spec §16 (which is later-phase work — no persistence, no scheduling, no
concurrency across companies). It exists to prove the Part I pipeline is
wired correctly end to end: source resolution -> ATS fingerprinting ->
extraction -> normalisation.

Since the ATS-API adapters (Greenhouse/Lever/Ashby) are Phase 1 placeholders,
most real companies will currently terminate at `not_implemented` once
routed to one of them. Only the JSON-LD path produces real `JobPosting`s in
Phase 1 — see discovery/extraction/jsonld.py.
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import click  # noqa: E402

from gtm_agent.core.fetch import Fetcher  # noqa: E402
from gtm_agent.core.logging import configure_logging, get_logger  # noqa: E402
from gtm_agent.discovery.ats_detection import identify_ats, route_extraction  # noqa: E402
from gtm_agent.discovery.extraction import get_adapter  # noqa: E402
from gtm_agent.discovery.normalization import normalize_batch  # noqa: E402
from gtm_agent.discovery.source_resolution import resolve_source  # noqa: E402
from gtm_agent.models.company import Company  # noqa: E402
from gtm_agent.models.results import (  # noqa: E402
    AtsFingerprintStatus,
    ExtractionStatus,
    SourceResolutionStatus,
)

logger = get_logger(__name__)


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


async def _discover(domain: str, company_name: str, manual_careers_url: str | None) -> None:
    company = Company(id=domain, name=company_name, domain=domain, added_at=datetime.now(UTC))

    async with Fetcher() as fetcher:
        click.echo(f"== Stage 1: Source Resolution ({domain}) ==")
        source_result = await resolve_source(company, fetcher, manual_override_url=manual_careers_url)

        if source_result.status != SourceResolutionStatus.RESOLVED or source_result.value is None:
            click.echo(f"  status: {source_result.status.value}")
            if source_result.detail:
                click.echo(f"  detail: {source_result.detail}")
            click.echo(
                "  No careers source resolved — stopping (spec §2.3: this is "
                "'unscraped', never 'no jobs')."
            )
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
            return

        extraction_result = await adapter.discover(source, fetcher)
        if extraction_result.status != ExtractionStatus.SUCCESS or extraction_result.value is None:
            click.echo(f"  status: {extraction_result.status.value}")
            if extraction_result.detail:
                click.echo(f"  detail: {extraction_result.detail}")
            return

        raw_postings = extraction_result.value
        click.echo(f"  discovered {len(raw_postings)} raw posting(s)")

        click.echo("\n== Stage 4: Normalisation ==")
        if not raw_postings:
            click.echo("  (no postings to normalise — a validated, empty board is a real result, per spec §2.3)")
            return

        job_postings = normalize_batch(raw_postings)
        for job in job_postings:
            click.echo(f"  - {job.title_canonical or '(untitled)'}  [{job.function}/{job.seniority}]")
            click.echo(f"      location: {job.location_raw}  workplace: {job.workplace_type}")
            click.echo(f"      posted_at: {job.posted_at} (inferred={job.posted_at_is_inferred})")
            click.echo(f"      degraded: {job.is_degraded}  confidence: {job.extraction_confidence:.2f}")


if __name__ == "__main__":
    cli()
