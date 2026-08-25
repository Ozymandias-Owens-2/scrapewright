"""The seam where a payment provider plugs in — and nothing more.

Deliberately empty of any provider. Choosing a processor is a business and
jurisdiction question that changes per operator, and hard-wiring one would make
this service unusable for anyone it doesn't fit. What the service *does* own is
the part every provider needs and none of them supply: identity (API keys),
entitlement (plans), and honest consumption data (meters).

To connect a provider, implement :class:`BillingProvider` and hand it to the
app. Two integration shapes cover essentially everything:

* **Subscription / entitlement** — the provider tells you which plan a customer
  is on; you call :meth:`plan_for` on each request and let quotas do the rest.
* **Usage-based** — you push metered consumption with :meth:`report_usage`
  after each job and the provider invoices it.

The default :class:`NoopBilling` runs the service free-of-charge, which is the
right behavior for a demo, a self-hosted deployment, or a launch before
payments exist.
"""

from __future__ import annotations

from typing import Protocol

from .plans import DEFAULT_PLAN
from .store import ApiKey


class BillingProvider(Protocol):
    """What the service needs from a payment provider — the whole contract."""

    def plan_for(self, key: ApiKey) -> str:
        """The plan name this key is entitled to right now."""
        ...

    def report_usage(self, key: ApiKey, usage: dict[str, int]) -> None:
        """Push one job's metered consumption. May be a no-op for flat plans."""
        ...


class NoopBilling:
    """Charges nothing; honors whatever plan the key was issued with."""

    def plan_for(self, key: ApiKey) -> str:
        return key.plan or DEFAULT_PLAN

    def report_usage(self, key: ApiKey, usage: dict[str, int]) -> None:
        return None
