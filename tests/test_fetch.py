"""core.fetch tests — deterministic, no real network (spec §20.1).

Uses httpx.MockTransport to exercise Fetcher's request/response handling
without touching the network, per spec's "no network in unit tests, ever".
"""

import httpx

from gtm_agent.core.fetch import Fetcher


async def test_request_count_and_bytes_fetched_increment_per_request():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello")

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        await fetcher.get("https://example.com/a")
        await fetcher.get("https://example.com/b")
    finally:
        await fetcher.aclose()

    assert fetcher.request_count == 2
    assert fetcher.bytes_fetched == len(b"hello") * 2


async def test_counters_start_at_zero_for_a_fresh_fetcher():
    fetcher = Fetcher()
    try:
        assert fetcher.request_count == 0
        assert fetcher.bytes_fetched == 0
    finally:
        await fetcher.aclose()


async def test_retried_requests_each_count_toward_request_count():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="try again")
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), backoff_base_seconds=0.01)
    try:
        result = await fetcher.get("https://example.com/retry-me")
    finally:
        await fetcher.aclose()

    assert result.status_code == 200
    # Two 503 attempts + the final success — every attempt sent over the wire counts.
    assert fetcher.request_count == 3
    assert fetcher.bytes_fetched == len(b"try again") * 2 + len(b"ok")


async def test_non_retryable_status_does_not_inflate_request_count():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        result = await fetcher.get("https://example.com/missing")
    finally:
        await fetcher.aclose()

    assert result.status_code == 404
    assert fetcher.request_count == 1
    assert fetcher.bytes_fetched == len(b"nope")
