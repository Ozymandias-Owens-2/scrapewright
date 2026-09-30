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
