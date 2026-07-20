# GTM Lead Discovery Agent

Implementation of the pipeline described in [`JOB_SCRAPING_AGENT.md`](JOB_SCRAPING_AGENT.md). That document is the
engineering specification; this README describes only what is actually built so far.

## Status: Phase 1 complete (per spec §22's own Phase 1 scope)

Phase 1 built the project foundation and **Part I — Job Discovery** (spec §4–§7, §15.1, §19.1):

- Project structure, configuration, logging
- Stage 1 — Source Resolution (spec §4): homepage-link and path-probe strategies, manual override
- Stage 2 — ATS Fingerprinting (spec §5): detection signal architecture and platform registry
- Stage 3 — Extraction (spec §6): the `BoardAdapter` interface and adapter registry. **Greenhouse, Lever, and
  Ashby adapters are real** (Phases 2A/2B/2C), against each platform's public job-board API; the generic-HTML
  fallback is still a placeholder.
- Stage 4 — Normalisation (spec §7): title canonicalisation, location parsing, rules-based function/seniority
  classification, `posted_at` inference — platform-aware, dispatching on `RawPosting.source_platform` to read
  each ATS's native field names, per spec §7's own goal statement and the explicit "ATS-native field" language
  in §7.5 and §7.7. JSON-LD's behaviour is unchanged — it's one dispatch branch among four.
- **`scrape_run` ledger** (spec §15.1): one JSONL-persisted row per company per execution attempt, recording
  status, timestamps, adapter used, job count, request/byte counts, and a raw-payload archive reference.
  Recorded for *every* attempt, including Stage 1 failures — a company that couldn't be scraped stays visible
  rather than silently disappearing (spec §2.3). See `core/run_ledger.py`.
- **Basic coverage metrics** (spec §19.1): scrape success rate, source resolution rate, ATS coverage, degraded
  extraction rate, and unscraped count — all computed directly from the `scrape_run` ledger. See
  `core/metrics.py` and `python main.py metrics` below.
- Placeholder service modules for Azure OpenAI (config/init only), Apollo, PDL, and Tavily — no integration logic

This closes out every item spec §22 names under "Phase 1 — Prove the ATS thesis." The exit criterion itself
(>60% of the target list scraped, >95% golden-set accuracy) isn't something this codebase can satisfy on its
own — it needs a real target company list and a hand-labelled golden set, both explicit inputs per §1.6/§19.4,
not something this agent generates.

**Not yet built:** lead discovery, matching, enrichment, company context, scoring, ranking, publication,
change detection/identity (spec §8), a real fetch-layer politeness stack (robots.txt, conditional requests,
per-domain rate limiting), and real generic-HTML extraction — all spec §22 Phase 2+ work. See inline `TODO`
markers and module docstrings for what's deferred and to which phase, and **Known limitations / follow-ups**
below for what's still intentionally out of scope within what's built.

## Project layout

```
src/gtm_agent/
├── config/       # environment-backed settings
├── core/         # logging, async HTTP fetch layer, scrape_run ledger, coverage metrics
├── models/       # canonical Pydantic domain models (CareersSource, JobPosting, ScrapeRun, ...)
├── discovery/    # Stages 1-4: source resolution, ATS fingerprinting, extraction, normalisation
│   └── extraction/   # BoardAdapter interface + per-platform adapters
└── services/     # placeholder clients: azure_openai, apollo, pdl, tavily
main.py           # CLI entry point (discover, metrics)
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
if resolution/extraction doesn't succeed). Real Greenhouse-, Lever-, and Ashby-hosted companies now all
return real postings with title, location, workplace type, department, employment type (where the platform
exposes one), and a real (non-inferred) `posted_at` populated correctly per platform. Every invocation also
records one row in the `scrape_run` ledger (`.data/scrape_runs.jsonl` by default) and prints a summary of it,
regardless of where the run terminated.

```bash
python main.py metrics
```

Prints the spec §19.1 coverage metrics (scrape success rate, source resolution rate, ATS coverage, degraded
extraction rate, unscraped count), computed from every run recorded in the ledger so far. Sample output after
a handful of `discover` runs across all three real ATS platforms plus one genuine failure:

```
== Coverage metrics (spec §19.1) ==
  scrape success rate:      83.3%  (5/6 runs)
  source resolution rate:   75.0%  (3/4 companies)
  ATS coverage:             100.0%  (3/3 resolved companies)
  degraded extraction rate: 0.0%  (0/5 successful runs)
  unscraped count:          1  (absolute, never a percentage — spec §19.1)
