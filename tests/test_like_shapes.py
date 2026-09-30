"""Two shapes that beat the first version, found on real dealer stock.

Both were reported from the field after by-example started working on five
of seven sites.
"""
import pytest

from scrapewright.like import _kin_listings, find_similar, siblings_on, url_template

WEST = ("https://west.test/Audi/A3-Sportback/2.0-TFSI-S3-quattro-5134013"
        "/2028/1/1/details.aspx?zoek=&so=gallerij")


def _car(brand, model, trim="trim", year="2028"):
    return f"https://west.test/{brand}/{model}/{trim}/{year}/1/1/details.aspx"


@pytest.fixture(autouse=True)
def no_sitemap(monkeypatch):
    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: [])
    monkeypatch.setattr("scrapewright.like._text_of", lambda url, session=None: None)


# ── the leading segments are the identity, not the route ────────────────────
def test_the_template_is_what_most_links_agree_on():
    """Two other Audis score higher than the rest -- they share the brand --
    and taking the top score alone kept only those two, leaving thirteen
    cars of other makes behind."""
    links = [_car("Audi", "A4"), _car("Audi", "A6"),
             _car("BMW", "X5"), _car("Kia", "Niro"),
             _car("VW", "Golf"), _car("Opel", "Corsa")]
    found = siblings_on(links, WEST, url_template(WEST), min_group=2)
    assert len(found) == 6
    assert any("/BMW/" in u for u in found)


def test_something_off_the_template_is_still_excluded():
    links = [_car("Audi", "A4"), _car("BMW", "X5"), _car("Kia", "Niro"),
             "https://west.test/over/ons/team/jan/foto/1/bio.aspx"]
    found = siblings_on(links, WEST, url_template(WEST), min_group=2)
    assert len(found) == 3
    assert not any("/over/" in u for u in found)


def test_depth_still_rules_out_a_subcategory():
    links = [_car("Audi", "A4"), _car("BMW", "X5"), _car("Kia", "Niro"),
             "https://west.test/Audi/A3-Sportback"]
    assert len(siblings_on(links, WEST, url_template(WEST), min_group=2)) == 3


def test_a_whole_stock_page_of_mixed_makes_is_read():
    listing = "https://west.test/occasions"
    cars = [_car(b, m) for b, m in
            [("Audi", "A4"), ("BMW", "X5"), ("Kia", "Niro"), ("VW", "Golf"),
             ("Opel", "Corsa"), ("Ford", "Focus"), ("Seat", "Ibiza")]]
    body = "<html><body>" + "".join(f'<a href="{c}">c</a>' for c in cars) + \
           '<a href="/contact">contact</a></body></html>'

    class _Web:
        def fetch(self, url):
            return body if url == listing else "<html><body></body></html>"

    assert len(find_similar(WEST, limit=50, fetcher=_Web(),
                            listing_url=listing)) == 7


# ── singular page, plural list ───────────────────────────────────────────────
CAMPER = "https://camp.test/camper/12630/"


def test_a_plural_sibling_of_the_first_segment_is_a_candidate():
    """/camper/12630/ has its stock at /campers/ -- no ancestor ever reaches
    it, and the site was found only when told where to look."""
    html = ('<a href="/campers">Campers</a><a href="/camper/999/x">a camper</a>'
            '<a href="/contact">contact</a>')
    assert _kin_listings(html, CAMPER) == ["https://camp.test/campers"]


def test_a_deeper_link_is_not_mistaken_for_the_list():
    html = '<a href="/camper/12630/similar">similar</a>'
    assert _kin_listings(html, CAMPER) == []


def test_the_plural_page_is_walked_before_the_ancestors():
    example = ('<html><body><a href="/campers">Campers</a></body></html>')
    # The real site writes /camper/hymer-b524/ -- the same depth as the
    # example, which is what makes them siblings at all.
    stock = ("<html><body>" + "".join(
        f'<a href="/camper/model-{i}/">c</a>' for i in range(4)) +
        "</body></html>")

    class _Web:
        def __init__(self): self.asked = []
        def fetch(self, url):
            self.asked.append(url)
            if url == CAMPER:
                return example
            if url == "https://camp.test/campers":
                return stock
            return "<html><body></body></html>"

    web = _Web()
    found = find_similar(CAMPER, limit=50, fetcher=web)
    assert len(found) == 4
    assert "https://camp.test/campers" in web.asked
