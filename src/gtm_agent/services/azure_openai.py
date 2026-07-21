"""Azure OpenAI service — configuration and client initialisation only.

Deliberately holds no prompts or business logic — every actual LLM call
site builds its own messages and owns its own retry/grounding/validation
policy, then gets a configured client from here rather than each
constructing its own:
    - `discovery.llm_residue` — Stage 4 title residue classification (§7.3)
    - `leads.tie_break` — Stage 7 optional matching tie-break (§10.7)
    - `scoring.rationale` — Stage 10 scoring and rationale (§13)
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

        Raises `AzureOpenAIConfigError` if credentials aren't set — every
        real call site checks `is_configured` first and degrades gracefully
        (see the module docstring's call sites) rather than calling this
        when it knows it isn't configured.
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
