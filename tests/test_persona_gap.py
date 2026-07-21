"""Persona-gap detection tests — spec §17.2, §19.6."""

from gtm_agent.leads.persona_gap import PersonaGapFinding, detect_persona_gaps


def test_below_threshold_is_not_a_finding():
    findings = detect_persona_gaps({"design": 3}, threshold=5)
    assert findings == []


def test_at_or_above_threshold_is_a_finding():
    findings = detect_persona_gaps({"design": 5}, threshold=5)
    assert findings == [PersonaGapFinding(function="design", occurrences=5)]


def test_sorted_by_occurrences_descending():
    findings = detect_persona_gaps({"design": 6, "sales": 10, "legal": 5}, threshold=5)
    assert [f.function for f in findings] == ["sales", "design", "legal"]


def test_empty_input_returns_no_findings():
    assert detect_persona_gaps({}, threshold=5) == []
