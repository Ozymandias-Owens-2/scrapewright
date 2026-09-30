"""A synthesized recipe has to pass the page it was written from.

Asked for a car's price, the model answered meta[name='description'], whose
content is "Occasion Volkswagen Taigo 1.0 TSI ..." -- confident, well formed,
and not a price. Cached, it produced that string for every car on the site.
"""
from decimal import Decimal

from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.selectors import prune_unusable
from scrapewright.schema import Schema

SCHEMA = Schema.from_names(["title", "price:number"])
PAGE = ('<html><head><meta name="description" content="Occasion Volkswagen Taigo">'
        '</head><body><h1>Taigo</h1><span class="amount">19.445</span></body></html>')
URL = "https://dealer.test/car/1"


def test_a_number_field_pointed_at_prose_is_dropped():
    recipe = SelectorRecipe(fields={"title": "h1", "price": "meta[name=description]"},
                            modes={"price": "attr:content"})
    pruned = prune_unusable(recipe, SCHEMA, PAGE, URL)
    assert "price" not in pruned.fields
    assert pruned.fields["title"] == "h1"


def test_a_number_field_pointed_at_a_number_is_kept():
    recipe = SelectorRecipe(fields={"title": "h1", "price": ".amount"})
    pruned = prune_unusable(recipe, SCHEMA, PAGE, URL)
    assert pruned.fields["price"] == ".amount"


def test_a_selector_that_matches_nothing_is_left_alone():
    """Not evidence of a bad guess -- the page may not have rendered. Throwing
    it away bought a second synthesis for a page already paid for."""
    recipe = SelectorRecipe(fields={"title": "h1", "price": ".not-here-yet"})
    pruned = prune_unusable(recipe, SCHEMA, PAGE, URL)
    assert pruned.fields["price"] == ".not-here-yet"


def test_text_fields_are_never_second_guessed():
    """Only a declared number can be checked against the page; prose is
    whatever the page says it is."""
    recipe = SelectorRecipe(fields={"title": "meta[name=description]"},
                            modes={"title": "attr:content"})
    pruned = prune_unusable(recipe, SCHEMA, PAGE, URL)
    assert "title" in pruned.fields


def test_the_callers_recipe_is_not_mutated():
    recipe = SelectorRecipe(fields={"title": "h1", "price": "meta[name=description]"},
                            modes={"price": "attr:content"})
    prune_unusable(recipe, SCHEMA, PAGE, URL)
    assert "price" in recipe.fields


def test_a_kept_number_still_parses():
    recipe = SelectorRecipe(fields={"price": ".amount"})
    pruned = prune_unusable(recipe, SCHEMA, PAGE, URL)
    from scrapewright.extract.selectors import SelectorExtractor
    values = SelectorExtractor(pruned, SCHEMA).extract_values(PAGE, URL)
    assert values["price"] == Decimal("19445")
