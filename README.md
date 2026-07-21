# GTM Lead Discovery Agent

Implementation of the pipeline described in [`JOB_SCRAPING_AGENT.md`](JOB_SCRAPING_AGENT.md). That document is the
engineering specification; this README describes only what is actually built so far.

## Status: Phases 1-5 complete (per spec §22's own rollout scope)

All five phases of spec §22's Phased Rollout are implemented: **Part I — Job Discovery** (stages 1-5),
**Part II — Lead Discovery & Matching** (stages 6-9), **Part III — Scoring & Output** (stages 10-11), and the
Phase 5 hardening/tuning surface (persona-gap detection, suppression/denylist tooling, golden-set automation,
feedback-agreement and budget-usage tuning inputs). 606 tests pass (`pytest -q`).

A subsequent full-project audit against the spec closed out several previously-deferred cross-cutting gaps:
robots.txt consultation, per-domain rate limiting (concurrency + minimum interval with jitter), honouring
`Retry-After`, ATS-fingerprinting Signals 5/6 (network-request matching + DNS/CNAME), Stage 4's LLM
title-residue classification (§7.3), and Stage 7's LLM matching tie-break (§10.7) — all previously TODOs, now
implemented, tested, and (where a live vendor was reachable) live-verified. See **Known limitations** for what
remains genuinely open.

**What this is not:** a real sweep orchestrator, scheduler, or production database. `main.py` is a
single-company CLI demo harness that exercises the full 11-stage pipeline end to end; every persisted "table"
in spec §15 is a local JSONL file, not a real database. Both are explicit, documented scope boundaries — see
**Known limitations** below.

### Part I — Job Discovery (spec §4-§8, §15.1, §19.1, §20.3)

- Stage 1 — Source Resolution: homepage-link, path-probe, sitemap, and Tavily-search-fallback strategies, plus
  manual override
- Stage 2 — ATS Fingerprinting: all six §5.1 detection signals — URL host match, redirect target, embedded
  script/iframe, DOM markers, network-request matching (`identify_from_captured_requests`), and DNS/CNAME
  (`_resolve_cname_aliases`, stdlib-only)
- Stage 3 — Extraction: the `BoardAdapter` interface with real, live-verified adapters for **Greenhouse, Lever,
  Ashby, Workable, SmartRecruiters, Recruitee, and Rippling**, plus a JSON-LD adapter, a heuristic generic-HTML
  fallback, and a Playwright-based rendered-DOM adapter with endpoint-learning
- Stage 4 — Normalisation: title canonicalisation, location parsing, rules-based function/seniority
  classification (exposed publicly — `classify_function`/`classify_seniority` — and reused by Stage 6), an
  optional LLM residue fallback for titles the rules can't resolve (`discovery/llm_residue.py`, spec §7.3,
  cached by canonical title, live-verified against Azure OpenAI), `posted_at` inference
- Stage 5 — Change Detection & Identity: the OPEN/MISSING/CLOSED lifecycle, the grace window, `zero_jobs_suspicious`
  handling, and the full event stream (`job_opened`, `job_closed`, `job_reopened`, `job_updated`, `board_emptied`,
  `board_first_seen`)
- `scrape_run` ledger, coverage metrics (§19.1), and a nightly canary suite (§20.3) against real live boards
- The shared fetch layer (`core/fetch.py`) now enforces robots.txt (§21.1, `core/robots.py`), a per-domain
  concurrency semaphore and a minimum-interval-with-jitter rate limit (§16.3/§6.3), and honours `Retry-After`
  (§6.3) — all previously deferred, all wired into every adapter's `discover()`

### Part II — Lead Discovery & Matching (spec §9-§12, §15.2, §18, §19.3-§19.5)

- Stage 6 — Lead Discovery: Apollo People Search integration, the Appendix C persona ladder, per-company
  caching with the §9.6 invalidation triggers, the §9.4 retrieval-cap `company_identity_suspect` check, and
  the full §9.7/§17.2 failure taxonomy
