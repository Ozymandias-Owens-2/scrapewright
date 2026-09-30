"""An account nobody has paid for must cost us nothing worth farming.

The two things that cost real money the moment they run are compiling a new
site (model tokens) and rendering in a browser (our CPU). An email address is
free to invent, so those are what the free tier limits.
"""
import pytest
from fastapi.testclient import TestClient

from scrapewright.service.app import (DISPOSABLE_EMAIL_DOMAINS, _signup_network,
                                      create_app)
from scrapewright.service.jobs import JobRegistry
from scrapewright.service.plans import get_tier
from scrapewright.service.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "s.db"))


def test_a_new_key_is_on_the_free_tier(store):
    _, key = store.create_key(label="new", plan="metered")
    assert not store.has_been_topped_up(key.id)


def test_paying_moves_the_key_off_it(store):
    _, key = store.create_key(label="payer", plan="metered")
    store.grant(key.id, 10_000, "pack", idempotency_key="stripe:cs_live_1")
    assert store.has_been_topped_up(key.id)


def test_a_gift_counts_as_well(store):
    """A key handed out by the operator must not be stuck on the free tier."""
    _, key = store.create_key(label="friend", plan="metered")
    store.grant(key.id, 10_000, "gift", idempotency_key="gift-friend-1")
    assert store.has_been_topped_up(key.id)


def test_the_monthly_allowance_does_not_count(store):
    """It is the thing the free tier already gets; it cannot promote anyone."""
    _, key = store.create_key(label="free", plan="metered")
    store.ensure_free_allowance(key.id, 1_000)
    assert not store.has_been_topped_up(key.id)


def test_the_free_tier_limits_what_costs_us_money():
    free, paid = get_tier("free"), get_tier("metered")
    assert free.daily_syntheses < paid.daily_syntheses
    assert free.daily_renders < paid.daily_renders
    assert free.max_items_per_job < paid.max_items_per_job


def test_rendering_is_refused_once_the_daily_count_is_spent(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    raw, key = store.create_key(label="free", plan="metered")
    store.record(key.id, renders=get_tier("free").daily_renders)

    with TestClient(create_app(store=store, jobs=JobRegistry())) as c:
        r = c.post("/v1/extract", headers={"X-API-Key": raw},
                   json={"url": "https://x.test/p", "js": True})
        assert r.status_code == 429
        assert "rendering" in r.json()["detail"]


def test_a_paid_key_renders_past_that(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    raw, key = store.create_key(label="payer", plan="metered")
    store.grant(key.id, 10_000, "pack", idempotency_key="stripe:cs_live_1")
    store.record(key.id, renders=get_tier("free").daily_renders + 5)

    with TestClient(create_app(store=store, jobs=JobRegistry())) as c:
        r = c.post("/v1/extract", headers={"X-API-Key": raw},
                   json={"url": "https://x.test/p", "js": True})
        assert r.status_code != 429


def test_signups_are_counted_per_network_not_per_address():
    """One host is routinely handed a whole IPv6 /64, so counting exact
    addresses counts nothing."""
    assert _signup_network("203.0.113.7") == _signup_network("203.0.113.200")
    assert _signup_network("2a09:8280:1::1") == _signup_network("2a09:8280:1::ffff")
    assert _signup_network("203.0.113.7") != _signup_network("203.0.114.7")


def test_a_throwaway_address_is_turned_away(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    with TestClient(create_app(store=store, jobs=JobRegistry())) as c:
        r = c.post("/v1/signup", json={"email": "x@mailinator.com"})
        assert r.status_code == 400
        assert "disposable" in r.json()["detail"]
        assert c.post("/v1/signup", json={"email": "x@example.com"}).status_code == 201


def test_the_blocklist_is_lowercase_and_bare_domains():
    for domain in DISPOSABLE_EMAIL_DOMAINS:
        assert domain == domain.lower() and "@" not in domain
