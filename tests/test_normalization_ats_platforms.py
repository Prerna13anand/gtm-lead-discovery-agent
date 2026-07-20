"""Stage 4 normalisation tests for the three real ATS payload shapes — spec §7.

Complements tests/test_normalization.py (which covers the schema.org/JSON-LD
path and stays unmodified/unaffected by this file). These payloads reflect
the *post-adapter* `RawPosting.raw_payload` shape — e.g. Greenhouse's
`content` field here is already HTML-unescaped, since that's the adapter's
job (see discovery/extraction/greenhouse.py), not normalisation's.
"""

from datetime import UTC, datetime

from gtm_agent.discovery.normalization import normalize
from gtm_agent.models.common import EmploymentType, JobFunction, Seniority, WorkplaceType
from gtm_agent.models.job import RawPosting


def _raw(source_platform: str, payload: dict, **overrides) -> RawPosting:
    defaults = dict(
        company_id="acme",
        source_platform=source_platform,
        source_job_id="job-1",
        posting_url="https://example.com/jobs/1",
        raw_payload=payload,
        fetched_at=datetime.now(UTC),
        is_hydrated=True,
    )
    defaults.update(overrides)
    return RawPosting(**defaults)


# --- Greenhouse ---


def test_greenhouse_title_extracted_from_title_key():
    job = normalize(_raw("greenhouse", {"title": "Senior Backend Engineer"}))
    assert job.title_raw == "Senior Backend Engineer"
    assert job.function == JobFunction.ENGINEERING
    assert job.seniority == Seniority.SENIOR


def test_greenhouse_description_extracted_from_content_key():
    job = normalize(
        _raw(
            "greenhouse",
            {"title": "Engineer", "content": "<p>We build things.</p><ul><li>5+ years</li></ul>"},
        )
    )
    assert job.description_text != ""
    assert "We build things." in job.description_text
    assert "5+ years" in job.description_text


def test_greenhouse_department_extracted_from_departments_list():
    job = normalize(
        _raw(
            "greenhouse",
            {"title": "Engineer", "departments": [{"id": 1, "name": "Engineering", "child_ids": [], "parent_id": None}]},
        )
    )
    assert job.department_raw == "Engineering"


def test_greenhouse_location_extracted_as_single_combined_entry():
    job = normalize(_raw("greenhouse", {"title": "Engineer", "location": {"name": "Remote, Italy"}}))
    assert job.location_raw == "Remote, Italy"
    # Must NOT be split on the comma into two fake locations (unlike "/"-separated schema.org strings).
    assert len(job.locations) == 1
    assert job.locations[0].raw == "Remote, Italy"
    assert job.workplace_type == WorkplaceType.REMOTE


def test_greenhouse_posted_at_uses_first_published_not_updated_at():
    job = normalize(
        _raw(
            "greenhouse",
            {
                "title": "Engineer",
                "first_published": "2026-05-15T00:00:00-04:00",
                "updated_at": "2026-06-20T00:00:00-04:00",
            },
        )
    )
    assert job.posted_at_is_inferred is False
    assert job.posted_at.month == 5
    assert job.posted_at.day == 15


def test_greenhouse_employment_type_absent_stays_none():
    # Greenhouse exposes no employment-type field in the observed shape.
    job = normalize(_raw("greenhouse", {"title": "Engineer"}))
    assert job.employment_type is None


# --- Lever ---


def test_lever_title_extracted_from_text_key():
    job = normalize(_raw("lever", {"text": "Senior Backend Engineer"}))
    assert job.title_raw == "Senior Backend Engineer"
    assert job.function == JobFunction.ENGINEERING
    assert job.seniority == Seniority.SENIOR


def test_lever_description_extracted_from_description_key():
    job = normalize(_raw("lever", {"text": "Engineer", "description": "<h3>About</h3><p>We build things.</p>"}))
    assert "We build things." in job.description_text


def test_lever_department_extracted_from_categories():
    job = normalize(_raw("lever", {"text": "Engineer", "categories": {"department": "Engineering"}}))
    assert job.department_raw == "Engineering"


def test_lever_uses_all_locations_array_directly():
    job = normalize(
        _raw(
            "lever",
            {
                "text": "Engineer",
                "categories": {
                    "location": "San Francisco, CA",
                    "allLocations": ["San Francisco, CA", "New York City, NY"],
                },
            },
        )
    )
    assert job.location_raw == "San Francisco, CA"
    assert len(job.locations) == 2
    assert {loc.raw for loc in job.locations} == {"San Francisco, CA", "New York City, NY"}


def test_lever_workplace_type_prefers_direct_signal_over_regex():
    # Location string says nothing about remote/hybrid; the direct
    # categories-adjacent workplaceType field must still be honoured.
    job = normalize(
        _raw(
            "lever",
            {
                "text": "Engineer",
                "workplaceType": "hybrid",
                "categories": {"location": "London", "allLocations": ["London"]},
            },
        )
    )
    assert job.workplace_type == WorkplaceType.HYBRID


