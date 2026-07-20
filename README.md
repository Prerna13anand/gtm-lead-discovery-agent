# GTM Lead Discovery Agent

Implementation of the pipeline described in [`JOB_SCRAPING_AGENT.md`](JOB_SCRAPING_AGENT.md). That document is the
engineering specification; this README describes only what is actually built so far.

## Status: Phase 2A (in progress)

Phase 1 built the project foundation and **Part I — Job Discovery** (spec §4–§7):

- Project structure, configuration, logging
- Stage 1 — Source Resolution (spec §4): homepage-link and path-probe strategies, manual override
- Stage 2 — ATS Fingerprinting (spec §5): detection signal architecture and platform registry
- Stage 3 — Extraction (spec §6): the `BoardAdapter` interface and adapter registry, with **placeholder**
  adapters for Greenhouse, Lever, and Ashby.
- Stage 4 — Normalisation (spec §7): title canonicalisation, location parsing, rules-based function/seniority
  classification, `posted_at` inference — built against **schema.org/JSON-LD field names only**
- Placeholder service modules for Azure OpenAI (config/init only), Apollo, PDL, and Tavily — no integration logic

Phase 2A replaces the Greenhouse placeholder with a real adapter against the public Greenhouse Job Board API
(`boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`). Lever and Ashby are still placeholders and
are the next milestones in Phase 2.

**Not yet built:** lead discovery, matching, enrichment, company context, scoring, ranking, publication,
change detection/identity (spec §8), a real fetch-layer politeness stack (robots.txt, conditional requests,
per-domain rate limiting), real Lever/Ashby/generic-HTML extraction. See inline `TODO` markers and module
docstrings for what's deferred and to which phase, and **Known limitations / follow-ups** below for the one
gap Greenhouse's real implementation exposed.

## Project layout

```
src/gtm_agent/
├── config/       # environment-backed settings
├── core/         # logging, async HTTP fetch layer
├── models/       # canonical Pydantic domain models (CareersSource, JobPosting, ...)
├── discovery/    # Stages 1-4: source resolution, ATS fingerprinting, extraction, normalisation
│   └── extraction/   # BoardAdapter interface + per-platform adapters
└── services/     # placeholder clients: azure_openai, apollo, pdl, tavily
main.py           # CLI entry point
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env           # fill in credentials as they become available
```

## Running

```bash
python main.py discover --domain example.com --name "Example Inc"
```

This runs Stages 1–4 for a single company and prints the resulting job postings (or the typed failure state
if resolution/extraction doesn't succeed). Real Greenhouse-hosted companies now return real postings; Lever-
and Ashby-hosted companies still terminate at `not_implemented` until those adapters are built. Note the
Greenhouse-specific normalisation gap above — job titles will show, but description/location/department
fields will be empty for Greenhouse postings until that follow-up lands.

## Known limitations / follow-ups

**`normalize()` doesn't yet understand Greenhouse-shaped payloads (follow-up for the next milestone).**
`discovery/normalization.py`'s `normalize()` reads schema.org/JSON-LD field names only (`description`,
`jobLocation`, `baseSalary`, `employmentType`, `datePosted`) because JSON-LD was the only real payload shape
that existed when it was written. The Greenhouse adapter deliberately preserves the *native* Greenhouse job
shape in `RawPosting.raw_payload` (fields: `content`, `location.name`, `departments`, `first_published`, no
`baseSalary`) rather than translating it to schema.org — per spec §6.4, the raw payload should be archived
untouched. The consequence: a Greenhouse `RawPosting` currently passes `normalize()`'s `isinstance(dict)`
check but comes out under-populated — `description_text`, `locations`, and `department_raw` stay empty even
though the data is right there in `raw_payload`. `title` happens to match by coincidence (both shapes use the
key `"title"`); nothing else does.

Fixing this needs per-platform field mapping in Stage 4 — most likely a small dispatch on
`RawPosting.source_platform` before the schema.org-shaped extraction logic runs, or a per-adapter "to
canonical fields" translation step done in Stage 3 instead. Left as an explicit next-milestone task rather
than folded into the Greenhouse adapter work, to keep that change scoped to Stage 3 only.

**Board-token resolution duplicates a Stage 2 fetch in one case.** The `BoardAdapter` interface (spec §6.1)
receives only `CareersSource`, not `AtsIdentification` — so `GreenhouseAdapter` resolves its own board token
from `source.careers_url` rather than reusing the token Stage 2 (`ats_detection.identify_ats`) already found.
When the token is already in the URL (the common case — Stage 1's homepage-link strategy often resolves
straight to an ATS link) this costs nothing extra. When Stage 2 found the platform via a redirect (the
company's own domain 30x-redirects to Greenhouse), the adapter re-fetches the source URL once to re-derive
the same redirect target Stage 2 already followed. Acceptable for now since there's no persistent
`careers_source.ats_board_token` store yet (spec §15.1 models the token as living on the same row as the
careers source); worth revisiting once a real orchestrator persists Stage 2 output back onto the source
record instead of recomputing it.

## Design reference

All architectural decisions here trace back to `JOB_SCRAPING_AGENT.md`. Notably:

- §2.1 — ATS-API-first, HTML second
- §2.2 — one adapter interface, many backends
- §2.3 — "scrape failed" is never "no open jobs"
- §2.6 — provenance on every field
- §3.2 — stage contracts

Read the spec before extending any stage — the "why" behind each module lives there, not in code comments.
