"""Generic-HTML adapter — terminal fallback (spec §6.2.4).

PLACEHOLDER in Phase 1. The real version is heuristic DOM extraction: find
repeated structural elements containing job-like links, cluster by DOM path,
extract title/location/URL by positional and textual heuristics. Spec §6.2.4
is explicit that this path is "low-confidence by construction" — results must
be marked `parse_degraded`, carry reduced confidence, and for a company that
has never successfully parsed by any other path, be surfaced for human
confirmation rather than trusted as a complete job set.

None of that heuristic logic is implemented yet; only the interface exists so
the adapter registry and routing (spec §5.3) have a real fallback target to
route to.
"""

from gtm_agent.core.fetch import Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult

logger = get_logger(__name__)


class GenericHtmlAdapter:
    platform = AtsPlatform.GENERIC_HTML

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        logger.info("generic_html_adapter_not_implemented", company_id=source.company_id)
        return StageResult(
            status=ExtractionStatus.NOT_IMPLEMENTED,
            detail="Generic-HTML heuristic adapter is a Phase 1 placeholder; implemented in Phase 2",
        )

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        raise NotImplementedError("Generic-HTML hydrate() is not implemented until Phase 2")
