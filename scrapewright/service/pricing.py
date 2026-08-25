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


def pack_margin(pack, costs: UnitCosts = COSTS) -> dict[str, float]:
    """A pack's price against the one action that costs us anything.

    Compiling a site is the binding constraint: if a pack clears margin there,
    it clears everywhere, because delivering records is free. Run this whenever
    a pack price or a credit cost changes -- both are margin edits.
    """
    from .credits import CREDITS_PER_SYNTHESIS

    revenue_per_site = CREDITS_PER_SYNTHESIS * pack.usd_per_credit
    return {
        "pack": pack.name,
        "credits": pack.credits,
        "price_usd": pack.price_usd,
        "usd_per_credit": round(pack.usd_per_credit, 5),
        "revenue_per_new_site_usd": round(revenue_per_site, 4),
        "cost_per_new_site_usd": costs.synthesis_usd,
        "margin_pct": round(100 * (revenue_per_site - costs.synthesis_usd)
                            / revenue_per_site, 1),
    }


def free_allowance_worst_case(costs: UnitCosts = COSTS) -> float:
    """What a free account can cost us at most in a month: every free credit
    spent on the most expensive action there is."""
    from .credits import CREDITS_PER_SYNTHESIS, FREE_MONTHLY_CREDITS

    return round(FREE_MONTHLY_CREDITS / CREDITS_PER_SYNTHESIS * costs.synthesis_usd, 2)


def value_of(credits: int, pack_name: str = "starter") -> float:
    """Dollar value of a credit balance, at a given pack's rate."""
    from .credits import PACKS_BY_NAME

    pack = PACKS_BY_NAME.get(pack_name) or next(iter(PACKS_BY_NAME.values()))
    return round(credits * pack.usd_per_credit, 2)
