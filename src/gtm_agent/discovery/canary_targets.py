"""Canary Suite target list (spec §20.3): "~20 real companies, one per ATS
plus several generic-path."

Every entry below was live-verified during the Phase 2 build with a direct
request against the real endpoint each adapter actually calls (not just a
homepage visit) — e.g. for Greenhouse, `curl
https://boards-api.greenhouse.io/v1/boards/{token}/jobs` returning a real,
non-empty job list — immediately before being added here. None are guessed;
`AtsPlatform.RENDERED_DOM` and `AtsPlatform.GENERIC_HTML` reuse companies
already verified live in earlier Phase 2 work (see `rendered_dom.py` and
`generic_html.py`'s own module docstrings) rather than re-verifying from
scratch.

**Discrepancy from the spec's "~20" figure, documented rather than padded
with guesses:** this list holds 17, two per ATS-API platform (redundancy
against any single company changing ATS on its own) plus one per
platform-independent path (JSON-LD, generic-HTML, rendered-DOM) — except
JSON-LD, where a live company using bare schema.org `JobPosting` markup with
no ATS behind it wasn't found within this pass's search budget. That's a
real gap, not a rounding difference — flagged explicitly rather than filling
the slot with an unverified guess, which would defeat the entire point of a
canary list (a broken canary target is worse than an honestly-missing one:
it would misreport drift against a baseline nobody ever confirmed was real).
"""

from __future__ import annotations

from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.canary import CanaryTarget

