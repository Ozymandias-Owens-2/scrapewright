"""Stripe integration — mostly a test of what it refuses.

The webhook endpoint has no API key, so its signature check is the only thing
standing between a stranger and free credits. Most of what follows is aimed at
that one seam.

No network and no Stripe account: the SDK is stubbed, which is why the adapter
takes it as an argument.
"""

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from scrapewright.service.app import create_app  # noqa: E402
from scrapewright.service.credits import PACKS_BY_NAME  # noqa: E402
from scrapewright.service.jobs import JobRegistry  # noqa: E402
from scrapewright.service.store import Store  # noqa: E402
from scrapewright.service.stripe_billing import (  # noqa: E402
    StripeBilling,
    StripeConfigError,
    StripeWebhookError,
)


# ── a stand-in for the Stripe SDK ────────────────────────────────────────────
class _FakeSession:
    def __init__(self, **kw):
        self.id = "cs_test_123"
        self.url = "https://checkout.stripe.com/c/pay/cs_test_123"
        self.kwargs = kw


class _FakeStripe:
    """Accepts one signature ('good') and rejects everything else."""

    class error:
        class SignatureVerificationError(Exception):
            pass

    def __init__(self):
        self.api_key = None
        self.created = []
        outer = self

        class _Sessions:
            @staticmethod
            def create(**kw):
                session = _FakeSession(**kw)
                outer.created.append(kw)
                return session

        class _Checkout:
            Session = _Sessions

        class _Webhook:
            @staticmethod
            def construct_event(payload, sig_header, secret, **kw):
                if sig_header != "good":
                    raise _FakeStripe.error.SignatureVerificationError("bad signature")
                return json.loads(payload)

        self.checkout = _Checkout
        self.Webhook = _Webhook


def paid_event(key_id="abc123", pack="starter", session_id="cs_test_123",
               status="paid", event_type="checkout.session.completed"):
    return json.dumps({
        "type": event_type,
        "data": {"object": {
            "id": session_id,
            "payment_status": status,
            "metadata": {"key_id": key_id, "pack": pack,
                         "credits": str(PACKS_BY_NAME[pack].credits)},
        }},
    }).encode()


@pytest.fixture
def billing():
    return StripeBilling(secret_key="sk_test_x", webhook_secret="whsec_x",
                         stripe=_FakeStripe())


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "stripe.db")


# ── checkout ─────────────────────────────────────────────────────────────────
def test_checkout_prices_the_pack_from_our_own_list(billing, store):
    _, key = store.create_key(label="alice")
    out = billing.checkout_session(key, PACKS_BY_NAME["growth"])

    assert out["checkout_url"].startswith("https://checkout.stripe.com/")
    sent = billing.stripe.created[-1]
    # The amount comes from the pack, in cents, not from anything a client said.
    assert sent["line_items"][0]["price_data"]["unit_amount"] == 40 * 100
    assert sent["mode"] == "payment"          # one-time, never a subscription
    # The key id must ride along, or the webhook has nobody to credit.
    assert sent["metadata"]["key_id"] == key.id
    assert sent["metadata"]["pack"] == "growth"


def test_checkout_declares_a_product_tax_code(billing, store):
    """Managed Payments refuses a line item that will not say what it is.

    Stripe is the merchant of record on this account, so it assesses VAT
    itself. A session without a tax code is rejected outright -- the live run
    failed here with "the product tax code is missing" before this was added.
    """
    _, key = store.create_key(label="alice")
    billing.checkout_session(key, PACKS_BY_NAME["starter"])

    sent = billing.stripe.created[-1]
    tax_code = sent["line_items"][0]["price_data"]["product_data"]["tax_code"]
    assert tax_code and tax_code.startswith("txcd_")


def test_the_tax_code_can_be_set_per_deployment(store, monkeypatch):
    """The default describes what we sell; a fork sells something else."""
    monkeypatch.setenv("STRIPE_TAX_CODE", "txcd_00000000")
    other = StripeBilling(secret_key="sk_test_x", webhook_secret="whsec_x",
                          stripe=_FakeStripe())
    _, key = store.create_key(label="alice")

    other.checkout_session(key, PACKS_BY_NAME["starter"])

    sent = other.stripe.created[-1]
    product = sent["line_items"][0]["price_data"]["product_data"]
    assert product["tax_code"] == "txcd_00000000"


