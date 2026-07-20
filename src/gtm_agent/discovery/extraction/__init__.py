"""Stage 3 — Extraction (spec §6). Registers the default set of adapters on import.

Greenhouse, Lever, Ashby, and generic-HTML are Phase 1 placeholders (see their
modules' docstrings). JSON-LD is fully implemented — it needs no third-party
API, only parsing of an already-fetched page.
"""

from gtm_agent.discovery.extraction.ashby import AshbyAdapter
from gtm_agent.discovery.extraction.base import BoardAdapter
from gtm_agent.discovery.extraction.generic_html import GenericHtmlAdapter
from gtm_agent.discovery.extraction.greenhouse import GreenhouseAdapter
from gtm_agent.discovery.extraction.jsonld import JsonLdAdapter
from gtm_agent.discovery.extraction.lever import LeverAdapter
from gtm_agent.discovery.extraction.registry import get_adapter, register_adapter, registered_platforms

register_adapter(GreenhouseAdapter())
register_adapter(LeverAdapter())
register_adapter(AshbyAdapter())
register_adapter(JsonLdAdapter())
register_adapter(GenericHtmlAdapter())

__all__ = [
    "AshbyAdapter",
    "BoardAdapter",
    "GenericHtmlAdapter",
    "GreenhouseAdapter",
    "JsonLdAdapter",
    "LeverAdapter",
    "get_adapter",
    "register_adapter",
    "registered_platforms",
]
