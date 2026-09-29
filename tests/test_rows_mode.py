"""A listing page is many records, not one.

Offline: the recipe is handed in, so nothing here calls a model or a network.
"""
from decimal import Decimal

from scrapewright.cache import RecipeCache
from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.selectors import SelectorExtractor
from scrapewright.pipeline import Scrapewright, _rows_cache_name
from scrapewright.schema import Schema

LISTING = """
<html><body>
  <main>
    <article class="card" data-make="audi">
      <h2 class="t">Audi A4</h2><span class="p">&euro; 4.250</span>
      <a class="l" href="/cars/1">see</a>
    </article>
    <article class="card" data-make="bmw">
      <h2 class="t">BMW 320i</h2><span class="p">&euro; 12.900</span>
      <a class="l" href="/cars/2">see</a>
    </article>
    <article class="card" data-make="opel">
      <h2 class="t">Opel Corsa</h2><span class="p">&euro; 999</span>
    </article>
  </main>
</body></html>
"""

SCHEMA = Schema.from_names(["title", "price:number", "link"])
RECIPE = SelectorRecipe(item="article.card",
                        fields={"title": ".t", "price": ".p", "link": ".l"},
                        modes={"link": "attr:href"})


def test_every_card_becomes_its_own_record():
    rows = SelectorExtractor(RECIPE, SCHEMA).extract_rows(LISTING, "https://cars.test/lst")
    assert len(rows) == 3
    assert [r.data["title"] for r in rows] == ["Audi A4", "BMW 320i", "Opel Corsa"]


def test_values_stay_on_their_own_row():
    rows = SelectorExtractor(RECIPE, SCHEMA).extract_rows(LISTING, "https://cars.test/lst")
    assert rows[1].data["price"] == Decimal("12900")
    assert rows[1].data["price_text"] == "€ 12.900"


def test_relative_links_are_resolved_per_row():
    rows = SelectorExtractor(RECIPE, SCHEMA).extract_rows(LISTING, "https://cars.test/lst")
    assert rows[0].data["link"] == "https://cars.test/cars/1"
    assert "link" not in rows[2].data          # that card has none


def test_a_document_scoped_selector_still_matches_inside_a_card():
    """Models write `article.card .p` as often as `.p`; both must work."""
    recipe = RECIPE.model_copy(update={"fields": {"title": "article.card .t",
                                                  "price": "article.card .p"}})
    rows = SelectorExtractor(recipe, SCHEMA).extract_rows(LISTING, "https://cars.test/lst")
    assert [r.data["title"] for r in rows] == ["Audi A4", "BMW 320i", "Opel Corsa"]


def test_without_an_item_selector_it_is_one_record_as_before():
    recipe = SelectorRecipe(fields={"title": ".t"})
    rows = SelectorExtractor(recipe, SCHEMA).extract_rows(LISTING, "https://cars.test/lst")
    assert len(rows) == 1


class _Fetcher:
    def __init__(self, html): self.html = html
    def fetch(self, url): return self.html


class _Llm:
    """Answers once; a second call means the cache did not do its job."""
    def __init__(self, recipe): self.recipe, self.calls = recipe, 0
    def synthesize(self, html, url, schema=None, *, rows=False):
        assert rows, "rows mode must ask for a listing recipe"
        self.calls += 1
        return self.recipe.model_copy()


def test_the_recipe_is_paid_for_once_and_replayed(tmp_path):
    llm = _Llm(RECIPE)
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm,
                      fetcher=_Fetcher(LISTING))
    first = sw.extract_rows("https://cars.test/lst", SCHEMA)
    second = sw.extract_rows("https://cars.test/lst?page=2", SCHEMA)
    assert len(first) == len(second) == 3
    assert llm.calls == 1


def test_rows_and_page_recipes_do_not_share_a_cache_entry():
    assert _rows_cache_name(SCHEMA) != SCHEMA.name


def test_the_metering_wrapper_passes_rows_through():
    """It sits between the pipeline and the extractor; an argument it has not
    been taught about failed the whole job in production."""
    from scrapewright.service.metering import Meter, _CountingLlm

    seen = {}

    class _Inner:
        def synthesize(self, html, url, schema=None, *, rows=False):
            seen["rows"] = rows
            return RECIPE

    meter = Meter()
    _CountingLlm(_Inner(), meter).synthesize("<html></html>", "u", SCHEMA, rows=True)
    assert seen["rows"] is True
    assert meter.syntheses == 1


PAGE_TWO = LISTING.replace("Audi A4", "Audi A6").replace("BMW 320i", "BMW 520d") \
                  .replace("Opel Corsa", "Opel Astra")


class _Pages:
    """Serves page 1, then page 2, then page 1 again — as a site that ignores
    ?page= does."""
    def __init__(self):
        self.asked = []

    def fetch(self, url):
        self.asked.append(url)
        return PAGE_TWO if "page=2" in url else LISTING


def test_pagination_is_guessed_and_stops_when_rows_repeat(tmp_path):
    pages = _Pages()
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=_Llm(RECIPE),
                      fetcher=pages)
    rows = list(sw.crawl_rows("https://cars.test/lst", SCHEMA, max_items=100))
    titles = [r.data["title"] for r in rows]
    assert titles == ["Audi A4", "BMW 320i", "Opel Corsa",
                      "Audi A6", "BMW 520d", "Opel Astra"]
    assert "https://cars.test/lst?page=2" in pages.asked
    # Page 3 repeats page 1, contributes nothing new, and ends the walk.
    assert len(pages.asked) == 3


def test_each_listing_page_is_fetched_once(tmp_path):
    """Fetching twice per page -- once to read, once to find the next link --
    billed the caller for a page they never saw."""
    pages = _Pages()
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=_Llm(RECIPE),
                      fetcher=pages)
    list(sw.crawl_rows("https://cars.test/lst", SCHEMA, max_items=3))
    assert pages.asked == ["https://cars.test/lst"]
