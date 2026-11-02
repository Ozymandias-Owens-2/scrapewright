"""Two ways a page answers without the model being asked.

Both were found by looking at why four live URLs in the production cache had
been written off as unreadable.
"""

import pytest

from scrapewright.cache import RecipeCache
from scrapewright.fetch import ItemSold, sold_marker
from scrapewright.pipeline import Scrapewright
from scrapewright.schema import Schema

from test_pipeline import _FakeLLM, _FakeSession

JSONLD_PAGE = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Flow Cosmetics Balm to Milk Cleanser",
 "offers":{"@type":"Offer","price":"14.76","priceCurrency":"EUR",
           "availability":"https://schema.org/InStock"}}
</script></head><body><div class="woo">Balm to Milk</div></body></html>
"""


def _sw(tmp_path, html, llm_recipe=None):
    """A pipeline with no browser and an LLM that teaches nothing."""
    return Scrapewright(
        cache=RecipeCache(tmp_path / "r.json"),
        llm=_FakeLLM(llm_recipe),
        session=_FakeSession(html),
    )


def test_custom_schema_reads_jsonld_without_paying_the_model(tmp_path):
    """The add-on never sends the "product" schema -- it sends the sheet's
    column headers. Gating the free JSON-LD hop on the schema's *name* meant
    every row bought synthesis for a price the page had already declared."""
    sw = _sw(tmp_path, JSONLD_PAGE)
    schema = Schema.from_names(["price"])
    assert schema.name.startswith("custom-")

    record = sw.extract("https://greenest.example.com/balm-to-milk/", schema)

    assert record is not None
    assert str(record.data["price"]) == "14.76"
    assert sw.llm.calls == 0, "the price was declared; the model was not needed"


def test_jsonld_hop_answers_only_what_the_sheet_asked_for(tmp_path):
    sw = _sw(tmp_path, JSONLD_PAGE)
    record = sw.extract("https://greenest.example.com/balm-to-milk/",
                        Schema.from_names(["price"]))
    assert set(record.data) == {"price"}


SOLD_PAGE = """
<html><body><h1>Adria Compact DL 60 years</h1>
<div class="status">Verkocht</div>
<p>Bouwjaar 2026, lengte 699 cm.</p></body></html>
"""

NAV_PAGE = """
<html><body><nav><a href="/verkochte-campers">Verkochte campers</a></nav>
<h1>Knaus</h1><div class="priceTo"><strong>79995</strong></div></body></html>
"""

SCRIPTED_PAGE = """
<html><body><h1>Balm to Milk</h1>
<script>var labels = {outOfStock: "verkocht"};</script>
<p>Nog op voorraad.</p></body></html>
"""


def test_a_sold_listing_says_sold_instead_of_nothing_found(tmp_path):
    sw = _sw(tmp_path, SOLD_PAGE)
    with pytest.raises(ItemSold) as caught:
        sw.extract("https://dealer.example.com/adria-1007573/",
                   Schema.from_names(["price"]))
    assert caught.value.marker == "verkocht"


def test_a_menu_link_is_not_a_sold_listing():
    # "Verkochte campers" in a navigation menu sits on every page of a
    # dealer's site, including the ones still for sale.
    assert sold_marker(NAV_PAGE) is None


def test_a_word_inside_a_script_is_not_a_sold_listing():
    assert sold_marker(SCRIPTED_PAGE) is None


def test_an_unreadable_page_that_says_nothing_still_reads_as_nothing(tmp_path):
    """Without a sold marker the old answer stands: no exception, no record.
    "Sold" must not become the excuse for every page we cannot read."""
    sw = _sw(tmp_path, "<html><body><h1>Knaus</h1></body></html>")
    record = sw.extract("https://dealer.example.com/knaus/",
                        Schema.from_names(["price"]))
    assert record is None or not record.data.get("price")