- Stage 7 — Matching: all six §10.3 signals (function alignment, seniority relationship, ownership language,
  recruiter role, location, tenure), the §10.4 headcount modulation (the segment-specific founders-own-everything
  correction), match-floor/top-K selection, typed `unmatched_job` reasons, and an optional LLM tie-break
  (`leads/tie_break.py`, spec §10.7, invoked only within the narrow score band, live-verified against Azure
  OpenAI)
- Stage 8 — Enrichment: PDL integration, the §11.2 field-level trust waterfall, §11.3 identity-corroboration
  (rejecting weak name-only matches), and 90-day caching
- Stage 9 — Company Context: Tavily-backed funding/hiring/careers-cross-check queries, summarised and cached
  ~7 days, non-blocking on failure
- Credit budgeting (§18.3) shared across Stages 6/8/9; feedback capture (§19.5); a hand-constructed 18-pair
  matching golden set (§19.4) — see **Known limitations**

### Part III — Scoring & Output (spec §13-§14, §15.2)

- Stage 10 — Scoring & Rationale: real Azure OpenAI structured-output integration (`openai`'s
  `beta.chat.completions.parse`), the §13.1 judge/explain/adjust boundary, `cited_signals` grounding validation,
  retry-once-on-violation, and a rules-score fallback on `scoring_failed` (never drops a pair)
- Stage 11 — Ranking & Publication: the §13.6 priority formula (relevance × confidence × recency × contactability
  × company-context weight), the `GtmLead` output contract, the `lead_ready`/`job_unmatched`/`lead_superseded`/
  `job_closed` event stream, and CSV export (spec §14.4)
- `disagrees_with_rules` monitoring (§13.4/§19.3)

### Phase 5 — Harden and tune (spec §22)

- Persona-gap detection (`leads/persona_gap.py`): turns a recurring `no_plausible_owner` pattern for one
  function into a ticket-worthy finding (§17.2, §19.6)
- `ats_unknown` frequency metric (`core/metrics.py`): prioritises which new ATS adapter to build next
- Company denylist and person suppression (§21.6): `main.py denylist-add` / `suppress-lead`, wired into Stage 1
  (checked before any resolution attempt) and Stage 6 (filtered out of every future Apollo sweep)
- Golden-set automation: `main.py golden-set` runs the same evaluator the test suite runs on every change
- Feedback-agreement (`leads/tuning.py`) and per-meter budget-usage summaries — starting inputs for a real
  matching-weight-tuning and cost-tuning process, not the process itself (see **Known limitations**)

## Project layout

