"""Stage 2 output — spec §5 and Appendix A.

Phase 1 wires detection and routing for Greenhouse, Lever, and Ashby, plus the
JSON-LD and generic-HTML fallback paths. The remaining platforms in Appendix A
(Workable, SmartRecruiters, Recruitee, Rippling, Personio) are listed here so
the enum is stable, but have no adapter until Phase 2.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class AtsPlatform(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"

    # Recognised per Appendix A, adapters deferred to Phase 2.
    WORKABLE = "workable"
    SMARTRECRUITERS = "smartrecruiters"
    RECRUITEE = "recruitee"
    RIPPLING = "rippling"
    PERSONIO = "personio"

    # Platform-independent extraction paths (spec §6.2.2-4).
    JSONLD = "jsonld"
    GENERIC_HTML = "generic_html"
    RENDERED_DOM = "rendered_dom"

    UNKNOWN = "unknown"


class DetectionSignal(StrEnum):
    """Signals in confidence order — spec §5.1. Multiple corroborating signals raise confidence."""

    URL_HOST_MATCH = "url_host_match"
    REDIRECT_TARGET = "redirect_target"
    EMBEDDED_SCRIPT_OR_IFRAME = "embedded_script_or_iframe"
    DOM_MARKERS = "dom_markers"
    NETWORK_REQUESTS = "network_requests"
    DNS_CNAME = "dns_cname"
    MANUAL = "manual"


class AtsIdentification(BaseModel):
    id: str | None = None
    company_id: str

    platform: AtsPlatform
    board_token: str | None = None  # None when platform is UNKNOWN/GENERIC_HTML
    confidence: float = Field(ge=0.0, le=1.0)
    detection_signal: DetectionSignal

    last_verified_at: datetime | None = None
    created_at: datetime
