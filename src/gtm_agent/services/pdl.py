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
before production use.

**Live-verified** (post-implementation audit, real `PDL_API_KEY`): the key
is now sent as an `X-Api-Key` header rather than an `api_key` query
parameter. This was changed for a real reason found during that
verification, not preemptively: a query-string key appears in full in any
request logging (this codebase's own fetch-layer debug logs included, which
is how a real key value was briefly exposed during manual testing) and in
any intermediary's access logs — a header avoids that exposure surface
regardless of which HTTP client or logging config is in front of it.
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
        # X-Api-Key header, not a query parameter — see module docstring.
        try:
            result = await fetcher.get(
                _ENRICH_URL, params=identifiers, headers={"X-Api-Key": self._settings.pdl_api_key}
            )
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
