"""Self-healing: a cached recipe that stops matching (site changed its DOM)
must trigger re-synthesis instead of silently returning empty products."""

from scrapewright.cache import RecipeCache
from scrapewright.extract.base import SelectorRecipe
from scrapewright.pipeline import Scrapewright
from tests.test_pipeline import _FakeSession, _FakeLLM, GENERIC_RECIPE

STALE_RECIPE = SelectorRecipe(title=".old-title", price=".old-price", origin="llm:old")
BAD_RECIPE = SelectorRecipe(title=".still-wrong", price=".nope", origin="llm:bad")


def test_stale_recipe_triggers_resynthesis(tmp_path, fixture):
    """The DOM 'changed': the cached selectors match nothing anymore."""
    cache = RecipeCache(tmp_path / "r.json")
    url = "https://boutique.example.com/products/ricki"
    cache.put(url, STALE_RECIPE)

    llm = _FakeLLM(GENERIC_RECIPE)  # the 'fixed' recipe for the new DOM
    sw = Scrapewright(cache=cache, llm=llm,
                      session=_FakeSession(fixture("generic_product.html")))

    product = sw.scrape_page(url)
    assert product is not None and product.title == "Ricki Leather Boots"
    assert llm.calls == 1                                   # healed via one synth
    assert cache.get(url).title == ".product__title"        # stale recipe replaced

    # Next call replays the healed recipe — no further model spend.
    again = sw.scrape_page(url)
    assert again is not None and llm.calls == 1


def test_healthy_recipe_is_never_resynthesized(tmp_path, fixture):
    cache = RecipeCache(tmp_path / "r.json")
    url = "https://boutique.example.com/products/ricki"
    cache.put(url, GENERIC_RECIPE)

    llm = _FakeLLM(GENERIC_RECIPE)
    sw = Scrapewright(cache=cache, llm=llm,
                      session=_FakeSession(fixture("generic_product.html")))
    product = sw.scrape_page(url)
    assert product is not None and product.is_usable()
    assert llm.calls == 0


def test_batch_healing_respects_synth_budget(tmp_path, fixture):
    """A site that resists synthesis must not burn one LLM call per page."""
    cache = RecipeCache(tmp_path / "r.json")
    llm = _FakeLLM(BAD_RECIPE)  # synthesis keeps producing a non-matching recipe
    sw = Scrapewright(cache=cache, llm=llm,
                      session=_FakeSession(fixture("generic_product.html")),
                      max_synth_per_run=2)

    urls = [f"https://boutique.example.com/products/p{i}" for i in range(5)]
    products = sw.scrape_pages(urls)

    assert products == []          # nothing usable came out (page has no JSON-LD)
    assert llm.calls == 2          # capped by max_synth_per_run, not 5
