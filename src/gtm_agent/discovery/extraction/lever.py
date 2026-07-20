"""Lever adapter — spec §6.2.1, Appendix A.

PLACEHOLDER. Per explicit Phase 1 scope, this does not call the real Lever
API yet. Phase 2 replaces the bodies below with a real implementation against
`api.lever.co/v0/postings/{company}?mode=json` — verify the endpoint shape
against current vendor docs before that build, per spec §5.3's build note.
"""

from gtm_agent.core.fetch import Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult

logger = get_logger(__name__)


class LeverAdapter:
    platform = AtsPlatform.LEVER

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        logger.info("lever_adapter_not_implemented", company_id=source.company_id)
        return StageResult(
            status=ExtractionStatus.NOT_IMPLEMENTED,
            detail="Lever adapter is a Phase 1 placeholder; real API integration lands in Phase 2",
        )

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        raise NotImplementedError("Lever hydrate() is not implemented until Phase 2")
