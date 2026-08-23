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
