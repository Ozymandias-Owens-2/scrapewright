"""Giving someone back the credits they paid for.

A key exists only as a hash, so a customer who loses theirs cannot be given
it back. Their credits are not theirs to lose, though. Rehearsing this by
hand over ssh took five statements and one of them was wrong, which is not a
thing to find out while somebody waits on their balance.
"""
import pytest

from scrapewright.cli import main
from scrapewright.service.store import Store

EMAIL = "drill@example.test"


@pytest.fixture()
def paid_customer(tmp_path):
    db = tmp_path / "s.db"
    store = Store(str(db))
    _, key = store.create_key(label="customer", plan="metered", email=EMAIL)
    store.ensure_free_allowance(key.id, 1_000)
    store.grant(key.id, 10_000, "starter pack", idempotency_key="stripe:cs_1")
    store.spend(key.id, 2_500, "a crawl")
    return str(db), store, key


def test_the_balance_moves_to_a_new_key(paid_customer, capsys):
    db, store, old = paid_customer
    before = store.balance(old.id)

    assert main(["keys", "recover", EMAIL, "--db", db]) == 0
    out = capsys.readouterr().out

    new_id = store.find_key_by_email(EMAIL)
    assert new_id != old.id
    assert store.balance(new_id) == before
    assert store.balance(old.id) == 0
    assert new_id in out and "sw_" in out          # the key is shown once


def test_the_old_key_stops_working(paid_customer):
    db, store, old = paid_customer
    main(["keys", "recover", EMAIL, "--db", db])
    assert not any(k.id == old.id and k.active for k in store.list_keys())


def test_the_replacement_keeps_the_paying_tier(paid_customer):
    """Moving a balance must not drop someone onto the free tier's limits."""
    db, store, _ = paid_customer
    main(["keys", "recover", EMAIL, "--db", db])
    assert store.has_been_topped_up(store.find_key_by_email(EMAIL))


def test_a_dry_run_moves_nothing(paid_customer, capsys):
    db, store, old = paid_customer
    before = store.balance(old.id)

    assert main(["keys", "recover", EMAIL, "--dry-run", "--db", db]) == 0
    assert "8,500" in capsys.readouterr().out
    assert store.balance(old.id) == before
    assert store.find_key_by_email(EMAIL) == old.id


def test_an_unknown_address_says_why_it_cannot_search(paid_customer, capsys):
    db, _, _ = paid_customer
    assert main(["keys", "recover", "typo@example.test", "--db", db]) == 1
    assert "hash" in capsys.readouterr().err


def test_the_move_is_written_in_both_ledgers(paid_customer):
    """Whoever reads this account later should see where the money went."""
    db, store, old = paid_customer
    main(["keys", "recover", EMAIL, "--db", db])
    new_id = store.find_key_by_email(EMAIL)

    assert any("key lost" in row["reason"] for row in store.ledger(old.id))
    assert any(old.id in row["reason"] for row in store.ledger(new_id))
