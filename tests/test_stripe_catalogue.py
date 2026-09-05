"""Charging against Stripe's product catalogue instead of one-off prices.

Inline prices work, but they mint a nameless product per purchase, so Stripe's
own catalogue stays empty and its reporting can never say which pack sells. The
catch is that a catalogue price is an amount somebody can edit in a dashboard,
which is exactly the thing this integration otherwise refuses to trust.
"""

import pytest

from scrapewright.service.credits import PACKS, PACKS_BY_NAME
from scrapewright.service.store import Store
from scrapewright.service.stripe_billing import (
    StripeBilling,
    StripeConfigError,
    price_lookup_key,
)


class FakeCatalogue:
    """A Stripe stand-in with a product catalogue that can be tampered with."""

    def __init__(self, prices=None, list_raises=False):
        self.prices = dict(prices or {})       # lookup_key -> {id, unit_amount}
        self.products, self.created_sessions = [], []
        self.list_raises = list_raises
        self.api_key = None
        outer = self

        class Price:
            @staticmethod
            def list(lookup_keys, **kw):
                if outer.list_raises:
                    raise RuntimeError("stripe is having a day")
                hits = [p for k, p in outer.prices.items() if k in lookup_keys]
                return {"data": hits}

            @staticmethod
            def create(**kw):
                row = {"id": f"price_{len(outer.prices)}",
                       "unit_amount": kw["unit_amount"], **kw}
                outer.prices[kw["lookup_key"]] = row
                return row

        class Product:
            @staticmethod
            def create(**kw):
                row = {"id": f"prod_{len(outer.products)}", **kw}
                outer.products.append(row)
                return row

        class Session:
            @staticmethod
            def create(**kw):
                outer.created_sessions.append(kw)
                return type("S", (), {"id": "cs_1",
                                      "url": "https://checkout.stripe.com/x"})()

        self.Price, self.Product = Price, Product
        self.checkout = type("C", (), {"Session": Session})()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "cat.db")


@pytest.fixture
def key(store):
    return store.create_key(label="alice", plan="metered", email="a@b.co")[1]


def billing(stripe):
    return StripeBilling(secret_key="sk_test_x", webhook_secret="w", stripe=stripe)


def test_sync_fills_an_empty_catalogue(store):
    fake = FakeCatalogue()

    results = billing(fake).sync_products()

    assert len(results) == len(PACKS)
    assert all(r["created"] for r in results)
    assert len(fake.products) == len(PACKS)
    # Every pack is priced exactly as our own list says.
    for pack in PACKS:
        assert fake.prices[price_lookup_key(pack)]["unit_amount"] == pack.price_usd * 100


def test_syncing_twice_creates_nothing(store):
    fake = FakeCatalogue()
    b = billing(fake)

    b.sync_products()
    before = len(fake.products)
    second = b.sync_products()

    assert len(fake.products) == before
    assert not any(r["created"] for r in second)


def test_products_carry_the_tax_code(store):
    """Managed Payments rejects a line item that will not say what it is."""
    fake = FakeCatalogue()

    billing(fake).sync_products()

    assert all(p["tax_code"].startswith("txcd_") for p in fake.products)


def test_checkout_uses_the_catalogue_price(store, key):
    fake = FakeCatalogue()
    b = billing(fake)
    b.sync_products()

    b.checkout_session(key, PACKS_BY_NAME["starter"])

    item = fake.created_sessions[-1]["line_items"][0]
    assert "price" in item and "price_data" not in item


def test_a_tampered_catalogue_price_is_ignored(store, key):
    """Someone edits the price in the dashboard. The customer pays ours."""
    pack = PACKS_BY_NAME["starter"]
    fake = FakeCatalogue({price_lookup_key(pack): {"id": "price_evil",
                                                   "unit_amount": 1}})

    billing(fake).checkout_session(key, pack)

    item = fake.created_sessions[-1]["line_items"][0]
    assert "price" not in item
    assert item["price_data"]["unit_amount"] == pack.price_usd * 100


def test_a_failing_price_lookup_does_not_block_a_sale(store, key):
    """Stripe wobbling must not stop someone giving us money."""
    fake = FakeCatalogue(list_raises=True)

    out = billing(fake).checkout_session(key, PACKS_BY_NAME["starter"])

    item = fake.created_sessions[-1]["line_items"][0]
    assert item["price_data"]["unit_amount"] == 10 * 100
    assert out["checkout_url"].startswith("https://checkout.stripe.com/")


def test_syncing_without_a_key_is_refused(store):
    with pytest.raises(StripeConfigError):
        StripeBilling(secret_key=None, webhook_secret="w",
                      stripe=FakeCatalogue()).sync_products()


def test_the_lookup_key_changes_when_the_price_does():
    """Stripe prices are immutable, so a price change must mint a new one."""
    pack = PACKS_BY_NAME["starter"]
    cheaper = type(pack)(**{**pack.__dict__, "price_usd": 5})

    assert price_lookup_key(pack) != price_lookup_key(cheaper)