def test_lever_employment_type_from_commitment():
    job = normalize(_raw("lever", {"text": "Engineer", "categories": {"commitment": "Full Time"}}))
    assert job.employment_type == EmploymentType.FULL_TIME


def test_lever_posted_at_uses_created_at_epoch_millis():
    # 2026-05-15T00:00:00Z in epoch milliseconds
    epoch_ms = int(datetime(2026, 5, 15, tzinfo=UTC).timestamp() * 1000)
    job = normalize(_raw("lever", {"text": "Engineer", "createdAt": epoch_ms}))
    assert job.posted_at_is_inferred is False
    assert job.posted_at.year == 2026
    assert job.posted_at.month == 5
    assert job.posted_at.day == 15


# --- Ashby ---


def test_ashby_title_extracted_from_title_key():
    job = normalize(_raw("ashby", {"title": "Senior Backend Engineer"}))
    assert job.title_raw == "Senior Backend Engineer"
    assert job.function == JobFunction.ENGINEERING
    assert job.seniority == Seniority.SENIOR


def test_ashby_description_extracted_from_description_html_key():
    job = normalize(_raw("ashby", {"title": "Engineer", "descriptionHtml": "<p>We build things.</p>"}))
    assert "We build things." in job.description_text


def test_ashby_department_extracted_from_flat_key():
    job = normalize(_raw("ashby", {"title": "Engineer", "department": "Engineering"}))
    assert job.department_raw == "Engineering"


def test_ashby_location_and_secondary_locations():
    job = normalize(
        _raw(
            "ashby",
            {
                "title": "Engineer",
                "location": "Remote - US",
                "secondaryLocations": ["San Francisco, CA"],
                "isRemote": True,
            },
        )
    )
    assert job.location_raw == "Remote - US"
    assert len(job.locations) == 2
    assert job.workplace_type == WorkplaceType.REMOTE


def test_ashby_workplace_type_from_direct_field():
    job = normalize(_raw("ashby", {"title": "Engineer", "location": "London", "workplaceType": "Onsite"}))
    assert job.workplace_type == WorkplaceType.ONSITE


def test_ashby_employment_type_from_pascal_case_value():
    job = normalize(_raw("ashby", {"title": "Engineer", "employmentType": "FullTime"}))
    assert job.employment_type == EmploymentType.FULL_TIME


def test_ashby_posted_at_uses_published_at():
    job = normalize(_raw("ashby", {"title": "Engineer", "publishedAt": "2026-05-15T00:00:00.000+00:00"}))
    assert job.posted_at_is_inferred is False
    assert job.posted_at.month == 5


# --- Generic-HTML ---
#
# Regression coverage for the Stage 3 -> Stage 4 integration bugs found in
# code review: generic-HTML output is a structured dict (like every other
# adapter here), so without platform-aware handling it silently normalised
# exactly like a fully-trusted ATS response — `is_degraded=False`, full
# confidence, and its heuristic `location` field dropped entirely because
# the default (schema.org) location parser reads a different key
# (`jobLocation`).


def test_generic_html_is_marked_degraded_with_reduced_confidence():
    job = normalize(_raw("generic_html", {"title": "Engineer"}))
    assert job.is_degraded is True
    assert job.extraction_confidence < 0.5


def test_generic_html_degraded_confidence_does_not_depend_on_hydration():
    # Low confidence by construction (spec §6.2.4) -- hydration only adds a
    # description; it doesn't make the heuristic title/location/URL
    # extraction any more trustworthy, so both states must stay degraded.
    hydrated = normalize(_raw("generic_html", {"title": "Engineer"}, is_hydrated=True))
    unhydrated = normalize(_raw("generic_html", {"title": "Engineer"}, is_hydrated=False))
    assert hydrated.is_degraded is True
    assert unhydrated.is_degraded is True
    assert hydrated.extraction_confidence == unhydrated.extraction_confidence


def test_generic_html_title_extracted_from_title_key():
    job = normalize(_raw("generic_html", {"title": "Founding Engineer"}))
    assert job.title_raw == "Founding Engineer"


def test_generic_html_location_is_preserved_not_dropped():
    # The exact bug: generic_html.py writes `payload["location"]`, but the
    # default location parser only reads `jobLocation` -- this asserts the
    # value survives normalisation instead of silently disappearing.
    job = normalize(_raw("generic_html", {"title": "Engineer", "location": "Remote, US"}))
    assert job.location_raw == "Remote, US"
    assert len(job.locations) == 1
    assert job.locations[0].raw == "Remote, US"
    assert job.workplace_type == WorkplaceType.REMOTE


def test_generic_html_no_location_key_produces_no_location_not_a_crash():
    job = normalize(_raw("generic_html", {"title": "Engineer"}))
    assert job.location_raw is None
    assert job.locations == []


def test_generic_html_other_platforms_location_parsing_is_unaffected():
    # The new generic_html branch in _parse_location must not change
    # behaviour for the platform it was carved out of a shared fallback with.
    job = normalize(_raw("jsonld", {"title": "Engineer", "jobLocation": "Berlin"}))
    assert job.location_raw == "Berlin"
