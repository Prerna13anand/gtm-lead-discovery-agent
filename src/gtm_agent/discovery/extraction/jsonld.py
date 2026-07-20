"""Structured-HTML adapter — `schema.org/JobPosting` JSON-LD (spec §6.2.2).

Unlike the ATS-API adapters, this one is implemented in Phase 1: it parses
JSON-LD out of a page we already fetched rather than integrating a
third-party API, so there is no vendor endpoint to verify against.

"Always attempt JSON-LD extraction before falling back to DOM parsing,
including on pages that were rendered — it costs one parse of already-fetched
content and frequently obviates the harder path." (spec §6.2.2)
"""

import json
from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from gtm_agent.core.fetch import FetchError, Fetcher
from gtm_agent.core.logging import get_logger
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult

logger = get_logger(__name__)


def _iter_jsonld_objects(data: Any) -> list[dict[str, Any]]:  # noqa: ANN401 — JSON-LD is untyped by nature
    """Flatten the shapes JSON-LD commonly appears in: a bare object, a list
    of objects, or an object with an `@graph` array.
    """
    if isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            return [obj for obj in data["@graph"] if isinstance(obj, dict)]
        return [data]
    if isinstance(data, list):
        return [obj for obj in data if isinstance(obj, dict)]
    return []


class JsonLdAdapter:
    platform = AtsPlatform.JSONLD

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        try:
            result = await fetcher.get(source.careers_url)
        except FetchError as exc:
            logger.info("jsonld_fetch_failed", company_id=source.company_id, error=str(exc))
            return StageResult(status=ExtractionStatus.RATE_LIMITED, detail=str(exc))

        if result.status_code in (401, 403):
            return StageResult(status=ExtractionStatus.BLOCKED_403, detail=f"HTTP {result.status_code}")
        if result.status_code >= 400:
            return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"HTTP {result.status_code}")

        tree = HTMLParser(result.text)
        postings: list[RawPosting] = []
        now = datetime.now(UTC)

        for script in tree.css('script[type="application/ld+json"]'):
            raw_text = script.text()
            if not raw_text or not raw_text.strip():
                continue
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            for obj in _iter_jsonld_objects(data):
                if obj.get("@type") != "JobPosting":
                    continue

                identifier = obj.get("identifier")
                source_job_id = None
                if isinstance(identifier, dict):
                    source_job_id = identifier.get("value")
                elif isinstance(identifier, str):
                    source_job_id = identifier

                postings.append(
                    RawPosting(
                        company_id=source.company_id,
                        source_platform=AtsPlatform.JSONLD.value,
                        source_job_id=str(source_job_id) if source_job_id else None,
                        posting_url=obj.get("url") or source.careers_url,
                        raw_payload=obj,
                        fetched_at=now,
                        is_hydrated=True,  # JSON-LD postings are inline (spec §6.2.2)
                    )
                )

        return StageResult(status=ExtractionStatus.SUCCESS, value=postings)

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        # No-op — JSON-LD postings are inline; discover() already has everything.
        return posting
