"""The webhook path, driven by the real Stripe SDK instead of a stub.

Every other Stripe test injects a fake SDK, which is what lets the suite run
with no account, no key and no network. That fake is also a blind spot: it
hands back dictionaries, while the real ``construct_event`` returns a
``stripe.Event`` that raises on ``.get()``. The stubbed tests passed for a
release while the live path answered every webhook with a 503.

So these tests use the genuine SDK. They still need no network and no account:
``construct_event`` is signature checking and JSON parsing, nothing more. The
signature is built here the way Stripe builds it, so the SDK's own verifier is
the thing being satisfied.
"""

import hashlib
import hmac
import json
import time

import pytest

stripe = pytest.importorskip("stripe")

from scrapewright.service.credits import PACKS_BY_NAME  # noqa: E402
from scrapewright.service.store import Store  # noqa: E402
from scrapewright.service.stripe_billing import (  # noqa: E402
    StripeBilling,
    StripeWebhookError,
)

SECRET = "whsec_realsdk"


def sign(payload: bytes, secret: str = SECRET) -> str:
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def event(key_id: str, pack: str = "starter", session: str = "cs_real_1",
          credits: str | None = None, paid: bool = True) -> bytes:
    """A payload shaped like Stripe's, so the SDK will parse it as an Event."""
    return json.dumps({
        "id": f"evt_{session}",
        "object": "event",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": session,
            "object": "checkout.session",
            "payment_status": "paid" if paid else "unpaid",
            "metadata": {"key_id": key_id, "pack": pack,
                         "credits": credits or str(PACKS_BY_NAME[pack].credits)},
        }},
    }).encode()


@pytest.fixture
def billing():
    # No `stripe=` argument: this is the real module, on purpose.
    return StripeBilling(secret_key="sk_test_x", webhook_secret=SECRET)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "real.db")


def test_a_real_stripe_event_is_readable(billing, store):
    """The regression itself: a genuine Event must not break the handler."""
    key = store.create_key(label="real")[1]
    payload = event(key.id)

    result = billing.handle_webhook(payload, sign(payload), store)

    assert result["granted"] is True
    assert result["credits"] == PACKS_BY_NAME["starter"].credits
    assert store.balance(key.id) >= PACKS_BY_NAME["starter"].credits


def test_verify_returns_plain_data_not_a_stripe_object(billing):
    """`verify` promises a dict. With the real SDK it has to earn that."""
    payload = event("k1")

    verified = billing.verify(payload, sign(payload))

    assert isinstance(verified, dict)
    assert verified.get("type") == "checkout.session.completed"
    # Nested pieces have to be plain too -- the handler reaches into them.
    assert isinstance(verified["data"]["object"]["metadata"], dict)
    assert verified["data"]["object"]["metadata"].get("key_id") == "k1"


def test_the_real_verifier_rejects_a_forged_signature(billing, store):
    payload = event("k1")

    with pytest.raises(StripeWebhookError):
        billing.handle_webhook(payload, "t=1,v1=deadbeef", store)


def test_a_replayed_payment_credits_once(billing, store):
    key = store.create_key(label="real")[1]
    payload = event(key.id, session="cs_real_replay")

    first = billing.handle_webhook(payload, sign(payload), store)
    after_first = store.balance(key.id)
    second = billing.handle_webhook(payload, sign(payload), store)

    assert first["granted"] is True
    assert second["granted"] is False
    assert store.balance(key.id) == after_first


def test_a_signed_payload_cannot_dictate_the_credit_amount(billing, store):
    """A valid signature proves who sent it, not that the numbers are ours."""
    key = store.create_key(label="real")
    key = key[1]
    payload = event(key.id, session="cs_real_tamper", credits="999999999")

    result = billing.handle_webhook(payload, sign(payload), store)

    assert result["credits"] == PACKS_BY_NAME["starter"].credits
    assert store.balance(key.id) < 999_999_999


def test_an_unpaid_session_grants_nothing(billing, store):
    key = store.create_key(label="real")[1]
    before = store.balance(key.id)
    payload = event(key.id, session="cs_real_unpaid", paid=False)

    result = billing.handle_webhook(payload, sign(payload), store)

    assert "ignored" in result
    assert store.balance(key.id) == before
