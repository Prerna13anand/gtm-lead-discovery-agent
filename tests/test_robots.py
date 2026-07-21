"""core.robots.RobotsCache tests — spec §21.1. Pure logic, no real network."""

from datetime import UTC, datetime, timedelta

from gtm_agent.core.robots import RobotsCache

_UA = "GTM-Lead-Discovery-Agent/0.1 (+mailto:test@example.com)"


async def test_allowed_when_no_disallow_rule_matches():
    calls = {"n": 0}

    async def fetch_robots_txt(url: str) -> str | None:
        calls["n"] += 1
        return "User-agent: *\nDisallow: /admin/"

    cache = RobotsCache()
    assert await cache.is_allowed("https://example.com/careers", user_agent=_UA, fetch_robots_txt=fetch_robots_txt) is True
    assert calls["n"] == 1


async def test_disallowed_path_is_rejected():
    async def fetch_robots_txt(url: str) -> str | None:
        return "User-agent: *\nDisallow: /careers/"

    cache = RobotsCache()
    allowed = await cache.is_allowed(
        "https://example.com/careers/engineer", user_agent=_UA, fetch_robots_txt=fetch_robots_txt
    )
    assert allowed is False


async def test_missing_robots_txt_allows_everything():
    async def fetch_robots_txt(url: str) -> str | None:
        return None  # 404 / unreachable

    cache = RobotsCache()
    assert await cache.is_allowed("https://example.com/careers", user_agent=_UA, fetch_robots_txt=fetch_robots_txt) is True


async def test_result_is_cached_per_host_within_ttl():
    calls = {"n": 0}

    async def fetch_robots_txt(url: str) -> str | None:
        calls["n"] += 1
        return "User-agent: *\nDisallow: /careers/"

    cache = RobotsCache()
    await cache.is_allowed("https://example.com/careers/a", user_agent=_UA, fetch_robots_txt=fetch_robots_txt)
    await cache.is_allowed("https://example.com/careers/b", user_agent=_UA, fetch_robots_txt=fetch_robots_txt)
    assert calls["n"] == 1  # second call reuses the cached parse for this host


async def test_cache_is_scoped_per_host():
    calls: dict[str, int] = {}

    async def fetch_robots_txt(url: str) -> str | None:
        calls[url] = calls.get(url, 0) + 1
        return "User-agent: *\nDisallow: /careers/"

    cache = RobotsCache()
    await cache.is_allowed("https://a.com/careers", user_agent=_UA, fetch_robots_txt=fetch_robots_txt)
    await cache.is_allowed("https://b.com/careers", user_agent=_UA, fetch_robots_txt=fetch_robots_txt)
    assert calls == {"https://a.com/robots.txt": 1, "https://b.com/robots.txt": 1}


async def test_stale_entry_is_refetched():
    calls = {"n": 0}

    async def fetch_robots_txt(url: str) -> str | None:
        calls["n"] += 1
        return "User-agent: *\nDisallow: /careers/"

    # A zero TTL means every lookup is immediately stale — simpler than
    # monkeypatching datetime.now() to simulate real elapsed time.
    zero_ttl_cache = RobotsCache(ttl=timedelta(seconds=0))
    await zero_ttl_cache.is_allowed("https://example.com/careers", user_agent=_UA, fetch_robots_txt=fetch_robots_txt)
    await zero_ttl_cache.is_allowed("https://example.com/careers", user_agent=_UA, fetch_robots_txt=fetch_robots_txt)
    assert calls["n"] == 2
