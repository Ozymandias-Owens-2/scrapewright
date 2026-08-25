"""Count what a job actually consumed, without touching the core library.

The pipeline takes its fetcher, browser and LLM as constructor arguments, so
metering is a decoration problem rather than a surgery problem: wrap each of
the three, pass the wrappers in, read the counters afterwards. The library
stays unaware that it is being billed for, which is how it should be.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..fetch import BrowserFetcher, StaticFetcher
from ..pipeline import Scrapewright


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
        close = getattr(self.inner, "close", None)
        if close:
            close()


@dataclass
class _CountingLlm:
    inner: object
    meter: Meter

    def synthesize(self, html: str, url: str, schema=None):
        self.meter.syntheses += 1
        # Older/injected extractors may not take a schema argument.
        try:
            return self.inner.synthesize(html, url, schema)
        except TypeError:
            return self.inner.synthesize(html, url)


def metered_scrapewright(*, js: bool = False, meter: Meter | None = None,
                         **kwargs) -> tuple[Scrapewright, Meter]:
    """Build a pipeline whose consumption is counted.

    The browser is only constructed when ``js`` is requested, so a static job
    never pays for Chromium — the wrapper preserves that laziness by wrapping
    the browser object rather than forcing one into existence.
    """
    meter = meter or Meter()
    fetcher = _CountingFetcher(StaticFetcher(), meter, "pages")
    browser = _CountingFetcher(BrowserFetcher(), meter, "renders") if js else None

    sw = Scrapewright(fetcher=fetcher, browser=browser, js=js, **kwargs)
    sw.llm = _CountingLlm(sw.llm, meter)
    return sw, meter
