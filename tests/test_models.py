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


def test_a_three_digit_tail_is_a_thousands_group_not_a_fraction():
    """A Dutch car listing writes 5950 euro as "€ 5.950"; reading that dot as
    a decimal point priced a real car at 5.95."""
    assert parse_price("€ 5.950") == Decimal("5950")
    assert parse_price("€ 129.900") == Decimal("129900")
    assert parse_price("€1.234.567") == Decimal("1234567")
    assert parse_price("1,234") == Decimal("1234")


def test_other_tails_stay_fractions():
    assert parse_price("47.82") == Decimal("47.82")
    assert parse_price("0.5") == Decimal("0.5")
    assert parse_price("1234.5678") == Decimal("1234.5678")   # too long to group
