"""One example page in, all its siblings out.

Offline: no network, no model — the sources are fed in as fixtures.
"""
import pytest

from scrapewright.like import UrlShape, _parse_sitemap, find_similar, url_template

EXAMPLE = "https://shop.test/products/blue-lamp"


def test_a_sibling_is_the_same_depth_under_the_same_prefix():
    shape = url_template(EXAMPLE)
    assert shape.matches("https://shop.test/products/red-chair")
    assert not shape.matches("https://shop.test/products/lamps/big")   # deeper
    assert not shape.matches("https://shop.test/collections/all")      # elsewhere
    assert not shape.matches("https://other.test/products/x")          # other host
    assert not shape.matches(EXAMPLE)                                  # itself


def test_an_index_file_does_not_change_the_shape():
    shape = url_template("https://shop.test/products/blue-lamp/index.html")
    assert shape.matches("https://shop.test/products/red-chair/index.html")
    assert shape.matches("https://shop.test/products/red-chair")


def test_a_numeric_id_is_not_a_slug():
    """`/p/1234` and `/p/about-us` are rarely the same kind of page."""
    numeric = url_template("https://shop.test/p/1234")
    assert numeric.matches("https://shop.test/p/5678")
    assert not numeric.matches("https://shop.test/p/about-us")


def test_query_shaped_urls_match_on_their_keys():
    shape = url_template("https://shop.test/detail?id=99&lang=nl")
    assert shape.matches("https://shop.test/detail?id=100&lang=nl")
    assert not shape.matches("https://shop.test/detail?id=99&lang=nl")   # itself
    assert not shape.matches("https://shop.test/detail?id=1")            # fewer keys
    assert not shape.matches("https://shop.test/list?id=1&lang=nl")      # other path


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.test/products/blue-lamp</loc></url>
  <url><loc>https://shop.test/products/red-chair</loc></url>
  <url><loc>https://shop.test/products/green-rug</loc></url>
  <url><loc>https://shop.test/about</loc></url>
