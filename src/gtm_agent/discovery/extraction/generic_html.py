"""Generic-HTML adapter — terminal fallback (spec §6.2.4).

Heuristic DOM extraction for the residue: companies on no known ATS, with no
`schema.org/JobPosting` JSON-LD, whose careers page shows job-like content
without needing a render (routed here by `discovery.ats_detection.route_extraction`
— see that module for why a page needing JS goes to the rendered-DOM adapter
instead, and only a page that already shows job links statically, or shows no
rendering signal at all, ends up here).

Spec §6.2.4: "Find repeated structural elements containing job-like links,
cluster by DOM path, extract title/location/URL by positional and textual
heuristics." Implementation:

    1. Collect every `<a href>` on the page whose href looks like a job
       detail link — the same `is_job_like_href` check §6.2.3's rendered-DOM
       adapter and Stage 2's routing check already share, reused here rather
       than reinvented (spec: "reuse the existing extraction architecture").
    2. Cluster those anchors by their parent element's tag+class signature —
       this recovers the "repeated structural element" the spec asks for:
       real job listings are near-universally siblings inside one repeating
       container (a `<ul>` of `<li>`s, a grid of cards, ...). When one
       cluster is a clear majority, keep only that cluster and drop the
       stray anchors outside it (typically nav chrome or a single "apply"
       CTA that happens to match the href pattern); otherwise — no dominant
       repeated pattern — keep every candidate, the conservative default for
       small or irregular boards.
    3. Per anchor, extract title and location by positional/textual
       heuristics: title from the first heading-like element (`h1`-`h6`,
       `strong`, `b`) inside the anchor, falling back to the anchor's own
       text; location from a nearby element whose class name or text looks
       location-shaped. "Nearby" is deliberately scoped to the anchor's own
       subtree first, and only escalates to the shared container when that
       container belongs to this anchor alone — otherwise a location string
       from a *different* job's cluster member would attach to the wrong
       posting, which live validation against a real board (below) found to
       be a real risk, not a theoretical one — see `_extract_location`.

This path is low-confidence by construction (spec §6.2.4) — every result is
returned as `PARSE_DEGRADED`, regardless of how many (or how few) postings
were found, mirroring the same honesty policy `RenderedDomAdapter`'s DOM-link
fallback already applies: finding nothing via a heuristic path is not the
same as a confirmed empty board, so it must never be reported as `SUCCESS`.

Verified live during the Phase 2 build against a real bespoke board:
`helpscout.com/company/careers/` — no known ATS signal matches it (it is
in fact a white-labelled Ashby board reachable only via an `?ashby_jid=...`
query parameter, which none of Stage 2's current detection signals check —
URL host, redirect target, embed script/iframe src, and DOM markers all key
off the *path*, not the query string, and there is no signal 6 DNS/CNAME
check implemented yet either), no JSON-LD `JobPosting`, and job postings are
server-rendered directly into the static HTML (confirmed with a plain
`curl` fetch, no JS execution) as:

    <a href=".../<uuid>/?ashby_jid=<uuid>" class="FeatureGridBlock--Item ...">
      <h6><div class="Icon ..."></div>Sr. Product Engineer</h6>
      <p>$162K-$182K. 100% remote in the US.</p>
    </a>

repeated once per posting as siblings under one shared container. This is
exactly the shape the heuristics above target: `is_job_like_href` matches
the `/company/careers/<uuid>/` path segment, the six anchors cluster into one
dominant group by shared parent signature, the `<h6>` inside each anchor
gives a clean title, and the sibling `<p>` (which must be searched via the
anchor's own subtree, not the shared container, to avoid pulling another
job's blurb — the exact bug live validation against this page caught before
`_extract_location` was scoped this way) gives a location/comp string via
the "remote"/"US" textual heuristic — not a clean structured location field,
but a real, honest, low-confidence signal, exactly what this path promises.

**Discrepancy from a literal reading of the spec, documented rather than
guessed around**: this live case is a company on a *real* ATS (Ashby)  that
Stage 2 fails to identify because of a routing gap (query-param-only board
tokens aren't one of the four implemented detection signals), not a company
with a genuinely bespoke board. The generic-HTML adapter still does the
right thing with it — extracts real postings at reduced confidence rather
than failing outright — which is the correct behavior for *whatever* lands
here, but the Stage-2 detection gap itself is a pre-existing condition of
`ats_detection.py`, not something in scope to fix as part of this adapter.

`hydrate()` fetches the individual posting page (this is the two-phase case
spec §6.1 describes: `discover()` only has title/URL/location) and takes the
page's cleaned-up body text as the description — the best a generic path can
do without site-specific structure to trust.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from gtm_agent.core.fetch import FetchError, Fetcher, RobotsDisallowedError
from gtm_agent.core.logging import get_logger
from gtm_agent.discovery.ats_detection import is_job_like_href
from gtm_agent.models.ats import AtsPlatform
from gtm_agent.models.careers_source import CareersSource
from gtm_agent.models.job import RawPosting
from gtm_agent.models.results import ExtractionStatus, StageResult

logger = get_logger(__name__)

_HEADING_SELECTOR = "h1, h2, h3, h4, h5, h6, strong, b"

_LOCATION_CLASS_RE = re.compile(r"locat|city|office|region", re.I)
_LOCATION_TEXT_RE = re.compile(
    r"\b(?:remote|hybrid|on-?site)\b|,\s*[A-Za-z]{2,}(?:\s|$)|\b(?:USA|US|UK|EU)\b", re.I
)
_MAX_LOCATION_TEXT_LEN = 120

# Below this many members, a cluster isn't trusted as "the" repeated
# structural element (spec §6.2.4) — see _select_job_anchors.
_MIN_DOMINANT_CLUSTER_SIZE = 2

_NON_DESCRIPTION_TAGS = ("script", "style", "nav", "header", "footer")


def _signature(node: Node | None) -> str:
    if node is None:
        return ""
    classes = (node.attributes.get("class") or "").split()
    return node.tag + "." + ".".join(sorted(classes))


def _cluster_by_container(anchors: list[Node]) -> list[list[Node]]:
    groups: dict[str, list[Node]] = {}
    order: list[str] = []
    for anchor in anchors:
        sig = _signature(anchor.parent)
        if sig not in groups:
            groups[sig] = []
            order.append(sig)
        groups[sig].append(anchor)
    return [groups[sig] for sig in order]


def _select_job_anchors(anchors: list[Node]) -> list[Node]:
    """Spec §6.2.4: "find repeated structural elements containing job-like
    links". When one cluster is a clear majority, trust only it — the
    common shape of noise here is a single nav/CTA anchor that happens to
    match the job-link pattern alongside a real, larger list. With no clear
    majority (including the single-cluster case), keep everything: a small
    or irregularly-structured board shouldn't have real postings dropped by
    an over-eager filter.
    """
    if len(anchors) <= 1:
        return anchors
    clusters = _cluster_by_container(anchors)
    if len(clusters) <= 1:
        return anchors
    largest = max(clusters, key=len)
    runner_up = max((len(c) for c in clusters if c is not largest), default=0)
    if len(largest) >= _MIN_DOMINANT_CLUSTER_SIZE and len(largest) > runner_up:
        return largest
    return anchors


def _extract_title(anchor: Node) -> str:
    heading = anchor.css_first(_HEADING_SELECTOR)
    if heading is not None:
        text = heading.text(strip=True)
        if text:
            return text
    return anchor.text(strip=True)


def _location_from(node: Node | None, *, title: str) -> str | None:
    if node is None:
        return None
    # `node.css("*")` matches the node itself as well as its descendants
    # (verified directly against selectolax — the CSS match includes the
    # starting node), which would let the node's own full concatenated text
    # (title + everything else) match the textual heuristic below before any
    # real descendant is even checked. `.iter()` is descendant-only, so it
    # doesn't have that problem.
    descendants = list(node.iter())
    for descendant in descendants:
        cls = descendant.attributes.get("class") or ""
        if not _LOCATION_CLASS_RE.search(cls):
            continue
        text = descendant.text(strip=True)
        if text and text != title and len(text) <= _MAX_LOCATION_TEXT_LEN:
            return text
    for descendant in descendants:
        text = descendant.text(strip=True)
        if not text or text == title or len(text) > _MAX_LOCATION_TEXT_LEN:
            continue
        if _LOCATION_TEXT_RE.search(text):
            return text
    return None


def _extract_location(anchor: Node, title: str) -> str | None:
    # Search the anchor's own subtree first — safe by construction, never
    # crosses into a sibling job's content. Live-verified case (module
    # docstring): title and location/comp both live inside the same anchor.
    location = _location_from(anchor, title=title)
    if location is not None:
        return location

    # Only escalate to the shared container when it holds no other job-like
    # anchor — otherwise a container-wide search risks attaching a different
    # job's location to this one (a real failure mode this heuristic must
    # not have, not a hypothetical one: it's exactly what a naive
    # container-wide search would do on the verified Help Scout structure
    # before this per-anchor scoping was added).
    container = anchor.parent
    if container is not None and len(container.css("a")) <= 1:
        return _location_from(container, title=title)
    return None


def _to_raw_posting(company_id: str, anchor: Node, url: str, fetched_at: datetime) -> RawPosting:
    title = _extract_title(anchor)
    location = _extract_location(anchor, title)
    payload: dict[str, Any] = {"title": title}
    if location:
        payload["location"] = location
    return RawPosting(
        company_id=company_id,
        source_platform=AtsPlatform.GENERIC_HTML.value,
        source_job_id=None,  # no stable ID available from a bare heuristic link (spec §8.1 fallback territory)
        posting_url=url,
        raw_payload=payload,
        fetched_at=fetched_at,
        is_hydrated=False,
    )


class GenericHtmlAdapter:
    platform = AtsPlatform.GENERIC_HTML

    async def discover(self, source: CareersSource, fetcher: Fetcher) -> StageResult[list[RawPosting], ExtractionStatus]:
        try:
            result = await fetcher.get(source.careers_url)
        except RobotsDisallowedError as exc:
            return StageResult(status=ExtractionStatus.ROBOTS_DISALLOWED, detail=str(exc))
        except FetchError as exc:
            logger.info("generic_html_fetch_failed", company_id=source.company_id, error=str(exc))
            return StageResult(status=ExtractionStatus.RATE_LIMITED, detail=str(exc))

        if result.status_code in (401, 403):
            return StageResult(status=ExtractionStatus.BLOCKED_403, detail=f"HTTP {result.status_code}")
        if result.status_code >= 400:
            return StageResult(status=ExtractionStatus.SCHEMA_VIOLATION, detail=f"HTTP {result.status_code}")
        if result.status_code == 304:
            # Conditional request (spec §6.3): unchanged since our last fetch.
            return StageResult(status=ExtractionStatus.SUCCESS, value=[])

        tree = HTMLParser(result.text)
        seen_urls: set[str] = set()
        anchors: list[Node] = []
        anchor_urls: dict[int, str] = {}
        for anchor in tree.css("a[href]"):
            href = anchor.attributes.get("href") or ""
            if not is_job_like_href(href):
                continue
            absolute_url = urljoin(result.url, href)
            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)
            anchors.append(anchor)
            anchor_urls[id(anchor)] = absolute_url

        selected = _select_job_anchors(anchors)

        now = datetime.now(UTC)
        postings = [
            _to_raw_posting(source.company_id, anchor, anchor_urls[id(anchor)], now) for anchor in selected
        ]

        # Low-confidence by construction (spec §6.2.4) — every result from
        # this path is degraded, whether it found postings or not (an empty
        # result here is "the heuristic found nothing", never a confirmed
        # "zero open roles").
        return StageResult(status=ExtractionStatus.PARSE_DEGRADED, value=postings)

    async def hydrate(self, posting: RawPosting, fetcher: Fetcher) -> RawPosting:
        if posting.is_hydrated or not posting.posting_url:
            return posting

        try:
            result = await fetcher.get(posting.posting_url)
        except FetchError as exc:
            logger.info(
                "generic_html_hydrate_fetch_failed", posting_url=posting.posting_url, error=str(exc)
            )
            return posting
        if result.status_code >= 400:
            return posting

        tree = HTMLParser(result.text)
        for tag in _NON_DESCRIPTION_TAGS:
            for node in tree.css(tag):
                node.decompose()
        body = tree.css_first("body")
        description = body.text(separator="\n", strip=True) if body is not None else ""

        payload = dict(posting.raw_payload) if isinstance(posting.raw_payload, dict) else {}
        payload["description"] = description
        return posting.model_copy(update={"raw_payload": payload, "is_hydrated": True})
