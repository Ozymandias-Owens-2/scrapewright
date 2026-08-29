"""Stripe: turn a completed payment into credits on a balance.

The whole integration is two moves. A customer asks for a pack and gets a
Checkout URL; Stripe later tells us the payment completed and we grant the
credits. Everything else is guarding that second move, because it is the one an
attacker would target.

Three rules hold it up:

**Verify the signature, always.** The webhook endpoint has no API key — Stripe
calls it, not the customer — so the signature *is* the authentication. An
unverified webhook endpoint is a free credit printer for anyone who can guess
the URL.

**Never trust an amount that came over the wire.** The event carries a pack
name in metadata; how many credits that pack is worth is looked up here, from
our own price list. A caller who edits the payload gets whatever the real pack
is worth, or nothing.

**Grant idempotently, keyed on the payment.** Stripe retries deliveries, and a
retry must not mint a second pack. The key is the Checkout session id rather
than the event id: two *different* event types describing one payment would
carry two event ids but the same session, and only the session identifies the
money that actually moved.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .credits import PACKS_BY_NAME, CreditPack
from .plans import DEFAULT_TIER
from .store import ApiKey, Store

# The only event we act on. A payment produces several; acting on more than one
# is how an integration double-credits itself.
PAID_EVENT = "checkout.session.completed"

# Managed Payments makes Stripe the merchant of record, so Stripe assesses VAT
# per jurisdiction itself -- and it rejects a line item that does not say what
# kind of product it is. This default describes what scrapewright sells; anyone
# selling something else must override it, via ``STRIPE_TAX_CODE`` or the
# constructor. ``examples/list_tax_codes.py`` finds the right code.
DEFAULT_TAX_CODE = "txcd_10103001"  # software as a service, business use


def _plain(value: Any) -> Any:
    """Flatten Stripe's response objects into ordinary dicts and lists.

    ``construct_event`` returns a ``stripe.Event``. It is not a mapping, and
    since SDK v8 it raises on ``.get()`` instead of quietly pretending to be
    one. Converting once, at the boundary, keeps every caller working with
    plain Python data -- and means a test that stubs the SDK with dicts
    exercises the same code path the real SDK takes.

    ``to_dict()`` is shallow in some versions, so recurse rather than trust it.
    """
    for method in ("to_dict_recursive", "to_dict"):
        convert = getattr(value, method, None)
        if callable(convert):
            return _plain(convert())
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value



class StripeConfigError(RuntimeError):
    """Raised when the keys needed for a live integration are missing."""


class StripeWebhookError(RuntimeError):
    """Raised when a webhook cannot be trusted. Answer these with a 400."""


@dataclass
class StripeBilling:
    """Billing provider backed by Stripe Checkout.

    Keys come from the environment (``STRIPE_SECRET_KEY``,
    ``STRIPE_WEBHOOK_SECRET``) so they never touch the repository. ``stripe``
    is injectable purely so the tests can run without the network or a key.
    """

    secret_key: str | None = None
    webhook_secret: str | None = None
    success_url: str = "https://github.com/Ozymandias-Owens-2/scrapewright"
    cancel_url: str = "https://github.com/Ozymandias-Owens-2/scrapewright"
    currency: str = "usd"
    tax_code: str | None = None
    stripe: Any = None

    def __post_init__(self) -> None:
        self.secret_key = self.secret_key or os.environ.get("STRIPE_SECRET_KEY")
        self.webhook_secret = (self.webhook_secret
                               or os.environ.get("STRIPE_WEBHOOK_SECRET"))
        self.tax_code = (self.tax_code or os.environ.get("STRIPE_TAX_CODE")
                         or DEFAULT_TAX_CODE)
        if self.stripe is None:
            try:
                import stripe as stripe_module
            except ImportError as e:  # pragma: no cover - env dependent
                raise StripeConfigError(
                    "Stripe billing needs the 'stripe' package. Install it with:\n"
                    '    pip install "scrapewright[stripe]"') from e
            self.stripe = stripe_module
        if self.secret_key:
            self.stripe.api_key = self.secret_key

    @property
    def live(self) -> bool:
        """True when a real (non-test) Stripe key is configured."""
        return bool(self.secret_key and self.secret_key.startswith("sk_live_"))

    # ── BillingProvider protocol ─────────────────────────────────────────────
    def plan_for(self, key: ApiKey) -> str:
        return key.plan or DEFAULT_TIER

    def report_usage(self, key: ApiKey, usage: dict[str, int]) -> None:
        # Nothing to push: credits are prepaid, and the ledger is the record.
        return None

    # ── buying credits ───────────────────────────────────────────────────────
    def checkout_session(self, key: ApiKey, pack: CreditPack) -> dict[str, str]:
        """A Checkout session for one credit pack.

        The customer's key id rides along in metadata; it is what the webhook
        uses to find the balance to credit. The price is built from our own
        pack list, so a tampered client cannot buy a pack cheaply.
        """
        if not self.secret_key:
            raise StripeConfigError(
                "STRIPE_SECRET_KEY is not set; cannot create a checkout session")

        session = self.stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": self.currency,
                    "unit_amount": pack.price_usd * 100,   # Stripe counts cents
                    "product_data": {
                        "name": f"scrapewright — {pack.credits:,} credits",
                        "description": (f"{pack.name} pack. Credits never expire "
                                        f"and are spent per record delivered."),
                        "tax_code": self.tax_code,
                    },
                },
            }],
            metadata={"key_id": key.id, "pack": pack.name,
                      "credits": str(pack.credits)},
            success_url=self.success_url,
            cancel_url=self.cancel_url,
        )
        return {"checkout_url": session.url, "session_id": session.id,
                "pack": pack.name, "credits": pack.credits,
                "price_usd": pack.price_usd}

    # ── the webhook ──────────────────────────────────────────────────────────
    def verify(self, payload: bytes, signature: str) -> dict:
        """Authenticate a webhook. Raises :class:`StripeWebhookError` if the
        payload is not provably from Stripe."""
        if not self.webhook_secret:
            raise StripeConfigError(
                "STRIPE_WEBHOOK_SECRET is not set; refusing to accept webhooks "
                "that cannot be verified")
        try:
            event = self.stripe.Webhook.construct_event(
                payload, signature, self.webhook_secret)
        except Exception as e:
            # Bad signature, stale timestamp, malformed body -- all untrusted.
            raise StripeWebhookError(f"unverified webhook: {e}") from e
        return _plain(event)

    def handle_webhook(self, payload: bytes, signature: str,
                       store: Store) -> dict[str, Any]:
        """Verify a webhook and, if it reports a paid pack, grant the credits."""
        event = self.verify(payload, signature)
        event_type = event.get("type")
        if event_type != PAID_EVENT:
            return {"ignored": event_type}

        session = event["data"]["object"]
        # "complete" does not mean "paid" -- some methods settle later.
        if session.get("payment_status") != "paid":
            return {"ignored": "session completed but not paid",
                    "session": session.get("id")}

        metadata = session.get("metadata") or {}
        key_id, pack_name = metadata.get("key_id"), metadata.get("pack")
        pack = PACKS_BY_NAME.get(pack_name or "")
        if not key_id or pack is None:
            # Nothing to credit and nobody to credit it to. Report rather than
            # guess: a silent success here would hide a broken checkout flow.
            return {"error": "payment has no usable key_id/pack metadata",
                    "session": session.get("id")}

        session_id = session.get("id")
        applied = store.grant(
            key_id, pack.credits,
            f"stripe: {pack.name} pack (${pack.price_usd})",
            idempotency_key=f"stripe:{session_id}")

        return {"granted": applied, "credits": pack.credits if applied else 0,
                "key_id": key_id, "pack": pack.name, "session": session_id,
                "balance": store.balance(key_id)}
