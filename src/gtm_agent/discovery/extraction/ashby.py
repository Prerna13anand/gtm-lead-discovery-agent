"""Ashby adapter — spec §6.2.1, Appendix A.

PLACEHOLDER. Per explicit Phase 1 scope, this does not call the real Ashby
API yet. Phase 2 replaces the bodies below with a real implementation against
Ashby's public posting API, keyed by board name — verify the current endpoint
shape against vendor docs before that build, per spec §5.3's build note.
"""

from gtm_agent.core.fetch import Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult

logger = get_logger(__name__)


class AshbyAdapter:
    platform = AtsPlatform.ASHBY

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        logger.info("ashby_adapter_not_implemented", company_id=source.company_id)
        return StageResult(
            status=ExtractionStatus.NOT_IMPLEMENTED,
            detail="Ashby adapter is a Phase 1 placeholder; real API integration lands in Phase 2",
        )

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        raise NotImplementedError("Ashby hydrate() is not implemented until Phase 2")
