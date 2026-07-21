"""Compliance & Politeness — spec §21.6: erasure and suppression (Phase 5).

"Two mechanisms, both cheap now and painful to retrofit":
- Company denylist — checked at Stage 1, honoured immediately, never re-resolved.
- Person suppression list — checked at Stage 6, so the next Apollo sweep
  doesn't silently re-add an erased lead. "Deletion without suppression is
  not erasure."
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CompanyDenylistEntry(BaseModel):
    domain: str
    reason: str | None = None
    added_at: datetime


class PersonSuppressionEntry(BaseModel):
    key: str
    """Normalised identity key — see `leads.compliance.suppression_key`.
    Not a `lead_id`: a suppression request must survive the lead being
    re-discovered under a fresh Apollo `source_person_id` later, so the key
    is derived from durable identity (email, or name+company) rather than
    any internal ID that changes across re-discovery.
    """
    reason: str | None = None
    added_at: datetime
