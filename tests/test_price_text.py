"""A price with the page's furniture stuck to it.

Forty pages across thirty-two shops produced three ways to return a number
that is not a number: a WooCommerce sale wrapper read as "10€7,20€", a
label glued to the front ("Precio de oferta€37,00") or a call to action to
the back ("€ 99.950Financieren?"), and "€ 0" standing in for "ask us".

All three store something a price watch will compare against yesterday and
report as a change.
"""
from decimal import Decimal

from bs4 import BeautifulSoup

from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.selectors import SelectorExtractor, current_price
from scrapewright.models import PRICE_ON_REQUEST, clean_money
from scrapewright.schema import PRODUCT_SCHEMA, Schema

URL = "https://shop.test/p/1"


def _value(html, selector=".price", field="price:number"):
    schema = Schema.from_names(["title", field])
    recipe = SelectorRecipe(fields={"title": "h1", field.split(":")[0]: selector},
                            schema_name=schema.name)
    return SelectorExtractor(recipe, schema).extract_values(html, URL)


# ── 1. <del>/<ins>: the amounts are too small to count ───────────────────────
def test_woocommerce_sale_wrapper():
    """cosmos-cafe.es: `.price` holds <del>10€</del><ins>7,20€</ins>, and the
    rule that counted amounts needed a floor of 100 to survive "incl. 21% btw"
    -- so a 10 euro bag of coffee sailed straight through it."""
    el = BeautifulSoup('<p class="price"><del><bdi>10<span>€</span></bdi></del>'
                       '<ins><bdi>7,20<span>€</span></bdi></ins></p>',
                       "html.parser").select_one("p.price")
    assert current_price(el) == "7,20€"


def test_a_small_price_with_no_discount_is_untouched():
    el = BeautifulSoup('<p class="price">7,20€</p>', "html.parser").select_one("p")
    assert current_price(el) == "7,20€"


# ── 2. labels, tails, tax notes ──────────────────────────────────────────────
def test_the_labels_and_tails_seen_in_the_wild():
    cases = {
        "Precio de oferta€37,00": "€37,00",        # sanjorge.cafe
        "Angebot17,90€": "17,90€",                 # diekaffeerei.com
        "Kopen voor € 31.000,-": "€ 31.000,-",     # huiskes-kokkeler.nl
        "Onze prijs:€ 54.900": "€ 54.900",         # ronhazenberg.nl
        "€ 99.950Financieren?": "€ 99.950",        # eurotrek.com
    }
    for raw, expected in cases.items():
        assert clean_money(raw) == (expected, None), raw


def test_a_tax_note_is_kept_beside_the_price_not_inside_it():
    """wensink.nl: "€ 31.850,-excl. BTW". Dropping the marker would say the
    van costs 31.850 to a buyer who will be charged 38.539."""
    assert clean_money("€ 31.850,-excl. BTW") == ("€ 31.850,-", "excl. BTW")
    assert clean_money("1.234,56 incl. 21% btw") == ("1.234,56", "incl. 21% btw")
    assert clean_money("18.900 zzgl. MwSt.") == ("18.900", "zzgl. MwSt.")


def test_the_note_reaches_the_record():
    values = _value('<h1>Van</h1><p class="price">€ 31.850,-excl. BTW</p>')
    assert values["price"] == 31850
    assert values["price_note"] == "excl. BTW"
    assert values["price_text"] == "€ 31.850,-"


def test_the_model_name_is_not_the_price():
    """"Audi A4 2.0 TFSI €24.950" -- the first digits on the line belong to
    the car, not to the money."""
    assert clean_money("Audi A4 2.0 TFSI €24.950")[0] == "€24.950"


def test_a_price_followed_by_a_year_stays_a_price():
    assert clean_money("€ 5 950 2026")[0] == "€ 5 950"


def test_text_with_no_number_comes_back_whole():
    """It is a selector pointing at the wrong element, and that is what gets
    the selector thrown away. Hiding it hides the evidence."""
    assert clean_money("Op aanvraag") == ("Op aanvraag", None)


# ── 3. the zero placeholder ──────────────────────────────────────────────────
def test_zero_means_ask_us():
    """morelo.nl prints "€ 0", rs-reisemobile.de "€ 0,00". Nobody sells a
    camper for nothing."""
    for raw in ("€ 0", "€ 0,00", "0,00 €"):
        assert clean_money(raw) == (None, PRICE_ON_REQUEST), raw


def test_the_zero_leaves_the_cell_empty():
    values = _value('<h1>Camper</h1><p class="price">€ 0</p>')
    assert "price" not in values
    assert "price_text" not in values
    assert values["price_note"] == PRICE_ON_REQUEST


def test_a_price_on_request_page_is_not_a_broken_recipe():
    """Otherwise every one of them buys a render and a synthesis to fix a
    page that is not wrong."""
    assert PRODUCT_SCHEMA.is_satisfied_by(
        {"title": "Camper", "price_note": PRICE_ON_REQUEST})
    assert not PRODUCT_SCHEMA.is_satisfied_by({"title": "Camper"})


# ── rows mode gets the same treatment ────────────────────────────────────────
def test_a_discounted_card_on_a_listing():
    """The listing path read its values somewhere else entirely, so the sale
    fix never applied to a single card."""
    schema = Schema.from_names(["title", "price:number"])
    recipe = SelectorRecipe(fields={"title": "h2", "price": ".price"},
                            item="li", schema_name=schema.name)
    rows = SelectorExtractor(recipe, schema).extract_rows(
        '<ul><li><h2>Coffee</h2><p class="price">'
        '<del>10€</del><ins>7,20€</ins></p></li>'
        '<li><h2>Beans</h2><p class="price">Onze prijs:€ 12,00</p></li></ul>',
        URL)
    assert [r.data["price"] for r in rows] == [Decimal("7.20"), Decimal("12.00")]
