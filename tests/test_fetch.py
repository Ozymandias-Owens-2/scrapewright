"""Browser escalation: static first, render only when static demonstrably fails.

No Playwright needed — the browser fetcher is a stand-in that counts calls, so
these assertions are about the *policy* (when do we pay for a render?), which is
the part worth pinning down.
"""

from scrapewright.cache import RecipeCache
from scrapewright.crawl import Frontier
from scrapewright.extract.base import SelectorRecipe
from scrapewright.fetch import looks_js_shelled, visible_text_length
from scrapewright.pipeline import Scrapewright
from tests.test_pipeline import GENERIC_RECIPE, _FakeLLM


class _CountingFetcher:
    """Serves one HTML body (or a URL→HTML map) and counts fetches."""

    def __init__(self, html=None, mapping=None):
        self.html = html
        self.mapping = mapping or {}
        self.calls = 0

    def fetch(self, url):
        self.calls += 1
        if self.mapping:
            return self.mapping.get(url) or self.mapping.get(url.split("?")[0])
        return self.html

    def close(self):
        pass


PAGE_URL = "https://atelier.example.com/products/coat"


# ── the shell heuristic ──────────────────────────────────────────────────────
def test_looks_js_shelled(fixture):
    assert looks_js_shelled(fixture("js_shell.html"))
    assert not looks_js_shelled(fixture("generic_product.html"))
    assert not looks_js_shelled(fixture("jsonld_product.html"))
    assert looks_js_shelled("")


def test_visible_text_length_ignores_scripts(fixture):
    # The shell's bundle reference and markup must not count as visible text.
    assert visible_text_length(fixture("js_shell.html")) < 100


# ── escalation policy ────────────────────────────────────────────────────────
def test_static_success_never_starts_the_browser(tmp_path, fixture):
    static = _CountingFetcher(fixture("generic_product.html"))
    browser = _CountingFetcher(fixture("generic_product.html"))
    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm,
                      fetcher=static, browser=browser)

    product = sw.scrape_page(PAGE_URL)
    assert product is not None and product.is_usable()
    assert static.calls == 1
    assert browser.calls == 0            # rendering is never paid for needlessly


def test_js_shell_escalates_and_tags_the_recipe(tmp_path, fixture):
    cache = RecipeCache(tmp_path / "r.json")
    static = _CountingFetcher(fixture("js_shell.html"))       # empty client-side shell
    browser = _CountingFetcher(fixture("generic_product.html"))  # rendered DOM
    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(cache=cache, llm=llm, fetcher=static, browser=browser)

    product = sw.scrape_page(PAGE_URL)
    assert product is not None and product.title == "Ricki Leather Boots"
    assert browser.calls == 1
    # The shell was never handed to the model — one synthesis, on real HTML.
    assert llm.calls == 1
    assert cache.get(PAGE_URL).needs_js is True


def test_needs_js_recipe_skips_the_static_hop(tmp_path, fixture):
    cache = RecipeCache(tmp_path / "r.json")
    js_recipe = GENERIC_RECIPE.model_copy(update={"needs_js": True})
    cache.put(PAGE_URL, js_recipe)

    static = _CountingFetcher(fixture("js_shell.html"))
    browser = _CountingFetcher(fixture("generic_product.html"))
    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(cache=cache, llm=llm, fetcher=static, browser=browser)

    product = sw.scrape_page(PAGE_URL)
    assert product is not None and product.is_usable()
    assert static.calls == 0             # we already know this site needs a render
    assert browser.calls == 1
    assert llm.calls == 0


def test_static_recipe_promoted_to_needs_js_when_site_goes_client_side(tmp_path, fixture):
    """The site switched to client-side rendering; the recipe still matches the
    rendered DOM, so it is promoted rather than re-synthesized."""
    cache = RecipeCache(tmp_path / "r.json")
    cache.put(PAGE_URL, GENERIC_RECIPE)              # needs_js defaults to False

    static = _CountingFetcher(fixture("js_shell.html"))
    browser = _CountingFetcher(fixture("generic_product.html"))
    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(cache=cache, llm=llm, fetcher=static, browser=browser)

    product = sw.scrape_page(PAGE_URL)
    assert product is not None and product.is_usable()
    assert llm.calls == 0                            # no synthesis needed
    assert cache.get(PAGE_URL).needs_js is True


def test_no_browser_configured_returns_best_effort(tmp_path, fixture):
    static = _CountingFetcher(fixture("js_shell.html"))
    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm, fetcher=static)

    assert sw.scrape_page(PAGE_URL) is None          # honest miss, no crash


# ── frontier escalation ──────────────────────────────────────────────────────
BASE = "https://boutique.example.com"


def test_frontier_escalates_a_shelled_listing(fixture):
    static = _CountingFetcher(fixture("js_shell.html"))
    browser = _CountingFetcher(mapping={f"{BASE}/collection": fixture("listing_page1.html")})
    urls = list(Frontier(fetcher=static, js_fetcher=browser,
                         max_listing_pages=1).discover(f"{BASE}/collection"))
    assert f"{BASE}/products/alpha-coat" in urls
    assert browser.calls == 1


def test_frontier_escalates_when_listing_has_no_product_links(fixture):
    # Renders fine, but the grid is client-side: real text, zero product links.
    static = _CountingFetcher(fixture("jsonld_product.html"))
    browser = _CountingFetcher(mapping={f"{BASE}/collection": fixture("listing_page1.html")})
    urls = list(Frontier(fetcher=static, js_fetcher=browser,
                         max_listing_pages=1).discover(f"{BASE}/collection"))
    assert len(urls) == 3
    assert browser.calls == 1


def test_frontier_does_not_escalate_when_static_works(fixture):
    static = _CountingFetcher(mapping={f"{BASE}/collection": fixture("listing_page1.html")})
    browser = _CountingFetcher(fixture("listing_page1.html"))
    list(Frontier(fetcher=static, js_fetcher=browser,
                  max_listing_pages=1).discover(f"{BASE}/collection"))
    assert browser.calls == 0
