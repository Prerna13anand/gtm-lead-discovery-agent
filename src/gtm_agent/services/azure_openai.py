"""Azure OpenAI service — configuration and client initialisation only.

Per Phase 1 scope: no prompts, no scoring, no LLM calls of any kind. This
module exists so later phases (function/seniority residue classification in
§7.3, matching tie-break in §10.7, scoring in Stage 10/§13) have a single,
already-wired place to get a configured client from, instead of each
inventing its own initialisation.
"""

from __future__ import annotations

from openai import AzureOpenAI

from gtm_agent.config import get_settings


class AzureOpenAIConfigError(Exception):
    """Raised when a client is requested but required settings are missing."""


class AzureOpenAIService:
    """Thin wrapper around client construction. Holds no prompts or business logic."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: AzureOpenAI | None = None

    @property
    def is_configured(self) -> bool:
        return self._settings.azure_openai_configured

    def get_client(self) -> AzureOpenAI:
        """Lazily construct and cache the Azure OpenAI client.

        Raises `AzureOpenAIConfigError` if credentials aren't set — callers
        in Phase 1 should not be calling this at all (see module docstring).
        """
        if not self.is_configured:
            raise AzureOpenAIConfigError(
                "AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, and AZURE_OPENAI_DEPLOYMENT "
                "must all be set before an Azure OpenAI client can be created."
            )
        if self._client is None:
            self._client = AzureOpenAI(
                api_key=self._settings.azure_openai_api_key,
                azure_endpoint=self._settings.azure_openai_endpoint,
                api_version=self._settings.azure_openai_api_version,
            )
        return self._client

    @property
    def deployment(self) -> str:
        return self._settings.azure_openai_deployment

    # TODO(phase 2+): title classification residue calls (§7.3)
    # TODO(phase 3): matching tie-break calls (§10.7)
    # TODO(phase 4): scoring + rationale calls (§13)
