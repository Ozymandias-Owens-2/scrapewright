"""Items served as `/thing/index.html` are still items.

Static-site catalogues (Jekyll, Hugo, anything exported to flat files) name
every item page `index.html` inside its own directory. Both discovery
heuristics used to read that filename as the item and its directory as the
template, which left every group with one member and the crawl empty.
"""
from bs4 import BeautifulSoup

from scrapewright.crawl import Frontier

BASE = "https://books.example/catalogue/category/mystery/index.html"


def _cards(hrefs):
    body = "".join(f'<a href="{h}"><img src="{i}.jpg"></a>'
                   for i, h in enumerate(hrefs))
    return BeautifulSoup(f"<html><body>{body}</body></html>", "html.parser")


def test_directory_index_urls_group_by_their_real_parent():
    soup = _cards(["../../sharp-objects_997/index.html",
                   "../../in-a-dark-wood_963/index.html",
                   "../../the-past-ends_942/index.html"])
    found = Frontier()._product_links(soup, BASE)
    assert len(found) == 3
    assert found[0] == "https://books.example/catalogue/sharp-objects_997/index.html"


def test_the_path_pattern_sees_past_an_index_file():
    soup = _cards(["/products/a-lamp/index.html"])
    assert Frontier()._product_links(soup, BASE) == [
        "https://books.example/products/a-lamp/index.html"]


def test_slug_urls_still_work():
    soup = _cards(["/shop/one", "/shop/two", "/shop/three"])
    assert len(Frontier()._product_links(soup, BASE)) == 3
