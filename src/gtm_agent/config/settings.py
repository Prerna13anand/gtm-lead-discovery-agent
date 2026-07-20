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
