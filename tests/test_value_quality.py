"""Values a human would not call values.

All three came off real shops during a day of live use for the Sheets
add-on, across thirty small Dutch stores.
"""
from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.selectors import (SelectorExtractor, holds_two_prices,
                                            is_plumbing, prune_unusable)
from scrapewright.schema import Schema

URL = "https://shop.test/p/1"


# ── class names are not content ──────────────────────────────────────────────
ELEMENTOR = ('<html><body><div class="elementor elementor-799 elementor-page">'
             '<span class="stock">Op voorraad</span></div></body></html>')
STOCK_SCHEMA = Schema.from_names(["in_stock"])


def test_a_mode_reading_class_or_id_is_plumbing():
    assert is_plumbing("attr:class") and is_plumbing("attr:id")
    assert not is_plumbing("text") and not is_plumbing("attr:content")


def test_a_field_pointed_at_the_class_attribute_comes_back_empty():
    """It came back as ['elementor', 'elementor-799', ...] on every Elementor
    site -- confident nonsense, repeated for every product."""
    recipe = SelectorRecipe(fields={"in_stock": "div"},
                            modes={"in_stock": "attr:class"})
    assert SelectorExtractor(recipe, STOCK_SCHEMA).extract_values(ELEMENTOR, URL) == {}


def test_such_a_selector_never_reaches_the_cache():
    recipe = SelectorRecipe(fields={"in_stock": "div"},
                            modes={"in_stock": "attr:class"})
    assert prune_unusable(recipe, STOCK_SCHEMA, ELEMENTOR, URL).fields == {}


def test_a_real_stock_line_is_kept():
    recipe = SelectorRecipe(fields={"in_stock": ".stock"})
    values = SelectorExtractor(recipe, STOCK_SCHEMA).extract_values(ELEMENTOR, URL)
    assert values["in_stock"] == "Op voorraad"


# ── two prices in one string are not a price ─────────────────────────────────
SALE = ('<html><body><div class="prices"><del>2.999,-</del><ins>1.899,-</ins></div>'
        '<span class="now">1.899,-</span></body></html>')
PRICE_SCHEMA = Schema.from_names(["price:number"])


def test_the_old_and_the_sale_price_glued_together_are_rejected():
    assert holds_two_prices("2,999,-1,899,-")
    assert holds_two_prices("€ 2.999 € 1.899")


def test_one_price_beside_a_tax_rate_is_still_one_price():
    assert not holds_two_prices("1.234,56 incl. 21% btw")
    assert not holds_two_prices("€ 1.899,-")


def test_a_container_holding_both_prices_yields_nothing():
    recipe = SelectorRecipe(fields={"price": ".prices"})
    assert SelectorExtractor(recipe, PRICE_SCHEMA).extract_values(SALE, URL) == {}


def test_the_sale_price_on_its_own_is_taken():
    recipe = SelectorRecipe(fields={"price": ".now"})
    values = SelectorExtractor(recipe, PRICE_SCHEMA).extract_values(SALE, URL)
    assert str(values["price"]) == "1899"


def test_only_number_fields_are_checked_this_way():
    """A description may mention two prices and still be a description."""
    schema = Schema.from_names(["blurb"])
    recipe = SelectorRecipe(fields={"blurb": ".prices"})
    values = SelectorExtractor(recipe, schema).extract_values(SALE, URL)
    assert values["blurb"]


# ── what the prompt now says ─────────────────────────────────────────────────
def test_the_prompt_warns_about_all_three():
    from scrapewright.extract.llm import _PROMPT, _ROWS_PROMPT

    for prompt in (_PROMPT, _ROWS_PROMPT):
        assert "current selling price" in prompt
        assert "add-to-cart button is not" in prompt
        assert "`class`, `id`, `style` or `role`" in prompt
