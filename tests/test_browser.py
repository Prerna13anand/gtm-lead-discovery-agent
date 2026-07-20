"""core.browser.BrowserRenderer tests.

Unlike every other test file in this suite, these use a *real* Playwright
Chromium instance — there's no way to meaningfully test browser lifecycle,
content-wait timing, or navigation without one. Network-free rather than
"no real browser": every test navigates to a `data:` URL, so nothing leaves
the machine (spec §20.1's "no network in unit tests" is about the network,
not about avoiding a real browser process — the browser itself is a fixed,
local dependency, the same way Playwright is for the eventual generic-HTML
adapter's own tests would be).

Analytics/resource blocking and the endpoint-learning capture path are
exercised against a real, live site in this task's live-validation step
(see the task summary) rather than here — `data:` URLs have no sub-resources
of their own to block or XHR calls to make, so there's nothing meaningful to
assert about blocking without real network traffic.
"""

from urllib.parse import quote

import pytest

from gtm_agent.core.browser import BrowserRenderer, RenderTimeoutError

_JOB_LINK_WAIT_JS = r"""
() => !!document.querySelector('a[href*="/jobs/"]')
"""


def _data_url(html: str) -> str:
    return f"data:text/html,{quote(html)}"


@pytest.fixture
async def renderer():
    async with BrowserRenderer() as r:
        yield r


async def test_render_returns_html_and_final_url(renderer: BrowserRenderer) -> None:
    url = _data_url("<html><body><h1>Careers</h1></body></html>")

    result = await renderer.render(url)

    assert "Careers" in result.html
    assert result.final_url == url


async def test_render_satisfies_content_wait_when_predicate_already_true(renderer: BrowserRenderer) -> None:
    url = _data_url('<html><body><a href="/jobs/engineer">Engineer</a></body></html>')

    result = await renderer.render(url, wait_js=_JOB_LINK_WAIT_JS)

    assert result.content_wait_satisfied is True


async def test_render_content_wait_timeout_is_soft_not_fatal() -> None:
    # A short content-wait ceiling on a page that will never satisfy the
    # predicate must not raise — see module docstring / core/browser.py.
    async with BrowserRenderer(content_wait_timeout_ms=300) as renderer:
        url = _data_url("<html><body><p>No job links here.</p></body></html>")

        result = await renderer.render(url, wait_js=_JOB_LINK_WAIT_JS)

    assert result.content_wait_satisfied is False
    assert "No job links" in result.html  # the page still rendered


async def test_render_without_wait_js_is_always_satisfied(renderer: BrowserRenderer) -> None:
    url = _data_url("<html><body>plain</body></html>")

    result = await renderer.render(url)

    assert result.content_wait_satisfied is True


async def test_render_no_xhr_responses_for_a_static_page_with_no_scripts(renderer: BrowserRenderer) -> None:
    url = _data_url("<html><body>plain</body></html>")

    result = await renderer.render(url)

    assert result.xhr_responses == []


async def test_browser_instance_is_reused_across_render_calls(renderer: BrowserRenderer) -> None:
    # spec §6.2.3: "reuse a browser instance across companies... never a
    # fresh browser per company" — verify the underlying Browser object
    # itself doesn't change between calls, only the context.
    await renderer.render(_data_url("<html><body>first</body></html>"))
    browser_after_first = renderer._browser
    await renderer.render(_data_url("<html><body>second</body></html>"))
    browser_after_second = renderer._browser

    assert browser_after_first is not None
    assert browser_after_first is browser_after_second


async def test_navigation_failure_raises_render_timeout_error() -> None:
    async with BrowserRenderer(navigation_timeout_ms=2000) as renderer:
        with pytest.raises(RenderTimeoutError):
            # A literal loopback IP on a port nothing listens on — connection
            # refused near-instantly, no DNS/external network involved at all.
            await renderer.render("http://127.0.0.1:1/")


async def test_aclose_is_idempotent() -> None:
    renderer = BrowserRenderer()
    await renderer.render(_data_url("<html><body>x</body></html>"))
    await renderer.aclose()
    await renderer.aclose()  # must not raise
