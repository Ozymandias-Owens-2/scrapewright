"""A page nobody can read must not be paid for twice.

Two real pages from a day of live use: one spent three model calls and
fourteen seconds to return nothing, every single time it was read. A daily
refresh of one such link burns the customer's credits and our tokens forever.
"""
from scrapewright.cache import FAILURE_TTL_SECONDS, RecipeCache
from scrapewright.extract.base import SelectorRecipe
from scrapewright.pipeline import Scrapewright
from scrapewright.schema import Schema

SCHEMA = Schema.from_names(["price:number", "in_stock"])
BLANK = "<html><body><div id='app'></div></body></html>"
URL = "https://shop.test/product/sigenergy"


class _Fetcher:
    def fetch(self, url): return BLANK
    def close(self): pass


class _Hopeless:
    """A model that cannot read this page -- which is the normal case here."""
    def __init__(self): self.calls = 0
    def synthesize(self, html, url, schema=None, **kw):
        self.calls += 1
        return None


def test_the_second_read_of_an_unreadable_page_calls_no_model(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    llm = _Hopeless()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_Fetcher())

    assert sw.extract(URL, SCHEMA) is None
    first = llm.calls
    assert first >= 1

    assert sw.extract(URL, SCHEMA) is None
    assert llm.calls == first          # nothing more was spent


def test_the_failure_is_remembered_per_schema(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    sw = Scrapewright(cache=cache, llm=_Hopeless(), fetcher=_Fetcher())
    sw.extract(URL, SCHEMA)

    assert cache.recently_failed(URL, SCHEMA.name)
    other = Schema.from_names(["title"])
    assert not cache.recently_failed(URL, other.name)


def test_retry_asks_again_on_purpose(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    llm = _Hopeless()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_Fetcher())
    sw.extract(URL, SCHEMA)
    spent = llm.calls

    sw.extract(URL, SCHEMA, retry=True)
    assert llm.calls > spent


def test_the_note_expires(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    cache.note_failure(URL, SCHEMA.name, "nothing")
    assert cache.recently_failed(URL, SCHEMA.name)
    assert not cache.recently_failed(URL, SCHEMA.name, within=0)
    assert FAILURE_TTL_SECONDS == 7 * 24 * 60 * 60


def test_success_retires_the_note(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    cache.note_failure(URL, SCHEMA.name, "nothing")
    cache.put(URL, SelectorRecipe(fields={"price": ".p"}), SCHEMA.name)
    assert not cache.recently_failed(URL, SCHEMA.name)


def test_compilations_are_capped_for_the_day(tmp_path):
    """A ceiling, not a ban. A recipe can be written, found wanting and
    rewritten, so a loop over a thousand pages of a half-readable site would
    otherwise pay a thousand times. It cannot be one a day either -- a site
    that redesigns overnight has to be allowed to heal, and healing is a
    compilation."""
    from scrapewright.pipeline import MAX_COMPILES_PER_DAY

    cache = RecipeCache(tmp_path / "r.json")

    class _Eager:
        def __init__(self): self.calls = 0
        def synthesize(self, html, url, schema=None, **kw):
            self.calls += 1
            return SelectorRecipe(fields={"price": ".still-nope"})

    llm = _Eager()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_Fetcher())
    for i in range(MAX_COMPILES_PER_DAY + 3):
        sw.extract(f"https://shop.test/product/{i}", SCHEMA)

    assert llm.calls == MAX_COMPILES_PER_DAY
    assert cache.compiles_today(URL, SCHEMA.name) == MAX_COMPILES_PER_DAY


def test_the_count_is_per_site_and_schema(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    cache.put(URL, SelectorRecipe(fields={"price": ".p"}), SCHEMA.name)
    assert cache.compiles_today(URL, SCHEMA.name) == 1
    assert cache.compiles_today("https://elsewhere.test/x", SCHEMA.name) == 0
    assert cache.compiles_today(URL, "another-schema") == 0


def test_a_failure_does_not_block_a_site_that_works(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    cache.note_failure(URL, SCHEMA.name, "nothing")
    assert not cache.recently_failed("https://other.test/p", SCHEMA.name)