```
src/gtm_agent/
├── config/       # environment-backed settings
├── core/         # logging, fetch layer, metrics, and every JSONL-backed "table" (run ledger,
│                 #   lifecycle, lead/matching/scoring stores, compliance)
├── models/       # canonical Pydantic domain models — one module per spec §15 table family
├── discovery/    # Part I, stages 1-5
│   └── extraction/   # BoardAdapter interface + one adapter per platform
├── leads/        # Part II, stages 6-9 + budget, compliance, persona-gap, tuning, golden-set
├── scoring/      # Part III, stages 10-11 (rationale, ranking, publication)
└── services/     # external API clients: apollo, pdl, tavily, azure_openai
main.py           # CLI entry point — discover (all 11 stages), metrics, canary, golden-set,
                  #   denylist-add, suppress-lead
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

Runs the full pipeline for one company: Stages 1-5 (job discovery), then — if any jobs are currently open —
Stages 6-11 (lead discovery through publication). Prints a per-stage summary and writes a CSV export
(`.data/gtm_leads.csv` by default) once at least one lead publishes.

```bash
python main.py metrics       # spec §19.1 coverage metrics from the scrape_run ledger
python main.py canary        # spec §20.3 nightly canary suite against real ATS boards
python main.py golden-set    # spec §19.4 matching-accuracy check against the hand-built golden set
python main.py denylist-add --domain acme.com --reason "..."   # spec §21.6
python main.py suppress-lead --email jane@acme.com --reason "GDPR erasure request"   # spec §21.6
```

## Known limitations / follow-ups

**Live verification was only possible for Azure OpenAI.** `AZURE_OPENAI_API_KEY`/`ENDPOINT`/`DEPLOYMENT` are
configured in this environment; `APOLLO_API_KEY`, `PDL_API_KEY`, and `TAVILY_API_KEY` are not. Concretely:

- Stage 10 (LLM scoring) was exercised against the real Azure OpenAI deployment (`gpt-5.4-mini`) with real
  job/lead pairs and produced well-grounded, correctly-hedged rationales — including consistently down-weighting
  the rules' founder-driven scores when the job function clearly didn't fit, and setting `disagrees_with_rules`
  accordingly. This is real evidence the §13.1 judge/explain/adjust design works as intended, not just that it
  passes mocked tests.
- Stage 4's LLM residue classification and Stage 7's LLM tie-break (added during the post-implementation audit)
  were each live-verified the same way: a genuinely rules-unclassifiable title ("Growth Ninja") was correctly
  classified `marketing`/`mid`, and a real tied SDR-role match correctly preferred the Head of Sales over a
  same-scoring founder once the job description named the reporting line.
- Stages 6 (Apollo), 8 (PDL), and 9 (Tavily company-context) could **not** be live-verified — there is no way
  to test an integration against a real vendor API without credentials. Their failure paths (not-configured,
  HTTP error, budget exhaustion) *were* exercised live end-to-end against a real company (`linear.app`'s real,
  live Ashby board, 24 real postings) and behaved correctly: jobs still published as `unmatched` with the
  correct typed reason, Stage 9 degraded non-blockingly. A synthetic seeded lead was also used to exercise
  Stages 7-11 against those same 24 real jobs end to end, including the real Stage 10 LLM call, confirmed above.
- Every third-party endpoint shape (`apollo.py`, `pdl.py`, `services/tavily.py`'s `get_company_context`) is
  therefore this codebase's best-effort mapping onto each vendor's public docs, explicitly flagged in each
  module's own docstring as unverified against the live API — per this project's own established convention
  for Part I's ATS adapters (Appendix A's build note), carried into Part II/III.

**The matching golden set (`tests/fixtures/matching_golden_set.json`) is a starting corpus, not real ground
truth.** Spec §19.4 asks for "~50 pairs... hand-labelled by someone with GTM judgement." No GTM person's
judgement is available to a coding agent, so this codebase built an 18-pair set from its own domain reasoning
(the §10.8 worked example, the §9.2 founders-own-everything insight, straightforward function/seniority
mismatches), over-sampling sub-20-headcount companies per the spec's own guidance. It measures 88.9% accuracy
against this codebase's own bucketing of continuous scores into correct/plausible/wrong labels — useful as a
regression lock (spec: "measured... on every rules change"), not as validation against independent truth.

**Matching weights (`leads/matching.py`), ranking weights (`scoring/ranking.py`), and the match floor/top-K
constants are explicitly-labelled starting points**, per the spec's own repeated acknowledgement that these
values need real feedback data to tune (§10.3, §13.6, open questions §23.10-§23.12). Nothing here claims to be
a tuned production weight.

**No sweep orchestrator, scheduler, or cadence tiering exists** (spec §16). `main.py discover` processes one
company per invocation; there is no multi-company concurrency, no §16.2 tiered cadence, and no real alerting/
paging behind any "alert"/"page" language in §17-§19 (this mirrors Part I's own pre-existing scope boundary,
carried through unchanged).

**No real database.** Every spec §15 table is a local, append-only JSONL file — the same stand-in convention
established in Phase 1/2 for `scrape_run`, extended consistently through every later phase's own tables
(`lead`, `lead_job_match`, `scored_lead`, `company_context`, `company_denylist`, etc.). Each store class is a
thin, swappable wrapper — replacing the backing storage later doesn't require touching any call site.

**The §21.5 compliance review gate has not run.** Spec §21.5: "the review should be a gate on Phase 3... not a
parallel workstream." This codebase implements Stage 6-8's lead-data handling (enrich-late, cache-narrowly,
suppression list) in a way *designed* to satisfy that review, but the review itself is a human/legal process
this codebase cannot perform. **Do not process real personal data at volume with this code before that review
completes and signs off**, per the spec's own explicit instruction.

**Job-version/lead-version cache keys are approximations.** Spec §13.5's `scored_lead` cache key needs a
`job_version`/`lead_version`; a real `job_posting_version` counter exists (Stage 5, §8.5), but no equivalent
`lead_version` counter exists anywhere in the spec's own Part II design. This codebase uses each record's own
natural last-changed timestamp (`job.last_seen_at`, `lead.enriched_at` or `retrieved_at`) as a stand-in — see
`scoring/rationale.py`'s module docstring for the full reasoning.

**Erasure is suppression-filtered, not physically deleted.** Spec §21.6: "a lead who requests erasure is
deleted and added to a suppression list." This codebase's `leads.compliance.erase_lead` adds the suppression
entry and filters the person out of every future read (Stage 6 sweep results, cached-lead reads) — which is
the compliance-relevant guarantee ("never silently re-add them") — but does not rewrite the underlying JSONL
row, consistent with every other store here being append-only. A real database migration would additionally
hard-delete or anonymise the row itself.

**Remaining pre-existing Part I limitations, unaffected by the phases above:** Lever's multi-field description
assembly (only `description`, not `opening`/`lists`/`additional`), Greenhouse's `offices[]` not cross-checked
as a secondary location source, full boilerplate stripping / non-English handling (§7.6), and the Stage 2/3
board-token double-fetch when Stage 2 identifies a platform via redirect. None of these were in this audit's
findings as required-and-missing; they're pre-existing, intentionally-scoped simplifications documented since
Phase 1/2.

**Rate limiting does not yet differentiate ATS-API hosts from startup origins.** Spec §16.3: "ATS API hosts
get a higher allowance than startup origins." `Fetcher`'s per-domain concurrency and minimum-interval
mechanisms apply the same configured values to every host; teaching the fetch layer about the ATS host list
(`discovery.ats_platforms`) would introduce a dependency in the wrong direction for a cross-cutting layer — see
`core/fetch.py`'s module docstring.

**ATS-fingerprinting Signal 5's live wiring is partial.** `identify_from_captured_requests` (the actual
XHR-URL-matching logic) is implemented and tested, but feeding a rendered-DOM render's captured requests back
into a live Stage 2 re-run needs a two-pass orchestration this codebase's schedulerless CLI doesn't have (spec
§16). The rendered-DOM adapter's own endpoint-learning cache already delivers Signal 5's main practical
benefit (avoiding repeated renders) at the extraction layer.

## Design reference

All architectural decisions here trace back to `JOB_SCRAPING_AGENT.md`. Notably:

- §2.1 — ATS-API-first, HTML second
- §2.3 — "scrape failed" is never "no open jobs"
- §2.7 — discover leads per company, match to jobs locally (the reason Stage 6 is a per-company sweep, not a
  per-job search)
- §2.8 — cheap signals before expensive ones (rules-based matching before PDL enrichment before LLM scoring)
- §2.9 — say "we don't know" rather than guess (empty lead sets carry an explicit reason; low evidence produces
  a low score, never a confident-sounding guess)
- §10.4 — headcount modulation, the single most consequential correction in Part II
- §13.1 — the LLM judges and explains an already-computed match; it never matches from scratch

Read the spec before extending any stage — the "why" behind each module lives there, not in code comments.
