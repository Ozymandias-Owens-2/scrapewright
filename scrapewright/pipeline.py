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
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from .cache import RecipeCache
from .crawl import Frontier
from .detect import detect
from .extract.jsonld import JsonLdExtractor
from .extract.llm import LlmExtractor
from .extract.selectors import SelectorExtractor
from .extract.shopify import ShopifyExtractor
from .extract.woocommerce import WooCommerceExtractor
from .fetch import BrowserFetcher, StaticFetcher, looks_js_shelled
from .like import find_similar, url_template
from .mend import Mender
from .models import Product, Record
from .schema import PRODUCT_SCHEMA, Schema
from .validate import Coverage, coverage

DEFAULT_ACCEPT_RATIO = 0.5
DEFAULT_MAX_SYNTH_PER_RUN = 3


def _rows_cache_name(schema: Schema) -> str:
    """A rows recipe and a page recipe for the same fields are different
    artifacts -- one names a container, the other does not -- so they must not
    share a cache entry and overwrite each other."""
    return f"{schema.name}+rows"


# How many pages "find every one of these" means when the caller named no
# number. Generous, but not a whole-site crawl nobody asked to pay for.
DEFAULT_LIKE_CAP = 200
# Room to keep discovering past the cap, so the queue does not starve on a
# page whose neighbours are mostly ones we have already seen.
LIKE_FRONTIER_SLACK = 3


class _RecordingFetcher:
    """Passes fetches through and keeps the last body.

    The by-example walk needs each page's links, and the extractor has just
    fetched that page. Asking for it again would double the caller's bill for
    nothing.
    """

    def __init__(self, inner):
        self.inner = inner
        self.last_html: str | None = None

    def fetch(self, url: str) -> str | None:
        html = self.inner.fetch(url)
        self.last_html = html
        return html

    def __getattr__(self, name):        # anything else belongs to the real one
        return getattr(self.inner, name)


def _matching_links(html: str, base_url: str, shape) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        candidate = urljoin(base_url, a["href"].split("#")[0])
        if candidate not in seen and shape.matches(candidate):
            seen.add(candidate)
            out.append(candidate)
    return out


