"""A recipe that covers one field of three is incomplete, not a verdict.

A dealer's car page carries its title in a meta tag and draws the price with
a script. The static pass found the title, the required field was present,
and extraction returned -- while the caller had asked for a browser and was
paying for renders that never happened.
"""
from scrapewright.cache import RecipeCache
from scrapewright.extract.base import SelectorRecipe
from scrapewright.pipeline import Scrapewright
from scrapewright.schema import Schema

SCHEMA = Schema.from_names(["title", "price:number", "mileage:number"])
STATIC = '<html><head><meta property="og:title" content="A car"></head><body></body></html>'
RENDERED = ('<html><head><meta property="og:title" content="A car"></head>'
            '<body><span class="p">12500</span><span class="km">80000</span></body></html>')

THIN = SelectorRecipe(fields={"title": "meta[property='og:title']"},
                      modes={"title": "attr:content"})
FULL = SelectorRecipe(fields={"title": "meta[property='og:title']",
                              "price": ".p", "mileage": ".km"},
                      modes={"title": "attr:content"})


class _Counting:
    def __init__(self, html): self.html, self.calls = html, 0
    def fetch(self, url):
        self.calls += 1
        return self.html
    def close(self): pass


class _Llm:
    """Answers thinly from static HTML, fully from rendered HTML — which is
    what a real model does, because the static page really is thin."""
    def __init__(self): self.calls = 0
    def synthesize(self, html, url, schema=None, **kw):
        self.calls += 1
        return FULL.model_copy(deep=True) if ".p" in html or "class=\"p\"" in html \
            else THIN.model_copy(deep=True)


def test_a_thin_recipe_sends_us_to_the_browser(tmp_path):
    static, browser = _Counting(STATIC), _Counting(RENDERED)
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=_Llm(),
                      fetcher=static, browser=browser)

    record = sw.extract("https://dealer.test/car/1", SCHEMA)
    assert browser.calls == 1
    assert record.data["title"] == "A car"
    assert record.data["price"] == 12500      # would have been missing before


def test_the_better_of_the_two_passes_wins(tmp_path):
    """Neither pass is trusted blindly: whichever answered more is kept."""
    static, browser = _Counting(STATIC), _Counting("<html><body></body></html>")
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=_Llm(),
                      fetcher=static, browser=browser)

    record = sw.extract("https://dealer.test/car/2", SCHEMA)
    assert record is not None and record.data["title"] == "A car"


def test_a_recipe_that_covers_enough_is_taken_at_its_word(tmp_path):
    """Otherwise every product page with no SKU would be rendered twice."""
    cache = RecipeCache(tmp_path / "r.json")
    cache.put("https://dealer.test/car/3", FULL.model_copy(deep=True), SCHEMA.name)
    static, browser = _Counting(RENDERED), _Counting(RENDERED)
    sw = Scrapewright(cache=cache, llm=_Llm(), fetcher=static, browser=browser)

    sw.extract("https://dealer.test/car/3", SCHEMA)
    assert browser.calls == 0


def test_without_a_browser_nothing_changes(tmp_path):
    static = _Counting(STATIC)
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=_Llm(),
                      fetcher=static)
    record = sw.extract("https://dealer.test/car/4", SCHEMA)
    assert record.data["title"] == "A car"


def test_the_product_schema_is_not_held_to_every_field(tmp_path):
    """Six built-in fields, most pages carry three. Demanding all of them
    recompiled every page -- 606 credits for one page, measured in
    production -- so only the required ones count there."""
    from scrapewright.schema import PRODUCT_SCHEMA

    static = "<html><body><h1>A Real Product</h1><b>10.00</b></body></html>"

    class _Once:
        def __init__(self): self.calls = 0
        def synthesize(self, html, url, schema=None, **kw):
            self.calls += 1
            return SelectorRecipe(fields={"title": "h1", "price": "b"})

    llm = _Once()
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm,
                      fetcher=_Counting(static), browser=_Counting(static))
    sw.extract("https://shop.test/item", PRODUCT_SCHEMA)
    assert llm.calls == 1          # title and price are all it required


def test_a_caller_who_names_three_fields_wants_three(tmp_path):
    assert SCHEMA.named_by_caller
    assert set(SCHEMA.expected_names) == {"title", "price", "mileage"}
