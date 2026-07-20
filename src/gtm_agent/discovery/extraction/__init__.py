"""Stage 3 — Extraction (spec §6). Registers the default set of adapters on import.

Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Rippling, and
JSON-LD are real adapters against live APIs/pages (see each module's
docstring). Generic-HTML is still a Phase 1 placeholder — the real
heuristic-DOM implementation is later Phase 2 work.
"""

from gtm_agent.discovery.extraction.ashby import AshbyAdapter
from gtm_agent.discovery.extraction.base import BoardAdapter
from gtm_agent.discovery.extraction.generic_html import GenericHtmlAdapter
from gtm_agent.discovery.extraction.greenhouse import GreenhouseAdapter
from gtm_agent.discovery.extraction.jsonld import JsonLdAdapter
from gtm_agent.discovery.extraction.lever import LeverAdapter
from gtm_agent.discovery.extraction.recruitee import RecruiteeAdapter
from gtm_agent.discovery.extraction.registry import get_adapter, register_adapter, registered_platforms
from gtm_agent.discovery.extraction.rippling import RipplingAdapter
from gtm_agent.discovery.extraction.smartrecruiters import SmartRecruitersAdapter
from gtm_agent.discovery.extraction.workable import WorkableAdapter

register_adapter(GreenhouseAdapter())
register_adapter(LeverAdapter())
register_adapter(AshbyAdapter())
register_adapter(WorkableAdapter())
register_adapter(SmartRecruitersAdapter())
register_adapter(RecruiteeAdapter())
register_adapter(RipplingAdapter())
register_adapter(JsonLdAdapter())
register_adapter(GenericHtmlAdapter())

__all__ = [
    "AshbyAdapter",
    "BoardAdapter",
    "GenericHtmlAdapter",
    "GreenhouseAdapter",
    "JsonLdAdapter",
    "LeverAdapter",
    "RecruiteeAdapter",
    "RipplingAdapter",
    "SmartRecruitersAdapter",
    "WorkableAdapter",
    "get_adapter",
    "register_adapter",
    "registered_platforms",
]