def test_checkout_without_a_key_configured_is_refused(store):
    b = StripeBilling(secret_key=None, webhook_secret="whsec_x", stripe=_FakeStripe())
    _, key = store.create_key()
    with pytest.raises(StripeConfigError):
        b.checkout_session(key, PACKS_BY_NAME["starter"])


# ── the webhook: what it refuses ─────────────────────────────────────────────
def test_a_forged_webhook_grants_nothing(billing, store):
    _, key = store.create_key()
    with pytest.raises(StripeWebhookError):
        billing.handle_webhook(paid_event(key_id=key.id), "forged", store)
    assert store.balance(key.id) == 0


def test_webhooks_are_refused_outright_when_no_secret_is_set(store):
    """Better to reject every webhook than to accept unverifiable ones."""
    b = StripeBilling(secret_key="sk_test_x", webhook_secret=None,
                      stripe=_FakeStripe())
    with pytest.raises(StripeConfigError):
        b.handle_webhook(paid_event(), "good", store)


def test_a_completed_but_unpaid_session_grants_nothing(billing, store):
    _, key = store.create_key()
    out = billing.handle_webhook(
        paid_event(key_id=key.id, status="unpaid"), "good", store)
    assert "ignored" in out
    assert store.balance(key.id) == 0


def test_other_event_types_are_ignored(billing, store):
    _, key = store.create_key()
    out = billing.handle_webhook(
        paid_event(key_id=key.id, event_type="payment_intent.created"),
        "good", store)
    assert out["ignored"] == "payment_intent.created"
    assert store.balance(key.id) == 0


def test_an_unknown_pack_in_metadata_grants_nothing(billing, store):
    """Credits come from our price list, never from the payload."""
    _, key = store.create_key()
    payload = json.loads(paid_event(key_id=key.id))
    payload["data"]["object"]["metadata"] = {"key_id": key.id, "pack": "enormous",
                                             "credits": "999999999"}
    out = billing.handle_webhook(json.dumps(payload).encode(), "good", store)
    assert "error" in out
    assert store.balance(key.id) == 0


def test_metadata_cannot_inflate_the_grant(billing, store):
    """A tampered 'credits' field is ignored: the pack decides."""
    _, key = store.create_key()
    payload = json.loads(paid_event(key_id=key.id, pack="starter"))
    payload["data"]["object"]["metadata"]["credits"] = "999999999"
    billing.handle_webhook(json.dumps(payload).encode(), "good", store)
    assert store.balance(key.id) == PACKS_BY_NAME["starter"].credits


# ── the webhook: what it does ────────────────────────────────────────────────
def test_a_paid_session_credits_the_balance(billing, store):
    _, key = store.create_key()
    out = billing.handle_webhook(paid_event(key_id=key.id), "good", store)
    assert out["granted"] is True
    assert out["credits"] == PACKS_BY_NAME["starter"].credits
    assert store.balance(key.id) == PACKS_BY_NAME["starter"].credits


def test_a_redelivered_webhook_does_not_double_credit(billing, store):
    """Stripe retries. A retry must be free."""
    _, key = store.create_key()
    for _ in range(3):
        out = billing.handle_webhook(paid_event(key_id=key.id), "good", store)
    assert out["granted"] is False
    assert store.balance(key.id) == PACKS_BY_NAME["starter"].credits


def test_two_separate_purchases_both_credit(billing, store):
    _, key = store.create_key()
    billing.handle_webhook(paid_event(key_id=key.id, session_id="cs_1"), "good", store)
    billing.handle_webhook(paid_event(key_id=key.id, session_id="cs_2"), "good", store)
    assert store.balance(key.id) == 2 * PACKS_BY_NAME["starter"].credits


def test_the_ledger_names_the_payment(billing, store):
    _, key = store.create_key()
    billing.handle_webhook(paid_event(key_id=key.id), "good", store)
    assert any("stripe" in e["reason"] for e in store.ledger(key.id))


def test_live_flag_distinguishes_test_keys(store):
    assert not StripeBilling(secret_key="sk_test_x", webhook_secret="w",
                             stripe=_FakeStripe()).live
    assert StripeBilling(secret_key="sk_live_x", webhook_secret="w",
                         stripe=_FakeStripe()).live


