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


# ── a failure without a browser is not a failure with one ────────────────────
RENDERED = '<html><body><h1>A car</h1><span class="p">19445</span></body></html>'


class _StaticOnly:
    """The page as a plain fetch sees it: a shell with nothing in it."""
    def fetch(self, url): return BLANK
    def close(self): pass


class _Browser:
    def __init__(self): self.calls = 0
    def fetch(self, url):
        self.calls += 1
        return RENDERED
    def close(self): pass
    alive = True


class _ReadsRendered:
    """A model that can only make sense of the rendered page -- which is the
    usual case for the sites this matters on."""
    def __init__(self): self.calls = 0
    def synthesize(self, html, url, schema=None, **kw):
        self.calls += 1
        if "19445" not in html:
            return None
        return SelectorRecipe(fields={"price": ".p"})


def test_a_static_failure_does_not_stop_the_browser_trying(tmp_path):
    """Live: a car page failed on static HTML, the failure was written down,
    and the next request with js=true skipped the browser entirely and
    returned nothing -- on a page whose price a browser finds at once."""
    cache = RecipeCache(tmp_path / "r.json")
    schema = Schema.from_names(["price:number"])

    # First, with no browser available at all.
    flat = Scrapewright(cache=cache, llm=_ReadsRendered(), fetcher=_StaticOnly())
    assert flat.extract(URL, schema) is None
    assert cache.recently_failed(URL, schema.name)
    assert cache.failure_mode(URL, schema.name) == "static"

    # Then with one. The verdict above was reached without it.
    browser, llm = _Browser(), _ReadsRendered()
    rich = Scrapewright(cache=cache, llm=llm, fetcher=_StaticOnly(),
                        browser=browser)
    record = rich.extract(URL, schema)
    assert browser.calls >= 1, "the browser was never tried"
    assert record is not None and str(record.data["price"]) == "19445"


def test_a_failure_with_a_browser_stops_everyone(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    schema = Schema.from_names(["price:number"])

    class _Hopeless:
        def __init__(self): self.calls = 0
        def synthesize(self, html, url, schema=None, **kw):
            self.calls += 1
            return None

    llm = _Hopeless()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_StaticOnly(),
                      browser=_Browser())
    sw.extract(URL, schema)
    assert cache.failure_mode(URL, schema.name) == "js"

    spent = llm.calls
    sw.extract(URL, schema)
    assert llm.calls == spent          # nothing stronger left to try


def test_retry_clears_a_failure_of_either_mode(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    schema = Schema.from_names(["price:number"])
    cache.note_failure(URL, schema.name, "nothing", mode="js")

    llm = _ReadsRendered()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_StaticOnly(),
                      browser=_Browser())
    assert sw.extract(URL, schema, retry=True) is not None
    assert not cache.recently_failed(URL, schema.name, can_js=True)
