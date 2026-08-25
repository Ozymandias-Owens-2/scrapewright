"""Plans: charge for what the customer receives, cap what costs us.

The quota that matters to a customer is **records delivered** — that is the
product. The quotas that matter to the operator are **new sites compiled** and
**browser renders**, because those are where the money goes (see
:mod:`scrapewright.service.pricing` for measured unit costs).

Pricing on records alone would let one customer point us at a thousand new
sites and lose money; capping requests alone would charge a customer who pulls
a million records from ten compiled sites as if they were expensive. Two
different meters for two different jobs is the whole design.

Run ``scrapewright plans`` to see each plan's price against the worst case its
caps allow — changing a cap changes a margin, and that command says by how much.
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import Usage


@dataclass(frozen=True)
class Plan:
    name: str
    price_usd_month: int

    # What the customer buys.
    monthly_records: int

    # What we protect. Compiling a site is the one genuinely expensive act.
    monthly_syntheses: int
    daily_syntheses: int
    monthly_renders: int

    max_items_per_job: int
    js_allowed: bool = True

    # None means a hard stop instead of overage billing — the right default for
    # a free plan, where there is no payment method to charge.
    overage_per_1k_records: float | None = None
    overage_per_site: float | None = None

    @property
    def bills_overage(self) -> bool:
        return self.overage_per_1k_records is not None

    def exceeded(self, month: Usage, today: Usage) -> str | None:
        """The first quota this usage breaks, or None.

        Returned as a message the caller can hand straight to the customer, so
        it says what to do next rather than only what went wrong.
        """
        if not self.bills_overage and month.records >= self.monthly_records:
            return (f"monthly record quota reached ({self.monthly_records:,} records) "
                    f"on the '{self.name}' plan")

        # Cost caps bind on every plan: overage is sold on records, not on the
        # expensive units, so these stop the run rather than billing it.
        if month.syntheses >= self.monthly_syntheses and self.overage_per_site is None:
            return (f"monthly new-site limit reached ({self.monthly_syntheses} sites) "
                    f"on the '{self.name}' plan; sites already compiled still work, "
                    f"and cost nothing")
        if today.syntheses >= self.daily_syntheses:
            return (f"daily new-site limit reached ({self.daily_syntheses}); "
                    f"sites already compiled still work, and cost nothing")
        if month.renders >= self.monthly_renders:
            return (f"monthly browser-render limit reached ({self.monthly_renders:,}) "
                    f"on the '{self.name}' plan; requests without js=true still work")
        return None


PLANS: dict[str, Plan] = {
    # Enough to evaluate the thing properly and run a small job for real.
    # Hard caps, no overage: there is no card on file to charge.
    "free": Plan(name="free", price_usd_month=0,
                 monthly_records=1_000,
                 monthly_syntheses=10, daily_syntheses=5,
                 monthly_renders=100,
                 max_items_per_job=100),

    # The working plan: a few dozen sites, a lot of records.
    "starter": Plan(name="starter", price_usd_month=19,
                    monthly_records=25_000,
                    monthly_syntheses=50, daily_syntheses=25,
                    monthly_renders=2_500,
                    max_items_per_job=1_000,
                    overage_per_1k_records=1.0,
                    overage_per_site=0.20),

    # Volume. Records are cheap for us at this point, so they are cheap here.
    "pro": Plan(name="pro", price_usd_month=79,
                monthly_records=250_000,
                monthly_syntheses=300, daily_syntheses=100,
                monthly_renders=25_000,
                max_items_per_job=10_000,
                overage_per_1k_records=0.50,
                overage_per_site=0.15),

    # Self-hosting: metered so the operator can see usage, never blocked.
    "unlimited": Plan(name="unlimited", price_usd_month=0,
                      monthly_records=10**9,
                      monthly_syntheses=10**9, daily_syntheses=10**9,
                      monthly_renders=10**9,
                      max_items_per_job=100_000),
}

DEFAULT_PLAN = "free"


def get_plan(name: str) -> Plan:
    return PLANS.get(name, PLANS[DEFAULT_PLAN])
