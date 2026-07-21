"""Persona ladder — spec §9.1 and Appendix C.

The title sets Stage 6 asks Apollo for, and (via `models.lead.Lead.function`
/ `.seniority` reusing the same taxonomy — spec §9.5) the ownership
expectations Stage 7 matching applies. "Starting points to be tuned against
the §19.4 labelled set — not fixed truths" (Appendix C).

Deviation from the spec's literal §9.1 pseudocode, documented per this
project's convention of flagging rather than silently resolving ambiguity:
the pseudocode's `personas_for(company, jobs)` signature takes a `company`
argument, but the algorithm it describes never reads it — only `jobs`
drives which functions to retrieve. Declaring an unused parameter would be
dead code, so `personas_for` here takes only `jobs`. Nothing about §9.1's
actual behaviour changes.
"""

from __future__ import annotations

from gtm_agent.models.common import JobFunction
from gtm_agent.models.job import JobPosting

# Appendix C.1's primary column only — the titles that genuinely *are* the
# function's owner, as distinct from a plausible-but-secondary contact.
# `leads.matching.score_ownership_language` (spec §10.3 Signal 3) keys off
# this set specifically: an exact match here is near-decisive evidence of
# ownership, which a merely-plausible secondary title (e.g. "Staff Engineer"
# for engineering) is not — conflating the two would give ordinary IC
# seniority titles the same "explicit ownership" credit as an actual
# functional head, which is not what Signal 3 is for.
PRIMARY_OWNER_TITLES: dict[JobFunction, tuple[str, ...]] = {
    JobFunction.ENGINEERING: (
        "VP Engineering", "Head of Engineering", "Director of Engineering", "Engineering Manager", "CTO",
    ),
    JobFunction.PRODUCT: (
        "VP Product", "Head of Product", "Director of Product", "Group Product Manager", "CPO",
    ),
    JobFunction.DESIGN: ("Head of Design", "Design Director", "Design Manager"),
    JobFunction.DATA: ("Head of Data", "Director of Data", "Data Science Manager", "Head of ML"),
    JobFunction.SALES: ("VP Sales", "Head of Sales", "Sales Director", "CRO", "Head of Revenue"),
    JobFunction.MARKETING: ("VP Marketing", "Head of Marketing", "CMO", "Head of Growth"),
    JobFunction.CUSTOMER_SUCCESS: ("VP Customer Success", "Head of Customer Success", "Head of Support"),
    JobFunction.OPERATIONS: ("COO", "Head of Operations", "Director of Operations", "Chief of Staff"),
    JobFunction.FINANCE: ("CFO", "VP Finance", "Head of Finance", "Controller"),
    JobFunction.PEOPLE: ("Head of People", "VP People", "CHRO", "Head of Talent"),
    JobFunction.LEGAL: ("General Counsel", "Head of Legal"),
    # JobFunction.OTHER has no Appendix C mapping.
}

# Appendix C.1's secondary column — plausible additional owners worth
# *retrieving* (Stage 6) but not strong enough on their own to count as
# "explicit ownership language" (Stage 7 Signal 3). Generic "Founder"/
# "Founder/CEO" secondary entries are omitted — they'd just duplicate
# `FOUNDER_TITLES`, which every company gets unconditionally regardless of
# function (spec §9.2).
_SECONDARY_OWNER_TITLES: dict[JobFunction, tuple[str, ...]] = {
    JobFunction.ENGINEERING: ("Staff Engineer", "Principal Engineer", "Tech Lead"),
    JobFunction.DESIGN: ("VP Product",),
    JobFunction.DATA: ("VP Engineering", "CTO"),
    JobFunction.CUSTOMER_SUCCESS: ("COO",),
    JobFunction.FINANCE: ("COO",),
    JobFunction.PEOPLE: ("COO",),
    JobFunction.LEGAL: ("COO", "CEO"),
}

# Retrieval list (Stage 6, spec §9.1): primary + secondary flattened — Apollo's
# title filter (spec §9.3) doesn't distinguish primary from secondary, only
# Stage 7 matching's ownership-language signal does.
FUNCTION_OWNER_TITLES: dict[JobFunction, tuple[str, ...]] = {
    function: tuple(dict.fromkeys(titles + _SECONDARY_OWNER_TITLES.get(function, ())))
    for function, titles in PRIMARY_OWNER_TITLES.items()
}

# Appendix C.2 — requested for every company regardless of which functions are open.
FOUNDER_TITLES: tuple[str, ...] = ("CEO", "Founder", "Co-Founder", "CTO", "COO")

RECRUITING_TITLES: tuple[str, ...] = (
    "Technical Recruiter", "Recruiter", "Talent Acquisition", "Head of Talent",
    "Talent Partner", "Recruiting Coordinator",
)


def function_owner_titles(function: JobFunction) -> list[str]:
    """Appendix C.1 lookup for one function. Empty list if the function has
    no dedicated persona mapping (currently only `JobFunction.OTHER`).
    """
    return list(FUNCTION_OWNER_TITLES.get(function, ()))


def founder_titles() -> list[str]:
    """Spec §9.2: "Retrieve founders for every company in the segment."""
    return list(FOUNDER_TITLES)


def recruiting_titles() -> list[str]:
    return list(RECRUITING_TITLES)


def personas_for(jobs: list[JobPosting]) -> list[str]:
    """Spec §9.1's `personas_for` — derive the personas worth retrieving from
    the roles actually open, plus founders and recruiting unconditionally.

    Jobs with no classified `function` (rules-classifier residue, spec §7.3)
    contribute no function-specific personas — there's nothing in Appendix
    C.1 to look up for an unknown function — but still count towards the
    always-on founder/recruiting personas below.
    """
    functions = {job.function for job in jobs if job.function is not None}

    personas: list[str] = []
    for function in functions:
        personas.extend(function_owner_titles(function))
    personas.extend(recruiting_titles())
    personas.extend(founder_titles())

    return _dedupe(personas)


def _dedupe(titles: list[str]) -> list[str]:
    """Order-preserving de-duplication — spec §9.1's `dedupe(personas)`."""
    seen: set[str] = set()
    result: list[str] = []
    for title in titles:
        if title not in seen:
            seen.add(title)
            result.append(title)
    return result
