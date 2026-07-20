"""Stage 4 — Normalisation (spec §7).

Goal: `RawPosting` (platform-shaped) -> `JobPosting` (canonical), with
provenance and confidence on every derived field. Per §7's own goal
statement, the input is explicitly "platform-shaped" — this module dispatches
on `RawPosting.source_platform` to read each ATS's native field names for
title, description, location, department, employment type, and `posted_at`
(spec §7.5, §7.7 both explicitly call for "ATS-native fields", not just
JSON-LD's). Greenhouse, Lever, and Ashby each get their own extraction
branch; JSON-LD (and any other/unknown platform) falls through to the
original schema.org-shaped logic, unchanged.

Only the *extraction* step is platform-aware. Title canonicalisation,
function/seniority classification, description cleaning, and location
structuring stay platform-agnostic — they operate on whatever the
extraction step hands them, exactly as before.

Phase 1 scope and simplifications still in force, called out explicitly
rather than hidden:
    - Only structured (dict) payloads are normalised for real. A string
      (HTML) payload — the generic-HTML adapter's eventual output — is
      normalised into a maximally-degraded `JobPosting` rather than guessed
      at.
    - Function/seniority classification (§7.3) is rules-only. Spec §7.3 also
      wants an LLM fallback for titles the rules can't resolve; that depends
      on `services.azure_openai`, which is a config-only stub in Phase 1 —
      see the TODO in `_classify_function`.
    - Location parsing (§7.4) and description cleaning (§7.6) implement the
      common cases (multi-location strings, remote/hybrid qualifiers,
      structured places, basic HTML-to-text) but not the full
      boilerplate-stripping and non-English handling the spec describes.
    - Compensation (§7.5) is extracted only from structured `baseSalary` —
      never inferred via regex over description text, per spec. No
      platform-specific compensation field was found on any of the three
      real ATS payloads examined, so this stays JSON-LD-only; that's an
      absence of data, not a gap in this normalizer.
    - `job_id` derivation implements the §8.1 identity ladder (ATS-native ID,
      canonical URL, content hash) because `JobPosting.job_id` is required —
      but this is *not* Stage 5. There is no diffing against a previous run,
      no lifecycle (OPEN/MISSING/CLOSED), and no event emission here; that's
      Phase 2 (spec §8).
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.common import (
    Compensation,
    EmploymentType,
    JobFunction,
    Location,
    Provenance,
    Seniority,
    WorkplaceType,
)
from gtm_agent.models.job import JobPosting, RawPosting

# --- Title canonicalisation (spec §7.2) ---
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]+",
    flags=re.UNICODE,
)
_TRAILING_PAREN_LOCATION_RE = re.compile(
    r"\s*\([^)]*(?:remote|hybrid|onsite|us|usa|uk|emea|nyc|sf)[^)]*\)\s*$", re.I
)
_REQ_ID_RE = re.compile(r"\s*[-–—(\[]?\s*(?:req|job)[\s#-]*\d{3,}\)?\]?\s*$", re.I)
_WHITESPACE_RE = re.compile(r"\s+")
_ABBREVIATIONS: dict[str, str] = {
    # `\b` sits right after the letters, not after the optional trailing dot —
    # a `.` is a non-word character, so `\bsr\.?\b` fails to match "Sr. " (no
    # boundary between "." and the following space). Consuming the dot after
    # the boundary check avoids leaving a stray "." in the output.
    r"\bsr\b\.?": "Senior",
    r"\bjr\b\.?": "Junior",
    r"\beng\b\.?": "Engineer",
    r"\bmgr\b\.?": "Manager",
    r"\bdir\b\.?": "Director",
}


def _canonicalize_title(title_raw: str) -> str:
    """Conservative by design (spec §7.2): over-normalisation destroys signal
    like the "Founding Engineer" vs "Engineer" distinction.
    """
    if not title_raw:
        return ""
    text = _EMOJI_RE.sub("", title_raw)
    text = _TRAILING_PAREN_LOCATION_RE.sub("", text)
    text = _REQ_ID_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = re.sub(r"\s*/\s*", " / ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    for pattern, replacement in _ABBREVIATIONS.items():
        text = re.sub(pattern, replacement, text, flags=re.I)
    if text and (text == text.lower() or text == text.upper()):
        text = text.title()
    return text.strip()


# --- Platform-aware field extraction (spec §7 goal: "RawPosting (platform-shaped)") ---
def _extract_title(platform: str, payload: dict[str, Any]) -> str:
    # Lever's title key is `text`; Greenhouse, Ashby, and JSON-LD all use `title`.
    key = "text" if platform == AtsPlatform.LEVER else "title"
    return str(payload.get(key) or "").strip()


def _extract_department(platform: str, payload: dict[str, Any]) -> str | None:
    if platform == AtsPlatform.GREENHOUSE:
        departments = payload.get("departments")
        if isinstance(departments, list) and departments and isinstance(departments[0], dict):
            name = departments[0].get("name")
            return name if isinstance(name, str) else None
        return None

    if platform == AtsPlatform.LEVER:
        categories = payload.get("categories")
        if isinstance(categories, dict):
            name = categories.get("department")
            return name if isinstance(name, str) else None
        return None

    # Ashby uses a flat top-level "department" string already; JSON-LD has no
    # standard equivalent but the same lookup is harmless (spec §7 predates
    # this dispatch and always used this key uniformly).
    value = payload.get("department")
    return value if isinstance(value, str) else None


def _extract_description_html(platform: str, payload: dict[str, Any]) -> str:
    if platform == AtsPlatform.GREENHOUSE:
        return str(payload.get("content") or "")
    if platform == AtsPlatform.ASHBY:
        return str(payload.get("descriptionHtml") or "")
    # Lever and JSON-LD both use "description".
    return str(payload.get("description") or "")


# --- Function / seniority classification (spec §7.3) ---
_FUNCTION_KEYWORDS: dict[JobFunction, tuple[str, ...]] = {
    JobFunction.ENGINEERING: (
        "engineer", "engineering", "developer", "swe", "sre", "devops",
        "infrastructure", "backend", "frontend", "full stack", "fullstack", "software",
    ),
    JobFunction.PRODUCT: ("product manager", "product owner", "product lead", "growth pm"),
    JobFunction.DESIGN: ("designer", "design ", " design", "ux", "ui"),
    JobFunction.DATA: (
        "data scientist", "data engineer", "data analyst", "machine learning",
        "ml engineer", "analytics",
    ),
    JobFunction.SALES: (
        "sales", "account executive", "account manager", "business development", "bdr", "sdr",
    ),
    JobFunction.MARKETING: ("marketing", "growth", "content", "brand", "seo"),
    JobFunction.CUSTOMER_SUCCESS: ("customer success", "customer support", "support engineer", "csm"),
    JobFunction.OPERATIONS: ("operations", " ops", "chief of staff", "program manager"),
    JobFunction.FINANCE: ("finance", "accounting", "controller", "fp&a"),
    JobFunction.PEOPLE: ("people ", "recruiter", "recruiting", "talent", "human resources", " hr "),
    JobFunction.LEGAL: ("legal", "counsel", "compliance"),
}

_SENIORITY_KEYWORDS: tuple[tuple[Seniority, tuple[str, ...]], ...] = (
    (Seniority.FOUNDING, ("founding",)),
    (Seniority.EXECUTIVE, ("chief ", "ceo", "cto", "coo", "cfo", "cpo", "cro", "cmo", "founder")),
    (Seniority.VP, ("vp ", "vice president")),
    (Seniority.DIRECTOR, ("director",)),
    (Seniority.MANAGER, ("manager", "head of")),
    (Seniority.PRINCIPAL, ("principal", "lead ")),
    (Seniority.STAFF, ("staff",)),
    (Seniority.SENIOR, ("senior",)),
    (Seniority.ENTRY, ("junior", "entry", "associate")),
    (Seniority.INTERN, ("intern",)),
)


def _classify_function(title_canonical: str, department_raw: str | None) -> tuple[JobFunction | None, Provenance]:
    """Rules first, per spec §7.3. Only titles the rules fail to classify would
    go to an LLM in a later phase — see the TODO below.
    """
    haystack = f"{title_canonical} {department_raw or ''}".lower()
    for function, keywords in _FUNCTION_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            return function, Provenance(
                source="rules_classifier", confidence=0.85, derived_at=datetime.now(UTC)
            )
    # TODO(phase 2+): fall back to an LLM call for titles the rules can't
    # classify, cached by canonical title (spec §7.3). `services.azure_openai`
    # has no scoring logic yet, so unclassified titles stay `None` in Phase 1.
    return None, Provenance(
        source="rules_classifier_no_match",
        confidence=0.3,
        derived_at=datetime.now(UTC),
        notes="no keyword matched; LLM residue path not implemented in Phase 1",
    )


def _classify_seniority(title_canonical: str) -> tuple[Seniority | None, Provenance]:
    haystack = title_canonical.lower()
    for seniority, keywords in _SENIORITY_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return seniority, Provenance(
                source="rules_classifier", confidence=0.85, derived_at=datetime.now(UTC)
            )
    return None, Provenance(
        source="rules_classifier_no_match",
        confidence=0.3,
        derived_at=datetime.now(UTC),
        notes="no keyword matched; LLM residue path not implemented in Phase 1",
    )


# --- Location parsing (spec §7.4) ---
def _stringify_schema_place(place: dict[str, Any]) -> str:
    address = place.get("address") if isinstance(place.get("address"), dict) else {}
    parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
    return ", ".join(p for p in parts if isinstance(p, str) and p)


def _location_from_schema_place(place: dict[str, Any]) -> Location:
    address = place.get("address") if isinstance(place.get("address"), dict) else {}
    country = address.get("addressCountry")
    return Location(
        city=address.get("addressLocality"),
        region=address.get("addressRegion"),
        country=country if isinstance(country, str) else None,
        raw=_stringify_schema_place(place),
    )


def _split_location_string(raw: str) -> list[Location]:
    parts = re.split(r"\s*/\s*|\s*;\s*", raw)
    locations = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        is_remote = bool(re.search(r"\bremote\b", part, re.I))
        locations.append(Location(raw=part, is_remote=is_remote, city=None if is_remote else part))
    return locations or [Location(raw=raw)]


def _parse_location_schema_org(
    payload: dict[str, Any], description_text: str
) -> tuple[str | None, list[Location], WorkplaceType | None, str | None]:
    """JSON-LD / schema.org-shaped location parsing. Unchanged from before this
    module dispatched by platform — kept verbatim so JSON-LD behaviour is
    identical to what it was.
    """
    job_location = payload.get("jobLocation")
    location_raw: str | None = None
    locations: list[Location] = []

    if isinstance(job_location, dict):
        location_raw = _stringify_schema_place(job_location) or None
        locations = [_location_from_schema_place(job_location)]
    elif isinstance(job_location, list):
        places = [p for p in job_location if isinstance(p, dict)]
        parts = [_stringify_schema_place(p) for p in places]
        location_raw = " / ".join(p for p in parts if p) or None
        locations = [_location_from_schema_place(p) for p in places]
    elif isinstance(job_location, str):
        location_raw = job_location
        locations = _split_location_string(job_location)

    workplace_type: WorkplaceType | None = None
    job_location_type = payload.get("jobLocationType")
    if isinstance(job_location_type, str) and job_location_type.upper() == "TELECOMMUTE":
        workplace_type = WorkplaceType.REMOTE
    elif location_raw and re.search(r"\bremote\b", location_raw, re.I):
        workplace_type = WorkplaceType.REMOTE
    elif location_raw and re.search(r"\bhybrid\b", location_raw, re.I):
        workplace_type = WorkplaceType.HYBRID

    # Spec §7.4: the description is often the only place hybrid expectations
    # are stated, and should win when it contradicts the location string.
    if re.search(r"\bhybrid\b", description_text, re.I) and workplace_type != WorkplaceType.HYBRID:
        workplace_type = WorkplaceType.HYBRID

    remote_scope: str | None = None
    remote_match = re.search(r"remote\s*\(([^)]+)\)", location_raw or "", re.I)
    if remote_match:
        remote_scope = remote_match.group(1)

    if workplace_type == WorkplaceType.REMOTE:
        for location in locations:
            location.is_remote = True
            location.remote_scope = location.remote_scope or remote_scope

    return location_raw, locations, workplace_type, remote_scope


def _resolve_hybrid_override(workplace_type: WorkplaceType | None, description_text: str) -> WorkplaceType | None:
    """Spec §7.4: the description is often the only place hybrid expectations
    are stated, and should win when it contradicts other signals.
    """
    if re.search(r"\bhybrid\b", description_text, re.I) and workplace_type != WorkplaceType.HYBRID:
        return WorkplaceType.HYBRID
    return workplace_type


def _extract_remote_scope(location_raw: str | None) -> str | None:
    remote_match = re.search(r"remote\s*\(([^)]+)\)", location_raw or "", re.I)
    return remote_match.group(1) if remote_match else None


def _apply_remote_flag(locations: list[Location], workplace_type: WorkplaceType | None, remote_scope: str | None) -> None:
    if workplace_type == WorkplaceType.REMOTE:
        for location in locations:
            location.is_remote = True
            location.remote_scope = location.remote_scope or remote_scope


def _parse_location_greenhouse(
    payload: dict[str, Any], description_text: str
) -> tuple[str | None, list[Location], WorkplaceType | None, str | None]:
    """Greenhouse's `location` is `{"name": "Remote, Italy"}` — a single
    combined string, not schema.org's nested Place. Splitting it on comma the
    way schema.org multi-location strings are split on `/` would wrongly turn
    "Remote, Italy" into two fake locations, so it's kept as one entry.
    Greenhouse exposes no direct workplace-type field, so the regex fallback
    (same as the schema.org path) is the only signal available.
    """
    location = payload.get("location")
    name = location.get("name") if isinstance(location, dict) else None
    location_raw = name if isinstance(name, str) and name else None

    locations: list[Location] = []
    if location_raw:
        is_remote = bool(re.search(r"\bremote\b", location_raw, re.I))
        locations = [Location(raw=location_raw, is_remote=is_remote)]

    workplace_type: WorkplaceType | None = None
    if location_raw:
        if re.search(r"\bremote\b", location_raw, re.I):
            workplace_type = WorkplaceType.REMOTE
        elif re.search(r"\bhybrid\b", location_raw, re.I):
            workplace_type = WorkplaceType.HYBRID
    workplace_type = _resolve_hybrid_override(workplace_type, description_text)

    remote_scope = _extract_remote_scope(location_raw)
    _apply_remote_flag(locations, workplace_type, remote_scope)

    return location_raw, locations, workplace_type, remote_scope


_LEVER_WORKPLACE_TYPE_MAP: dict[str, WorkplaceType] = {
    "remote": WorkplaceType.REMOTE,
    "hybrid": WorkplaceType.HYBRID,
    "onsite": WorkplaceType.ONSITE,
}


def _parse_location_lever(
    payload: dict[str, Any], description_text: str
) -> tuple[str | None, list[Location], WorkplaceType | None, str | None]:
    """Lever's `categories.allLocations` is already a clean, pre-split array —
    a strictly better source than regex-splitting a combined string, so it's
    used directly when present. `categories.workplaceType` is a direct
    structured signal and is preferred over the regex fallback (spec's
    general preference for structured fields over guessing, §7.5).
    """
    categories = payload.get("categories") if isinstance(payload.get("categories"), dict) else {}

    primary_location = categories.get("location")
    location_raw = primary_location if isinstance(primary_location, str) and primary_location else None

    locations: list[Location] = []
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list) and all_locations:
        for entry in all_locations:
            if isinstance(entry, str) and entry:
                is_remote = bool(re.search(r"\bremote\b", entry, re.I))
                locations.append(Location(raw=entry, is_remote=is_remote))
    elif location_raw:
        is_remote = bool(re.search(r"\bremote\b", location_raw, re.I))
        locations = [Location(raw=location_raw, is_remote=is_remote)]

    workplace_type: WorkplaceType | None = None
    workplace_type_raw = payload.get("workplaceType")
    if isinstance(workplace_type_raw, str):
        workplace_type = _LEVER_WORKPLACE_TYPE_MAP.get(workplace_type_raw.strip().lower())

    if workplace_type is None and location_raw:
        if re.search(r"\bremote\b", location_raw, re.I):
            workplace_type = WorkplaceType.REMOTE
        elif re.search(r"\bhybrid\b", location_raw, re.I):
            workplace_type = WorkplaceType.HYBRID
    workplace_type = _resolve_hybrid_override(workplace_type, description_text)

    remote_scope = _extract_remote_scope(location_raw)
    _apply_remote_flag(locations, workplace_type, remote_scope)

    return location_raw, locations, workplace_type, remote_scope


_ASHBY_WORKPLACE_TYPE_MAP: dict[str, WorkplaceType] = {
    "remote": WorkplaceType.REMOTE,
    "hybrid": WorkplaceType.HYBRID,
    "onsite": WorkplaceType.ONSITE,
}


def _parse_location_ashby(
    payload: dict[str, Any], description_text: str
) -> tuple[str | None, list[Location], WorkplaceType | None, str | None]:
    """Ashby's `location` is a flat string; `secondaryLocations` holds any
    additional ones. `workplaceType` and the boolean `isRemote` are direct
    structured signals, preferred over the regex fallback. Every board
    observed live during this build had an empty `secondaryLocations`, so its
    populated shape (plain strings vs. `{"location": ...}` objects) is
    handled defensively rather than from a verified example — flagging that
    explicitly rather than overclaiming certainty.
    """
    primary = payload.get("location")
    location_raw = primary if isinstance(primary, str) and primary else None

    locations: list[Location] = []
    if location_raw:
        is_remote = bool(payload.get("isRemote")) or bool(re.search(r"\bremote\b", location_raw, re.I))
        locations.append(Location(raw=location_raw, is_remote=is_remote))

    secondary = payload.get("secondaryLocations")
    if isinstance(secondary, list):
        for entry in secondary:
            name = entry if isinstance(entry, str) else (entry.get("location") if isinstance(entry, dict) else None)
            if isinstance(name, str) and name:
                is_remote = bool(re.search(r"\bremote\b", name, re.I))
                locations.append(Location(raw=name, is_remote=is_remote))

    workplace_type: WorkplaceType | None = None
    workplace_type_raw = payload.get("workplaceType")
    if isinstance(workplace_type_raw, str):
        workplace_type = _ASHBY_WORKPLACE_TYPE_MAP.get(workplace_type_raw.strip().lower())
    if workplace_type is None and payload.get("isRemote") is True:
        workplace_type = WorkplaceType.REMOTE

    if workplace_type is None and location_raw:
        if re.search(r"\bremote\b", location_raw, re.I):
            workplace_type = WorkplaceType.REMOTE
        elif re.search(r"\bhybrid\b", location_raw, re.I):
            workplace_type = WorkplaceType.HYBRID
    workplace_type = _resolve_hybrid_override(workplace_type, description_text)

    remote_scope = _extract_remote_scope(location_raw)
    _apply_remote_flag(locations, workplace_type, remote_scope)

    return location_raw, locations, workplace_type, remote_scope


def _parse_location(
    platform: str, payload: dict[str, Any], description_text: str
) -> tuple[str | None, list[Location], WorkplaceType | None, str | None]:
    if platform == AtsPlatform.GREENHOUSE:
        return _parse_location_greenhouse(payload, description_text)
    if platform == AtsPlatform.LEVER:
        return _parse_location_lever(payload, description_text)
    if platform == AtsPlatform.ASHBY:
        return _parse_location_ashby(payload, description_text)
    return _parse_location_schema_org(payload, description_text)


# --- Employment type + compensation (spec §7.5) ---
_EMPLOYMENT_TYPE_MAP: dict[str, EmploymentType] = {
    "FULL_TIME": EmploymentType.FULL_TIME,
    "PART_TIME": EmploymentType.PART_TIME,
    "CONTRACTOR": EmploymentType.CONTRACT,
    "TEMPORARY": EmploymentType.CONTRACT,
    "INTERN": EmploymentType.INTERNSHIP,
}


def _map_employment_type(value: Any) -> EmploymentType | None:  # noqa: ANN401 — schema.org field is loosely typed
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    return _EMPLOYMENT_TYPE_MAP.get(value.upper())


_LEVER_EMPLOYMENT_TYPE_MAP: dict[str, EmploymentType] = {
    "full time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
}

_ASHBY_EMPLOYMENT_TYPE_MAP: dict[str, EmploymentType] = {
    "fulltime": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
}


def _extract_employment_type(platform: str, payload: dict[str, Any]) -> EmploymentType | None:
    if platform == AtsPlatform.LEVER:
        categories = payload.get("categories") if isinstance(payload.get("categories"), dict) else {}
        commitment = categories.get("commitment")
        if isinstance(commitment, str):
            return _LEVER_EMPLOYMENT_TYPE_MAP.get(commitment.strip().lower())
        return None

    if platform == AtsPlatform.ASHBY:
        value = payload.get("employmentType")
        if isinstance(value, str):
            return _ASHBY_EMPLOYMENT_TYPE_MAP.get(value.strip().lower())
        return None

    # Greenhouse exposes no employment-type field in the observed shape —
    # falls through here and correctly returns None. JSON-LD keeps its
    # existing schema.org enum handling, unchanged.
    return _map_employment_type(payload.get("employmentType"))


def _extract_compensation(base_salary: Any) -> Compensation | None:  # noqa: ANN401
    """Only from structured fields; never inferred from description text (spec §7.5)."""
    if not isinstance(base_salary, dict):
        return None
    value = base_salary.get("value")
    if not isinstance(value, dict):
        return None

    min_amount = value.get("minValue")
    max_amount = value.get("maxValue")
    if min_amount is None and max_amount is None:
        single = value.get("value")
        min_amount = max_amount = single
    if min_amount is None and max_amount is None:
        return None

    period = value.get("unitText")
    return Compensation(
        min_amount=float(min_amount) if min_amount is not None else None,
        max_amount=float(max_amount) if max_amount is not None else None,
        currency=base_salary.get("currency"),
        period=str(period).lower() if isinstance(period, str) else None,
        equity_mentioned=False,  # regex-over-description equity detection deferred
    )


# --- Description cleaning (spec §7.6) ---
def _clean_description(description_html: str) -> tuple[str, str]:
    """Basic HTML -> text pass. Boilerplate stripping (EEO statements, benefits
    blocks) and structure-preserving markdown are deferred to a later phase.
    """
    if not description_html:
        return "", ""
    tree = HTMLParser(description_html)
    text = (tree.body.text(separator="\n", strip=True) if tree.body else tree.text(separator="\n", strip=True))
    return text, text


# --- posted_at inference (spec §7.7) ---
def _native_posted_at_field(platform: str, payload: dict[str, Any]) -> tuple[Any, str]:
    """Each ATS's own authoritative posting-date field — spec §7.7 step 1:
    "Use the ATS-native field if present (authoritative)". Returns the raw
    value plus a provenance label; step 2 (JSON-LD `datePosted`) and step 3
    (inference) are handled by the caller, unchanged.
    """
    if platform == AtsPlatform.GREENHOUSE:
        # Not `updated_at` — that's a last-modified timestamp, not a posting date.
        return payload.get("first_published"), "greenhouse_first_published"
    if platform == AtsPlatform.LEVER:
        return payload.get("createdAt"), "lever_createdAt"
    if platform == AtsPlatform.ASHBY:
        return payload.get("publishedAt"), "ashby_publishedAt"
    return None, ""


def _parse_native_posted_at(platform: str, value: Any) -> datetime | None:  # noqa: ANN401
    if platform == AtsPlatform.LEVER:
        # Lever's createdAt is epoch milliseconds, not an ISO string.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(value / 1000, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return None
        return None

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _infer_posted_at(
    platform: str, payload: dict[str, Any], fetched_at: datetime
) -> tuple[datetime | None, bool, Provenance]:
    native_value, native_source = _native_posted_at_field(platform, payload)
    if native_value is not None:
        parsed = _parse_native_posted_at(platform, native_value)
        if parsed is not None:
            return parsed, False, Provenance(source=native_source, confidence=0.95, derived_at=datetime.now(UTC))

    date_posted = payload.get("datePosted")
    if isinstance(date_posted, str):
        try:
            parsed = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return parsed, False, Provenance(
                source="jsonld_datePosted", confidence=0.95, derived_at=datetime.now(UTC)
            )

    return fetched_at, True, Provenance(
        source="inferred_first_seen",
        confidence=0.3,
        derived_at=datetime.now(UTC),
        notes="lower bound only; unreliable on a company's first run (spec §7.7)",
    )


# --- Job identity (spec §8.1 ladder; not full Stage 5 — see module docstring) ---
def _derive_job_id(raw: RawPosting, title_canonical: str, department_raw: str | None) -> str:
    if raw.source_job_id:
        return f"{raw.company_id}:{raw.source_platform}:{raw.source_job_id}"
    if raw.posting_url:
        digest = hashlib.sha256(raw.posting_url.encode()).hexdigest()[:16]
        return f"{raw.company_id}:url:{digest}"
    # Terminal fallback. Deliberately excludes the description (spec §8.1) so
    # a typo fix doesn't create a phantom new job.
    basis = f"{raw.company_id}|{title_canonical}|{department_raw or ''}"
    digest = hashlib.sha256(basis.encode()).hexdigest()[:16]
    return f"{raw.company_id}:hash:{digest}"


def _degraded_posting(raw: RawPosting) -> JobPosting:
    """For raw payloads normalisation can't yet handle (unstructured HTML)."""
    now = datetime.now(UTC)
    return JobPosting(
        job_id=_derive_job_id(raw, "", None),
        company_id=raw.company_id,
        source_job_id=raw.source_job_id,
        source_platform=raw.source_platform,
        posting_url=raw.posting_url or "",
        title_raw="",
        title_canonical="",
        description_text="",
        description_markdown="",
        first_seen_at=raw.fetched_at,
        last_seen_at=raw.fetched_at,
        field_provenance={
            "_all": Provenance(
                source=raw.source_platform,
                confidence=0.0,
                derived_at=now,
                notes="raw_payload is unstructured HTML; generic-HTML normalisation is Phase 2 work",
            )
        },
        extraction_confidence=0.0,
        is_degraded=True,
    )


