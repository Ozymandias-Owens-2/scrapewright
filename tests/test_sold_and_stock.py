"""Three columns that lied, from one live run of the spreadsheet.

A sold camper's price column held the word "Verkocht". An availability
column held "Anadir al carrito" -- the add-to-cart button, which says the
same thing whether the product is in stock or not. And a climbing shop
returned a price for every product on sale and nothing at all for the ones
at full price, because the recipe had been learned on a discounted page
and named the discount.
"""
from decimal import Decimal

from bs4 import BeautifulSoup

from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.selectors import (SelectorExtractor, is_cart_control,
                                            pick_stock_element, prune_unusable,
                                            widen_sale_selector,
                                            widen_sale_selectors)
from scrapewright.models import PRICE_ON_REQUEST, clean_money
from scrapewright.schema import Schema

URL = "https://shop.test/p/1"


def _values(html, fields, selectors, modes=None):
    schema = Schema.from_names(fields)
    recipe = SelectorRecipe(fields=selectors, modes=modes or {},
                            schema_name=schema.name)
    return SelectorExtractor(recipe, schema).extract_values(html, URL)


# ── 1. a word is not a price ────────────────────────────────────────────────
def test_a_sold_camper_has_no_price(camper="Verkocht"):
    """avereschcampers.nl and drentscamperhuis.nl both put the Dutch for
    "sold" where the price lives. The cell must be empty; the word is worth
    keeping beside it, because it says why."""
    v = _values(f'<p class="price">{camper}</p>',
                ["price:number"], {"price": ".price"})
    assert "price" not in v
    assert v["price_note"] == "Verkocht"


def test_a_real_price_is_untouched():
    v = _values('<p class="price">&euro; 54.900</p>',
                ["price:number"], {"price": ".price"})
    assert v["price"] == Decimal("54900")
    assert "price_note" not in v


def test_price_on_request_still_reads_as_price_on_request():
    assert clean_money("€ 0")[1] == PRICE_ON_REQUEST


def test_words_get_the_selector_thrown_away_at_compile_time():
    """The evidence survives the move into the note: a model that answered
    the meta description must still lose the field."""
    schema = Schema.from_names(["price:number"])
    recipe = SelectorRecipe(fields={"price": "meta[name='description']"},
                            modes={"price": "attr:content"},
                            schema_name=schema.name)
    html = '<meta name="description" content="Occasion Volkswagen Taigo TSI">'
    assert "price" not in prune_unusable(recipe, schema, html, URL).fields


def test_price_on_request_keeps_its_selector():
    """A page that says the price is zero has answered. Throwing the
    selector away there would buy a compile that cannot fix anything."""
    schema = Schema.from_names(["price:number"])
    recipe = SelectorRecipe(fields={"price": ".price"}, schema_name=schema.name)
    pruned = prune_unusable(recipe, schema, '<p class="price">&euro; 0</p>', URL)
    assert pruned.fields["price"] == ".price"


# ── 2. the button is not a statement of availability ────────────────────────
CART = '<button class="single_add_to_cart_button">Añadir al carrito</button>'
WAREN = '<form><input type="submit" value="In den Warenkorb"></form>'


def test_the_add_to_cart_button_is_recognised():
    for html, sel in ((CART, "button"), (WAREN, "input")):
        el = BeautifulSoup(html, "html.parser").select_one(sel)
        assert is_cart_control(el), html


def test_the_words_on_the_page_win_over_the_button():
    """One selector, two matches: the button first, the sentence second."""
    html = ('<button class="single_add_to_cart_button">'
            '<span class="x">Añadir al carrito</span></button>'
            '<p class="x">En stock</p>')
    v = _values(html, ["title", "in stock"],
                {"title": "h1", "in stock": ".x"})
    assert v["in stock"] == "En stock"


def test_a_button_and_nothing_else_leaves_the_cell_empty():
    """Empty says "the page did not tell us", which is true. The button
    says "Add to cart" on a sold-out product just as cheerfully."""
    v = _values(CART, ["title", "in stock"],
                {"title": "h1", "in stock": "button"})
    assert "in stock" not in v


def test_nothing_to_choose_from_is_still_nothing():
    assert pick_stock_element([]) is None


def test_a_cart_selector_does_not_survive_compilation():
    schema = Schema.from_names(["title", "in stock"])
    recipe = SelectorRecipe(fields={"title": "h1", "in stock": "button"},
                            schema_name=schema.name)
    pruned = prune_unusable(recipe, schema, f"<h1>A</h1>{CART}", URL)
    assert "in stock" not in pruned.fields
    assert pruned.fields["title"] == "h1"


def test_a_real_availability_selector_survives():
    schema = Schema.from_names(["title", "in stock"])
    recipe = SelectorRecipe(fields={"title": "h1", "in stock": "p.stock"},
                            schema_name=schema.name)
    html = '<h1>A</h1><p class="stock">26 vorrätig</p>'
    assert prune_unusable(recipe, schema, html, URL).fields["in stock"] == "p.stock"


# ── 3. a price selector that only exists on sale days ───────────────────────
def test_the_discount_is_taken_out_of_the_selector():
    assert widen_sale_selector("p.price ins .woocommerce-Price-amount") \
        == "p.price .woocommerce-Price-amount"
    assert widen_sale_selector(".summary ins.amount") == ".summary .amount"
    assert widen_sale_selector(".price .sale-prijs") == ".price"
    assert widen_sale_selector("p.price .amount") is None


ESCALADA_SALE = """<div class="summary">
  <p class="price"><del><span class="amount">80,00&euro;</span></del>
  <ins><span class="amount">72,00&euro;</span></ins></p></div>"""
