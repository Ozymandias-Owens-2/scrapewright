"""A shop with two layouts must not recompile on every variant.

fietshokje.nl wraps some product prices in `.prijs` and others not. Each
recipe replaced the last, so the pages took turns being readable until the
daily compile ceiling stopped the merry-go-round and the next product read
as nothing at all.
"""
from scrapewright.cache import RecipeCache
from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.selectors import SelectorExtractor
from scrapewright.pipeline import Scrapewright, _merged_recipe
from scrapewright.schema import Schema

SCHEMA = Schema.from_names(["price"])
LAYOUT_A = '<html><body><div class="prijs"><p class="price">2,099,-</p></div></body></html>'
LAYOUT_B = '<html><body><p class="price">1,499,-</p></body></html>'


def test_both_selectors_survive_a_recompile():
    old = SelectorRecipe(fields={"price": ".prijs p.price"})
    new = SelectorRecipe(fields={"price": "p.price"})
    merged = _merged_recipe(old, new)

    assert merged.selectors_for("price") == ["p.price", ".prijs p.price"]


def test_the_freshly_proved_selector_goes_first():
    """We are here because the old one failed on the page in hand. Leaving
    it in front risks it matching something it should not."""
    merged = _merged_recipe(SelectorRecipe(fields={"price": ".stale"}),
                            SelectorRecipe(fields={"price": ".proved"}))
    assert merged.fields["price"] == ".proved"


def test_a_field_only_the_old_recipe_knew_is_kept():
    old = SelectorRecipe(fields={"price": ".p", "stock": ".s"})
    merged = _merged_recipe(old, SelectorRecipe(fields={"price": ".p2"}))
    assert merged.fields["stock"] == ".s"


def test_modes_follow_the_selector_they_belong_to():
    old = SelectorRecipe(fields={"image": "img"}, modes={"image": "attr:src"})
    merged = _merged_recipe(old, SelectorRecipe(fields={"price": ".p"}))
    assert merged.modes["image"] == "attr:src"


def test_needs_js_is_sticky():
    old = SelectorRecipe(fields={"price": ".p"}, needs_js=True)
    assert _merged_recipe(old, SelectorRecipe(fields={"price": ".q"})).needs_js


def test_the_merged_recipe_reads_both_layouts():
    merged = _merged_recipe(SelectorRecipe(fields={"price": ".prijs p.price"}),
                            SelectorRecipe(fields={"price": "p.price"}))
    reader = SelectorExtractor(merged, SCHEMA)
    assert reader.extract_values(LAYOUT_A, "https://s.test/a")["price"] == "2,099,-"
    assert reader.extract_values(LAYOUT_B, "https://s.test/b")["price"] == "1,499,-"


# ── through the pipeline ─────────────────────────────────────────────────────
class _TwoLayouts:
    def __init__(self): self.pages = {"https://s.test/a": LAYOUT_A,
                                      "https://s.test/b": LAYOUT_B,
                                      "https://s.test/c": LAYOUT_A}
    def fetch(self, url): return self.pages.get(url)
    def close(self): pass


class _Llm:
    """Writes the tightest selector for whichever page it is shown."""
    def __init__(self): self.calls = 0
    def synthesize(self, html, url, schema=None, **kw):
        self.calls += 1
        selector = ".prijs p.price" if 'class="prijs"' in html else "p.price"
        return SelectorRecipe(fields={"price": selector})


def test_a_third_page_replays_what_the_first_two_taught(tmp_path):
    llm = _Llm()
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm,
                      fetcher=_TwoLayouts())

    assert sw.extract("https://s.test/a", SCHEMA).data["price"] == "2,099,-"
    assert sw.extract("https://s.test/b", SCHEMA).data["price"] == "1,499,-"
    spent = llm.calls

    # Back to the first layout: already known, so no model call.
    assert sw.extract("https://s.test/c", SCHEMA).data["price"] == "2,099,-"
    assert llm.calls == spent
