"""Stage 4 normalisation tests — deterministic, no network (spec §20.1)."""

from datetime import UTC, datetime

from gtm_agent.discovery.normalization import _canonicalize_title, normalize
from gtm_agent.models.common import JobFunction, Seniority, WorkplaceType
from gtm_agent.models.job import RawPosting


def _raw(payload, **overrides):
    defaults = dict(
        company_id="acme",
        source_platform="jsonld",
        source_job_id="job-1",
        posting_url="https://example.com/jobs/1",
        raw_payload=payload,
        fetched_at=datetime.now(UTC),
        is_hydrated=True,
    )
    defaults.update(overrides)
    return RawPosting(**defaults)


def test_title_canonicalisation_strips_emoji_location_and_expands_abbreviations():
    assert _canonicalize_title("Sr. Backend Eng, Platform (Remote - US) \U0001f680") == (
        "Senior Backend Engineer, Platform"
    )


def test_title_canonicalisation_preserves_founding_engineer_distinction():
    # spec §7.2: "Founding Engineer" and "Engineer" are genuinely different roles.
    assert _canonicalize_title("Founding Engineer") == "Founding Engineer"
    assert _canonicalize_title("Engineer") == "Engineer"


def test_title_canonicalisation_strips_req_id():
    assert _canonicalize_title("Product Manager (req-4821)") == "Product Manager"


def test_title_raw_is_never_overwritten():
    raw_title = "sr. eng 🚀"
    job = normalize(_raw({"title": raw_title}))
    assert job.title_raw == raw_title


def test_function_and_seniority_classification():
    job = normalize(_raw({"title": "VP of Engineering"}))
    assert job.function == JobFunction.ENGINEERING
    assert job.seniority == Seniority.VP


def test_founding_seniority_takes_priority_over_generic_engineer_match():
    job = normalize(_raw({"title": "Founding Engineer"}))
    assert job.function == JobFunction.ENGINEERING
    assert job.seniority == Seniority.FOUNDING


def test_unclassifiable_title_returns_none_rather_than_guessing():
    job = normalize(_raw({"title": "Zyxwvutsrq Specialist"}))
    assert job.function is None
    assert job.seniority is None


def test_remote_location_and_workplace_type_from_jobLocationType():
    job = normalize(
        _raw(
            {
                "title": "Engineer",
                "jobLocation": {"address": {"addressLocality": "Remote", "addressCountry": "US"}},
                "jobLocationType": "TELECOMMUTE",
            }
        )
    )
    assert job.workplace_type == WorkplaceType.REMOTE
    assert len(job.locations) == 1
    assert job.locations[0].is_remote is True


def test_description_hybrid_signal_overrides_location_string():
    # spec §7.4: the description is often the only place hybrid expectations
    # are stated and should win when it contradicts the location string.
    job = normalize(
        _raw(
            {
                "title": "Engineer",
                "jobLocation": "London",
                "description": "<p>Hybrid — 3 days in the London office.</p>",
            }
        )
    )
    assert job.workplace_type == WorkplaceType.HYBRID


def test_compensation_extracted_only_from_structured_field():
    job = normalize(
        _raw(
            {
                "title": "Engineer",
                "baseSalary": {
                    "currency": "USD",
                    "value": {"minValue": 120000, "maxValue": 150000, "unitText": "YEAR"},
                },
            }
        )
    )
    assert job.compensation is not None
    assert job.compensation.min_amount == 120000
    assert job.compensation.max_amount == 150000
    assert job.compensation.currency == "USD"


def test_compensation_absent_when_not_structured():
    job = normalize(_raw({"title": "Engineer", "description": "We pay $150k-$200k, trust us."}))
    assert job.compensation is None


def test_posted_at_inferred_when_datePosted_absent():
    job = normalize(_raw({"title": "Engineer"}))
    assert job.posted_at_is_inferred is True
    assert job.posted_at is not None


def test_posted_at_authoritative_from_jsonld():
    job = normalize(_raw({"title": "Engineer", "datePosted": "2026-01-15T00:00:00Z"}))
    assert job.posted_at_is_inferred is False
    assert job.posted_at.year == 2026
    assert job.posted_at.month == 1


def test_job_id_prefers_source_job_id_over_url_and_hash():
    job = normalize(_raw({"title": "Engineer"}, source_job_id="job-42"))
    assert job.job_id == "acme:jsonld:job-42"


def test_job_id_falls_back_to_url_when_no_source_job_id():
    job = normalize(_raw({"title": "Engineer"}, source_job_id=None))
    assert job.job_id.startswith("acme:url:")


def test_unstructured_html_payload_produces_degraded_posting_not_a_guess():
    job = normalize(_raw("<html>some careers page</html>"))
    assert job.is_degraded is True
    assert job.extraction_confidence == 0.0
    assert job.title_raw == ""
