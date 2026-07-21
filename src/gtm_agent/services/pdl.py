"""People Data Labs service — Stage 8, Enrichment (spec §11).

Identity waterfall per spec §11.3: "PDL is queried by the strongest
available key: LinkedIn URL, then work email, then (name + company domain)."
Implemented here as sequential requests — the strongest key is tried first
and only a miss falls through to the next, rather than sending every known
identifier in one combined query, so a match on a strong key is never
diluted by also supplying `leads.enrichment`'s weakest, most error-prone key
(name-only) in the same request.

Endpoint shape follows PDL's public Person Enrichment API
(`GET /v5/person/enrich`) as documented at integration time — same caveat as
`services.apollo` and `services.tavily`: verify against current PDL docs
before production use. **This client has not been exercised against the
live PDL API** — `PDL_API_KEY` is unset in this environment.
"""

from __future__ import annotations

import json
from typing import Any

from gtm_agent.config import get_settings
from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.core.logging import get_logger

logger = get_logger(__name__)

_ENRICH_URL = "https://api.peopledatalabs.com/v5/person/enrich"


class PDLNotConfiguredError(Exception):
    pass


class PDLEnrichError(Exception):
    """Raised when a PDL request fails or returns an unusable response."""


class PDLClient:
    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.pdl_api_key)

    async def enrich_person(
        self,
        *,
        fetcher: Fetcher,
        linkedin_url: str | None = None,
        work_email: str | None = None,
        full_name: str | None = None,
        company_domain: str | None = None,
    ) -> dict[str, Any] | None:
        """Identity waterfall per spec §11.3. Tries the strongest available
        key first, falling through only on a miss. `None` if nothing was
        tried (no identifiers supplied) or every attempted key missed.

        Note: this client returns whatever PDL matched on the name+domain
        key without judging its quality — requiring a corroborating field
        before *accepting* a name-only match (spec §11.3) is a caller-side
        policy decision (`leads.enrichment`), not something this thin client
        should decide, matching this codebase's existing split between pure
        decision logic and I/O clients (see `discovery.lifecycle`'s module
        docstring for the same separation).
        """
        if not self.is_configured:
            raise PDLNotConfiguredError("PDL_API_KEY is not set")

        if linkedin_url:
            record = await self._enrich(fetcher, {"profile": linkedin_url})
            if record is not None:
                return record

        if work_email:
            record = await self._enrich(fetcher, {"email": work_email})
            if record is not None:
                return record

        if full_name and company_domain:
            record = await self._enrich(fetcher, {"name": full_name, "company": company_domain})
            if record is not None:
                return record

        return None

    async def _enrich(self, fetcher: Fetcher, identifiers: dict[str, str]) -> dict[str, Any] | None:
        params = {**identifiers, "api_key": self._settings.pdl_api_key}
        try:
            result = await fetcher.get(_ENRICH_URL, params=params)
        except FetchError as exc:
            raise PDLEnrichError(f"PDL enrich request failed: {exc}") from exc

        if result.status_code == 404:
            return None  # no match on this key — a legitimate miss, not an error
        if result.status_code >= 400:
            raise PDLEnrichError(f"PDL enrich returned HTTP {result.status_code}")

        try:
            data = json.loads(result.text)
        except ValueError as exc:
            raise PDLEnrichError(f"PDL enrich returned invalid JSON: {exc}") from exc

        record = data.get("data") if isinstance(data, dict) else None
        return record if isinstance(record, dict) else None