```

## Known limitations / follow-ups

**`normalize()`'s per-platform field mapping covers the required fields only — some enhancements were
deliberately left out.** The Stage 4 fix (dispatching on `RawPosting.source_platform` for title, description,
location, department, employment type, and `posted_at`) was scoped strictly to what spec §7 requires. Left
out on purpose, not forgotten:

- **Lever's description is `categories`-adjacent `description` only** — not assembled with Lever's separate
  `opening`, `lists`, and `additional` fields. Spec §7.6 calls the requirements list "the highest-signal part
  of a posting," and Lever's `lists` field (e.g. a "You will:" heading + bullets) is exactly that, kept
  separate from `description` in Lever's own shape. The current fix already yields a non-empty, usable
  description from `description` alone; assembling the other sections into one richer blob is a real quality
  improvement but wasn't required to close the gap, so it's left for a future pass.
- **Greenhouse's `offices[]` array** isn't consulted as a secondary/cross-check location source alongside
  `location.name`.
- **Compensation extraction** stays JSON-LD-only. No structured compensation field was found on any of the
  three real ATS payloads examined during this build — that's an absence of data on those boards, not a gap
  in the normalizer (spec §7.5: never infer compensation from free text).
- **Full boilerplate stripping / non-English handling (§7.6)** and **markdown structure preservation**
  (`description_markdown` still equals `description_text`) remain pre-existing simplifications, unrelated to
  platform field-mapping specifically.
- **LLM residue classification (§7.3)** for titles the rules-based classifier can't resolve is still
  unimplemented, pending `services.azure_openai` having real scoring logic.

**Board-token resolution duplicates a Stage 2 fetch in one case.** The `BoardAdapter` interface (spec §6.1)
receives only `CareersSource`, not `AtsIdentification` — so `GreenhouseAdapter`, `LeverAdapter`, and
`AshbyAdapter` all resolve their own board token from `source.careers_url` rather than reusing the token
Stage 2 (`ats_detection.identify_ats`) already found. When the token is already in the URL (the common case —
Stage 1's homepage-link strategy often resolves straight to an ATS link) this costs nothing extra. When Stage
2 found the platform via a redirect (the company's own domain 30x-redirects to the ATS), the adapter
re-fetches the source URL once to re-derive the same redirect target Stage 2 already followed. Acceptable for
now since there's no persistent `careers_source.ats_board_token` store yet (spec §15.1 models the token as
living on the same row as the careers source); worth revisiting once a real orchestrator persists Stage 2
output back onto the source record instead of recomputing it.

**Coverage metrics (`core/metrics.py`) read two spec §19.1 definitions literally rather than with inferred
nuance.** Both choices are documented in the module's own docstring, but worth surfacing here too:

- **"Unscraped count"** is defined as "Companies in a **non-success** terminal state" — implemented as
  literally `status != SUCCESS`. A more elaborate reading could exclude `parse_degraded`/`partial`, since
  spec §17's *downstream* column calls those "published" rather than truly unscraped — but that distinction
  belongs to a different column of a different table, and isn't what this metric's own definition says.
- **"Degraded extraction rate"'s** denominator, "successful", is read as `status == SUCCESS` specifically —
  not "success-or-degraded". `parse_degraded` is its own distinct terminal `ScrapeRunStatus`, never a variant
  of `success`, in this ledger.

Neither choice changes any current output: `parse_degraded` isn't reachable yet (the generic-HTML adapter
that would produce it is a Phase 2 placeholder), so that numerator is always 0 today regardless.

Also: **"Source resolution rate" and "ATS coverage" are computed over each company's most recent run**, not
every historical attempt — a company retried after an earlier failure is counted by its current state, not
its past churn. "Scrape success rate" and "Degraded extraction rate", by contrast, are computed over *every*
closed run, matching the spec's own "runs" (not "companies") wording for those two.

## Design reference

All architectural decisions here trace back to `JOB_SCRAPING_AGENT.md`. Notably:

- §2.1 — ATS-API-first, HTML second
- §2.2 — one adapter interface, many backends
- §2.3 — "scrape failed" is never "no open jobs"
- §2.6 — provenance on every field
- §3.2 — stage contracts

Read the spec before extending any stage — the "why" behind each module lives there, not in code comments.
