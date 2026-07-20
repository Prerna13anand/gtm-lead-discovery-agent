"""Adapter registry — the concrete realisation of spec §2.2: "one interface,
many backends." The orchestrator asks the registry for an adapter by
platform and never branches on platform itself.
"""

from gtm_agent.discovery.extraction.base import BoardAdapter
from gtm_agent.models.ats import AtsPlatform

_REGISTRY: dict[AtsPlatform, BoardAdapter] = {}


def register_adapter(adapter: BoardAdapter) -> None:
    _REGISTRY[adapter.platform] = adapter


def get_adapter(platform: AtsPlatform) -> BoardAdapter | None:
    return _REGISTRY.get(platform)


def registered_platforms() -> list[AtsPlatform]:
    return list(_REGISTRY.keys())
