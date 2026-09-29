"""A field declared `number` must come back as a number."""
from decimal import Decimal

from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.selectors import SelectorExtractor
from scrapewright.schema import Schema
from scrapewright.service.app import _jsonable

HTML = """
<html><body>
  <h1 class="t">A Light in the Attic</h1>
  <p class="p">&pound;51.77</p>
  <p class="s">4.5</p>
  <p class="bad">call us</p>
</body></html>
"""


def _extract(fields, selectors):
    schema = Schema.from_names(fields)
    recipe = SelectorRecipe(fields=selectors)
    return SelectorExtractor(recipe, schema).extract_values(HTML, "https://x.test/a")


def test_a_price_becomes_a_number_and_keeps_its_currency():
    v = _extract(["title", "price:number"], {"title": ".t", "price": ".p"})
    assert v["price"] == Decimal("51.77")
    assert v["price_text"] == "£51.77"


def test_a_bare_number_needs_no_sibling():
    v = _extract(["rating:number"], {"rating": ".s"})
    assert v["rating"] == Decimal("4.5")
    assert "rating_text" not in v


def test_text_that_holds_no_number_is_left_alone():
    v = _extract(["price:number"], {"price": ".bad"})
    assert v["price"] == "call us"
    assert "price_text" not in v


def test_text_fields_are_untouched():
    v = _extract(["title"], {"title": ".t"})
    assert v["title"] == "A Light in the Attic"


def test_decimals_reach_json_as_numbers_not_strings():
    assert _jsonable(Decimal("51.77")) == 51.77
    assert _jsonable("keep") == "keep"
    assert _jsonable(None) is None


def test_jsonl_export_writes_numbers_not_quoted_strings(tmp_path):
    """`default=str` would quote a parsed number straight back into a string."""
    import json

    from scrapewright.export import write_any
    from scrapewright.models import Record

    out = write_any([Record(url="https://x.test/1",
                            data={"salary": Decimal("72000")})],
                    str(tmp_path / "o.jsonl"))
    assert json.loads(out.read_text(encoding="utf-8"))["salary"] == 72000.0
