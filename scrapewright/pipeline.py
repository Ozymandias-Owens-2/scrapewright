"""The orchestrator: detect → extract → validate → cache → heal.

Three entry points:

* :meth:`Scrapewright.scrape_catalog` — platforms with a product list API
  (Shopify, WooCommerce). Deterministic, free.
* :meth:`Scrapewright.scrape_page` — one custom-HTML product page. Order:
  cached recipe → JSON-LD → LLM synthesis. **Self-healing:** if a cached recipe
  stops producing usable products (the site changed its DOM), the page falls
  through to the free JSON-LD path and, failing that, a fresh synthesis replaces
  the stale recipe — the site heals on the next run instead of silently
  returning empty fields.
* :meth:`Scrapewright.crawl` — a listing/category URL on ANY site. Known
  platforms route to catalog mode; custom sites go through the
  :class:`~scrapewright.crawl.Frontier` and page mode. At most
  ``max_synth_per_run`` LLM calls per run, so a crawl's model spend is bounded
  no matter how many pages it visits.
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
from .http import get, make_session
from .models import Product
from .validate import Coverage, coverage

DEFAULT_ACCEPT_RATIO = 0.5
DEFAULT_MAX_SYNTH_PER_RUN = 3


class Scrapewright:
    def __init__(self, cache: RecipeCache | None = None,
                 llm: LlmExtractor | None = None,
                 session: requests.Session | None = None,
                 accept_ratio: float = DEFAULT_ACCEPT_RATIO,
                 max_synth_per_run: int = DEFAULT_MAX_SYNTH_PER_RUN):
        self.cache = cache or RecipeCache()
        self.llm = llm or LlmExtractor()
        self.session = session or make_session()
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

    # ── Page mode (self-healing) ─────────────────────────────────────────────
    def scrape_page(self, url: str, *, allow_llm: bool = True) -> Product | None:
        """Extract one product page: cached recipe → JSON-LD → LLM synthesis.

        A cached recipe whose output is no longer usable does NOT end the run —
        it falls through to the free paths and, if allowed, a re-synthesis that
        overwrites the stale recipe. That is the self-healing loop.
        """
        html: str | None = None

        recipe = self.cache.get(url)
        if recipe is not None:
            html = self._fetch(url)
            product = SelectorExtractor(recipe).extract_page(html, url)
            if product is not None and product.is_usable():
                return product
            # Stale/weak recipe — the site probably changed. Heal below.

        if html is None:
            html = self._fetch(url)

        jsonld = JsonLdExtractor().extract_page(html, url)
        if jsonld is not None and jsonld.is_usable():
            return jsonld

        if not allow_llm:
            return jsonld  # best effort (may be None or partial)

        new_recipe = self._synthesize(html, url)
        if new_recipe is None:
            return jsonld
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
        frontier = Frontier(session=self.session, max_listing_pages=max_listing_pages)
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
    def _synthesize(self, html: str, url: str):
        self._synth_calls += 1
        return self.llm.synthesize(html, url)

    def _fetch(self, url: str) -> str:
        r = get(url, session=self.session)
        r.raise_for_status()
        return r.text


def check(products: list[Product]) -> Coverage:
    """Convenience re-export so callers can score a batch without importing
    :mod:`scrapewright.validate` directly."""
    return coverage(products)
