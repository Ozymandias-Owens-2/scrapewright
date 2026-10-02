"""Two prices in one element, and which of them the customer would pay.

fietshokje.nl learned its recipe on a product with no discount, where the
selector returned "2,099,-". On a discounted product the same selector
returns "2,999,-1,899,-" -- the old price and the new one, read as one
string. A wrong number is worse than an empty cell: somebody watching
prices will not notice it.
"""
from bs4 import BeautifulSoup

from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.selectors import (SelectorExtractor, current_price,
                                            is_money_field)
from scrapewright.schema import Schema

URL = "https://shop.test/p/1"


def _price(html, selector="p.price"):
    return current_price(BeautifulSoup(html, "html.parser").select_one(selector))


def test_the_dutch_markup_that_started_this():
    """No <del> anywhere: the shop writes .normale-prijs beside .sale-prijs."""
    assert _price('<p class="price"><div class="normale-prijs">2,999<span>,</span>-'
                  '</div> <div class="sale-prijs">1,899<span>,</span>-</div></p>') \
        == "1,899,-"


def test_struck_through_markup():
    assert _price('<p class="price"><del>2.999,-</del><ins>1.899,-</ins></p>') \
        == "1.899,-"


def test_a_single_price_is_left_exactly_as_it_was():
    assert _price('<p class="price">2,099,-</p>') == "2,099,-"


def test_two_prices_with_nothing_to_tell_them_apart_give_nothing():
    """Guessing which is current would be a wrong number, and a wrong number
    is the thing this exists to prevent."""
    assert _price('<p class="price"><span>2.999,-</span>'
                  '<span>1.899,-</span></p>') is None


def test_a_price_beside_a_tax_rate_is_still_one_price():
    assert _price('<p class="price">1.234,56 incl. 21% btw</p>') \
        == "1.234,56 incl. 21% btw"


# ── which fields this applies to ─────────────────────────────────────────────
def test_a_field_called_price_counts_as_money_without_a_kind():
    """The caller asked for ["price", "in stock"] -- no `:number` anywhere --
    and the check that would have caught this only looked at number fields."""
    for name in ("price", "Price", "prijs", "preis", "cost", "bedrag"):
        assert is_money_field(Schema.from_names([name]).fields[0]), name


def test_an_ordinary_text_field_is_left_alone():
    for name in ("title", "in stock", "description"):
        assert not is_money_field(Schema.from_names([name]).fields[0]), name


# ── end to end through the extractor ─────────────────────────────────────────
SALE = ('<html><body><div class="wrap"><p class="price">'
        '<div class="normale-prijs">2,999,-</div>'
        '<div class="sale-prijs">1,899,-</div></p></div></body></html>')
PLAIN = ('<html><body><div class="wrap"><p class="price">2,099,-</p>'
         '</div></body></html>')
RECIPE = SelectorRecipe(fields={"Price": "p.price"})
SCHEMA = Schema.from_names(["Price"])


def test_the_recipe_learned_on_a_plain_page_still_works_on_a_sale_page():
    values = SelectorExtractor(RECIPE, SCHEMA).extract_values(SALE, URL)
    assert values["Price"] == "1,899,-"


def test_and_still_works_on_the_page_it_learned_from():
    values = SelectorExtractor(RECIPE, SCHEMA).extract_values(PLAIN, URL)
    assert values["Price"] == "2,099,-"


def test_an_unresolvable_pair_leaves_the_field_empty_for_the_mender():
    ambiguous = ('<html><body><p class="price"><span>2.999,-</span>'
                 '<span>1.899,-</span></p></body></html>')
    assert "Price" not in SelectorExtractor(RECIPE, SCHEMA).extract_values(
        ambiguous, URL)
