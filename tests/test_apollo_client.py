"""services.apollo.ApolloClient.search_people() tests — spec §9.3-9.4.

Deterministic, no real network (spec §20.1): MockTransport-backed Fetcher,
same pattern as tests/test_tavily_client.py.
"""

import json

import httpx
import pytest

from gtm_agent.config.settings import Settings
from gtm_agent.core.fetch import Fetcher
from gtm_agent.services.apollo import ApolloClient, ApolloNotConfiguredError, ApolloSearchError


def _client(monkeypatch: pytest.MonkeyPatch, api_key: str = "apollo-test-key") -> ApolloClient:
    monkeypatch.setattr(
        "gtm_agent.services.apollo.get_settings",
        lambda: Settings(apollo_api_key=api_key),
    )
    return ApolloClient()


def _people_page(n: int) -> dict:
    return {"people": [{"id": f"p{i}", "name": f"Person {i}", "title": "Engineer"} for i in range(n)]}


async def test_not_configured_raises_without_making_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, api_key="")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"people": []}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        with pytest.raises(ApolloNotConfiguredError):
            await client.search_people(company_domain="acme.com", titles=["CEO"], fetcher=fetcher)
    finally:
        await fetcher.aclose()
    assert calls["n"] == 0


async def test_search_posts_domain_titles_and_seniority(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, api_key="secret")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        return httpx.Response(200, text=json.dumps({"people": []}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await client.search_people(
            company_domain="acme.com", titles=["CEO", "CTO"], fetcher=fetcher
        )
    finally:
        await fetcher.aclose()

    assert seen["url"] == "https://api.apollo.io/v1/mixed_people/search"
    body = seen["body"]
    assert body["q_organization_domains"] == "acme.com"
    assert body["person_titles"] == ["CEO", "CTO"]
    assert "manager" in body["person_seniorities"]
    assert result.people == []
    assert result.total_entries is None

    # Live-verified (post-implementation audit): Apollo's real API rejects
    # the key in the JSON body (422 INVALID_API_KEY_LOCATION) — it must be
    # sent as an `X-Api-Key` header instead.
    assert seen["headers"]["x-api-key"] == "secret"
    assert "api_key" not in body


async def test_search_paginates_until_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        pages["n"] += 1
        if pages["n"] == 1:
            return httpx.Response(200, text=json.dumps(_people_page(25)))
        return httpx.Response(200, text=json.dumps(_people_page(10)))  # short page — stop

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await client.search_people(company_domain="acme.com", titles=["CEO"], fetcher=fetcher)
    finally:
        await fetcher.aclose()

    assert len(result.people) == 35
    assert pages["n"] == 2


async def test_search_stops_at_retrieval_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(_people_page(25)))  # always full pages

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await client.search_people(
            company_domain="acme.com", titles=["CEO"], fetcher=fetcher, limit=50
        )
    finally:
        await fetcher.aclose()

    assert len(result.people) == 50  # capped, not 75


async def test_total_entries_read_from_first_page_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _people_page(5)
        payload["pagination"] = {"total_entries": 250}
        return httpx.Response(200, text=json.dumps(payload))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await client.search_people(company_domain="acme.com", titles=["CEO"], fetcher=fetcher)
    finally:
        await fetcher.aclose()

    assert result.total_entries == 250


async def test_http_error_raises_apollo_search_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=json.dumps({"error": "invalid API key"}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        with pytest.raises(ApolloSearchError):
            await client.search_people(company_domain="acme.com", titles=["CEO"], fetcher=fetcher)
    finally:
        await fetcher.aclose()
