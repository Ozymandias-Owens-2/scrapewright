"""The seam where a payment provider plugs in — and nothing more.

Deliberately empty of any provider. Choosing a processor is a business and
jurisdiction question that changes per operator, and hard-wiring one would make
this service unusable for anyone it doesn't fit. What the service *does* own is
the part every provider needs and none of them supply: identity (API keys),
entitlement (plans), and honest consumption data (meters).

To connect a provider, implement :class:`BillingProvider` and hand it to the
app. Two integration shapes cover essentially everything:

* **Top-ups** — a completed payment becomes a credit grant. The provider's
  webhook handler calls ``store.grant(key_id, pack.credits, ...)`` with the
  payment id as the idempotency key, so a replayed webhook cannot double-credit.
* **Reporting** — push consumption with :meth:`report_usage` if the provider
  wants usage data; a no-op is fine, since the ledger is already the record.

The default :class:`NoopBilling` runs the service free-of-charge, which is the
right behavior for a demo, a self-hosted deployment, or a launch before
payments exist.
"""

from __future__ import annotations

from typing import Protocol

from .plans import DEFAULT_TIER
from .store import ApiKey


class BillingProvider(Protocol):
    """What the service needs from a payment provider — the whole contract."""

    def plan_for(self, key: ApiKey) -> str:
        """The tier this key runs under — 'metered' or 'unlimited'."""
        ...

    def report_usage(self, key: ApiKey, usage: dict[str, int]) -> None:
        """Push one job's metered consumption. May be a no-op for flat plans."""
        ...


class NoopBilling:
    """Sells nothing; credits arrive only by an operator grant."""

    def plan_for(self, key: ApiKey) -> str:
        return key.plan or DEFAULT_TIER

    def report_usage(self, key: ApiKey, usage: dict[str, int]) -> None:
        return None
