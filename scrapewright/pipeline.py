"""The orchestrator: detect → extract → validate → cache.

Two entry points, matching the two extractor kinds:

* :meth:`Scrapewright.scrape_catalog` — for platforms that expose a product
  list API (Shopify, WooCommerce). Iterates the whole catalog deterministically.
* :meth:`Scrapewright.scrape_page` — for a single custom-HTML product page.
  Tries the free JSON-LD path first, falls back to a cached recipe, and only
  synthesizes a new recipe via the LLM when neither exists.
"""

from __future__ import annotations

from collections.abc import Iterator

import requests

from .cache import RecipeCache
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


class Scrapewright:
    def __init__(self, cache: RecipeCache | None = None,
                 llm: LlmExtractor | None = None,
                 session: requests.Session | None = None,
                 accept_ratio: float = DEFAULT_ACCEPT_RATIO):
        self.cache = cache or RecipeCache()
        self.llm = llm or LlmExtractor()
        self.session = session or make_session()
        self.accept_ratio = accept_ratio

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
                f"Use scrape_page(product_url) for custom-HTML sites."
            )

        for i, product in enumerate(extractor.iter_catalog()):
            if max_items is not None and i >= max_items:
                return
            yield product

    # ── Page mode ────────────────────────────────────────────────────────────
    def scrape_page(self, url: str, *, allow_llm: bool = True) -> Product | None:
        """Extract one product page. Order: cached recipe → JSON-LD → LLM synth."""
        recipe = self.cache.get(url)
        if recipe is not None:
            html = self._fetch(url)
            return SelectorExtractor(recipe).extract_page(html, url)

        html = self._fetch(url)

        jsonld = JsonLdExtractor().extract_page(html, url)
        if jsonld is not None and jsonld.is_usable():
            return jsonld

        if not allow_llm:
            return jsonld  # best effort (may be None or partial)

        recipe = self.llm.synthesize(html, url)
        if recipe is None:
            return jsonld
        self.cache.put(url, recipe)
        return SelectorExtractor(recipe).extract_page(html, url)

    def _fetch(self, url: str) -> str:
        r = get(url, session=self.session)
        r.raise_for_status()
        return r.text


def check(products: list[Product]) -> Coverage:
    """Convenience re-export so callers can score a batch without importing
    :mod:`scrapewright.validate` directly."""
    return coverage(products)
