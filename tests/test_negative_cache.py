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
    cache.note_compile(URL, SCHEMA.name)
    assert cache.compiles_today(URL, SCHEMA.name) == 1
    assert cache.compiles_today("https://elsewhere.test/x", SCHEMA.name) == 0
    assert cache.compiles_today(URL, "another-schema") == 0


def test_a_compilation_that_produced_nothing_still_counts(tmp_path):
    """The ceiling has to bind on the pages it exists for.

    Counting compilations on the recipe they wrote meant the ones that
    wrote no recipe went uncounted, so an unreadable page was never capped
    -- it bought a fresh synthesis on every refresh, for ever.
    """
    cache = RecipeCache(tmp_path / "r.json")
    llm = _Hopeless()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_Fetcher())
    sw.extract(URL, SCHEMA)

    assert llm.calls >= 1
    assert cache.compiles_today(URL, SCHEMA.name) == llm.calls
    assert cache.get(URL, SCHEMA.name) is None   # nothing to hang a count on


def test_one_readable_page_does_not_absolve_the_rest_of_the_site(tmp_path):
    """Two products of one shop, one readable and one not.

    The note used to be filed by domain, so compiling the readable page
    retired the unreadable page's note on every refresh and the unreadable
    one paid again -- 310 credits a run, indefinitely, on a live sheet.
    """
    cache = RecipeCache(tmp_path / "r.json")
    unreadable = "https://shop.test/product/nope"
    cache.note_failure(unreadable, SCHEMA.name, "nothing")

    cache.put(URL, SelectorRecipe(fields={"price": ".p"}), SCHEMA.name)

    assert not cache.recently_failed(URL, SCHEMA.name)
    assert cache.recently_failed(unreadable, SCHEMA.name)


def test_a_page_is_written_off_though_the_site_has_a_recipe(tmp_path):
    """A recipe learned elsewhere on the site is not evidence about this page.

    Two live rows, eurotrek and campolife, each kept a recipe that no longer
    satisfied the schema: the note could not be written because a recipe
    existed, so the recipe was recompiled on every single run.
    """
    cache = RecipeCache(tmp_path / "r.json")
    cache.put("https://shop.test/product/works",
              SelectorRecipe(fields={"price": ".nowhere"}), SCHEMA.name)

    llm = _Hopeless()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_Fetcher())
    assert sw.extract(URL, SCHEMA) is None
    assert cache.recently_failed(URL, SCHEMA.name)

    spent = llm.calls
    assert sw.extract(URL, SCHEMA) is None
    assert llm.calls == spent


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


# ── four rows of a spreadsheet arrive at once ────────────────────────────────
def test_concurrent_writers_do_not_drop_each_others_changes(tmp_path):
    """Read-modify-write on one file, from several threads at once.

    Unlocked, two writers read the same file, each adds its own entry and
    the second write drops the first -- a compile counted and forgotten,
    which is how a domain compiled three times under a ceiling of two. On
    Windows it was worse than a lost update: both threads wrote the same
    temporary name and the loser's os.replace raised, killing the request.
    """
    import threading

    cache = RecipeCache(tmp_path / "r.json")
    errors = []

    def hammer(n):
        try:
            for _ in range(5):
                cache.note_compile(URL, SCHEMA.name)
                cache.note_failure(f"https://shop.test/p/{n}", SCHEMA.name, "no")
        except Exception as e:                     # recorded, asserted on
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert errors == []
    assert cache.compiles_today(URL, SCHEMA.name) == 20
    for n in range(4):
        assert cache.recently_failed(f"https://shop.test/p/{n}", SCHEMA.name)


def test_retry_still_obeys_the_daily_ceiling(tmp_path):
    """"Try it again" is not "spend without limit".

    The add-on set retry on every row whose status read "Nothing found" and
    did it on every press, including the automatic continuation. Each press
    retired the note and bought a fresh compilation: two such rows were 615
    of one live run's 727 credits.
    """
    from scrapewright.pipeline import MAX_COMPILES_PER_DAY

    cache = RecipeCache(tmp_path / "r.json")
    llm = _Hopeless()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_Fetcher())

    for _ in range(MAX_COMPILES_PER_DAY + 3):
        sw.extract(URL, SCHEMA, retry=True)

    assert llm.calls == MAX_COMPILES_PER_DAY


