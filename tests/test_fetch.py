"""core.fetch tests — deterministic, no real network (spec §20.1).

Uses httpx.MockTransport to exercise Fetcher's request/response handling
without touching the network, per spec's "no network in unit tests, ever".

Most tests here pass `respect_robots=False` — they're exercising retry,
caching, and counting behaviour unrelated to robots.txt, and would
otherwise also need to stub a `/robots.txt` response for every mock
handler. The robots.txt/per-domain-semaphore/Retry-After sections below are
the exception: those specifically test the spec §21.1/§16.3/§6.3 behaviour
`respect_robots=False` would suppress.
"""

import asyncio

import httpx

from gtm_agent.core.fetch import Fetcher, RobotsDisallowedError


async def test_request_count_and_bytes_fetched_increment_per_request():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
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

    fetcher = Fetcher(transport=httpx.MockTransport(handler), backoff_base_seconds=0.01, respect_robots=False, min_request_interval_seconds=0)
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

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
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

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
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

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
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

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
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

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
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

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
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

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
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

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        await fetcher.get("https://example.com/board")
        await fetcher.get("https://example.com/board", headers={"X-Custom": "value"})
    finally:
        await fetcher.aclose()

    assert seen_headers[1]["x-custom"] == "value"
    assert seen_headers[1]["if-none-match"] == '"abc123"'


# --- use_cache=False (exploratory reads that must not collide with a later
# real read of the same URL — see this parameter's docstring in fetch.py) ---


async def test_use_cache_false_does_not_store_a_validator():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="board", headers={"ETag": '"abc123"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        await fetcher.get("https://example.com/board", use_cache=False)
        result = await fetcher.get("https://example.com/board")  # default use_cache=True
    finally:
        await fetcher.aclose()

    # The second (default) request must see a real body, not a 304 caused by
    # a validator the first, exploratory request had no business storing.
    assert result.status_code == 200
    assert result.text == "board"


async def test_use_cache_false_does_not_send_a_previously_stored_validator():
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, text="board", headers={"ETag": '"abc123"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        await fetcher.get("https://example.com/board")  # stores "abc123"
        await fetcher.get("https://example.com/board", use_cache=False)
    finally:
        await fetcher.aclose()

    assert "if-none-match" not in seen_headers[1]


async def test_use_cache_false_still_returns_the_real_response_even_with_a_stored_validator():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=f"board-v{calls['n']}", headers={"ETag": '"abc123"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        await fetcher.get("https://example.com/board")  # stores "abc123"
        result = await fetcher.get("https://example.com/board", use_cache=False)
    finally:
        await fetcher.aclose()

    # A server that would 304 a conditional request still gets a plain
    # request here, so this always sees a real (non-empty) body.
    assert result.status_code == 200
    assert result.text == "board-v2"


async def test_default_use_cache_true_is_unaffected():
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, text="board", headers={"ETag": '"abc123"'})

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        await fetcher.get("https://example.com/board")
        await fetcher.get("https://example.com/board")
    finally:
        await fetcher.aclose()

    assert seen_headers[1]["if-none-match"] == '"abc123"'


# --- robots.txt consultation (spec §21.1) --------------------------------


async def test_disallowed_path_raises_before_any_request_is_sent():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /careers/")
        return httpx.Response(200, text="should never be reached")

    fetcher = Fetcher(transport=httpx.MockTransport(handler))  # respect_robots=True (default)
    try:
        try:
            await fetcher.get("https://example.com/careers/engineer")
            raised = False
        except RobotsDisallowedError:
            raised = True
    finally:
        await fetcher.aclose()

    assert raised is True
    # Only the robots.txt fetch happened — the disallowed URL itself was never requested.
    assert calls == ["https://example.com/robots.txt"]
    # Spec §21.1: "No retry" — and robots.txt fetches aren't counted (see fetch.py's module docstring).
    assert fetcher.request_count == 0


async def test_robots_disallowed_error_is_a_fetch_error_subclass():
    """Every existing `except FetchError` call site in this codebase keeps
    working unchanged — spec §21.1's "no exceptions" is enforced by always
    raising, not by needing every caller to special-case a new exception type.
    """
    from gtm_agent.core.fetch import FetchError

    assert issubclass(RobotsDisallowedError, FetchError)


async def test_allowed_path_proceeds_normally():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404, text="")  # no robots.txt -> allow everything
        return httpx.Response(200, text="job board")

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        result = await fetcher.get("https://example.com/careers")
    finally:
        await fetcher.aclose()

    assert result.status_code == 200
    assert result.text == "job board"


async def test_robots_txt_is_fetched_once_per_host_across_requests():
    robots_fetches = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            robots_fetches["n"] += 1
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    try:
        await fetcher.get("https://example.com/a")
        await fetcher.get("https://example.com/b")
    finally:
        await fetcher.aclose()

    assert robots_fetches["n"] == 1  # cached across both requests to the same host


async def test_respect_robots_false_skips_the_check_entirely():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            raise AssertionError("robots.txt should never be fetched when respect_robots=False")
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)
    try:
        result = await fetcher.get("https://example.com/careers/engineer")
    finally:
        await fetcher.aclose()

    assert result.status_code == 200


