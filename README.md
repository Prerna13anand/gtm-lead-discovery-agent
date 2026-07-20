# GTM Lead Discovery Agent

Implementation of the pipeline described in [`JOB_SCRAPING_AGENT.md`](JOB_SCRAPING_AGENT.md). That document is the
engineering specification; this README describes only what is actually built so far.

## Status: Phase 1

Phase 1 builds the project foundation and **Part I — Job Discovery** (spec §4–§7) only:

- Project structure, configuration, logging
- Stage 1 — Source Resolution (spec §4): homepage-link and path-probe strategies, manual override
- Stage 2 — ATS Fingerprinting (spec §5): detection signal architecture and platform registry
- Stage 3 — Extraction (spec §6): the `BoardAdapter` interface and adapter registry, with **placeholder**
  adapters for Greenhouse, Lever, and Ashby. Real endpoint integration is Phase 2 work.
- Stage 4 — Normalisation (spec §7): title canonicalisation, location parsing, rules-based function/seniority
  classification, `posted_at` inference
- Placeholder service modules for Azure OpenAI (config/init only), Apollo, PDL, and Tavily — no integration logic

**Not in Phase 1:** lead discovery, matching, enrichment, company context, scoring, ranking, publication,
change detection/identity (spec §8), a real fetch-layer politeness stack (robots.txt, conditional requests,
per-domain rate limiting), and working ATS API calls. These are explicit placeholders or omitted entirely —
see inline `TODO` markers and the module docstrings for what's deferred and to which phase.

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
if resolution/extraction doesn't succeed). Since only placeholder ATS adapters exist in Phase 1, most real
companies will currently terminate at `ats_unknown` or with an empty result — this is expected until Phase 2
wires up real adapter logic.

## Design reference

All architectural decisions here trace back to `JOB_SCRAPING_AGENT.md`. Notably:

- §2.1 — ATS-API-first, HTML second
- §2.2 — one adapter interface, many backends
- §2.3 — "scrape failed" is never "no open jobs"
- §2.6 — provenance on every field
- §3.2 — stage contracts

Read the spec before extending any stage — the "why" behind each module lives there, not in code comments.
