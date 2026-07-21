"""Stage 4 — LLM residue classification (spec §7.3).

"Rules first, LLM for the residue. A curated keyword-and-pattern classifier
resolves the large majority of titles deterministically, at zero cost, with
reproducible output. Only titles the rules fail to classify go to an LLM
call, and results are cached by canonical title so each distinct title is
classified once ever."

Deliberately **not** part of `discovery.normalization.normalize()` /
`normalize_batch()` — those stay synchronous and network-free, so every
existing call site (this codebase's ~20 normalisation tests, `main.py`'s
Stage 4 section) keeps working completely unchanged. This module is an
optional, explicitly-invoked async second pass over `normalize_batch`'s
output: it finds postings `classify_function`/`classify_seniority` left
unresolved (`function is None` or `seniority is None`), and fills them in
via a cached LLM call. A caller that never invokes it (or has no Azure
OpenAI credentials configured) gets exactly Phase 1-3's original behaviour —
unresolved titles simply stay `None`, which is a real, honest state per
spec §2.9, not a regression.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from openai import APIError
from pydantic import BaseModel, ValidationError

from gtm_agent.core.logging import get_logger
from gtm_agent.core.title_classification_cache import TitleClassificationCache, TitleClassificationEntry
from gtm_agent.models.common import JobFunction, Provenance, Seniority
from gtm_agent.models.job import JobPosting
from gtm_agent.services.azure_openai import AzureOpenAIConfigError, AzureOpenAIService

logger = get_logger(__name__)

_MAX_ATTEMPTS = 2  # same "retry once" convention as Stage 10 (spec §13.5)

_SYSTEM_PROMPT = """You classify a startup job title into exactly one function and one seniority level, for a \
GTM (go-to-market) lead-matching pipeline. A deterministic rules-based classifier already tried and failed to \
resolve this title from keywords alone — you are the fallback for titles that don't contain an obvious keyword \
(e.g. an unusual title, a non-English title, or a title that only makes sense from context).

Pick the single best-fitting function and seniority from the categories you are given, even if imperfect. Do \
not invent a category outside the provided enum. If a title is genuinely ambiguous, prefer the more common, \
plainer reading over a speculative one."""


class ResidueClassification(BaseModel):
    function: JobFunction
    seniority: Seniority


def _user_prompt(title_canonical: str, department_raw: str | None) -> str:
    lines = [f"Title: {title_canonical}"]
    if department_raw:
        lines.append(f"Department: {department_raw}")
    return "\n".join(lines)


async def classify_title_residue(
    title_canonical: str,
    department_raw: str | None,
    *,
    llm: AzureOpenAIService | None = None,
) -> ResidueClassification | None:
    """One LLM call (with one retry on schema violation, spec §13.5's
    convention reused here). Returns `None` — never raises — on
    misconfiguration or exhausted retries, so a caller can degrade to
    "still unclassified" exactly as if this module didn't exist.
    """
    service = llm or AzureOpenAIService()
    if not service.is_configured:
        return None

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(title_canonical, department_raw)},
    ]

    for attempt in range(_MAX_ATTEMPTS):
        try:
            client = service.get_client()
            completion = await asyncio.to_thread(
                client.beta.chat.completions.parse,
                model=service.deployment,
                messages=messages,
                response_format=ResidueClassification,
            )
        except (APIError, AzureOpenAIConfigError) as exc:
            logger.warning("title_residue_attempt_failed", attempt=attempt, title=title_canonical, error=str(exc))
            continue

        parsed = completion.choices[0].message.parsed
        if parsed is None:
            logger.warning("title_residue_attempt_failed", attempt=attempt, title=title_canonical, detail="no parsed output")
            continue

        try:
            return ResidueClassification.model_validate(parsed.model_dump())
        except ValidationError as exc:
            logger.warning("title_residue_attempt_failed", attempt=attempt, title=title_canonical, error=str(exc))
            continue

    logger.warning("title_residue_classification_failed", title=title_canonical)
    return None


async def resolve_unclassified(
    jobs: list[JobPosting],
    *,
    cache: TitleClassificationCache | None = None,
    llm: AzureOpenAIService | None = None,
) -> list[JobPosting]:
    """Second pass over `normalize_batch`'s output. Only calls the LLM for
    postings still missing `function` or `seniority` after the rules
    classifier ran, and only once per distinct `title_canonical` ever
    (spec §7.3) — a title seen for the tenth time across ten companies
    costs zero additional LLM tokens.
    """
    cache = cache or TitleClassificationCache()
    now = datetime.now(UTC)
    resolved: list[JobPosting] = []

    for job in jobs:
        if job.function is not None and job.seniority is not None:
            resolved.append(job)
            continue

        cached = cache.get(job.title_canonical)
        if cached is None:
            classification = await classify_title_residue(job.title_canonical, job.department_raw, llm=llm)
            if classification is None:
                resolved.append(job)  # stays unclassified — spec §2.9, honest "we don't know"
                continue
            cached = TitleClassificationEntry(
                title_canonical=job.title_canonical, function=classification.function, seniority=classification.seniority
            )
            cache.save(cached)

        provenance = Provenance(
            source="llm_residue_classifier", confidence=0.7, derived_at=now,
            notes="rules classifier found no keyword match; LLM residue fallback per spec §7.3",
        )
        updates: dict[str, object] = {}
        field_provenance = dict(job.field_provenance)
        if job.function is None:
            updates["function"] = cached.function
            field_provenance["function"] = provenance
        if job.seniority is None:
            updates["seniority"] = cached.seniority
            field_provenance["seniority"] = provenance
        updates["field_provenance"] = field_provenance

        resolved.append(job.model_copy(update=updates))

    return resolved
