"""services.pdl.PDLClient.enrich_person() tests — spec §11.3's identity waterfall."""

import json

import httpx
import pytest

from gtm_agent.config.settings import Settings
from gtm_agent.core.fetch import Fetcher
from gtm_agent.services.pdl import PDLClient, PDLEnrichError, PDLNotConfiguredError


def _client(monkeypatch: pytest.MonkeyPatch, api_key: str = "pdl-test-key") -> PDLClient:
    monkeypatch.setattr("gtm_agent.services.pdl.get_settings", lambda: Settings(pdl_api_key=api_key))
    return PDLClient()


async def test_not_configured_raises_without_making_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, api_key="")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"data": {}}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        with pytest.raises(PDLNotConfiguredError):
            await client.enrich_person(fetcher=fetcher, linkedin_url="https://linkedin.com/in/x")
    finally:
        await fetcher.aclose()
    assert calls["n"] == 0


async def test_no_identifiers_returns_none_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps({"data": {}}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await client.enrich_person(fetcher=fetcher)
    finally:
        await fetcher.aclose()
    assert result is None
    assert calls["n"] == 0


async def test_tries_linkedin_first_and_stops_on_match(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    seen_params: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, text=json.dumps({"data": {"job_title": "CTO"}}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await client.enrich_person(
            fetcher=fetcher,
            linkedin_url="https://linkedin.com/in/x",
            work_email="x@acme.com",
            full_name="X Y",
            company_domain="acme.com",
        )
    finally:
        await fetcher.aclose()

    assert result == {"job_title": "CTO"}
    assert len(seen_params) == 1
    assert seen_params[0]["profile"] == "https://linkedin.com/in/x"


async def test_falls_through_linkedin_miss_to_email(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        if "profile" in params:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=json.dumps({"data": {"job_title": "VP Eng"}}))

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await client.enrich_person(
            fetcher=fetcher, linkedin_url="https://linkedin.com/in/x", work_email="x@acme.com"
        )
    finally:
        await fetcher.aclose()

    assert result == {"job_title": "VP Eng"}
    assert len(calls) == 2


async def test_falls_through_to_name_and_domain_as_last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if "name" in params and "company" in params:
            return httpx.Response(200, text=json.dumps({"data": {"job_title": "Head of Sales"}}))
        return httpx.Response(404, text="not found")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await client.enrich_person(fetcher=fetcher, full_name="Jane Doe", company_domain="acme.com")
    finally:
        await fetcher.aclose()

    assert result == {"job_title": "Head of Sales"}


async def test_all_keys_miss_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await client.enrich_person(
            fetcher=fetcher, linkedin_url="https://linkedin.com/in/x", full_name="Jane Doe", company_domain="acme.com"
        )
    finally:
        await fetcher.aclose()
    assert result is None


async def test_http_error_raises_pdl_enrich_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        with pytest.raises(PDLEnrichError):
            await client.enrich_person(fetcher=fetcher, linkedin_url="https://linkedin.com/in/x")
    finally:
        await fetcher.aclose()
