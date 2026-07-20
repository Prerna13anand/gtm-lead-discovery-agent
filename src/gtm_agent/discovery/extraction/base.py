"""The adapter interface — spec §6.1.

Every extraction path (ATS API, JSON-LD, rendered DOM, generic HTML)
implements this one interface. The orchestrator never knows which path
served a company (spec §2.2) — it only calls `discover` and `hydrate`.

The spec's pseudocode signatures are synchronous (`def discover(...)`); this
implementation makes them async because every adapter goes through the
shared async fetch layer (`core.fetch.Fetcher`) — the contract is otherwise
identical.

`discover` / `hydrate` are split because platforms differ in whether the list
endpoint includes full descriptions:
    - Inline (Greenhouse with `content=true`, Lever): `discover` returns
      everything; `hydrate` is a no-op.
    - Two-phase (most HTML boards, some ATS list endpoints): `discover`
      returns titles and URLs; `hydrate` fetches each job detail page.
"""

from typing import Protocol

from gtm_agent.core.fetch import Fetcher
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult


class BoardAdapter(Protocol):
    platform: AtsPlatform

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        """Return every currently-open posting. May return shallow records."""
        ...

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        """Fill in full description and detail fields. No-op if discover() was complete."""
        ...