def _guess_next_page(url: str, page_number: int) -> str:
    """``?page=N`` when the markup offered no next link.

    Plenty of listings paginate with a script, so the static HTML carries no
    next link at all -- autoscout24 is one. The guess is safe because the
    caller stops as soon as a page yields no row it has not already seen, so
    a site that ignores the parameter and re-serves page one ends the walk
    instead of looping.
    """
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != "page"]
    query.append(("page", str(page_number)))
    return urlunsplit(parts._replace(query=urlencode(query)))


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
                 max_synth_per_run: int = DEFAULT_MAX_SYNTH_PER_RUN,
                 max_scrolls: int = 0):
        self.cache = cache or RecipeCache()
        self.llm = llm or LlmExtractor()
        self.session = session
        self.fetcher = fetcher or StaticFetcher(session)
        # `browser` may be injected (tests) or created lazily when js=True.
        self._browser = browser
        self._js_enabled = js or browser is not None
        self.accept_ratio = accept_ratio
        self.max_synth_per_run = max_synth_per_run
        # Listings that load more as you scroll look twenty items long to a
        # single render. Only meaningful with a browser, hence 0 by default.
        self.max_scrolls = max_scrolls
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
        # The static hop may have just written a recipe. Read it back before
        # rendering, or the browser pass starts from nothing and pays the model
        # a second time for the page we already bought -- twice the cost, to us
        # and to whoever is being billed. Selectors learned from static HTML
        # very often still match once the page has rendered.
        recipe = self.cache.get(url, schema.name) or recipe
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

    # ── Rows mode: one listing page, many records ────────────────────────────
    def extract_rows(self, url: str, schema: Schema = PRODUCT_SCHEMA, *,
                     allow_llm: bool = True) -> list[Record]:
        """Every item on one listing page, as one record each.

        ``extract`` answers "what is this page about", which on a search
        result is the first card. This answers "what is on this page", which
        is what anyone pointing at a category actually wanted.
        """
        html, js_used = self._listing_html(url, _rows_cache_name(schema))
        if html is None:
            return []
        return self._rows_from_html(html, url, schema,
                                    allow_llm=allow_llm, js_used=js_used)

    def _listing_html(self, url: str, cache_name: str) -> tuple[str | None, bool]:
        """Fetch a listing once, escalating to the browser when it is a shell."""
        recipe = self.cache.get(url, cache_name)
        html = None
        if recipe is not None and recipe.needs_js and self._can_js():
            html = self._browser_fetch(url)
        if html is None:
            html = self.fetcher.fetch(url)
        if html is None or (self._can_js() and looks_js_shelled(html)):
            rendered = self._browser_fetch(url) if self._can_js() else None
            if rendered is not None:
                return rendered, True
        return html, False

    def _rows_from_html(self, html: str, url: str, schema: Schema, *,
                        allow_llm: bool, js_used: bool) -> list[Record]:
        cache_name = _rows_cache_name(schema)
        recipe = self.cache.get(url, cache_name)

        if recipe is not None:
            rows = SelectorExtractor(recipe, schema).extract_rows(html, url)
            if rows:
                return rows
            # A recipe that matches nothing is stale, not authoritative.

        if not allow_llm:
            return []
        self._synth_calls += 1
        recipe = self.llm.synthesize(html, url, schema, rows=True)
        if recipe is None or not recipe.item:
            return []
        recipe.needs_js = js_used
        rows = SelectorExtractor(recipe, schema).extract_rows(html, url)
        if rows:
            # Only a recipe that produced something is worth replaying.
            self.cache.put(url, recipe, cache_name)
        return rows

    def crawl_rows(self, listing_url: str, schema: Schema = PRODUCT_SCHEMA, *,
                   max_items: int | None = None, allow_llm: bool = True,
                   max_listing_pages: int = 5) -> Iterator[Record]:
        """Rows from a listing, following its pagination.

        The first page pays for a recipe; every page after it replays that
        recipe for nothing, which is the same bargain as the rest of the tool.
        """
        self._synth_calls = 0
        frontier = Frontier(fetcher=self.fetcher,
                            js_fetcher=self._get_browser() if self._can_js() else None,
                            max_listing_pages=max_listing_pages)
        cache_name = _rows_cache_name(schema)
        visited: set[str] = set()
        fingerprints: set[str] = set()
        count = 0
        url: str | None = listing_url

        for page_number in range(2, max_listing_pages + 2):
            if url is None or url in visited:
                return
            if max_items is not None and count >= max_items:
                return       # never fetch a page whose rows cannot be used
            visited.add(url)
            # One fetch serves both the rows and the hunt for the next page.
            # Fetching twice billed the caller for a page they never saw.
            html, js_used = self._listing_html(url, cache_name)
            if html is None:
                return

            can_llm = allow_llm and self._synth_calls < self.max_synth_per_run
            fresh = 0
            for record in self._rows_from_html(html, url, schema,
                                               allow_llm=can_llm, js_used=js_used):
                mark = repr(sorted((k, str(v)) for k, v in record.data.items()))
                if mark in fingerprints:
                    continue          # the site handed back a page we have read
                fingerprints.add(mark)
                if max_items is not None and count >= max_items:
                    return
                yield record
                count += 1
                fresh += 1

            # Nothing new on this page means the end, however the site chose to
            # signal it -- an empty page, or page 9 quietly serving page 1.
            if not fresh:
                return
            url = (frontier._next_page(BeautifulSoup(html, "html.parser"), url)
                   or _guess_next_page(url, page_number))

    # ── By example: one card in, all its siblings out ────────────────────────
    def crawl_like(self, example_url: str, schema: Schema = PRODUCT_SCHEMA, *,
                   max_items: int | None = None, allow_llm: bool = True,
                   include_example: bool = True) -> Iterator[Record]:
        """Every page shaped like this one.

        The example does double duty: it says which pages the caller wants,
        and it is the page the recipe is compiled from -- an item page, which
        is exactly the page a recipe should be learned on. Sibling pages then
        replay it for nothing.
        """
        self._synth_calls = 0
        shape = url_template(example_url)
        cap = max_items if max_items is not None else DEFAULT_LIKE_CAP

        # Every page we extract has already been fetched; reading its links on
        # the way past costs nothing and is what lets a site with no sitemap
        # be walked at all -- item pages link to their neighbours ("related",
        # "next"), so the set grows as it is consumed.
        recorder = _RecordingFetcher(self.fetcher)
        self.fetcher, original = recorder, self.fetcher
        try:
            seen: set[str] = {example_url}
            count = 0

            # The example is read first, and its HTML then serves discovery
            # too -- fetching it once for the record and again to look at its
            # links would bill the caller twice for one page.
            mender = self._mender(example_url, schema, allow_llm)
            recorder.last_html = None
            if include_example:
                record = self.extract(example_url, schema, allow_llm=allow_llm)
                if record is not None and record.data:
                    # Through the mender like every other record: it is the
                    # page that shows which fields this site fills at all, and
                    # routing it around meant nothing was ever "missing".
                    ready = (mender.offer(record, recorder.last_html, example_url)
                             if mender else record)
                    if ready is not None:
                        yield ready
                        count += 1
            example_html = recorder.last_html

            queue: list[str] = []
            for url in find_similar(example_url, limit=cap, session=self.session,
                                    fetcher=original, example_html=example_html):
                if url not in seen:
                    seen.add(url)
                    queue.append(url)

            while queue and count < cap:
                url = queue.pop(0)
                can_llm = allow_llm and self._synth_calls < self.max_synth_per_run
                recorder.last_html = None
                record = self.extract(url, schema, allow_llm=can_llm)
                page_html = recorder.last_html
                if record is not None and record.data:
                    # A record missing a field that other pages had is held
                    # back rather than shipped with a hole in it.
                    ready = mender.offer(record, page_html, url) if mender else record
                    if ready is not None:
                        yield ready
                        count += 1
                if mender is not None and mender.ready:
                    for mended in mender.mend():
                        if count >= cap:
                            break
                        yield mended
                        count += 1
                if page_html and len(seen) < cap * LIKE_FRONTIER_SLACK:
                    for neighbour in _matching_links(page_html, url, shape):
                        if neighbour not in seen:
                            seen.add(neighbour)
                            queue.append(neighbour)

            if mender is not None:
                for leftover in mender.drain():
                    if count >= cap:
                        break
                    yield leftover
                    count += 1
        finally:
            self.fetcher = original

    # ── internals ────────────────────────────────────────────────────────────
    def _mender(self, site_url: str, schema: Schema,
                allow_llm: bool) -> Mender | None:
        """Watches for a field the recipe keeps missing. Needs a model: the
        repair is asking where else that field lives. The cache is keyed by
        domain, so any URL on the site names the same recipe."""
        if not allow_llm:
            return None

        def synthesize(html: str, url: str, narrowed: Schema):
            self._synth_calls += 1
            return self.llm.synthesize(html, url, narrowed)

        def reread(recipe, html: str, url: str) -> Record | None:
            if recipe is None:
                return None
            return SelectorExtractor(recipe, schema).extract_record(html, url)

        return Mender(schema=schema, synthesize=synthesize, reread=reread,
                      save=lambda recipe: self.cache.put(site_url, recipe, schema.name),
                      recipe_of=lambda: self.cache.get(site_url, schema.name))

    def _can_js(self) -> bool:
        return self._js_enabled

    def _get_browser(self):
        if self._browser is None:
            self._browser = BrowserFetcher(max_scrolls=self.max_scrolls)
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