CANARY_TARGETS: list[CanaryTarget] = [
    # --- Greenhouse ---
    CanaryTarget(
        company_id="canary-greenhouse-greenhouse",
        company_name="Greenhouse",
        domain="greenhouse.io",
        careers_url="https://boards.greenhouse.io/greenhouse",
        expected_platform=AtsPlatform.GREENHOUSE,
        notes="Verified live: boards-api.greenhouse.io board 'greenhouse', 23 jobs at verification time.",
    ),
    CanaryTarget(
        company_id="canary-greenhouse-airbnb",
        company_name="Airbnb",
        domain="airbnb.com",
        careers_url="https://boards.greenhouse.io/airbnb",
        expected_platform=AtsPlatform.GREENHOUSE,
        notes="Verified live: boards-api.greenhouse.io board 'airbnb' returns 200.",
    ),
    # --- Lever ---
    CanaryTarget(
        company_id="canary-lever-pointclickcare",
        company_name="PointClickCare",
        domain="pointclickcare.com",
        careers_url="https://jobs.lever.co/pointclickcare",
        expected_platform=AtsPlatform.LEVER,
        notes="Verified live: api.lever.co/v0/postings/pointclickcare, 85 postings at verification time.",
    ),
    CanaryTarget(
        company_id="canary-lever-doola",
        company_name="doola",
        domain="doola.com",
        careers_url="https://jobs.lever.co/doola",
        expected_platform=AtsPlatform.LEVER,
        notes="Verified live: api.lever.co/v0/postings/doola returns 200.",
    ),
    # --- Ashby ---
    CanaryTarget(
        company_id="canary-ashby-workos",
        company_name="WorkOS",
        domain="workos.com",
        careers_url="https://jobs.ashbyhq.com/workos",
        expected_platform=AtsPlatform.ASHBY,
        notes="Verified live: api.ashbyhq.com/posting-api/job-board/workos, 23 postings at verification time.",
    ),
    CanaryTarget(
        company_id="canary-ashby-socure",
        company_name="Socure",
        domain="socure.com",
        careers_url="https://jobs.ashbyhq.com/socure",
        expected_platform=AtsPlatform.ASHBY,
        notes="Verified live: api.ashbyhq.com/posting-api/job-board/socure returns 200.",
    ),
    # --- Workable ---
    CanaryTarget(
        company_id="canary-workable-cloudfactory",
        company_name="CloudFactory",
        domain="cloudfactory.com",
        careers_url="https://apply.workable.com/cloudfactory/",
        expected_platform=AtsPlatform.WORKABLE,
        notes="Verified live: apply.workable.com/api/v1/widget/accounts/cloudfactory, 75 jobs at verification time.",
    ),
    CanaryTarget(
        company_id="canary-workable-crewbloom",
        company_name="CrewBloom",
        domain="crewbloom.com",
        careers_url="https://apply.workable.com/crewbloom/",
        expected_platform=AtsPlatform.WORKABLE,
        notes="Verified live: apply.workable.com/api/v1/widget/accounts/crewbloom returns 200.",
    ),
    # --- SmartRecruiters ---
    CanaryTarget(
        company_id="canary-smartrecruiters-westerndigital",
        company_name="Western Digital",
        domain="westerndigital.com",
        careers_url="https://careers.smartrecruiters.com/westerndigital",
        expected_platform=AtsPlatform.SMARTRECRUITERS,
        notes="Verified live: api.smartrecruiters.com/v1/companies/westerndigital/postings, 278 total at verification time.",
    ),
    CanaryTarget(
        company_id="canary-smartrecruiters-ifs",
        company_name="IFS",
        domain="ifs.com",
        careers_url="https://careers.smartrecruiters.com/ifs1",
        expected_platform=AtsPlatform.SMARTRECRUITERS,
        notes="Verified live: api.smartrecruiters.com/v1/companies/ifs1/postings returns 200.",
    ),
    # --- Recruitee ---
    CanaryTarget(
        company_id="canary-recruitee-greatminds",
        company_name="Great Minds",
        domain="greatminds.org",
        careers_url="https://greatminds.recruitee.com/",
        expected_platform=AtsPlatform.RECRUITEE,
        notes="Verified live: greatminds.recruitee.com/api/offers/, 21 offers at verification time.",
    ),
    CanaryTarget(
        company_id="canary-recruitee-xite",
        company_name="XITE",
        domain="xite.com",
        careers_url="https://xite.recruitee.com/",
        expected_platform=AtsPlatform.RECRUITEE,
        notes="Verified live: xite.recruitee.com/api/offers/ returns 200.",
    ),
    # --- Rippling ---
    CanaryTarget(
        company_id="canary-rippling-reverb",
        company_name="Reverb",
        domain="reverb.com",
        careers_url="https://ats.rippling.com/reverb-careers/jobs",
        expected_platform=AtsPlatform.RIPPLING,
        notes="Verified live: ats.rippling.com/reverb-careers/jobs returns 200 with job listings.",
    ),
    CanaryTarget(
        company_id="canary-rippling-steno",
        company_name="Steno",
        domain="steno.com",
        careers_url="https://ats.rippling.com/steno-careers-page/jobs",
        expected_platform=AtsPlatform.RIPPLING,
        notes="Verified live: ats.rippling.com/steno-careers-page/jobs returns 200.",
    ),
    # --- Rendered-DOM (reuses the board verified live in rendered_dom.py's own build) ---
    CanaryTarget(
        company_id="canary-rendered-dom-retool",
        company_name="Retool",
        domain="retool.com",
        careers_url="https://retool.com/careers",
        expected_platform=AtsPlatform.RENDERED_DOM,
        notes=(
            "Reuses the board verified live during rendered_dom.py's own Phase 2 build: "
            "no known ATS, no JSON-LD, zero job content in static HTML, real job links "
            "present only after rendering."
        ),
    ),
    # --- Generic-HTML (reuses the board verified live in generic_html.py's own build) ---
    CanaryTarget(
        company_id="canary-generic-html-helpscout",
        company_name="Help Scout",
        domain="helpscout.com",
        careers_url="https://www.helpscout.com/company/careers/",
        expected_platform=AtsPlatform.GENERIC_HTML,
        notes=(
            "Reuses the board verified live during generic_html.py's own Phase 2 build: "
            "a white-labelled Ashby board undetectable by current Stage 2 signals (query-param "
            "board token), server-rendered job links in static HTML, no JSON-LD."
        ),
    ),
    CanaryTarget(
        company_id="canary-generic-html-37signals",
        company_name="37signals",
        domain="37signals.com",
        careers_url="https://37signals.com/jobs",
        expected_platform=AtsPlatform.GENERIC_HTML,
        notes=(
            "Verified live: no known ATS, no JSON-LD, static HTML. Deliberately a "
            "'quiet' canary — 0 open roles at verification time, useful as a stable "
            "known-zero baseline the way a real board_emptied case should look."
        ),
    ),
]
