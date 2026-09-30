"""Finding the listing, which is where by-example failed in the field.

Tried on seven dealer and camper sites, it found pages on two. The others
keep their stock several directories above the car, or render the list in the
browser, or link the same car twice with a share parameter. Every case below
is one of those, offline.
"""
import pytest

from scrapewright.like import (_ancestor_listings, canonical, find_similar,
                               url_template)

CAR = "https://autovoorraad.test/occasions/voertuig/56896140/volkswagen-taigo"
STOCK = "https://autovoorraad.test/occasions"


def _listing(paths):
    return ("<html><body>" +
            "".join(f'<a href="{p}"><img src="x.jpg"></a>' for p in paths) +
            "</body></html>")


@pytest.fixture(autouse=True)
def no_sitemap(monkeypatch):
    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: [])
    monkeypatch.setattr("scrapewright.like._text_of", lambda url, session=None: None)


# ── 1. the listing is several levels up ──────────────────────────────────────
def test_every_ancestor_is_tried_nearest_first():
    assert _ancestor_listings(CAR) == [
        "https://autovoorraad.test/occasions/voertuig/56896140",
        "https://autovoorraad.test/occasions/voertuig",
        "https://autovoorraad.test/occasions",
        "https://autovoorraad.test/",
    ]


def test_a_stock_page_three_levels_up_is_found():
    """The car's own directory and /occasions/voertuig are not listings; the
    walk has to climb past both."""
    cars = [f"/occasions/voertuig/{i}/car-{i}" for i in range(3)]

    class _Web:
        def __init__(self): self.asked = []
        def fetch(self, url):
            self.asked.append(url)
            return _listing(cars) if url == STOCK else "<html><body></body></html>"

    web = _Web()
    found = find_similar(CAR, limit=10, fetcher=web)
    assert len(found) == 3
    assert STOCK in web.asked


def test_climbing_stops_at_the_first_level_that_answers():
    deeper = "https://autovoorraad.test/occasions/voertuig"

    class _Web:
        def __init__(self): self.asked = []
        def fetch(self, url):
            self.asked.append(url)
            if url == deeper:
                return _listing([f"/occasions/voertuig/{i}/car-{i}" for i in range(3)])
            return "<html><body></body></html>"

    web = _Web()
    assert len(find_similar(CAR, limit=10, fetcher=web)) == 3
    assert STOCK not in web.asked          # never had to climb that far


# ── 2. the caller names the listing ──────────────────────────────────────────
def test_a_named_listing_is_used_first():
    named = "https://autovoorraad.test/aanbod?page=1"

    class _Web:
        def __init__(self): self.asked = []
        def fetch(self, url):
            self.asked.append(url)
            if url == named:
                return _listing([f"/occasions/voertuig/{i}/car-{i}" for i in range(4)])
            return "<html><body></body></html>"

    web = _Web()
    found = find_similar(CAR, limit=10, fetcher=web, listing_url=named)
    assert len(found) == 4
    assert web.asked[0] == named           # before any guessing


def test_a_named_listing_that_leads_nowhere_falls_back_to_guessing():
    """A wrong answer from the caller costs one request, not the run."""
    class _Web:
        def fetch(self, url):
            if url == STOCK:
                return _listing([f"/occasions/voertuig/{i}/car-{i}" for i in range(3)])
            return "<html><body></body></html>"

    found = find_similar(CAR, limit=10, fetcher=_Web(),
                         listing_url="https://autovoorraad.test/nothing-here")
    assert len(found) == 3


def test_a_named_listing_is_believed_when_it_holds_only_two_items():
    """A guessed page has to prove itself with a group of three; one the
    caller pointed at does not -- a shop with two cars is still a shop."""
    named = "https://autovoorraad.test/aanbod"

    class _Web:
        def fetch(self, url):
            if url == named:
                return _listing([f"/occasions/voertuig/{i}/car-{i}" for i in range(2)])
            return "<html><body></body></html>"

    assert len(find_similar(CAR, limit=10, fetcher=_Web(), listing_url=named)) == 2


# ── 3. the listing renders in the browser ────────────────────────────────────
def test_a_client_side_listing_is_retried_in_a_browser():
    """Static HTML carries no links at all; the same URL in a browser does."""
    class _Static:
        def fetch(self, url):
            return "<html><body><div id='app'></div></body></html>"

    class _Browser:
        def __init__(self): self.asked = []
        def fetch(self, url):
            self.asked.append(url)
            return _listing([f"/occasions/voertuig/{i}/car-{i}" for i in range(3)])

    browser = _Browser()
    found = find_similar(CAR, limit=10, fetcher=_Static(), js_fetcher=browser)
    assert len(found) == 3
    assert browser.asked


def test_without_a_browser_the_same_page_yields_nothing():
    class _Static:
        def fetch(self, url):
            return "<html><body><div id='app'></div></body></html>"

    assert find_similar(CAR, limit=10, fetcher=_Static()) == []


# ── 4. the same car linked twice ─────────────────────────────────────────────
def test_a_share_parameter_does_not_make_a_second_page():
    shape = url_template("https://ron.test/auto/123/golf")
    assert (canonical("https://ron.test/auto/9/polo?share=x", shape)
            == canonical("https://ron.test/auto/9/polo", shape))


def test_a_declared_canonical_wins_over_the_link_we_followed():
    html = '<link rel="canonical" href="https://ron.test/auto/9/polo">'
    assert canonical("https://ron.test/auto/9/polo?share=x", None, html) == \
        "https://ron.test/auto/9/polo"


def test_duplicates_are_collapsed_in_the_result():
    cars = ["/occasions/voertuig/1/car-1",
            "/occasions/voertuig/1/car-1?share=facebook",
            "/occasions/voertuig/2/car-2"]

    class _Web:
        def fetch(self, url):
            return _listing(cars) if url == STOCK else "<html><body></body></html>"

    found = find_similar(CAR, limit=10, fetcher=_Web())
    assert len(found) == 2


def test_the_example_itself_is_never_returned_under_another_name():
    cars = [CAR.replace("https://autovoorraad.test", "") + "?share=x",
            "/occasions/voertuig/2/car-2",
            "/occasions/voertuig/3/car-3",
            "/occasions/voertuig/4/car-4"]

    class _Web:
        def fetch(self, url):
            return _listing(cars) if url == STOCK else "<html><body></body></html>"

    found = find_similar(CAR, limit=10, fetcher=_Web())
    assert CAR not in found
    assert not any("share=" in u for u in found)
    assert len(found) == 3
