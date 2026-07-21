"""TavilyClient.get_company_context() tests — spec §12.2."""

import json

import httpx
import pytest

from gtm_agent.config.settings import Settings
from gtm_agent.core.fetch import Fetcher
from gtm_agent.services.tavily import TavilyClient


def _client(monkeypatch: pytest.MonkeyPatch, api_key: str = "tvly-test-key") -> TavilyClient:
    monkeypatch.setattr("gtm_agent.services.tavily.get_settings", lambda: Settings(tavily_api_key=api_key))
    return TavilyClient()


async def test_issues_three_queries_funding_hiring_careers(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=json.dumps({"results": []}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        result = await client.get_company_context(company_domain="acme.com", company_name="Acme", fetcher=fetcher)
    finally:
        await fetcher.aclose()

    assert len(bodies) == 3
    assert set(result.keys()) == {"funding_results", "hiring_results", "careers_results"}

    # Funding/hiring queries must NOT be domain-restricted — that news lives
    # on third-party sites, not the company's own domain (spec §12.2).
    assert "include_domains" not in bodies[0]
    assert "include_domains" not in bodies[1]
    # Only the careers cross-check is restricted to the company's own domain.
    assert bodies[2]["include_domains"] == ["acme.com"]
