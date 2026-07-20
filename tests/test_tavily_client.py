"""services.tavily.TavilyClient.search() tests — spec §4.1 Strategy D.

Deterministic, no real network (spec §20.1): exercises the client against a
MockTransport-backed Fetcher, the same pattern as tests/test_fetch.py.
"""

import json

import httpx
import pytest

from gtm_agent.config.settings import Settings
from gtm_agent.core.fetch import Fetcher
from gtm_agent.services.tavily import TavilyClient, TavilyNotConfiguredError, TavilySearchError


def _client(monkeypatch: pytest.MonkeyPatch, api_key: str = "tvly-test-key") -> TavilyClient:
    monkeypatch.setattr(
        "gtm_agent.services.tavily.get_settings",
        lambda: Settings(tavily_api_key=api_key),
    )
    return TavilyClient()


async def test_not_configured_raises_without_making_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, api_key="")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"results": []}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(TavilyNotConfiguredError):
            await client.search("acme careers", fetcher=fetcher)
    finally:
        await fetcher.aclose()

    assert calls["n"] == 0


async def test_search_posts_api_key_query_and_include_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, api_key="tvly-secret")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=json.dumps({"results": [{"url": "https://acme.com/careers"}]}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        results = await client.search(
            '"Acme" careers open positions site:acme.com', fetcher=fetcher, restrict_domain="acme.com"
        )
    finally:
        await fetcher.aclose()

    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["body"] == {
        "api_key": "tvly-secret",
        "query": '"Acme" careers open positions site:acme.com',
        "include_domains": ["acme.com"],
    }
    assert results == [{"url": "https://acme.com/careers"}]


async def test_search_without_restrict_domain_omits_include_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=json.dumps({"results": []}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        await client.search('"Acme" jobs', fetcher=fetcher)
    finally:
        await fetcher.aclose()

    assert "include_domains" not in seen["body"]


async def test_missing_results_key_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"answer": "no results field here"}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        results = await client.search("query", fetcher=fetcher)
    finally:
        await fetcher.aclose()

    assert results == []


async def test_http_error_raises_tavily_search_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=json.dumps({"error": "invalid API key"}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(TavilySearchError):
            await client.search("query", fetcher=fetcher)
    finally:
        await fetcher.aclose()


async def test_invalid_json_raises_tavily_search_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(TavilySearchError):
            await client.search("query", fetcher=fetcher)
    finally:
        await fetcher.aclose()
