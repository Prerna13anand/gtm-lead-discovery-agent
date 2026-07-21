"""Stage 9 — Company Context (spec §12).

**Goal:** add public, current company signals so the GTM team can
prioritise. Once per company, cached ~7 days (spec §12.1); failure is
**non-blocking** (spec §12.4) — this stage never stops the pipeline.

Spec §12.3: "this is prioritisation signal, not matching signal... it must
not influence *which lead matched which job*." Enforced structurally, not
just by convention: `CompanyContext` never appears as a parameter to
anything in `leads.matching` — there is no call site through which it
*could* leak into Stage 7's scores.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gtm_agent.core.fetch import Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.leads.budget import BudgetMeter, CreditBudget
from gtm_agent.models.company_context import CompanyContext, CompanyContextStatus
from gtm_agent.services.tavily import TavilyClient, TavilyNotConfiguredError, TavilySearchError

logger = get_logger(__name__)

# Spec §12.1: "cached ~7 days."
_CONTEXT_TTL_DAYS = 7

# Spec §12.2: "Results are summarised into a compact `CompanyContext` record
# — the LLM receives a summary, not raw search results, to keep token cost
# bounded and prompts stable." No LLM is in the loop in Phase 3 (spec §22:
# "deliberately rules-only"), so the summary here is an extractive
# truncation of the top result snippets rather than an LLM-generated one —
# consistent with this codebase's existing "rules first" convention
# (`discovery.normalization`'s classifiers) and avoiding a premature,
# unverified LLM prompt for a non-blocking, best-effort field. Phase 4's
# real Stage 10 LLM integration can replace this with genuine summarisation
# without changing `CompanyContext`'s shape.
_SUMMARY_MAX_CHARS = 500
_SNIPPETS_PER_SECTION = 2


def is_context_stale(fetched_at: datetime | None, *, now: datetime) -> bool:
    if fetched_at is None:
        return True
    return (now - fetched_at).days >= _CONTEXT_TTL_DAYS


_FUNDING_KEYWORDS = ("raised", "funding", "series a", "series b", "seed round", "investment")
_HIRING_KEYWORDS = ("hiring", "growth", "expanding", "new head", "joins as", "appoints")


def _best_snippet(results: list[dict[str, Any]], keywords: tuple[str, ...]) -> str | None:
    for result in results:
        content = result.get("content") or result.get("title") or ""
        if isinstance(content, str) and any(keyword in content.lower() for keyword in keywords):
            return content
    if results:
        content = results[0].get("content") or results[0].get("title")
        return content if isinstance(content, str) else None
    return None


def summarize_context(raw: dict[str, Any]) -> tuple[str, str | None, str | None, list[str]]:
    """Extractive summary + per-signal snippets + source URLs from
    `TavilyClient.get_company_context`'s raw result dict.
    """
    funding_results = raw.get("funding_results") or []
    hiring_results = raw.get("hiring_results") or []
    careers_results = raw.get("careers_results") or []

    funding_signal = _best_snippet(funding_results, _FUNDING_KEYWORDS)
    hiring_signal = _best_snippet(hiring_results, _HIRING_KEYWORDS)

    all_results = funding_results + hiring_results + careers_results
    snippets: list[str] = []
    for results in (funding_results, hiring_results):
        for result in results[:_SNIPPETS_PER_SECTION]:
            content = result.get("content") or result.get("title")
            if isinstance(content, str) and content:
                snippets.append(content)

    summary = " ".join(snippets)[:_SUMMARY_MAX_CHARS] or "No public context found."
    sources = [r["url"] for r in all_results if isinstance(r, dict) and isinstance(r.get("url"), str)]

    return summary, funding_signal, hiring_signal, sources


async def run_stage9(
    *,
    company_id: str,
    company_domain: str,
    company_name: str,
    fetcher: Fetcher,
    budget: CreditBudget,
    tavily_client: TavilyClient | None = None,
    now: datetime | None = None,
) -> tuple[CompanyContextStatus, CompanyContext | None]:
    """Perform the Stage 9 sweep. Callers should only invoke this once a
    cached `CompanyContext` is missing or stale (spec §12.1) — a cache hit
    never reaches this function, same convention as Stage 6.
    """
    now = now or datetime.now(UTC)
    client = tavily_client or TavilyClient()

    if not budget.try_consume(BudgetMeter.TAVILY_CALLS, 1):
        logger.warning("company_context_budget_exhausted", company_id=company_id)
        return CompanyContextStatus.CONTEXT_UNAVAILABLE, None

    try:
        raw = await client.get_company_context(company_domain=company_domain, company_name=company_name, fetcher=fetcher)
    except (TavilyNotConfiguredError, TavilySearchError) as exc:
        # Spec §12.4: "non-blocking" — log and move on, never raise past this point.
        logger.warning("context_unavailable", company_id=company_id, error=str(exc))
        return CompanyContextStatus.CONTEXT_UNAVAILABLE, None

    summary, funding_signal, hiring_signal, sources = summarize_context(raw)
    context = CompanyContext(
        company_id=company_id,
        summary=summary,
        funding_signal=funding_signal,
        hiring_signal=hiring_signal,
        sources=sources,
        fetched_at=now,
        expires_at=now + timedelta(days=_CONTEXT_TTL_DAYS),
    )
    return CompanyContextStatus.CONTEXT_OK, context
