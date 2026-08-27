"""Send a correctly-signed webhook to a locally running service.

Useful when you want to exercise the grant path without installing the Stripe
CLI. The signature is computed the way Stripe computes it, so the service's
real verification code runs — this is not a stub or a bypass.

What it proves: our endpoint accepts a valid signature, rejects an invalid one,
credits the right balance, and refuses to double-credit a replay.

What it does NOT prove: that Stripe's own servers send exactly this shape.
Only `stripe listen` against a real payment shows that end of the wire.

    python examples/stripe_local_webhook.py <key_id> [--pack starter] [--tamper]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

from scrapewright.service.credits import PACKS_BY_NAME


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header: t=<ts>,v1=<hmac sha256 of "ts.payload">."""
    timestamp = timestamp or int(time.time())
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def build_event(key_id: str, pack_name: str, session_id: str) -> bytes:
    pack = PACKS_BY_NAME[pack_name]
    return json.dumps({
        "id": f"evt_local_{session_id}",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": session_id,
            "payment_status": "paid",
            "amount_total": pack.price_usd * 100,
            "metadata": {"key_id": key_id, "pack": pack.name,
                         "credits": str(pack.credits)},
        }},
    }).encode()


def post(url: str, payload: bytes, signature: str) -> tuple[int, str]:
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Stripe-Signature": signature})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("key_id", help="the key id to credit (from `scrapewright keys list`)")
    ap.add_argument("--pack", default="starter", choices=list(PACKS_BY_NAME))
    ap.add_argument("--url", default="http://127.0.0.1:8000/v1/webhooks/stripe")
    ap.add_argument("--session", default=None, help="reuse to simulate a retry")
    ap.add_argument("--tamper", action="store_true",
                    help="sign correctly, then inflate the credits in the payload")
    args = ap.parse_args()

    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        print("STRIPE_WEBHOOK_SECRET is not set — set the same value the "
              "service is running with.")
        return 1

    session_id = args.session or f"cs_local_{int(time.time())}"
    payload = build_event(args.key_id, args.pack, session_id)

    if args.tamper:
        # Sign the tampered body properly: the point is to show that a valid
        # signature still does not let a payload dictate the credit amount.
        event = json.loads(payload)
        event["data"]["object"]["metadata"]["credits"] = "999999999"
        payload = json.dumps(event).encode()

    status, body = post(args.url, payload, sign(payload, secret))
    print(f"signed webhook   -> HTTP {status}  {body}")

    status, body = post(args.url, payload, "t=1,v1=deadbeef")
    print(f"forged signature -> HTTP {status}  {body}")
    print()
    print(f"Now check the balance:\n    scrapewright credits balance {args.key_id} "
          f"--db smoke.db")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
