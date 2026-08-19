from decimal import Decimal

from scrapewright.models import Product, parse_price


def test_parse_price_us_and_eu():
    assert parse_price("1,250.00") == Decimal("1250.00")   # US
    assert parse_price("1.250,00") == Decimal("1250.00")   # EU
    assert parse_price("€1290") == Decimal("1290")
    assert parse_price("1290") == Decimal("1290")
    assert parse_price(1290) == Decimal("1290")
    assert parse_price("380,50") == Decimal("380.50")      # single comma, 2 dp


def test_parse_price_junk_returns_none():
    assert parse_price("") is None
    assert parse_price(None) is None
    assert parse_price("sold out") is None


def test_product_usability():
    complete = Product(url="u", title="Coat", price="100")
    assert complete.is_usable()

    missing_price = Product(url="u", title="Coat")
    assert not missing_price.is_usable()
    assert missing_price.core_fields_present() == {"title": True, "price": False, "url": True}