</urlset>"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://shop.test/sitemap-products-1</loc></sitemap>
</sitemapindex>"""


def test_a_sitemap_yields_pages_and_an_index_yields_sitemaps():
    pages, nested = _parse_sitemap(SITEMAP)
    assert len(pages) == 4 and nested == []
    pages, nested = _parse_sitemap(INDEX)
    # An index's entries are sitemaps however they are spelled — this one has
    # no .xml suffix, which an extension check would have mistaken for a page.
    assert pages == [] and nested == ["https://shop.test/sitemap-products-1"]


def test_junk_xml_is_not_an_exception():
    assert _parse_sitemap("<not xml") == ([], [])


class _Web:
    """Answers the handful of URLs these tests reach for."""
    def __init__(self, pages): self.pages, self.asked = pages, []

    def fetch(self, url):
        self.asked.append(url)
        return self.pages.get(url)


def test_the_sitemap_is_preferred_and_the_example_is_excluded(monkeypatch):
    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: ["https://shop.test/sitemap.xml"])
    monkeypatch.setattr("scrapewright.like._text_of",
                        lambda url, session=None: SITEMAP if "sitemap" in url else None)
    found = find_similar(EXAMPLE, limit=10)
    assert found == ["https://shop.test/products/red-chair",
                     "https://shop.test/products/green-rug"]


def test_without_a_sitemap_it_falls_back_to_links_on_the_page(monkeypatch):
    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: [])
    monkeypatch.setattr("scrapewright.like._text_of", lambda url, session=None: None)
    web = _Web({EXAMPLE: '<a href="/products/red-chair">next</a>'
                         '<a href="/collections/all">back</a>'})
    assert find_similar(EXAMPLE, limit=10, fetcher=web) == [
        "https://shop.test/products/red-chair"]


def test_and_then_to_the_parent_listing(monkeypatch):
    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: [])
    monkeypatch.setattr("scrapewright.like._text_of", lambda url, session=None: None)
    web = _Web({EXAMPLE: "<p>no links here</p>",
                "https://shop.test/products": '<a href="/products/green-rug">rug</a>'})
    assert find_similar(EXAMPLE, limit=10, fetcher=web) == [
        "https://shop.test/products/green-rug"]
    assert "https://shop.test/products" in web.asked


def test_the_limit_is_respected(monkeypatch):
    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: ["https://shop.test/sitemap.xml"])
    monkeypatch.setattr("scrapewright.like._text_of",
                        lambda url, session=None: SITEMAP)
    assert len(find_similar(EXAMPLE, limit=1)) == 1


def test_the_pipeline_compiles_once_and_replays_on_the_siblings(monkeypatch, tmp_path):
    """The example is both the specimen and the teaching page: the recipe is
    learned on it, and every sibling replays that recipe for nothing."""
    from scrapewright.cache import RecipeCache
    from scrapewright.extract.base import SelectorRecipe
    from scrapewright.pipeline import Scrapewright
    from scrapewright.schema import Schema

    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: ["https://shop.test/sitemap.xml"])
    monkeypatch.setattr("scrapewright.like._text_of",
                        lambda url, session=None: SITEMAP)

    class _Llm:
        calls = 0
        def synthesize(self, html, url, schema=None, **kw):
            type(self).calls += 1
            return SelectorRecipe(fields={"title": "h1"})

    class _Fetcher:
        def fetch(self, url): return "<html><h1>A lamp</h1></html>"

    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=_Llm(),
                      fetcher=_Fetcher())
    rows = list(sw.crawl_like(EXAMPLE, Schema.from_names(["title"]), max_items=10))
    assert len(rows) == 3                 # the example plus its two siblings
    assert _Llm.calls == 1


def test_neighbours_are_harvested_from_pages_already_fetched(monkeypatch, tmp_path):
    """A site with no sitemap is still walkable: item pages link to each
    other, and those pages are fetched for extraction anyway."""
    from scrapewright.cache import RecipeCache
    from scrapewright.extract.base import SelectorRecipe
    from scrapewright.pipeline import Scrapewright
    from scrapewright.schema import Schema

    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: [])
    monkeypatch.setattr("scrapewright.like._text_of", lambda url, session=None: None)

    # A chain: each page names only the next one.
    chain = {
        "https://shop.test/products/a": "b",
        "https://shop.test/products/b": "c",
        "https://shop.test/products/c": "d",
        "https://shop.test/products/d": None,
    }

    class _Fetcher:
        def __init__(self): self.asked = []
        def fetch(self, url):
            self.asked.append(url)
            if url not in chain:
                return None
            nxt = chain[url]
            link = f'<a href="/products/{nxt}">next</a>' if nxt else ""
            return f"<html><h1>{url[-1]}</h1>{link}</html>"

    class _Llm:
        def synthesize(self, html, url, schema=None, **kw):
            return SelectorRecipe(fields={"title": "h1"})

    fetcher = _Fetcher()
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=_Llm(),
                      fetcher=fetcher)
    rows = list(sw.crawl_like("https://shop.test/products/a",
                              Schema.from_names(["title"]), max_items=10))
    assert [r.data["title"] for r in rows] == ["a", "b", "c", "d"]
    # Each item page fetched exactly once -- for extraction, never again for
    # its links, not even the example. (The parent directory is probed once as
    # a discovery source, and answers nothing.)
    for page in chain:
        assert fetcher.asked.count(page) == 1, fetcher.asked


def test_the_fetcher_is_put_back_afterwards(tmp_path):
    from scrapewright.cache import RecipeCache
    from scrapewright.pipeline import Scrapewright

    class _Fetcher:
        def fetch(self, url): return None

    original = _Fetcher()
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), fetcher=original)
    list(sw.crawl_like("https://shop.test/products/a", max_items=1))
    assert sw.fetcher is original


BREADCRUMB_PAGE = """
<html><body>
  <ul class="breadcrumb">
    <li><a href="/">Home</a></li>
    <li><a href="/catalogue/category/mystery">Mystery</a></li>
    <li class="active">Sharp Objects</li>
  </ul>
  <h1>Sharp Objects</h1>
</body></html>"""


def test_the_breadcrumb_names_the_category_nearest_first():
    from scrapewright.like import _breadcrumb_links

    found = _breadcrumb_links(BREADCRUMB_PAGE, "https://shop.test/products/blue-lamp")
    assert found == ["https://shop.test/catalogue/category/mystery",
                     "https://shop.test/"]


def test_the_category_is_walked_when_the_item_page_links_nowhere(monkeypatch):
    """A product page that links only to the same four neighbours is a dead
    end; its category, with pagination, is not."""
    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: [])
    monkeypatch.setattr("scrapewright.like._text_of", lambda url, session=None: None)

    listing = ("<html><body>"
               + "".join(f'<a href="/products/item-{i}"><img src="{i}.jpg"></a>'
                         for i in range(3))
               + "</body></html>")

    class _Web:
        def fetch(self, url):
            if url == EXAMPLE:
                return BREADCRUMB_PAGE
            if url == "https://shop.test/catalogue/category/mystery":
                return listing
            return None

    found = find_similar(EXAMPLE, limit=10, fetcher=_Web())
    assert found == [f"https://shop.test/products/item-{i}" for i in range(3)]