# ── through the HTTP surface ─────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path, billing):
    store = Store(tmp_path / "svc.db")
    raw, key = store.create_key(label="alice", plan="metered")
    app = create_app(store=store, billing=billing, jobs=JobRegistry(max_workers=1))
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": raw})
        yield c, store, key


def test_prices_are_public(client):
    c, *_ = client
    r = TestClient(c.app).get("/v1/credits/packs")      # no key
    assert r.status_code == 200
    assert {p["name"] for p in r.json()["packs"]} == set(PACKS_BY_NAME)


def test_checkout_endpoint_returns_a_url(client):
    c, *_ = client
    r = c.post("/v1/credits/checkout", json={"pack": "scale"})
    assert r.status_code == 200
    assert r.json()["checkout_url"].startswith("https://checkout.stripe.com/")


def test_checkout_rejects_an_unknown_pack(client):
    c, *_ = client
    r = c.post("/v1/credits/checkout", json={"pack": "gigantic"})
    assert r.status_code == 400


def test_checkout_needs_an_api_key(client):
    c, *_ = client
    r = TestClient(c.app).post("/v1/credits/checkout", json={"pack": "starter"})
    assert r.status_code == 401


def test_webhook_endpoint_needs_no_api_key_but_needs_a_signature(client):
    c, store, key = client
    anon = TestClient(c.app)          # deliberately no X-API-Key

    bad = anon.post("/v1/webhooks/stripe", content=paid_event(key_id=key.id),
                    headers={"stripe-signature": "forged"})
    assert bad.status_code == 400
    assert store.balance(key.id) == 0

    good = anon.post("/v1/webhooks/stripe", content=paid_event(key_id=key.id),
                     headers={"stripe-signature": "good"})
    assert good.status_code == 200
    assert store.balance(key.id) == PACKS_BY_NAME["starter"].credits


def test_paying_restores_a_drained_account(client):
    """The whole point, end to end: 402, pay, work again."""
    c, store, key = client
    from scrapewright.service.credits import FREE_MONTHLY_CREDITS

    store.ensure_free_allowance(key.id, FREE_MONTHLY_CREDITS)
    store.spend(key.id, FREE_MONTHLY_CREDITS, "earlier jobs")
    assert c.post("/v1/extract", json={"url": "https://x/1"}).status_code == 402

    TestClient(c.app).post("/v1/webhooks/stripe", content=paid_event(key_id=key.id),
                           headers={"stripe-signature": "good"})
    assert store.balance(key.id) == PACKS_BY_NAME["starter"].credits


def test_a_deployment_without_stripe_says_so_plainly(tmp_path):
    """No provider configured is a 501, not a crash."""
    from scrapewright.service.billing import NoopBilling

    store = Store(tmp_path / "free.db")
    raw, _ = store.create_key()
    app = create_app(store=store, billing=NoopBilling(), jobs=JobRegistry(max_workers=1))
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": raw})
        assert c.post("/v1/credits/checkout", json={"pack": "starter"}).status_code == 501


def test_a_broken_stripe_setup_does_not_take_the_service_down(monkeypatch, tmp_path):
    """Losing the ability to sell must not lose the ability to serve.

    A deployment configured for Stripe whose SDK will not load used to raise
    out of create_app, so the container crash-looped: existing customers lost
    access to credits they had already paid for, over a problem that only
    affects buying more. It now degrades to no-payments and says so loudly.
    """
    import sys

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    # None in sys.modules makes `import stripe` raise, exactly as a container
    # built without the extra does.
    monkeypatch.setitem(sys.modules, "stripe", None)
    monkeypatch.delitem(sys.modules, "scrapewright.service.stripe_billing",
                        raising=False)

    store = Store(tmp_path / "degraded.db")
    client = TestClient(create_app(store=store, jobs=JobRegistry()))

    assert client.get("/health").status_code == 200

    raw = store.create_key(label="alice", plan="metered")[0]
    # Buying is refused in a way the caller can act on, not a crash.
    r = client.post("/v1/credits/checkout", json={"pack": "starter"},
                    headers={"X-API-Key": raw})
    assert r.status_code == 501
