"""Rebuilding the ledger from Stripe.

The ledger lives on one volume, and its snapshot can be up to a day behind --
measured, not guessed: a real restore on 2026-08-31 came back missing a payment
made 30 minutes earlier. Stripe is the second, more durable record of the same
money, so the ledger can be rebuilt from it.

Everything below is about that rebuild being safe to run: never twice, never
into an account that no longer exists, never on someone else's payment.
"""

import pytest

from scrapewright.service.credits import PACKS_BY_NAME
from scrapewright.service.store import Store
from scrapewright.service.stripe_billing import StripeBilling, StripeConfigError


def session(session_id: str, key_id: str, pack: str = "starter",
            paid: bool = True, metadata: dict | None = None) -> dict:
    meta = {"key_id": key_id, "pack": pack} if metadata is None else metadata
    return {"id": session_id, "object": "checkout.session",
            "payment_status": "paid" if paid else "unpaid",
            "metadata": meta}


class FakeSessions:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def list(self, **params):
        self.calls.append(params)
        rows = self.rows
        return {"data": rows}


class FakeStripe:
    def __init__(self, rows):
        self.checkout = type("C", (), {"Session": FakeSessions(rows)})()
        self.api_key = None


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "reconcile.db")


def billing_for(rows):
    return StripeBilling(secret_key="sk_test_x", webhook_secret="whsec_x",
                         stripe=FakeStripe(rows))


def test_a_missing_payment_is_restored(store):
    """The whole point: the money is in Stripe, so the credits come back."""
    _, key = store.create_key(label="alice", plan="metered")
    billing = billing_for([session("cs_1", key.id)])

    result = billing.reconcile(store)

    assert len(result.applied) == 1
    assert store.balance(key.id) == PACKS_BY_NAME["starter"].credits


def test_running_it_twice_does_not_double_credit(store):
    """Safe to run whenever you are unsure -- which is when it gets run."""
    _, key = store.create_key(label="alice", plan="metered")
    billing = billing_for([session("cs_1", key.id)])

    billing.reconcile(store)
    after_first = store.balance(key.id)
    second = billing.reconcile(store)

    assert second.applied == []
    assert len(second.already) == 1
    assert store.balance(key.id) == after_first


def test_it_does_not_undo_what_the_webhook_already_did(store):
    """The webhook and reconciliation key on the same session id, so the two
    can never disagree about whether one payment was credited."""
    _, key = store.create_key(label="alice", plan="metered")
    pack = PACKS_BY_NAME["starter"]
    store.grant(key.id, pack.credits, f"stripe: {pack.name} pack (${pack.price_usd})",
                idempotency_key="stripe:cs_1")

    result = billing_for([session("cs_1", key.id)]).reconcile(store)

    assert result.applied == []
    assert store.balance(key.id) == pack.credits


def test_a_payment_with_no_account_is_reported_never_granted(store):
    """A grant to a missing key is a row nobody can spend and a number that
    quietly stops adding up. Say it out loud instead."""
    result = billing_for([session("cs_1", "ghost")]).reconcile(store)

    assert len(result.orphaned) == 1
    assert result.orphaned[0]["key_id"] == "ghost"
    assert result.applied == []


def test_unpaid_and_foreign_sessions_are_left_alone(store):
    _, key = store.create_key(label="alice", plan="metered")
    rows = [
        session("cs_unpaid", key.id, paid=False),
        session("cs_other", key.id, metadata={"note": "not one of ours"}),
        session("cs_badpack", key.id, pack="enormous"),
    ]

    result = billing_for(rows).reconcile(store)

    assert len(result.ignored) == 3
    assert store.balance(key.id) == 0


def test_credits_come_from_our_price_list_not_the_session(store):
    """Same rule as the webhook: Stripe says who paid, we say what it buys."""
    _, key = store.create_key(label="alice", plan="metered")
    rows = [session("cs_1", key.id,
                    metadata={"key_id": key.id, "pack": "starter",
                              "credits": "999999999"})]

    billing_for(rows).reconcile(store)

    assert store.balance(key.id) == PACKS_BY_NAME["starter"].credits


def test_dry_run_changes_nothing(store):
    _, key = store.create_key(label="alice", plan="metered")
    billing = billing_for([session("cs_1", key.id)])

    result = billing.reconcile(store, dry_run=True)

    assert len(result.applied) == 1        # it would have applied one
    assert store.balance(key.id) == 0      # but it did not


def test_since_is_passed_to_stripe(store):
    billing = billing_for([])

    billing.reconcile(store, since=1700000000)

    assert billing.stripe.checkout.Session.calls[-1]["created"] == {"gte": 1700000000}


def test_reconciling_without_a_key_is_refused(store, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    billing = StripeBilling(secret_key=None, webhook_secret="w",
                            stripe=FakeStripe([]))

    with pytest.raises(StripeConfigError):
        billing.reconcile(store)


def test_dry_run_tells_two_payments_of_one_pack_apart(store):
    """A dry run that matched on the description would call this whole."""
    _, key = store.create_key(label="alice", plan="metered")
    pack = PACKS_BY_NAME["starter"]
    store.grant(key.id, pack.credits, f"stripe: {pack.name} pack (${pack.price_usd})",
                idempotency_key="stripe:cs_1")

    result = billing_for([session("cs_1", key.id),
                          session("cs_2", key.id)]).reconcile(store, dry_run=True)

    assert [r["session"] for r in result.already] == ["cs_1"]
    assert [r["session"] for r in result.applied] == ["cs_2"]
