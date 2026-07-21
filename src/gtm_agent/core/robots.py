"""robots.txt consultation — spec §21.1.

"Fetched, cached with TTL, and consulted before every request. `Disallow`
on a careers path is respected — the run terminates as `robots_disallowed`
and the company is recorded as unscraped. No exceptions, no override flag."

`RobotsCache` is pure caching/parsing logic; the actual HTTP fetch of a
`robots.txt` file is injected as a callback (`fetch_robots_txt`) so this
module has no direct dependency on `httpx` or `core.fetch.Fetcher` — the
same "logic separate from I/O" split used throughout this codebase (see
`discovery.lifecycle`'s module docstring for the same convention).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

# Spec §21.1 says "cached with TTL" without naming a duration. 24 hours is
# this codebase's own conservative default — long enough that a normal
# sweep cadence (spec §16.2: daily at the fastest) doesn't re-fetch
# `robots.txt` on every single request to the same host, short enough that
# a site owner's changed policy takes effect the same day.
DEFAULT_TTL = timedelta(hours=24)


@dataclass
class _CachedRobots:
    parser: RobotFileParser
    fetched_at: datetime


class RobotsCache:
    """Per-host `robots.txt` cache. One instance is meant to be shared for
    the lifetime of a `Fetcher` — same per-process, in-memory scope as the
    conditional-request validator cache it sits alongside.
    """

    def __init__(self, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._cache: dict[str, _CachedRobots] = {}

    def _is_stale(self, entry: _CachedRobots, *, now: datetime) -> bool:
        return now - entry.fetched_at > self._ttl

    async def is_allowed(
        self,
        url: str,
        *,
        user_agent: str,
        fetch_robots_txt: Callable[[str], Awaitable[str | None]],
    ) -> bool:
        """`fetch_robots_txt` returns the file's text content, or `None` if
        it couldn't be fetched (network failure, or a non-2xx response —
        most commonly 404, meaning the site simply has no `robots.txt`).

        A missing/unreachable `robots.txt` is treated as "nothing
        disallowed" — the conservative, widely-adopted convention (and
        Python's own `urllib.robotparser` default): absence of a policy is
        not itself a disallow signal. The spec is silent on this specific
        case; "no exceptions, no override flag" governs what to do with an
        *existing* `Disallow`, not whether to invent one where none exists.
        """
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        now = datetime.now(UTC)

        entry = self._cache.get(host_key)
        if entry is None or self._is_stale(entry, now=now):
            robots_url = f"{host_key}/robots.txt"
            content = await fetch_robots_txt(robots_url)
            parser = RobotFileParser()
            if content is None:
                parser.allow_all = True
            else:
                parser.parse(content.splitlines())
            entry = _CachedRobots(parser=parser, fetched_at=now)
            self._cache[host_key] = entry

        return entry.parser.can_fetch(user_agent, url)
