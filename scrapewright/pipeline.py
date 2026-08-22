"""The orchestrator: fetch → detect → extract → validate → cache → heal.

Three entry points:

* :meth:`Scrapewright.scrape_catalog` — platforms with a product list API
  (Shopify, WooCommerce). Deterministic, free.
* :meth:`Scrapewright.scrape_page` — one product page. Order: cached recipe →
  JSON-LD → LLM synthesis. **Self-healing:** a cached recipe that stops
  producing usable products falls through to the free paths and, failing those,
  is replaced by a fresh synthesis. **Browser escalation:** when the static
  fetch is a client-side shell (or extraction fails on it) and JS mode is on,
  the page is re-fetched in a real browser and the chain runs again; a recipe
  learned that way is tagged ``needs_js`` so later runs skip straight to the
  browser.
* :meth:`Scrapewright.crawl` — a listing URL on ANY site. Known platforms route
  to catalog mode; custom sites go through the
  :class:`~scrapewright.crawl.Frontier` and page mode.

Expensive things stay rare by construction: the LLM runs once per site (capped
at ``max_synth_per_run`` per run), and the browser starts at most once per run
and only for sites that actually need it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

import requests

from .cache import RecipeCache
from .crawl import Frontier
from .detect import detect
from .extract.jsonld import JsonLdExtractor
from .extract.llm import LlmExtractor
from .extract.selectors import SelectorExtractor
from .extract.shopify import ShopifyExtractor
from .extract.woocommerce import WooCommerceExtractor
from .fetch import BrowserFetcher, StaticFetcher, looks_js_shelled
from .models import Product
from .validate import Coverage, coverage

DEFAULT_ACCEPT_RATIO = 0.5
DEFAULT_MAX_SYNTH_PER_RUN = 3


class Scrapewright:
    def __init__(self, cache: RecipeCache | None = None,
                 llm: LlmExtractor | None = None,
                 session: requests.Session | None = None,
                 fetcher=None,
                 js: bool = False,
                 browser=None,
                 accept_ratio: float = DEFAULT_ACCEPT_RATIO,
                 max_synth_per_run: int = DEFAULT_MAX_SYNTH_PER_RUN):
        self.cache = cache or RecipeCache()
        self.llm = llm or LlmExtractor()
        self.session = session
        self.fetcher = fetcher or StaticFetcher(session)
        # `browser` may be injected (tests) or created lazily when js=True.
        self._browser = browser
        self._js_enabled = js or browser is not None
        self.accept_ratio = accept_ratio
        # Hard cap on LLM calls within one batch/crawl run — the model bill is
        # bounded even if a site resists synthesis on every page.
        self.max_synth_per_run = max_synth_per_run
        self._synth_calls = 0

    # ── Catalog mode ─────────────────────────────────────────────────────────
    def scrape_catalog(self, url: str, max_items: int | None = None) -> Iterator[Product]:
        det = detect(url, session=self.session)
        if det.kind == "shopify":
            extractor: object = ShopifyExtractor(det.catalog_endpoint, session=self.session)
        elif det.kind == "woocommerce":
            extractor = WooCommerceExtractor(det.catalog_endpoint, session=self.session)
        else:
            raise ValueError(
                f"{det.base} has no known catalog API ({det.kind}). "
                f"Use crawl(listing_url) or scrape_page(product_url) for custom sites."
            )

        for i, product in enumerate(extractor.iter_catalog()):
            if max_items is not None and i >= max_items:
                return
            yield product

    # ── Page mode (self-healing + browser escalation) ────────────────────────
    def scrape_page(self, url: str, *, allow_llm: bool = True) -> Product | None:
        recipe = self.cache.get(url)

        # A recipe learned from rendered HTML tells us to skip the static hop.
        if recipe is not None and recipe.needs_js and self._can_js():
            html = self._browser_fetch(url)
            if html is not None:
                return self._extract_chain(html, url, recipe, allow_llm, js_used=True)

        html = self.fetcher.fetch(url)

        # An empty client-side shell can't be extracted from and isn't worth an
        # LLM call — go straight to the browser when one is available.
        if html is not None and not (self._can_js() and looks_js_shelled(html)):
            product = self._extract_chain(html, url, recipe, allow_llm, js_used=False)
            if product is not None and product.is_usable():
                return product
        else:
            product = None

        if not self._can_js():
            return product

        rendered = self._browser_fetch(url)
        if rendered is None:
            return product
        return self._extract_chain(rendered, url, recipe, allow_llm, js_used=True) or product

    def _extract_chain(self, html: str, url: str, recipe, allow_llm: bool,
                       js_used: bool) -> Product | None:
        """cached recipe → JSON-LD → LLM synthesis, against one HTML document."""
        if recipe is not None:
            product = SelectorExtractor(recipe).extract_page(html, url)
            if product is not None and product.is_usable():
                if js_used and not recipe.needs_js:
                    # The recipe only works on rendered HTML — remember that.
                    recipe.needs_js = True
                    self.cache.put(url, recipe)
                return product
            # Stale/weak recipe — the site probably changed. Heal below.

        jsonld = JsonLdExtractor().extract_page(html, url)
        if jsonld is not None and jsonld.is_usable():
            return jsonld

        if not allow_llm:
            return jsonld  # best effort (may be None or partial)

        new_recipe = self._synthesize(html, url)
        if new_recipe is None:
            return jsonld
        new_recipe.needs_js = js_used
        self.cache.put(url, new_recipe)
        return SelectorExtractor(new_recipe).extract_page(html, url) or jsonld

    # ── Batch mode (budgeted healing) ────────────────────────────────────────
    def scrape_pages(self, urls: Iterable[str], *, allow_llm: bool = True) -> list[Product]:
        """Extract many pages. LLM synthesis (first-time or healing) is capped
        at ``max_synth_per_run`` calls for the whole batch."""
        self._synth_calls = 0
        products: list[Product] = []
        for url in urls:
            can_llm = allow_llm and self._synth_calls < self.max_synth_per_run
            p = self.scrape_page(url, allow_llm=can_llm)
            if p is not None:
                products.append(p)
        return products

    # ── Crawl mode ───────────────────────────────────────────────────────────
    def crawl(self, listing_url: str, *, max_items: int | None = None,
              allow_llm: bool = True,
              max_listing_pages: int = 5) -> Iterator[Product]:
        """Walk a whole store from one listing URL.

        Known platforms short-circuit to catalog mode; custom sites are
        discovered via the frontier, and the first product page pays the single
        synthesis cost while every subsequent page replays the cached recipe.
        """
        det = detect(listing_url, session=self.session)
        if det.kind in ("shopify", "woocommerce"):
            yield from self.scrape_catalog(listing_url, max_items=max_items)
            return

        self._synth_calls = 0
        frontier = Frontier(fetcher=self.fetcher,
                            js_fetcher=self._get_browser() if self._can_js() else None,
                            max_listing_pages=max_listing_pages)
        count = 0
        for url in frontier.discover(listing_url):
            if max_items is not None and count >= max_items:
                return
            can_llm = allow_llm and self._synth_calls < self.max_synth_per_run
            product = self.scrape_page(url, allow_llm=can_llm)
            if product is not None:
                yield product
                count += 1

    # ── internals ────────────────────────────────────────────────────────────
    def _can_js(self) -> bool:
        return self._js_enabled

    def _get_browser(self):
        if self._browser is None:
            self._browser = BrowserFetcher()
        return self._browser

    def _browser_fetch(self, url: str) -> str | None:
        return self._get_browser().fetch(url)

    def _synthesize(self, html: str, url: str):
        self._synth_calls += 1
        return self.llm.synthesize(html, url)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def close(self) -> None:
        """Shut down the browser, if one was started."""
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def __enter__(self) -> Scrapewright:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def check(products: list[Product]) -> Coverage:
    """Convenience re-export so callers can score a batch without importing
    :mod:`scrapewright.validate` directly."""
    return coverage(products)
