"""Find the Stripe tax code that describes what you are selling.

Managed Payments makes Stripe the merchant of record, so it works out VAT per
jurisdiction on your behalf -- and it can only do that if each line item says
what kind of product it is. That is the tax code.

There are several hundred of them. This narrows the list:

    python examples/list_tax_codes.py saas
    python examples/list_tax_codes.py software
    python examples/list_tax_codes.py "digital service"

Needs STRIPE_SECRET_KEY (a test key is fine -- the catalogue is identical).
"""

from __future__ import annotations

import os
import sys

import stripe


def main() -> int:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        print("STRIPE_SECRET_KEY is not set.", file=sys.stderr)
        return 1
    stripe.api_key = key

    term = " ".join(sys.argv[1:]).lower() or "saas"
    hits = [code for code in stripe.TaxCode.list(limit=100).auto_paging_iter()
            if term in code.name.lower() or term in (code.description or "").lower()]

    if not hits:
        print(f"Nothing matched {term!r}. Try a broader word.")
        return 1

    for code in hits:
        print(f"{code.id}  {code.name}")
        if code.description:
            print(f"    {code.description}")
    print(f"\n{len(hits)} matches. Pick the one that describes what you sell.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
