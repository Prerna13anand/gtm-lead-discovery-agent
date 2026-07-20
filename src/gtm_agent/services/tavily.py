"""Tavily service — placeholder interface only.

Used by two different stages, at different phases:
    - Stage 1, Strategy D — a domain-restricted then unrestricted careers-page
      search fallback (spec §4.1 Strategy D). Referenced by
      `discovery.source_resolution.TavilySearchStrategy`, itself a Phase 1
      placeholder that always declines.
    - Stage 9, Company Context — funding/hiring/news summary, once per company,
      cached ~7 days (spec §12). Not implemented until Phase 3.

No API calls in Phase 1.
"""

from __future__ import annotations

from typing import Any

from gtm_agent.config import get_settings


class TavilyNotConfiguredError(Exception):
    pass


class TavilyClient:
    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.tavily_api_key)

    async def search(self, query: str, *, restrict_domain: str | None = None) -> list[dict[str, Any]]:
        """Generic search — backs both the Stage 1 fallback and Stage 9 context queries."""
        raise NotImplementedError("Tavily integration is not implemented until a later phase — see spec §4.1, §12")

    async def get_company_context(self, *, company_domain: str, company_name: str) -> dict[str, Any] | None:
        """Stage 9 — spec §12. Funding, hiring/growth news, leadership changes."""
        raise NotImplementedError("Tavily company-context integration is Phase 3 work — see spec §12")