# --- Per-domain concurrency (spec §16.3) ----------------------------------


async def test_concurrent_requests_to_the_same_host_are_bounded():
    in_flight = {"current": 0, "max_seen": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        in_flight["current"] += 1
        in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
        await asyncio.sleep(0.02)
        in_flight["current"] -= 1
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), respect_robots=False, per_domain_concurrency=2, min_request_interval_seconds=0
    )
    try:
        await asyncio.gather(*(fetcher.get(f"https://example.com/{i}") for i in range(6)))
    finally:
        await fetcher.aclose()

    assert in_flight["max_seen"] <= 2


async def test_different_hosts_are_not_bounded_by_each_others_semaphore():
    in_flight = {"current": 0, "max_seen": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        in_flight["current"] += 1
        in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
        await asyncio.sleep(0.02)
        in_flight["current"] -= 1
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), respect_robots=False, per_domain_concurrency=1, min_request_interval_seconds=0
    )
    try:
        await asyncio.gather(
            fetcher.get("https://a.com/x"),
            fetcher.get("https://b.com/x"),
            fetcher.get("https://c.com/x"),
        )
    finally:
        await fetcher.aclose()

    # Three different hosts, each capped at 1 -- but concurrently, so more
    # than 1 can be in flight globally at the same instant.
    assert in_flight["max_seen"] >= 2


# --- Retry-After (spec §6.3) -----------------------------------------------


async def test_retry_after_seconds_is_honoured():
    calls = {"n": 0}
    sleep_calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down", headers={"Retry-After": "5"})
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    original_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        result = await fetcher.get("https://example.com/board")
    finally:
        asyncio.sleep = original_sleep
        await fetcher.aclose()

    assert result.status_code == 200
    assert sleep_calls == [5.0]


async def test_retry_after_http_date_is_parsed():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    calls = {"n": 0}
    sleep_calls: list[float] = []

    future = datetime.now(UTC) + timedelta(seconds=10)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="slow down", headers={"Retry-After": format_datetime(future)})
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    original_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        result = await fetcher.get("https://example.com/board")
    finally:
        asyncio.sleep = original_sleep
        await fetcher.aclose()

    assert result.status_code == 200
    assert len(sleep_calls) == 1
    assert 8.0 <= sleep_calls[0] <= 10.0  # allow a little slack for test execution time


async def test_missing_retry_after_falls_back_to_fixed_backoff():
    calls = {"n": 0}
    sleep_calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="slow down")  # no Retry-After header
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), respect_robots=False, backoff_base_seconds=0.01, min_request_interval_seconds=0
    )

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    original_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        result = await fetcher.get("https://example.com/board")
    finally:
        asyncio.sleep = original_sleep
        await fetcher.aclose()

    assert result.status_code == 200
    assert sleep_calls == [0.01]  # fixed backoff for attempt 0


# --- Minimum request interval / rate limiting (spec §6.3) ------------------


async def test_second_request_to_same_host_waits_the_minimum_interval():
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=1.0
    )
    original_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        await fetcher.get("https://example.com/a")
        await fetcher.get("https://example.com/b")  # same host as /a
    finally:
        asyncio.sleep = original_sleep
        await fetcher.aclose()

    assert len(sleep_calls) == 1  # only the second request had to wait
    assert 0.7 <= sleep_calls[0] <= 1.3  # jittered around 1.0s (+/-20%)


async def test_different_hosts_are_not_rate_limited_against_each_other():
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=1.0
    )
    original_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        await fetcher.get("https://a.com/x")
        await fetcher.get("https://b.com/x")
    finally:
        asyncio.sleep = original_sleep
        await fetcher.aclose()

    assert sleep_calls == []  # different hosts, no shared interval


async def test_min_request_interval_zero_disables_rate_limiting():
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), respect_robots=False, min_request_interval_seconds=0
    )
    original_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        await fetcher.get("https://example.com/a")
        await fetcher.get("https://example.com/a")
    finally:
        asyncio.sleep = original_sleep
        await fetcher.aclose()

    assert sleep_calls == []
