"""Persona-gap detection — spec §17.2, §19.6 (Phase 5).

"`persona_gap` is the one to actually act on. A single `no_plausible_owner`
is ordinary. The same *function* unmatched across many companies means
§9.1 isn't requesting the right titles — a fixable bug with compounding
cost." Spec §19.6's alerting table: "`persona_gap` for any function ->
Ticket — fix §9.1 coverage."

Pure logic over `core.metrics.compute_matching_metrics`'s
`no_plausible_owner_by_function` — no new counting here, just the
threshold decision that turns a count into a ticket-worthy finding.
"""

from __future__ import annotations

from dataclasses import dataclass

# No threshold is given by the spec — open question §23.4's sibling question
# for personas rather than adapters. 5 is chosen to match this codebase's
# other "recurring signal" threshold (spec §19.6's own "New `ats_unknown`
# platform seen >= 5 times -> Ticket"), for consistency, not because the
# spec pins this number down.
_DEFAULT_THRESHOLD = 5


@dataclass(frozen=True)
class PersonaGapFinding:
    function: str
    occurrences: int


def detect_persona_gaps(
    no_plausible_owner_by_function: dict[str, int], *, threshold: int = _DEFAULT_THRESHOLD
) -> list[PersonaGapFinding]:
    """Spec §17.2: "not a data gap" once a function crosses the threshold —
    a real bug in the §9.1 persona ladder for that function, ticket-worthy
    per §19.6. Sorted by occurrence count, most urgent first.
    """
    findings = [
        PersonaGapFinding(function=function, occurrences=count)
        for function, count in no_plausible_owner_by_function.items()
        if count >= threshold
    ]
    return sorted(findings, key=lambda f: -f.occurrences)