ESCALADA_PLAIN = """<div class="summary">
  <p class="price"><span class="amount">25,00&euro;</span></p></div>"""


def test_the_narrow_selector_still_wins_on_a_sale_page():
    schema = Schema.from_names(["price:number"])
    recipe = widen_sale_selectors(
        SelectorRecipe(fields={"price": "p.price ins .amount"},
                       schema_name=schema.name), schema)
    v = SelectorExtractor(recipe, schema).extract_values(ESCALADA_SALE, URL)
    assert v["price"] == Decimal("72.00")


def test_and_the_wider_one_answers_when_there_is_no_discount():
    """escaladaymas.com: one product on offer, the next at full price, and
    the full-price one came back empty every time."""
    schema = Schema.from_names(["price:number"])
    narrow = SelectorRecipe(fields={"price": "p.price ins .amount"},
                            schema_name=schema.name)
    assert "price" not in SelectorExtractor(narrow, schema).extract_values(
        ESCALADA_PLAIN, URL)

    widened = widen_sale_selectors(narrow, schema)
    v = SelectorExtractor(widened, schema).extract_values(ESCALADA_PLAIN, URL)
    assert v["price"] == Decimal("25.00")


def test_widening_leaves_an_ordinary_selector_alone():
    schema = Schema.from_names(["price:number"])
    recipe = SelectorRecipe(fields={"price": "p.price .amount"},
                            schema_name=schema.name)
    assert widen_sale_selectors(recipe, schema).alternates == {}


# ── 4. the shop's caption is not the value ──────────────────────────────────
def test_a_label_glued_to_its_value_comes_off():
    """gharosport.net prints "Estado:" above "1 disponibles" and
    mobiledrome.nl "Op voorraad:" above "Ja". The whitespace between two
    block elements is lost when the text is read, so both arrive as one
    word, and the caption is the shop's name for a column the reader has
    already named themselves."""
    v = _values('<p class="s">Estado:1 disponibles</p>',
                ["in stock"], {"in stock": ".s"})
    assert v["in stock"] == "1 disponibles"
    v = _values('<p class="s">Op voorraad:Ja</p>',
                ["in stock"], {"in stock": ".s"})
    assert v["in stock"] == "Ja"


def test_a_colon_somebody_typed_is_left_alone():
    """"Nike: Air Max" is a name. A space after the colon is the whole
    difference between a caption and a sentence."""
    v = _values('<h1>Nike: Air Max 90</h1>', ["title"], {"title": "h1"})
    assert v["title"] == "Nike: Air Max 90"


def test_a_link_is_not_a_label():
    v = _values('<a class="l" href="https://shop.test/x">go</a>',
                ["link:url"], {"link": ".l"}, modes={"link": "attr:href"})
    assert v["link"] == "https://shop.test/x"


def test_a_paragraph_is_not_an_answer_to_is_this_in_stock():
    """A cheap model with nothing better to point at offers the nearest
    block of prose: one answered a van's availability with its WLTP
    consumption figures, another with a paragraph of the description.
    Availability is said in a few words or not at all."""
    schema = Schema.from_names(["title", "in stock"])
    recipe = SelectorRecipe(fields={"title": "h1", "in stock": ".spec"},
                            schema_name=schema.name)
    html = ('<h1>Van</h1><p class="spec">Gemiddeld elektriciteitsverbruik '
            '(WLTP): 20,3 kWh/100km. CO2-uitstoot (WLTP): 0 g/km. EU '
            'verantwoordelijke: Mercedes-Benz Nederland B.V.</p>')
    assert "in stock" not in prune_unusable(recipe, schema, html, URL).fields


def test_a_short_answer_is_still_an_answer():
    schema = Schema.from_names(["title", "in stock"])
    recipe = SelectorRecipe(fields={"title": "h1", "in stock": ".s"},
                            schema_name=schema.name)
    for said in ("En stock", "26 vorrätig", "Nog 3 op voorraad, morgen in huis"):
        html = f'<h1>A</h1><p class="s">{said}</p>'
        assert prune_unusable(recipe, schema, html, URL).fields["in stock"] == ".s", said


# ── 5. a sold camper is an answer, not a broken recipe ─────────────────────
def test_a_sold_camper_does_not_buy_a_compilation_every_morning():
    """The most expensive line in the whole ledger.

    avereschcampers.nl, one sold camper, one spreadsheet row: a credit a
    refresh until the note was introduced, 306 a refresh afterwards -- a
    render and a compilation, bought again and again to be told the same
    thing, because an empty price read as a recipe that needed healing.
    """
    schema = Schema.from_names(["price", "in stock"])
    recipe = SelectorRecipe(fields={"price": ".price", "in stock": ".stock"},
                            schema_name=schema.name)
    html = '<p class="price">Verkocht</p><p class="stock">Verkocht</p>'
    values = SelectorExtractor(recipe, schema).extract_values(html, URL)

    assert "price" not in values            # the word never stands in for a number
    assert values["price_note"] == "Verkocht"
    assert schema.is_satisfied_by(values)   # and the page has been read


def test_a_selector_that_only_ever_finds_words_still_goes():
    """Which is what makes the note above worth trusting: the evidence is
    collected once, when the recipe is written."""
    schema = Schema.from_names(["price", "in stock"])
    recipe = SelectorRecipe(fields={"price": "meta[name='description']"},
                            modes={"price": "attr:content"},
                            schema_name=schema.name)
    html = '<meta name="description" content="Occasion Volkswagen Taigo TSI">'
    assert "price" not in prune_unusable(recipe, schema, html, URL).fields


def test_an_unanswered_field_is_still_unanswered():
    schema = Schema.from_names(["price", "in stock"])
    assert not schema.is_satisfied_by({"in stock": "En stock"})
