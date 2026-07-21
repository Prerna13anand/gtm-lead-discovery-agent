"""Tavily service.

Used by two different stages, at different phases:
    - Stage 1, Strategy D — a domain-restricted then unrestricted careers-page
      search fallback (spec §4.1 Strategy D). `search()` is implemented in
      Phase 2 for this caller: `discovery.source_resolution.TavilySearchStrategy`.
    - Stage 9, Company Context — funding/hiring/news summary, once per company,
      cached ~7 days (spec §12). Implemented in Phase 3 — see
      `get_company_context` and, for the summarisation into a `CompanyContext`
      record, `leads.company_context`.

`search()`'s request/response shape follows Tavily's public Search API as
documented at integration time. Per this codebase's existing convention for
third-party APIs (spec §5.3's build note on Appendix A), treat it as a
starting point and verify against current Tavily API docs before relying on
it in production — it has not been exercised against the live API here.
"""

from __future__ import annotations

import json
from typing import Any

from gtm_agent.config import get_settings
from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.core.logging import get_logger

logger = get_logger(__name__)

_SEARCH_URL = "https://api.tavily.com/search"


class TavilyNotConfiguredError(Exception):
    pass


class TavilySearchError(Exception):
    """Raised when a Tavily search request fails or returns an unusable response."""


class TavilyClient:
    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.tavily_api_key)

    async def search(
        self,
        query: str,
        *,
        fetcher: Fetcher,
        restrict_domain: str | None = None,
    ) -> list[dict[str, Any]]:
        """Generic search — backs both the Stage 1 fallback and Stage 9 context queries.

        Routes through the shared `Fetcher` rather than a private HTTP client,
        per this codebase's fetch-layer convention (`core.fetch` module docstring).
        """
        if not self.is_configured:
            raise TavilyNotConfiguredError("TAVILY_API_KEY is not set")

        payload: dict[str, Any] = {"api_key": self._settings.tavily_api_key, "query": query}
        if restrict_domain:
            payload["include_domains"] = [restrict_domain]

        try:
            result = await fetcher.post(_SEARCH_URL, json=payload)
        except FetchError as exc:
            raise TavilySearchError(f"Tavily search request failed: {exc}") from exc

        if result.status_code >= 400:
            raise TavilySearchError(f"Tavily search returned HTTP {result.status_code}")

        try:
            data = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise TavilySearchError(f"Tavily search returned invalid JSON: {exc}") from exc

        results = data.get("results") if isinstance(data, dict) else None
        return results if isinstance(results, list) else []

    async def get_company_context(
        self, *, company_domain: str, company_name: str, fetcher: Fetcher
    ) -> dict[str, Any]:
        """Stage 9 — spec §12.2: "recent funding announcements, hiring/growth
        news, notable product or leadership changes, and the careers page as
        a cross-check." Three templated, narrow queries rather than one
        broad one — narrow queries are what spec §12.2 asks for ("kept
        narrow"), and separating funding from hiring/careers lets
        `leads.company_context` derive `funding_signal`/`hiring_signal`
        independently instead of guessing which result answered which
        question.

        Returns a raw dict of {"funding_results", "hiring_results",
        "careers_results"} — each Tavily's own result list. Summarising this
        into a compact `CompanyContext` (spec: "the LLM receives a summary,
        not raw search results") is `leads.company_context`'s job, not this
        client's; this method's only responsibility is the three fetches.
        """
        # Funding/hiring news is published by third parties (press, Crunchbase,
        # etc.), not on the company's own site — unlike Stage 1's Strategy D
        # (spec §4.1), domain-restricting these two would return almost
        # nothing. Only the careers cross-check genuinely belongs on the
        # company's own domain.
        funding_results = await self.search(f'"{company_name}" funding OR raised OR "series" round', fetcher=fetcher)
        hiring_results = await self.search(f'"{company_name}" hiring growth news leadership', fetcher=fetcher)
        careers_results = await self.search(
            f'"{company_name}" careers open positions', fetcher=fetcher, restrict_domain=company_domain
        )
        return {
            "funding_results": funding_results,
            "hiring_results": hiring_results,
            "careers_results": careers_results,
        }