RENDERED_NO_PRICE = "<html><body><h1>A thing</h1><p>still no price</p></body></html>"


def test_one_compilation_per_request_even_when_the_browser_gives_nothing(tmp_path):
    """A silent browser must not buy the model two goes at one page.

    The one-compile-per-request guard was a flag set by the teach-on-rendered
    hop. When that hop could not get a page -- a full render queue -- it
    reported nothing paid for, and the static and rendered hops then bought
    a compilation each: three sites spent 610 credits in a single live
    request that way, against a daily ceiling of two.
    """
    cache = RecipeCache(tmp_path / "r.json")

    class _BusyThenFree:
        """The render queue as it actually behaved: full, then not.

        Both hops that can compile need a page. Give the first one nothing
        and the second one a page, and the request pays twice.
        """
        alive = True
        def __init__(self): self.calls = 0
        def fetch(self, url):
            self.calls += 1
            return None if self.calls == 1 else RENDERED_NO_PRICE
        def close(self): pass

    class _RealPage:
        """Not a client-side shell -- the static hop will work on this."""
        def fetch(self, url):
            return "<html><body><h1>A thing</h1><p>prose, no price</p></body></html>"
        def close(self): pass

    browser, llm = _BusyThenFree(), _Hopeless()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_RealPage(), browser=browser)
    sw.extract(URL, SCHEMA)
    assert browser.calls == 2, "the second hop never reached the browser"
    assert llm.calls == 1


# ── a waiting-room is not a page ────────────────────────────────────────────
CHALLENGE = ("<html><head><title>Just a moment...</title></head><body>"
             "<div id='cf-browser-verification'>Checking your browser before "
             "accessing the site.</div></body></html>")


def test_a_challenge_page_is_never_compiled(tmp_path):
    """koffieruis.be, live: Cloudflare answered 200 with a holding page. The
    extractor found no price on it -- correctly -- so the row read "Nothing
    found. Check the column names", about column names that were right, and
    the site looked worth compiling on every single refresh.
    """
    from scrapewright.fetch import SiteRefused

    class _Challenged:
        def fetch(self, url): return CHALLENGE
        def close(self): pass

    cache = RecipeCache(tmp_path / "r.json")
    llm = _Hopeless()
    sw = Scrapewright(cache=cache, llm=llm, fetcher=_Challenged())

    try:
        sw.extract(URL, SCHEMA)
    except SiteRefused as e:
        assert "cf-browser-verification" in e.marker or "just a moment" in e.marker
    else:
        raise AssertionError("a challenge page was read as an ordinary page")

    assert llm.calls == 0, "paid the model to read a waiting-room"
    assert cache.compiles_today(URL, SCHEMA.name) == 0


def test_a_browser_gets_its_turn_before_the_site_is_written_off(tmp_path):
    """The static copy being a challenge says nothing about the rendered
    one: plenty of walls let a real browser through."""
    from scrapewright.extract.base import SelectorRecipe

    class _Challenged:
        def fetch(self, url): return CHALLENGE
        def close(self): pass

    class _BrowserGetsThrough:
        alive = True
        def fetch(self, url):
            return '<html><body><p class="p">&euro;52,15</p></body></html>'
        def close(self): pass

    class _Reads:
        def __init__(self): self.calls = 0
        def synthesize(self, html, url, schema=None, **kw):
            self.calls += 1
            return SelectorRecipe(fields={"price": ".p"})

    cache = RecipeCache(tmp_path / "r.json")
    schema = Schema.from_names(["price:number"])
    sw = Scrapewright(cache=cache, llm=_Reads(), fetcher=_Challenged(),
                      browser=_BrowserGetsThrough())
    record = sw.extract(URL, schema)
    assert record is not None and float(record.data["price"]) == 52.15
