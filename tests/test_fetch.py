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


# --- Conditional requests (spec §6.3) -----------------------------------


async def test_first_request_sends_no_conditional_headers():
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, text="board", headers={"ETag": '"abc123"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        await fetcher.get("https://example.com/board")
    finally:
        await fetcher.aclose()

    assert "if-none-match" not in seen_headers[0]
    assert "if-modified-since" not in seen_headers[0]


async def test_etag_from_response_is_sent_as_if_none_match_on_next_request():
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, text="board", headers={"ETag": '"abc123"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        await fetcher.get("https://example.com/board")
        await fetcher.get("https://example.com/board")
    finally:
        await fetcher.aclose()

    assert seen_headers[1]["if-none-match"] == '"abc123"'


async def test_last_modified_from_response_is_sent_as_if_modified_since_on_next_request():
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(
            200, text="board", headers={"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"}
        )

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        await fetcher.get("https://example.com/board")
        await fetcher.get("https://example.com/board")
    finally:
        await fetcher.aclose()

    assert seen_headers[1]["if-modified-since"] == "Wed, 21 Oct 2015 07:28:00 GMT"


async def test_304_response_is_returned_without_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(304, headers={"ETag": '"abc123"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        result = await fetcher.get("https://example.com/board")
    finally:
        await fetcher.aclose()

    assert result.status_code == 304
    assert calls["n"] == 1
    assert fetcher.request_count == 1


async def test_validators_are_scoped_per_url():
    seen_headers: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers[str(request.url)] = request.headers
        return httpx.Response(200, text="board", headers={"ETag": '"abc123"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        await fetcher.get("https://example.com/a")
        await fetcher.get("https://example.com/b")
    finally:
        await fetcher.aclose()

    assert "if-none-match" not in seen_headers["https://example.com/a"]
    assert "if-none-match" not in seen_headers["https://example.com/b"]


async def test_error_response_does_not_overwrite_stored_validator():
    calls = {"n": 0}
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        seen_headers.append(request.headers)
        if calls["n"] == 1:
            return httpx.Response(200, text="board", headers={"ETag": '"good-etag"'})
        if calls["n"] == 2:
            return httpx.Response(404, text="nope")  # must not clobber the stored validator
        return httpx.Response(200, text="board")

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        await fetcher.get("https://example.com/board")  # 200, stores "good-etag"
        await fetcher.get("https://example.com/board")  # 404, no ETag in response
        await fetcher.get("https://example.com/board")  # should still carry "good-etag"
    finally:
        await fetcher.aclose()

    assert seen_headers[2]["if-none-match"] == '"good-etag"'


async def test_caller_supplied_headers_are_preserved_alongside_conditional_headers():
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, text="board", headers={"ETag": '"abc123"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        await fetcher.get("https://example.com/board")
        await fetcher.get("https://example.com/board", headers={"X-Custom": "value"})
    finally:
        await fetcher.aclose()

    assert seen_headers[1]["x-custom"] == "value"
    assert seen_headers[1]["if-none-match"] == '"abc123"'
