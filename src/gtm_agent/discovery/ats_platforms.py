"""Static ATS platform reference data — spec Appendix A.

Shared by Stage 1 (source resolution scores a homepage link higher when it
points at a known ATS domain) and Stage 2 (ATS fingerprinting uses the same
domains as its primary detection signal).

Build note (spec §5.3): the specific endpoint shapes are documented from
public ATS board APIs but must be verified against current vendor
documentation before Phase 2 implementation. This module holds detection
domains only, not endpoint shapes.
"""

import re

from gtm_agent.models.ats import AtsPlatform

# Host suffixes that identify a known ATS. A match is a decisive detection
# signal (spec §5.1 #1) and, in Stage 1, the strongest single scoring signal
# for a homepage anchor (spec §4.1 Strategy A: "+5 href host is a known ATS
# domain — strongest single signal").
ATS_HOST_SUFFIXES: dict[AtsPlatform, tuple[str, ...]] = {
    AtsPlatform.GREENHOUSE: ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    AtsPlatform.LEVER: ("jobs.lever.co",),
    AtsPlatform.ASHBY: ("jobs.ashbyhq.com",),
    AtsPlatform.WORKABLE: ("apply.workable.com",),
    AtsPlatform.SMARTRECRUITERS: ("careers.smartrecruiters.com",),
    AtsPlatform.RECRUITEE: ("recruitee.com",),
    AtsPlatform.RIPPLING: ("ats.rippling.com",),
    AtsPlatform.PERSONIO: ("jobs.personio.de", "jobs.personio.com"),
}

# Embed script / iframe src fragments — spec §5.1 #3. Decisive when present;
# the board token is in the URL or an adjacent `data-` attribute.
ATS_EMBED_SRC_FRAGMENTS: dict[AtsPlatform, tuple[str, ...]] = {
    AtsPlatform.GREENHOUSE: ("boards.greenhouse.io/embed/job_board",),
    AtsPlatform.LEVER: ("cdn.lever.co",),
    AtsPlatform.ASHBY: ("embed.ashbyhq.com",),
}

# DOM markers — spec §5.1 #4. Strong signal, checked when no URL/embed signal fired.
ATS_DOM_MARKERS: dict[AtsPlatform, tuple[str, ...]] = {
    AtsPlatform.GREENHOUSE: ("#grnhse_app",),
    AtsPlatform.LEVER: (".lever-job", "[class*='lever-jobs']"),
    AtsPlatform.ASHBY: ("[data-ashby-embed]", "[id*='ashby']"),
}


def known_ats_platform_for_host(host: str) -> AtsPlatform | None:
    """Return the ATS platform for a hostname, if it matches a known ATS domain."""
    host = host.lower().lstrip(".")
    for platform, suffixes in ATS_HOST_SUFFIXES.items():
        for suffix in suffixes:
            if host == suffix or host.endswith(f".{suffix}"):
                return platform
    return None


def known_ats_platform_for_embed_src(src: str) -> AtsPlatform | None:
    """Return the ATS platform if a script/iframe src matches a known embed pattern."""
    src_lower = src.lower()
    for platform, fragments in ATS_EMBED_SRC_FRAGMENTS.items():
        if any(fragment in src_lower for fragment in fragments):
            return platform
    return None


# Board-token extraction — spec §5.2. Starting map only; verify before relying
# on a new platform's pattern for real API calls.
#
# Public and shared (moved here from Stage 2 in Phase 2A) so that an ATS-API
# adapter (Stage 3) can resolve its own board token straight from a
# `CareersSource.careers_url` when that URL already points at the ATS domain
# — the common case, since Stage 1's homepage-link strategy often resolves
# directly to an ATS link. `ats_detection.identify_ats` (Stage 2) uses the
# same patterns; keeping one copy avoids the two stages drifting apart.
BOARD_TOKEN_PATTERNS: dict[AtsPlatform, re.Pattern[str]] = {
    AtsPlatform.GREENHOUSE: re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([\w-]+)",
        re.I,
    ),
    AtsPlatform.LEVER: re.compile(r"jobs\.lever\.co/([\w-]+)", re.I),
    AtsPlatform.ASHBY: re.compile(r"(?:jobs|embed)\.ashbyhq\.com/([\w-]+)", re.I),
    # Excludes the `/j/{shortcode}` shortlink form (no account segment) —
    # without the lookahead, "j" would be mistaken for an account slug.
    AtsPlatform.WORKABLE: re.compile(r"apply\.workable\.com/(?!j/)([\w-]+)", re.I),
    AtsPlatform.SMARTRECRUITERS: re.compile(r"careers\.smartrecruiters\.com/([\w-]+)", re.I),
    # Recruitee's token is the subdomain itself, not a path segment after a
    # fixed host — the only adapter here shaped that way.
    AtsPlatform.RECRUITEE: re.compile(r"([\w-]+)\.recruitee\.com", re.I),
    # Rippling's token is the first path segment after the fixed host —
    # works for the bare company path, `/jobs`, or `/jobs/{id}` alike.
    AtsPlatform.RIPPLING: re.compile(r"ats\.rippling\.com/([\w-]+)", re.I),
}


def extract_board_token(platform: AtsPlatform, text: str) -> str | None:
    """Extract a board token from a URL or embed src for the given platform."""
    pattern = BOARD_TOKEN_PATTERNS.get(platform)
    if pattern is None:
        return None
    match = pattern.search(text)
    return match.group(1) if match else None
