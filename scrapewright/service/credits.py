"""Prepaid credits: pay for what you use, no subscription.

One action anchors the whole scheme — **compiling a new site**, the only step
that costs real money (~$0.06 of model tokens). Everything else is parsing.
So credits are priced off that: a synthesis must earn enough to keep the
margin, and every other unit is denominated relative to it.

    1 record delivered   =   1 credit
    1 browser render     =   5 credits
    1 new site compiled  = 300 credits

Page fetches are not charged separately: a record's credit already covers the
fetch that produced it, with room to spare (a page costs us ~$0.00001 against
$0.001 of revenue), and billing customers for our own retries would be rude.

Compared with a subscription, this favors light and bursty use — a customer who
pulls 5,000 records off ten known sites pays for exactly that, instead of a
monthly fee sized for someone else. Heavy users pay more, which is the point:
the bill tracks the work.
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import Usage

# Credits charged per unit consumed.
CREDITS_PER_RECORD = 1
CREDITS_PER_RENDER = 5
CREDITS_PER_SYNTHESIS = 300

# Enough to evaluate the tool properly — a couple of new sites and a real
# batch of records — while capping what a free account can cost us (~$0.20/mo
# if every credit went to synthesis, which is the worst case).
FREE_MONTHLY_CREDITS = 1_000


@dataclass(frozen=True)
class CreditPack:
    name: str
    credits: int
    price_usd: int

    @property
    def usd_per_credit(self) -> float:
        return self.price_usd / self.credits

    def margin_on_synthesis(self, synthesis_cost_usd: float = 0.06) -> float:
        """Margin on the costliest action — the number that binds.

        If this holds, everything else in the pack is comfortably profitable,
        because nothing else we do costs meaningful money.
        """
        revenue = CREDITS_PER_SYNTHESIS * self.usd_per_credit
        return round(100 * (revenue - synthesis_cost_usd) / revenue, 1)


# The ladder: buying more lowers the unit price, and the margin steps down with
# it — deliberately, and only on the binding action.
PACKS: tuple[CreditPack, ...] = (
    CreditPack(name="starter", credits=10_000, price_usd=10),
    CreditPack(name="growth", credits=50_000, price_usd=40),
    CreditPack(name="scale", credits=250_000, price_usd=150),
)

PACKS_BY_NAME = {p.name: p for p in PACKS}


def credits_for(usage: Usage) -> int:
    """What this consumption costs in credits."""
    return (usage.records * CREDITS_PER_RECORD
            + usage.renders * CREDITS_PER_RENDER
            + usage.syntheses * CREDITS_PER_SYNTHESIS)


def describe_costs() -> dict[str, int]:
    return {"record": CREDITS_PER_RECORD,
            "render": CREDITS_PER_RENDER,
            "new_site": CREDITS_PER_SYNTHESIS}
