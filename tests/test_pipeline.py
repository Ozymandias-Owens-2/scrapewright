import pytest

from scrapewright.cache import RecipeCache
from scrapewright.extract.base import SelectorRecipe
from scrapewright.pipeline import Scrapewright


class _FakeResponse:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeSession:
    """Returns the same HTML for any GET — enough for page-mode tests."""

    def __init__(self, html):
        self.html = html

    def get(self, url, **kw):
        return _FakeResponse(self.html)


class _FakeLLM:
    def __init__(self, recipe):
        self.recipe = recipe
        self.calls = 0

    def synthesize(self, html, url, schema=None):
        self.calls += 1
        return self.recipe


GENERIC_RECIPE = SelectorRecipe(
    title=".product__title",
    price=".price",
    brand=".brand",
    images=".gallery__img",
    modes={"images": "attr:src"},
)


def test_page_mode_synthesizes_then_caches(tmp_path, fixture):
    html = fixture("generic_product.html")
    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(
        cache=RecipeCache(tmp_path / "r.json"),
        llm=llm,
        session=_FakeSession(html),
    )
    url = "https://boutique.example.com/products/ricki"

    first = sw.scrape_page(url)
    assert first is not None and first.title == "Ricki Leather Boots"
    assert llm.calls == 1                      # synthesized once

    second = sw.scrape_page(url)
    assert second is not None and second.title == "Ricki Leather Boots"
    assert llm.calls == 1                      # replayed from cache, no new call


def test_page_mode_prefers_jsonld_over_llm(tmp_path, fixture):
    html = fixture("jsonld_product.html")
    llm = _FakeLLM(GENERIC_RECIPE)             # would be wrong for this page
    sw = Scrapewright(
        cache=RecipeCache(tmp_path / "r.json"),
        llm=llm,
        session=_FakeSession(html),
    )
    p = sw.scrape_page("https://maison.example.com/overcoat")
    assert p is not None and p.title == "Wool Overcoat"
    assert llm.calls == 0                       # free JSON-LD path won


def test_no_llm_flag_never_calls_model(tmp_path, fixture):
    html = fixture("generic_product.html")     # no JSON-LD
    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(
        cache=RecipeCache(tmp_path / "r.json"),
        llm=llm,
        session=_FakeSession(html),
    )
    result = sw.scrape_page("https://boutique.example.com/x", allow_llm=False)
    assert llm.calls == 0
    assert result is None                       # nothing free succeeded, and LLM was off


def test_rendering_reuses_a_recipe_the_static_hop_just_bought(tmp_path):
    """One page must not pay for two syntheses.

    Compiling a client-rendered site used to call the model twice: once on the
    static HTML, then again after rendering, because the freshly cached recipe
    was never read back. Measured in production at 606 credits for a single
    page instead of 306.
    """
    from scrapewright.cache import RecipeCache
    from scrapewright.extract.base import SelectorRecipe
    from scrapewright.pipeline import Scrapewright
    from scrapewright.schema import PRODUCT_SCHEMA

    static_html = "<html><body><h1>Nothing useful</h1></body></html>"
    rendered_html = "<html><body><h1>A Real Product</h1><b>10.00</b></body></html>"

    class OneShotLlm:
        """Answers once, then refuses -- a second call is the bug."""

        def __init__(self):
            self.calls = 0

        def synthesize(self, html, url, schema=None):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("the model was called twice for one page")
            return SelectorRecipe(origin=url, schema_name=PRODUCT_SCHEMA.name,
                                  fields={"title": "h1", "price": "b"})

    class StaticFetcher:
        kind = "static"
        def fetch(self, url): return static_html
        def close(self): pass

    class Browser:
        kind = "browser"
        def fetch(self, url): return rendered_html
        def close(self): pass

    llm = OneShotLlm()
    sw = Scrapewright(fetcher=StaticFetcher(), browser=Browser(), js=True, llm=llm,
                      cache=RecipeCache(tmp_path / "recipes.json"))

    record = sw.extract("https://shop.test/item")

    assert llm.calls == 1
    assert record is not None
    assert record.data["title"] == "A Real Product"