def normalize(raw: RawPosting) -> JobPosting:
    """`RawPosting` -> `JobPosting`. See module docstring for Phase 1 scope."""
    if not isinstance(raw.raw_payload, dict):
        return _degraded_posting(raw)

    payload = raw.raw_payload
    platform = raw.source_platform

    title_raw = _extract_title(platform, payload)
    title_canonical = _canonicalize_title(title_raw)

    department_raw = _extract_department(platform, payload)

    function, function_provenance = _classify_function(title_canonical, department_raw)
    seniority, seniority_provenance = _classify_seniority(title_canonical)

    description_html = _extract_description_html(platform, payload)
    description_text, description_markdown = _clean_description(description_html)

    location_raw, locations, workplace_type, remote_scope = _parse_location(platform, payload, description_text)

    employment_type = _extract_employment_type(platform, payload)
    compensation = _extract_compensation(payload.get("baseSalary"))

    posted_at, posted_at_is_inferred, posted_at_provenance = _infer_posted_at(platform, payload, raw.fetched_at)

    job_id = _derive_job_id(raw, title_canonical, department_raw)

    field_provenance = {
        "title_canonical": Provenance(source=raw.source_platform, confidence=0.9, derived_at=datetime.now(UTC)),
        "function": function_provenance,
        "seniority": seniority_provenance,
        "posted_at": posted_at_provenance,
    }

    return JobPosting(
        job_id=job_id,
        company_id=raw.company_id,
        source_job_id=raw.source_job_id,
        source_platform=raw.source_platform,
        posting_url=raw.posting_url or "",
        title_raw=title_raw,
        title_canonical=title_canonical,
        description_text=description_text,
        description_markdown=description_markdown,
        department_raw=department_raw,
        function=function,
        seniority=seniority,
        location_raw=location_raw,
        locations=locations,
        workplace_type=workplace_type,
        remote_scope=remote_scope,
        employment_type=employment_type,
        compensation=compensation,
        posted_at=posted_at,
        posted_at_is_inferred=posted_at_is_inferred,
        first_seen_at=raw.fetched_at,
        last_seen_at=raw.fetched_at,
        field_provenance=field_provenance,
        extraction_confidence=1.0 if raw.is_hydrated else 0.7,
        is_degraded=False,
    )


def normalize_batch(raw_postings: list[RawPosting]) -> list[JobPosting]:
    return [normalize(raw) for raw in raw_postings]
