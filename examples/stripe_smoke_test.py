"""End-to-end check of the payment flow against Stripe's test mode.

Nothing here touches real money: it refuses to run on anything but a test key.

    pip install "scrapewright[service,stripe]"
    setx STRIPE_SECRET_KEY "sk_test_..."        # restart the terminal after
    python examples/stripe_smoke_test.py

It mints a throwaway API key, creates a real Checkout session, and prints the
URL. Open it, pay with Stripe's test card 4242 4242 4242 4242 (any future
expiry, any CVC), and the webhook does the rest -- provided something is
forwarding webhooks to the service:

    scrapewright serve --db smoke.db --port 8000
    stripe listen --forward-to localhost:8000/v1/webhooks/stripe

`stripe listen` prints a signing secret (whsec_...). Put it in
STRIPE_WEBHOOK_SECRET before starting the service, or the webhook will be
refused -- which is the correct behavior, not a bug.
"""

from __future__ import annotations

import os
import sys

from scrapewright.service.credits import PACKS_BY_NAME
from scrapewright.service.store import Store
from scrapewright.service.stripe_billing import StripeBilling

DB = os.environ.get("SCRAPEWRIGHT_DB", "smoke.db")
PACK = os.environ.get("PACK", "starter")


def main() -> int:
    secret = os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret:
        print("STRIPE_SECRET_KEY is not set.", file=sys.stderr)
        return 1
    if not secret.startswith("sk_test_"):
        # A guard, not a formality: this script is meant to be run casually.
        print("Refusing to run: this is a smoke test and only accepts a TEST "
              "key (sk_test_...). Never point it at a live key.", file=sys.stderr)
        return 1

    store = Store(DB)
    raw_key, api_key = store.create_key(label="stripe smoke test")
    pack = PACKS_BY_NAME[PACK]

    billing = StripeBilling()
    session = billing.checkout_session(api_key, pack)

    print(f"API key     : {raw_key}")
    print(f"key id      : {api_key.id}")
    print(f"balance now : {store.balance(api_key.id):,} credits")
    print()
    print(f"Buying      : {pack.name} — {pack.credits:,} credits for ${pack.price_usd}")
    print(f"Checkout    : {session['checkout_url']}")
    print()
    print("Pay with test card 4242 4242 4242 4242, then check the balance:")
    print(f"    scrapewright credits balance {api_key.id} --db {DB}")
    print()
    print(f"Expected after payment: {pack.credits:,} credits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
