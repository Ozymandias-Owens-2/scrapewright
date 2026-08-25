"""What serving a customer costs, and what to charge for it.

The two are deliberately different meters, because in this product they come
apart. Serving cost is concentrated almost entirely in **compiling a new site**
— one LLM pass over a page, measured at roughly $0.02 on a small product page
and $0.15 on a heavy rendered one. Everything after that is BeautifulSoup:
delivering the ten-thousandth record from an already-compiled site costs
essentially nothing.

So the pricing follows the value, not the invoice:

* **Customers are charged for records delivered** — the thing they came for,
  and the thing that scales with how useful the service is to them.
* **Syntheses and renders carry fair-use caps** — the things that cost us, kept
  in check so one customer pointing us at a thousand new sites cannot quietly
  become unprofitable.

A flat "requests per month" cap would price both customers identically while
one costs a hundred times the other. These numbers are estimates for planning,
not billing truth; :func:`operator_cost` is what tells you whether a plan's
price still covers the plan's caps.
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import Usage


@dataclass(frozen=True)
class UnitCosts:
    """Estimated cost to us, per unit consumed. Adjust to your own numbers.

    ``synthesis_usd`` is the average of measured runs: ~$0.02 for a small
    product page, ~$0.15 for a 94k-character rendered one.
    """

    synthesis_usd: float = 0.06
    render_usd: float = 0.0002     # ~2-5s of a headless browser
    page_usd: float = 0.00001      # an HTTP fetch and some parsing
    record_usd: float = 0.0        # free once the site is compiled

    def of(self, usage: Usage) -> float:
        return round(
            usage.syntheses * self.synthesis_usd
            + usage.renders * self.render_usd
            + usage.pages * self.page_usd
            + usage.records * self.record_usd,
            4,
        )


COSTS = UnitCosts()


def operator_cost(usage: Usage, costs: UnitCosts = COSTS) -> float:
    """What this usage cost us to serve, in USD."""
    return costs.of(usage)


def worst_case_plan_cost(plan, costs: UnitCosts = COSTS) -> float:
    """Cost of a customer who consumes an entire plan's caps in a month.

    The number a price has to beat. Run it whenever a cap changes — a quota
    edit is a margin edit, and this is the line that says so out loud.
    """
    return costs.of(Usage(syntheses=plan.monthly_syntheses,
                          renders=plan.monthly_renders,
                          records=plan.monthly_records,
                          pages=plan.monthly_records))


def plan_margin(plan, costs: UnitCosts = COSTS) -> dict[str, float]:
    """Price, worst-case cost, and the margin between them."""
    cost = worst_case_plan_cost(plan, costs)
    price = plan.price_usd_month
    return {
        "price_usd": price,
        "worst_case_cost_usd": cost,
        "worst_case_margin_usd": round(price - cost, 2),
        "worst_case_margin_pct": round(100 * (price - cost) / price, 1) if price else 0.0,
    }


def bill_for(plan, usage: Usage) -> dict[str, float | int]:
    """What the customer owes for this month: subscription plus any overage."""
    over_records = max(0, usage.records - plan.monthly_records)
    over_sites = max(0, usage.syntheses - plan.monthly_syntheses)

    record_overage = 0.0
    site_overage = 0.0
    if plan.overage_per_1k_records is not None:
        record_overage = round(over_records / 1000 * plan.overage_per_1k_records, 2)
    if plan.overage_per_site is not None:
        site_overage = round(over_sites * plan.overage_per_site, 2)

    return {
        "plan": plan.name,
        "subscription_usd": plan.price_usd_month,
        "records_over_quota": over_records,
        "records_overage_usd": record_overage,
        "sites_over_quota": over_sites,
        "sites_overage_usd": site_overage,
        "total_usd": round(plan.price_usd_month + record_overage + site_overage, 2),
    }
