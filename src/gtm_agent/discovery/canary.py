"""Canary Suite (spec §20.3).

"~20 real companies, one per ATS plus several generic-path, scraped nightly
against the live web. This is the only test that catches real-world drift —
an ATS quietly changing a field, a company migrating platforms, a careers
page being redesigned. Fixtures by definition cannot catch any of these,
because fixtures are frozen."

This deliberately reuses Stage 2 (`identify_ats`) and Stage 3
(`route_extraction`, `get_adapter`, `adapter.discover`) exactly as `main.py`'s
`_discover` does — the canary isn't a separate scraping path, it's the same
pipeline run against a curated, known-good target list instead of one
company, with the result compared against what was last observed. It skips
Stage 1 (source resolution) because canary targets carry a known-stable
`careers_url` already — spec §20.3 is about catching *extraction-layer*
drift (an ATS migrating, a field changing shape), not source-resolution
drift, which the canary's own `careers_url` staying correct is itself
evidence against.

"Failures here are informational rather than build-blocking, but they open a
ticket automatically." `core.canary_store.CanaryFindingLog` is the local
stand-in for that — see its module docstring.

`run_canary_suite` checks targets one at a time rather than concurrently.
Spec §16.3's concurrency model (global cap, per-domain semaphore) exists for
sweeping the *full* company list under throughput pressure; a nightly run
over ~20 targets has none, so the added complexity isn't justified here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.discovery.ats_detection import identify_ats, route_extraction
from gtm_agent.discovery.extraction import get_adapter
from gtm_agent.models.canary import CanaryFinding, CanaryRunResult, CanaryTarget
from gtm_agent.models.careers_source import CareersSource, ResolutionStrategy
from gtm_agent.models.results import AtsFingerprintStatus, ExtractionStatus

# A canary result counts as "healthy" if it's either a clean success or a
# degraded-but-real extraction — the same two statuses `main.py` treats as
# "reached Stage 4" (spec §17: parse_degraded is "Published", not failed).
_HEALTHY_STATUSES = frozenset({ExtractionStatus.SUCCESS.value, ExtractionStatus.PARSE_DEGRADED.value})


def _synthetic_source(target: CanaryTarget) -> CareersSource:
    """Canary targets carry their own known-stable `careers_url` — same
    manual-override shape as `main.py --manual-careers-url`, since Stage 1
    resolution isn't what the canary is checking (see module docstring).
    """
    return CareersSource(
        company_id=target.company_id,
        careers_url=target.careers_url,
        resolution_strategy=ResolutionStrategy.MANUAL_OVERRIDE,
        resolution_confidence=1.0,
        is_manual_override=True,
        created_at=datetime.now(UTC),
    )


async def check_target(target: CanaryTarget, fetcher: Fetcher) -> CanaryRunResult:
    """Run Stage 2 + 3 against one canary target, live."""
    source = _synthetic_source(target)

    ats_result = await identify_ats(source, fetcher)
    identification = ats_result.value if ats_result.status == AtsFingerprintStatus.IDENTIFIED else None

    try:
        # use_cache=False: see main.py's identical peek fetch and
        # core.fetch.Fetcher._request's docstring — without this, the
        # adapter's own read of the same URL moments later can see a
        # spurious 304 instead of real content.
        page = await fetcher.get(source.careers_url, use_cache=False)
        page_html: str | None = page.text
    except FetchError:
        page_html = None

    routed_platform = route_extraction(identification, page_html)
    adapter = get_adapter(routed_platform)

    if adapter is None:
        extraction_status = ExtractionStatus.NOT_IMPLEMENTED
        job_count = 0
    else:
        extraction_result = await adapter.discover(source, fetcher)
        extraction_status = extraction_result.status
        job_count = len(extraction_result.value) if extraction_result.value else 0

    return CanaryRunResult(
        id=str(uuid.uuid4()),
        company_id=target.company_id,
        run_at=datetime.now(UTC),
        detected_platform=routed_platform,
        extraction_status=extraction_status.value,
        job_count=job_count,
        adapter_used=routed_platform.value,
    )


def detect_drift(
    target: CanaryTarget, previous: CanaryRunResult | None, current: CanaryRunResult
) -> list[str]:
    """Spec §20.3's three named drift shapes, as mechanically checkable
    proxies: "an ATS quietly changing a field" and "a company migrating
    platforms" both surface as a platform change; "a careers page being
    redesigned" surfaces as either a platform change (if the redesign moved
    the board) or an extraction/job-count regression (if it broke parsing).
    """
    reasons: list[str] = []

    if current.detected_platform != target.expected_platform:
        reasons.append(
            f"platform drift: expected {target.expected_platform.value}, "
            f"detected {current.detected_platform.value}"
        )

    if previous is not None and previous.detected_platform != current.detected_platform:
        reasons.append(
            f"platform changed since last canary run: {previous.detected_platform.value} "
            f"-> {current.detected_platform.value}"
        )

    if (
        previous is not None
        and previous.extraction_status in _HEALTHY_STATUSES
        and current.extraction_status not in _HEALTHY_STATUSES
    ):
        reasons.append(
            f"extraction status regressed: {previous.extraction_status} -> {current.extraction_status}"
        )

    if (
        previous is not None
        and previous.job_count > 0
        and current.job_count == 0
        and current.extraction_status in _HEALTHY_STATUSES
    ):
        reasons.append(f"job count dropped to zero (was {previous.job_count})")

    return reasons


@dataclass
class CanarySuiteReport:
    results: list[CanaryRunResult] = field(default_factory=list)
    findings: list[CanaryFinding] = field(default_factory=list)


async def run_canary_suite(
    targets: list[CanaryTarget],
    *,
    fetcher: Fetcher,
    previous_results: dict[str, CanaryRunResult],
) -> CanarySuiteReport:
    """`previous_results` is each target's last recorded run, keyed by
    `company_id` (`CanaryResultLog.latest_per_target`) — loaded by the
    caller, same load-then-diff split as Stage 5's `run_stage5`.
    """
    report = CanarySuiteReport()
    for target in targets:
        current = await check_target(target, fetcher)
        report.results.append(current)

        reasons = detect_drift(target, previous_results.get(target.company_id), current)
        if reasons:
            report.findings.append(
                CanaryFinding(
                    id=str(uuid.uuid4()),
                    company_id=target.company_id,
                    company_name=target.company_name,
                    detected_at=current.run_at,
                    reasons=reasons,
                    previous=previous_results.get(target.company_id),
                    current=current,
                )
            )
    return report
