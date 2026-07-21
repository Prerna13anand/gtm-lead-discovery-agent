"""Environment-backed configuration.

All credentials and environment-dependent values are loaded here and nowhere else.
No module outside this file should read `os.environ` or `os.getenv` directly.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Azure OpenAI (config/init only in Phase 1 — see services/azure_openai.py) ---
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-08-01-preview"

    # --- Apollo / PDL / Tavily (not integrated until later phases) ---
    apollo_api_key: str = ""
    pdl_api_key: str = ""
    tavily_api_key: str = ""

    # --- Database ---
    database_url: str = ""

    # --- Fetch layer ---
    http_user_agent: str = "GTM-Lead-Discovery-Agent/0.1 (+mailto:reubenjacob@syphonlabs.com)"
    http_timeout_seconds: float = Field(default=15.0, gt=0)

    # --- Logging ---
    log_level: str = "INFO"
    log_json: bool = False

    # --- scrape_run ledger (spec §15.1) — no database yet, so this is a local file ---
    scrape_run_ledger_path: str = ".data/scrape_runs.jsonl"
    raw_payload_archive_dir: str = ".data/raw_payloads"

    # --- Stage 5 lifecycle stores (spec §15.1) — same local-file pattern as the run ledger ---
    job_posting_store_path: str = ".data/job_postings.jsonl"
    job_posting_version_path: str = ".data/job_posting_versions.jsonl"
    scrape_event_log_path: str = ".data/scrape_events.jsonl"

    # --- Canary suite (spec §20.3) ---
    canary_result_log_path: str = ".data/canary_results.jsonl"
    canary_finding_log_path: str = ".data/canary_findings.jsonl"

    # --- Part II stores (spec §15.2) — same local-file pattern, no database yet ---
    lead_store_path: str = ".data/leads.jsonl"
    lead_discovery_run_path: str = ".data/lead_discovery_runs.jsonl"
    lead_job_match_path: str = ".data/lead_job_matches.jsonl"
    unmatched_job_path: str = ".data/unmatched_jobs.jsonl"
    company_context_path: str = ".data/company_contexts.jsonl"
    lead_feedback_path: str = ".data/lead_feedback.jsonl"

    # --- Credit budget ceilings (spec §18.3) — per-sweep; no real figures
    # are given by the spec (open question §23.14), so these are
    # conservative defaults sized for the CLI demo harness, not a production
    # allowance. See leads/budget.py.
    apollo_credit_ceiling: int = 500
    pdl_credit_ceiling: int = 500
    tavily_call_ceiling: int = 500

    # --- Part III stores (spec §15.2, §14) ---
    scored_lead_path: str = ".data/scored_leads.jsonl"
    publication_event_path: str = ".data/publication_events.jsonl"
    gtm_lead_table_path: str = ".data/gtm_leads.jsonl"
    gtm_lead_csv_path: str = ".data/gtm_leads.csv"

    # --- Compliance (spec §21.6, Phase 5) ---
    company_denylist_path: str = ".data/company_denylist.jsonl"
    person_suppression_path: str = ".data/person_suppression.jsonl"

    # --- Stage 4 LLM title-residue classification cache (spec §7.3) ---
    title_classification_cache_path: str = ".data/title_classification_cache.jsonl"

    @property
    def azure_openai_configured(self) -> bool:
        """Whether enough Azure OpenAI config is present to initialise a client.

        Phase 1 never calls the client, but downstream phases can check this
        before attempting to build one.
        """
        return bool(self.azure_openai_api_key and self.azure_openai_endpoint and self.azure_openai_deployment)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance, loaded once and cached."""
    return Settings()
