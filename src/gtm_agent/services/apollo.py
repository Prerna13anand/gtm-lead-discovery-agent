"""Apollo service — Stage 6, Lead Discovery (spec §9).

Query construction follows spec §9.3: company identified by **domain, not
name**; titles from the Stage 6 persona ladder (spec §9.1); no location
filter (spec §9.3: "a common cause of silently missing the correct lead");
contactability preferred but never required. Pagination follows spec §9.4:
paginate to the retrieval cap (default ~50), then stop — a result count
*exceeding* the cap is the caller's signal to raise `company_identity_suspect`
(spec §9.4, §17.2), not something this client decides.

Endpoint shape follows Apollo's public People Search API
(`POST /v1/mixed_people/search`) as documented at integration time. Per this
codebase's existing convention for third-party APIs (spec Appendix A's build
note, and `services.tavily`'s identical caveat for its own endpoint): treat
this as a starting point, not a contract, and verify against Apollo's current
API docs before relying on it in production.

**Live-verified** (post-implementation audit, real `APOLLO_API_KEY`): the
first live call returned `422 INVALID_API_KEY_LOCATION` — Apollo's current
API requires the key in an `X-Api-Key` request header, not the `api_key`
JSON body field its older documented shape used. Fixed below; the rest of
the request shape (`q_organization_domains`, `person_titles`,
`person_seniorities`, pagination) has not yet been independently confirmed
against a real non-empty result set and should still be treated as
best-effort pending that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from gtm_agent.config import get_settings
from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.core.logging import get_logger

logger = get_logger(__name__)

_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search"

# Spec §9.3: "Seniority: Manager and above, plus recruiting at any level."
# Apollo's `person_seniorities` filter is additive (an OR across values), so
# recruiting-relevant seniorities are simply included alongside the
# manager-and-above floor rather than needing a separate unfiltered request.
_SENIORITY_FLOOR: tuple[str, ...] = (
    "manager", "director", "vp", "c_suite", "founder", "partner", "head",
    "senior", "entry",  # recruiters/talent are legitimate at any level (spec §9.3)
)

# Spec §9.4: "Cap retrieval at ~50 people per company."
_DEFAULT_LIMIT = 50
_PER_PAGE = 25


class ApolloNotConfiguredError(Exception):
    pass


class ApolloSearchError(Exception):
    """Raised when an Apollo request fails or returns an unusable response."""


@dataclass(frozen=True)
class ApolloSearchResult:
    """`people` is capped at the retrieval limit (spec §9.4: "paginate to the
    cap, then stop"). `total_entries` — Apollo's own reported match count,
    read from the first page's pagination metadata — lets the caller detect
    "more than the cap exists" *without* over-fetching just to count it, per
    §9.4: "If a company returns more than the cap... treat it as
    `company_identity_suspect` rather than truncating silently." `None` if
    Apollo's response didn't include pagination metadata.
    """

    people: list[dict[str, Any]] = field(default_factory=list)
    total_entries: int | None = None


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
        fetcher: Fetcher,
        seniority_floor: tuple[str, ...] = _SENIORITY_FLOOR,
        limit: int = _DEFAULT_LIMIT,
    ) -> ApolloSearchResult:
        """Apollo People Search — spec §9.3. One sweep per company (§2.7), not
        per job. Returns raw person dicts as Apollo returns them; mapping to
        `Lead` happens in `leads.discovery`, which is where the retrieval-cap
        check (§9.4) and `company_identity_suspect` decision also live —
        this client just fetches, paginates, and reports Apollo's own count.
        """
        if not self.is_configured:
            raise ApolloNotConfiguredError("APOLLO_API_KEY is not set")

        people: list[dict[str, Any]] = []
        total_entries: int | None = None
        page = 1
        # Live-verified: Apollo rejects the key in the JSON body
        # (422 INVALID_API_KEY_LOCATION) — it must be an `X-Api-Key` header.
        headers = {"X-Api-Key": self._settings.apollo_api_key}
        while len(people) < limit:
            payload: dict[str, Any] = {
                "q_organization_domains": company_domain,
                "person_titles": titles,
                "person_seniorities": list(seniority_floor),
                "page": page,
                "per_page": _PER_PAGE,
            }
            try:
                result = await fetcher.post(_SEARCH_URL, json=payload, headers=headers)
            except FetchError as exc:
                raise ApolloSearchError(f"Apollo search request failed: {exc}") from exc

            if result.status_code >= 400:
                raise ApolloSearchError(f"Apollo search returned HTTP {result.status_code}")

            try:
                data = json.loads(result.text)
            except ValueError as exc:
                raise ApolloSearchError(f"Apollo search returned invalid JSON: {exc}") from exc

            if page == 1 and isinstance(data, dict):
                pagination = data.get("pagination")
                if isinstance(pagination, dict) and isinstance(pagination.get("total_entries"), int):
                    total_entries = pagination["total_entries"]

            batch = data.get("people") if isinstance(data, dict) else None
            if not isinstance(batch, list) or not batch:
                break

            people.extend(batch)
            if len(batch) < _PER_PAGE:
                break  # short page — no more results
            page += 1

        people = people[:limit] if len(people) > limit else people
        logger.info(
            "apollo_search_people",
            company_domain=company_domain,
            titles_requested=len(titles),
            people_returned=len(people),
            total_entries=total_entries,
        )
        return ApolloSearchResult(people=people, total_entries=total_entries)
