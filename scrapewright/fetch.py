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

import os

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


def scroll_to_end(page, max_scrolls: int, pause_ms: int = 900) -> int:
    """Scroll until the page stops growing. Returns how many rounds it took.

    Kept separate from the fetcher so the stopping rule can be tested without a
    browser, because the rule is the whole difficulty: stop too early and half
    the catalogue is missing, never stop and one page eats the job. It gives up
    when a scroll adds no height -- the honest signal that there is no more --
    and otherwise at ``max_scrolls``, which is what stops a feed that is
    genuinely endless from running forever.
    """
    # Measured before the first scroll, so a page that does not grow costs one
    # round instead of two -- and most pages do not grow.
    previous = page.evaluate("document.body.scrollHeight")
    for round_number in range(1, max_scrolls + 1):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(pause_ms)
        height = page.evaluate("document.body.scrollHeight")
        if height <= previous:
            return round_number
        previous = height
    return max_scrolls


# What Chromium needs to start, and nothing else. Everything absent from
# this list -- every API key, every token -- is absent from the browser.
_BROWSER_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ",
                     "DISPLAY", "XDG_RUNTIME_DIR", "PLAYWRIGHT_BROWSERS_PATH")


def _browser_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in _BROWSER_ENV_KEEP
           if name in os.environ}
    env.setdefault("HOME", "/tmp")
    return env


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
                 user_agent: str | None = None, max_scrolls: int = 0,
                 scroll_pause_ms: int = 900):
        self.headless = headless
        self.wait_until = wait_until
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.user_agent = user_agent or USER_AGENT
        # Infinite scroll: a listing that loads more as you go looks like a
        # twenty-item page to anyone who only reads the first render. Off by
        # default, because scrolling a page that does not grow is wasted time.
        self.max_scrolls = max_scrolls
        self.scroll_pause_ms = scroll_pause_ms
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
        self._browser = self._launch(self._playwright)
        context = self._browser.new_context(user_agent=self.user_agent)
        self._page = context.new_page()
        return self._page

    def _launch(self, playwright):
        """Start Chromium with as little of our world as it needs.

        It is about to open pages chosen by strangers. Two things are cheap
        and worth doing even though neither is isolation:

        * **A scrubbed environment.** By default the browser inherits ours,
          which holds the Stripe key, the model key and the bucket
          credentials. It needs none of them, and handing them over is
          handing over the service.
        * **Chromium's own sandbox**, which Playwright leaves off by default.
          It needs user namespaces and quietly fails without them, so the
          launch falls back rather than refusing to render.

        What this does *not* do is isolate anything. The browser still runs
        as the same user, on the same filesystem, in the same network
        namespace -- an escape can read the API's own /proc entry and find
        the secrets there. Only moving the browser to its own machine fixes
        that; see SECURITY.md.
        """
        try:
            return playwright.chromium.launch(headless=self.headless,
                                              env=_browser_env(),
                                              chromium_sandbox=True)
        except Exception:
            # No user namespaces here (many container hosts). Still scrubbed.
            return playwright.chromium.launch(headless=self.headless,
                                              env=_browser_env())

    @property
    def alive(self) -> bool:
        """Is this browser still usable, or did it die under us?

        When the kernel killed Chromium for memory, every later render
        answered "Connection closed while reading from the driver" until the
        machine was restarted. A fetcher that knows it is dead can be thrown
        away and replaced, which costs one request instead of the service.
        """
        if self._browser is None:
            return False
        try:
            return bool(self._browser.is_connected())
        except Exception:
            return False

    def fetch(self, url: str) -> str | None:
        # Rendering is still fetching: the browser path must obey robots too,
        # and it does not go through http.get.
        from .robots import RobotsDisallowed, check
        from .safeurl import UnsafeUrl, check_url
        try:
            check_url(url)      # rendering is fetching, and so is a redirect
            check(url)
        except (RobotsDisallowed, UnsafeUrl):
            return None
        try:
            page = self._ensure_page()
        except Exception:
            self._discard()
            return None
        try:
            response = page.goto(url, wait_until=self.wait_until,
                                 timeout=self.timeout_ms)
            # A browser renders an error page as happily as a real one, and
            # the static fetcher has always refused anything but a 200.
            # Without the same rule here a dead link in a spreadsheet looked
            # like a page with no price on it, and every refresh paid three
            # hundred credits to have a model read "404 not found".
            if response is not None and not 200 <= response.status < 300:
                return None
            if self.settle_ms:
                page.wait_for_timeout(self.settle_ms)
            if self.max_scrolls:
                scroll_to_end(page, self.max_scrolls, self.scroll_pause_ms)
            return page.content()
        except Exception:
            # A render failure is a miss, not a crash — the caller falls back.
            # But a dead browser must not be kept: the next caller would get
            # the same error forever.
            if not self.alive:
                self._discard()
            return None

    def _discard(self) -> None:
        """Drop a browser that has died so the next request builds a new one."""
        try:
            self.close()
        except Exception:
            pass
        self._playwright = self._browser = self._page = None

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
