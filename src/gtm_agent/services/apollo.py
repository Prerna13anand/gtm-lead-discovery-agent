"""Apollo service — placeholder interface only. Spec §9 (Stage 6, Lead Discovery).

Not implemented until Phase 3. No API calls, no credentials read beyond what
`config.Settings` already loads. The interface shape exists now so Phase 3 can
implement bodies without redesigning the call sites that will depend on it.
"""

from __future__ import annotations

from typing import Any

from gtm_agent.config import get_settings


class ApolloNotConfiguredError(Exception):
    pass


class ApolloClient:
    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.apollo_api_key)

    async def search_people(
        self,
        *,
        company_domain: str,
        titles: list[str],
        seniority_floor: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Apollo People Search — spec §9.3. One sweep per company (§2.7), not per job.

        Query construction (title/seniority filters, no location filter,
        contactability preferred but not required, headcount sanity check) is
        specified in §9.3-§9.4 and implemented in Phase 3.
        """
        raise NotImplementedError("Apollo integration is Phase 3 work — see spec §9")
