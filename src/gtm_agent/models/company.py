"""The `company` table — spec §15.1. The input list to the whole pipeline."""

from datetime import datetime

from pydantic import BaseModel


class Company(BaseModel):
    id: str
    name: str
    domain: str
    funding_stage: str | None = None
    added_at: datetime
    is_active: bool = True
