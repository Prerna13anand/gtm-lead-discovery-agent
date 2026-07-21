"""Persona ladder tests — spec §9.1, Appendix C."""

from datetime import UTC, datetime

from gtm_agent.leads.personas import (
    founder_titles,
    function_owner_titles,
    personas_for,
    recruiting_titles,
)
from gtm_agent.models.common import JobFunction
from gtm_agent.models.job import JobPosting


def _job(function: JobFunction | None) -> JobPosting:
    t = datetime.now(UTC)
    return JobPosting(
        job_id="j1",
        company_id="acme",
        source_platform="greenhouse",
        posting_url="https://acme.com/jobs/1",
        title_raw="Some Role",
        title_canonical="Some Role",
        description_text="",
        description_markdown="",
        function=function,
        first_seen_at=t,
        last_seen_at=t,
    )


def test_function_owner_titles_engineering_includes_expected_titles():
    titles = function_owner_titles(JobFunction.ENGINEERING)
    assert "VP Engineering" in titles
    assert "Head of Engineering" in titles
    assert "CTO" in titles


def test_function_owner_titles_other_is_empty():
    assert function_owner_titles(JobFunction.OTHER) == []


def test_founder_titles_and_recruiting_titles_are_nonempty():
    assert "CEO" in founder_titles()
    assert "Founder" in founder_titles()
    assert "Technical Recruiter" in recruiting_titles()


def test_personas_for_includes_founders_and_recruiting_unconditionally():
    jobs = [_job(JobFunction.ENGINEERING)]
    personas = personas_for(jobs)
    for title in founder_titles():
        assert title in personas
    for title in recruiting_titles():
        assert title in personas


def test_personas_for_only_requests_titles_for_open_functions():
    jobs = [_job(JobFunction.ENGINEERING)]
    personas = personas_for(jobs)
    # Sales-specific titles should not be requested when no sales role is open.
    assert "VP Sales" not in personas
    assert "VP Engineering" in personas


def test_personas_for_multiple_functions_unions_and_dedupes():
    jobs = [_job(JobFunction.ENGINEERING), _job(JobFunction.SALES), _job(JobFunction.ENGINEERING)]
    personas = personas_for(jobs)
    assert "VP Engineering" in personas
    assert "VP Sales" in personas
    # No duplicates.
    assert len(personas) == len(set(personas))


def test_personas_for_job_with_no_function_still_gets_founders_and_recruiting():
    jobs = [_job(None)]
    personas = personas_for(jobs)
    assert "CEO" in personas
    assert "Technical Recruiter" in personas


def test_personas_for_empty_jobs_still_returns_founders_and_recruiting():
    personas = personas_for([])
    assert set(founder_titles()) <= set(personas)
    assert set(recruiting_titles()) <= set(personas)
