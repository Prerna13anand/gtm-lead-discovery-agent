"""People Data Labs service — placeholder interface only. Spec §11 (Stage 8, Enrichment).

Not implemented until Phase 3. Enrichment only ever runs for leads that
matched at least one job above the match floor (spec §11.1), which itself
doesn't exist until Stage 7 (Phase 3) is built — there is nothing for this
client to be called with yet.
"""

from __future__ import annotations

from typing import Any

from gtm_agent.config import get_settings


class PDLNotConfiguredError(Exception):
    pass


class PDLClient:
    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.pdl_api_key)

    async def enrich_person(
        self,
        *,
        linkedin_url: str | None = None,
        work_email: str | None = None,
        full_name: str | None = None,
        company_domain: str | None = None,
    ) -> dict[str, Any] | None:
        """Identity waterfall per spec §11.3: LinkedIn URL, then work email,
        then (name + domain) as the weakest key, requiring a corroborating
        field before accepting a name-only match.
        """
        raise NotImplementedError("PDL integration is Phase 3 work — see spec §11")
