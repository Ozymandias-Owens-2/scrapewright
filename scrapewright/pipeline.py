"""The orchestrator: fetch → detect → extract → validate → cache → heal.

Entry points:

* :meth:`Scrapewright.extract` — the general one. Pull any declared
  :class:`~scrapewright.schema.Schema` off any page, returning a
  :class:`~scrapewright.models.Record`.
* :meth:`Scrapewright.scrape_page` — the typed product path (a thin wrapper
  over ``extract`` that returns a :class:`~scrapewright.models.Product`).
* :meth:`Scrapewright.scrape_catalog` — platforms with a product list API
  (Shopify, WooCommerce). Deterministic, free.
* :meth:`Scrapewright.crawl` / :meth:`Scrapewright.crawl_records` — a listing
  URL on ANY site.

Three policies keep the expensive things rare:

**Self-healing** — a cached recipe that stops producing usable records falls
through to the free paths and, failing those, is replaced by a fresh synthesis.

**Browser escalation** — when the static fetch is a client-side shell (or
extraction fails on it) and JS mode is on, the page is re-fetched in a real
browser and the chain runs again; a recipe learned that way is tagged
``needs_js`` so later runs skip straight to the browser.

**Bounded spend** — the LLM runs once per site, capped at
``max_synth_per_run`` per run; the browser starts at most once per run.
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
from .models import Product, Record
from .schema import PRODUCT_SCHEMA, Schema
from .validate import Coverage, coverage

DEFAULT_ACCEPT_RATIO = 0.5
DEFAULT_MAX_SYNTH_PER_RUN = 3


def _record_from_product(product: Product, schema_name: str = "product") -> Record:
    """Adapt a typed product (JSON-LD / platform APIs) into a generic record."""
    data = {
        "title": product.title,
        "price": product.price,
        "brand": product.brand,
        "images": product.images,
        "description": product.description,
        "sku": product.sku,
    }
    return Record(url=product.url, schema_name=schema_name,
                  data={k: v for k, v in data.items() if v},
                  source_platform=product.source_platform)


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

    # ── Generic extraction (self-healing + browser escalation) ───────────────
    def extract(self, url: str, schema: Schema = PRODUCT_SCHEMA, *,
                allow_llm: bool = True) -> Record | None:
        """Pull ``schema``'s fields off one page."""
        recipe = self.cache.get(url, schema.name)

        # A recipe learned from rendered HTML tells us to skip the static hop.
        if recipe is not None and recipe.needs_js and self._can_js():
            html = self._browser_fetch(url)
            if html is not None:
                return self._extract_chain(html, url, schema, recipe, allow_llm, True)

        html = self.fetcher.fetch(url)

        # An empty client-side shell can't be extracted from and isn't worth an
        # LLM call — go straight to the browser when one is available.
        record = None
        if html is not None and not (self._can_js() and looks_js_shelled(html)):
            record = self._extract_chain(html, url, schema, recipe, allow_llm, False)
            if record is not None and schema.is_satisfied_by(record.data):
                return record

        if not self._can_js():
            return record

        rendered = self._browser_fetch(url)
        if rendered is None:
            return record
        return self._extract_chain(rendered, url, schema, recipe, allow_llm, True) or record

    def _extract_chain(self, html: str, url: str, schema: Schema, recipe,
                       allow_llm: bool, js_used: bool) -> Record | None:
        """cached recipe → JSON-LD → LLM synthesis, against one HTML document."""
        if recipe is not None:
            record = SelectorExtractor(recipe, schema).extract_record(html, url)
            if record is not None and schema.is_satisfied_by(record.data):
                if js_used and not recipe.needs_js:
                    # The recipe only works on rendered HTML — remember that.
                    recipe.needs_js = True
                    self.cache.put(url, recipe, schema.name)
                return record
            # Stale/weak recipe — the site probably changed. Heal below.

        # schema.org markup describes products; it has nothing to say about a
        # caller-defined schema, so this free hop is product-only.
        jsonld = None
        if schema.name == PRODUCT_SCHEMA.name:
            product = JsonLdExtractor().extract_page(html, url)
            if product is not None:
                jsonld = _record_from_product(product, schema.name)
                if schema.is_satisfied_by(jsonld.data):
                    return jsonld

        if not allow_llm:
            return jsonld  # best effort (may be None or partial)

        new_recipe = self._synthesize(html, url, schema)
        if new_recipe is None:
            return jsonld
        new_recipe.needs_js = js_used
        self.cache.put(url, new_recipe, schema.name)
        fresh = SelectorExtractor(new_recipe, schema).extract_record(html, url)
        return fresh or jsonld

    # ── Typed product path ───────────────────────────────────────────────────
    def scrape_page(self, url: str, *, allow_llm: bool = True) -> Product | None:
        record = self.extract(url, PRODUCT_SCHEMA, allow_llm=allow_llm)
        if record is None or not record.data.get("title"):
            return None
        return record.to_product()

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
    def crawl_records(self, listing_url: str, schema: Schema = PRODUCT_SCHEMA, *,
                      max_items: int | None = None, allow_llm: bool = True,
                      max_listing_pages: int = 5) -> Iterator[Record]:
        """Walk a whole site from one listing URL, pulling ``schema`` per page."""
        if schema.name == PRODUCT_SCHEMA.name:
            det = detect(listing_url, session=self.session)
            if det.kind in ("shopify", "woocommerce"):
                for product in self.scrape_catalog(listing_url, max_items=max_items):
                    yield _record_from_product(product, schema.name)
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
            record = self.extract(url, schema, allow_llm=can_llm)
            if record is not None and record.data:
                yield record
                count += 1

    def crawl(self, listing_url: str, *, max_items: int | None = None,
              allow_llm: bool = True,
              max_listing_pages: int = 5) -> Iterator[Product]:
        """Product-schema crawl, yielding typed products."""
        for record in self.crawl_records(listing_url, PRODUCT_SCHEMA,
                                         max_items=max_items, allow_llm=allow_llm,
                                         max_listing_pages=max_listing_pages):
            if record.data.get("title"):
                yield record.to_product()

    # ── internals ────────────────────────────────────────────────────────────
    def _can_js(self) -> bool:
        return self._js_enabled

    def _get_browser(self):
        if self._browser is None:
            self._browser = BrowserFetcher()
        return self._browser

    def _browser_fetch(self, url: str) -> str | None:
        return self._get_browser().fetch(url)

    def _synthesize(self, html: str, url: str, schema: Schema = PRODUCT_SCHEMA):
        self._synth_calls += 1
        return self.llm.synthesize(html, url, schema)

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
