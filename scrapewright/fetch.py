"""Fetchers — how HTML gets into the pipeline.

Two implementations behind one tiny interface (``fetch(url) -> str | None``):

* :class:`StaticFetcher` — plain HTTP. Fast, free, no dependencies. Correct for
  server-rendered pages, which is most of the long tail.
* :class:`BrowserFetcher` — a real Chromium page via Playwright, for stores that
  render their catalog client-side. Slower and heavier, so it is opt-in and, in
  the pipeline, only reached when the static path demonstrably fails.

The split matters because it keeps the expensive path *rare* rather than
default: same philosophy as the LLM being a one-time compiler. A browser is
started at most once per run and reused across every page.
"""

from __future__ import annotations

import re

import requests

from .http import USER_AGENT, get, make_session

_TAG = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>", re.DOTALL | re.I)
_TAGS = re.compile(r"<[^>]+>")

# Below this much visible text, a 200-OK page is almost certainly a JS shell
# rather than a rendered product page.
SHELL_TEXT_THRESHOLD = 600

# Framework mount points that appear in an unrendered shell.
_SHELL_MARKERS = re.compile(
    r'id=["\'](root|app|__next|__nuxt|q-app)["\']|__NUXT__|__NEXT_DATA__', re.I
)


def visible_text_length(html: str) -> int:
    """Rough count of text a human would actually see."""
    stripped = _TAG.sub(" ", html)
    text = _TAGS.sub(" ", stripped)
    return len(" ".join(text.split()))


def looks_js_shelled(html: str) -> bool:
    """True when the HTML is a client-side shell with no rendered content.

    Used to skip a doomed static extraction (and the LLM call it would spend)
    and go straight to the browser.
    """
    if not html:
        return True
    return visible_text_length(html) < SHELL_TEXT_THRESHOLD and bool(_SHELL_MARKERS.search(html))


class StaticFetcher:
    """Plain HTTP fetch. The default everywhere."""

    kind = "static"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or make_session()

    def fetch(self, url: str) -> str | None:
        try:
            r = get(url, session=self.session)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        return r.text

    def close(self) -> None:  # symmetry with BrowserFetcher
        pass


class BrowserFetcher:
    """Render a page in headless Chromium and return the resulting DOM.

    The browser is started lazily on first use and reused for every subsequent
    fetch, so a crawl pays the startup cost once. Install with::

        pip install "scrapewright[js]"
        playwright install chromium
    """

    kind = "browser"

    def __init__(self, headless: bool = True, wait_until: str = "networkidle",
                 timeout_ms: int = 30000, settle_ms: int = 0,
                 user_agent: str | None = None):
        self.headless = headless
        self.wait_until = wait_until
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.user_agent = user_agent or USER_AGENT
        self._playwright = None
        self._browser = None
        self._page = None

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover - env dependent
            raise RuntimeError(
                "Browser fetching needs Playwright. Install it with:\n"
                '    pip install "scrapewright[js]"\n'
                "    playwright install chromium"
            ) from e
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        context = self._browser.new_context(user_agent=self.user_agent)
        self._page = context.new_page()
        return self._page

    def fetch(self, url: str) -> str | None:
        page = self._ensure_page()
        try:
            page.goto(url, wait_until=self.wait_until, timeout=self.timeout_ms)
            if self.settle_ms:
                page.wait_for_timeout(self.settle_ms)
            return page.content()
        except Exception:
            # A render failure is a miss, not a crash — the caller falls back.
            return None

    def close(self) -> None:
        for obj in (self._browser, self._playwright):
            try:
                obj.close() if obj is self._browser else obj.stop()
            except Exception:
                pass
        self._page = self._browser = self._playwright = None

    def __enter__(self) -> BrowserFetcher:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
