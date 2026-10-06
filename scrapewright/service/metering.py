"""Count what a job actually consumed, without touching the core library.

The pipeline takes its fetcher, browser and LLM as constructor arguments, so
metering is a decoration problem rather than a surgery problem: wrap each of
the three, pass the wrappers in, read the counters afterwards. The library
stays unaware that it is being billed for, which is how it should be.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..fetch import StaticFetcher
from ..pipeline import Scrapewright
from .browser_pool import get_pool


@dataclass
class Meter:
    pages: int = 0
    renders: int = 0
    syntheses: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"pages": self.pages, "renders": self.renders,
                "syntheses": self.syntheses}


@dataclass
class _CountingFetcher:
    inner: object
    meter: Meter
    counter: str  # "pages" or "renders"

    def fetch(self, url: str):
        setattr(self.meter, self.counter, getattr(self.meter, self.counter) + 1)
        return self.inner.fetch(url)

    def close(self) -> None:
        # Deliberately not closing the inner fetcher: the browser belongs to
        # the process, not to this request. Closing it here is what made
        # every job pay to start Chromium again.
        pass


@dataclass
class _Slotted:
    """Holds a browser slot for exactly as long as the render takes.

    The ceiling used to be taken around the whole request, which meant a
    `js=true` extract held the only slot through the static fetch and
    through a model call that runs ten to sixty seconds. A spreadsheet
    sending two rows at once -- both of them plain Shopify pages that
    never touch a browser -- had the second wait twenty seconds and then
    get "all 1 browser slots are busy". Half a live run failed that way.

    The slot belongs around the browser, not around the request.
    """

    inner: object
    wait: float | None = None

    def fetch(self, url: str):
        with get_pool().slot(wait=self.wait):
            return self.inner.fetch(url)

    def close(self) -> None:
        pass            # the browser belongs to the process, as below


@dataclass
class _CountingLlm:
    inner: object
    meter: Meter

    def synthesize(self, html: str, url: str, schema=None, **kwargs):
        # Counted after it returns, not before. A call that raised produced no
        # recipe and was never billed to us either, so charging a customer 300
        # credits for it is taking money for nothing.
        #
        # **kwargs rather than a named `rows`: this wrapper sits between the
        # pipeline and the extractor, so an argument it has not been taught
        # about kills the job at runtime -- which is how rows mode failed the
        # first time it ran in production, and would happen again on the next
        # option added upstream.
        try:
            recipe = self.inner.synthesize(html, url, schema, **kwargs)
        except TypeError:
            # Older/injected extractors may not take a schema argument.
            recipe = self.inner.synthesize(html, url)
        self.meter.syntheses += 1
        return recipe


def metered_scrapewright(*, js: bool = False, meter: Meter | None = None,
                         max_scrolls: int = 0, slot_wait: float | None = None,
                         **kwargs) -> tuple[Scrapewright, Meter]:
    """Build a pipeline whose consumption is counted.

    The browser is only constructed when ``js`` is requested, so a static job
    never pays for Chromium — the wrapper preserves that laziness by wrapping
    the browser object rather than forcing one into existence.

    The browser itself comes from the process pool rather than being started
    here. Starting one per request is what let eight concurrent calls start
    eight Chromiums on a one-gigabyte machine and get the whole service OOM
    killed. Each render takes a slot from that same pool for its own
    duration -- see :class:`_Slotted`; ``slot_wait`` is how long it may
    wait for one, and a crawl passes a long time because it is a background
    job nobody is holding a connection open for.
    """
    meter = meter or Meter()
    fetcher = _CountingFetcher(StaticFetcher(), meter, "pages")
    browser = None
    if js:
        shared = get_pool().fetcher(max_scrolls=max_scrolls)
        # Slot outside, counter inside: a render that never got a slot is
        # not a render, and must not appear on anybody's bill.
        browser = _Slotted(_CountingFetcher(shared, meter, "renders"),
                           slot_wait)

    sw = Scrapewright(fetcher=fetcher, browser=browser, js=js, **kwargs)
    sw.llm = _CountingLlm(sw.llm, meter)
    return sw, meter
