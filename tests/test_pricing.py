"""The pricing model: charge for value, cap the cost.

These are the assertions that stop a well-meaning quota edit from quietly
turning a plan unprofitable.
"""

from scrapewright.service.plans import PLANS, get_plan
from scrapewright.service.pricing import (
    COSTS,
    bill_for,
    operator_cost,
    plan_margin,
    worst_case_plan_cost,
)
from scrapewright.service.store import Usage

PAID = [p for p in PLANS.values() if p.price_usd_month > 0]


def test_cost_is_concentrated_in_compiling_new_sites():
    """The premise of the whole model, stated per unit.

    Compiling one site costs thousands of times what serving one record does --
    which is why the two are metered separately. (At volume the cheap unit does
    add up: a million page fetches is real money, so the plan margins count
    pages too. The point is the per-unit ratio, not that pages are free.)
    """
    per_site = operator_cost(Usage(syntheses=1))
    per_record = operator_cost(Usage(records=1, pages=1))
    assert per_site > 1_000 * per_record
    assert COSTS.record_usd == 0.0        # free once the site is compiled


def test_every_paid_plan_is_profitable_at_its_own_limits():
    for plan in PAID:
        margin = plan_margin(plan)
        assert margin["worst_case_margin_usd"] > 0, plan.name
        assert margin["worst_case_margin_pct"] >= 50, (plan.name, margin)


def test_free_plan_cost_is_bounded_and_small():
    """A free user must be cheap enough to absorb, and unable to run away."""
    free = get_plan("free")
    assert worst_case_plan_cost(free) < 1.00
    assert free.overage_per_1k_records is None      # hard stop, nothing to bill


def test_records_are_the_billed_unit_and_expensive_units_are_capped():
    starter = get_plan("starter")
    assert starter.overage_per_1k_records is not None   # records: sold
    assert starter.monthly_syntheses < starter.monthly_records  # sites: capped


def test_bill_adds_overage_only_past_the_quota():
    starter = get_plan("starter")
    within = bill_for(starter, Usage(records=starter.monthly_records - 1))
    assert within["total_usd"] == starter.price_usd_month
    assert within["records_over_quota"] == 0

    over = bill_for(starter, Usage(records=starter.monthly_records + 5_000))
    assert over["records_over_quota"] == 5_000
    assert over["total_usd"] > starter.price_usd_month


def test_free_plan_never_bills_overage():
    free = get_plan("free")
    bill = bill_for(free, Usage(records=free.monthly_records * 10))
    assert bill["total_usd"] == 0


def test_a_million_records_off_compiled_sites_stays_cheap_to_serve():
    """The customer the model is designed to welcome."""
    heavy_but_cheap = Usage(records=1_000_000, pages=1_000_000, syntheses=10)
    assert operator_cost(heavy_but_cheap) < 15.0


def test_a_thousand_new_sites_is_the_expensive_customer():
    """...and the one the caps exist to catch."""
    expensive = Usage(records=1_000, pages=1_000, syntheses=1_000)
    assert operator_cost(expensive) > 50.0
    # No plan lets that happen unmetered.
    assert all(p.monthly_syntheses < 1_000 for p in PAID)
