"""Prepaid credits: the ledger, the prices, and the margin they have to clear.

These are the assertions that stop a well-meaning price edit from quietly
selling work below cost.
"""

import pytest

from scrapewright.service.credits import (
    CREDITS_PER_RECORD,
    CREDITS_PER_RENDER,
    CREDITS_PER_SYNTHESIS,
    FREE_MONTHLY_CREDITS,
    PACKS,
    PACKS_BY_NAME,
    credits_for,
)
from scrapewright.service.pricing import (
    COSTS,
    free_allowance_worst_case,
    operator_cost,
    pack_margin,
    value_of,
)
from scrapewright.service.store import Store, Usage


# ── the price ladder ─────────────────────────────────────────────────────────
def test_every_pack_clears_margin_on_the_action_that_costs_us():
    """Compiling a site is the binding constraint: clear it and the rest is
    free money, because delivering a record costs essentially nothing."""
    for pack in PACKS:
        margin = pack_margin(pack)
        assert margin["margin_pct"] >= 60, (pack.name, margin)
        assert margin["revenue_per_new_site_usd"] > COSTS.synthesis_usd


def test_buying_more_costs_less_per_credit():
    per_credit = [p.usd_per_credit for p in PACKS]
    assert per_credit == sorted(per_credit, reverse=True), per_credit


def test_a_credit_is_never_sold_below_what_a_synthesis_costs():
    """The floor: 300 credits must always be worth more than $0.06."""
    cheapest = min(p.usd_per_credit for p in PACKS)
    assert CREDITS_PER_SYNTHESIS * cheapest > COSTS.synthesis_usd


def test_free_allowance_is_small_enough_to_absorb():
    assert free_allowance_worst_case() < 0.50
    assert FREE_MONTHLY_CREDITS <= 2_000


def test_credit_costs_track_real_costs_in_order():
    """A synthesis must cost far more credits than a record, because it costs
    far more money. Getting this ordering wrong is how a price list lies."""
    assert CREDITS_PER_SYNTHESIS > CREDITS_PER_RENDER > CREDITS_PER_RECORD
    assert operator_cost(Usage(syntheses=1)) > operator_cost(Usage(renders=1))
    assert operator_cost(Usage(renders=1)) > operator_cost(Usage(records=1, pages=1))


# ── what a job costs ─────────────────────────────────────────────────────────
def test_credits_for_a_typical_new_site_crawl():
    # 100 products off a site we have never seen: one synthesis, 100 records.
    cost = credits_for(Usage(records=100, syntheses=1, pages=100))
    assert cost == 100 * CREDITS_PER_RECORD + CREDITS_PER_SYNTHESIS


def test_the_second_crawl_of_a_site_is_far_cheaper():
    """The product's whole argument, in the price list."""
    first = credits_for(Usage(records=100, syntheses=1))
    later = credits_for(Usage(records=100, syntheses=0))
    assert first >= 4 * later          # 400 credits vs 100, on this size of job
    assert later == 100


def test_page_fetches_are_not_charged_separately():
    assert credits_for(Usage(pages=10_000)) == 0


def test_value_of_a_balance_is_quoted_in_dollars():
    starter = PACKS_BY_NAME["starter"]
    assert value_of(starter.credits, "starter") == pytest.approx(starter.price_usd)


# ── the ledger ───────────────────────────────────────────────────────────────
@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "credits.db")


def test_balance_starts_empty_and_follows_the_ledger(store):
    _, key = store.create_key()
    assert store.balance(key.id) == 0

    store.grant(key.id, 10_000, "pack: starter")
    store.spend(key.id, 350, "crawl")
    assert store.balance(key.id) == 9_650


def test_free_allowance_is_granted_once_per_month(store):
    _, key = store.create_key()
    for _ in range(3):
        store.ensure_free_allowance(key.id, FREE_MONTHLY_CREDITS, "2026-08")
    assert store.balance(key.id) == FREE_MONTHLY_CREDITS

    store.ensure_free_allowance(key.id, FREE_MONTHLY_CREDITS, "2026-09")
    assert store.balance(key.id) == 2 * FREE_MONTHLY_CREDITS


def test_a_replayed_payment_cannot_double_credit(store):
    """A payment webhook that fires twice must not hand out two packs."""
    _, key = store.create_key()
    assert store.grant(key.id, 10_000, "pack: starter", idempotency_key="pay_123")
    assert not store.grant(key.id, 10_000, "pack: starter", idempotency_key="pay_123")
    assert store.balance(key.id) == 10_000


def test_spending_past_zero_is_recorded_not_silently_dropped(store):
    """Work already done gets charged even if it overdraws; the next request
    is what gets refused."""
    _, key = store.create_key()
    store.grant(key.id, 100, "small top-up")
    store.spend(key.id, 400, "a crawl that ran long")
    assert store.balance(key.id) == -300


def test_ledger_is_auditable_newest_first(store):
    _, key = store.create_key()
    store.grant(key.id, 500, "free allowance 2026-08")
    store.spend(key.id, 50, "extract A")
    entries = store.ledger(key.id)
    assert [e["delta"] for e in entries] == [-50, 500]
    assert entries[0]["reason"] == "extract A"


def test_grant_rejects_a_non_positive_amount(store):
    _, key = store.create_key()
    with pytest.raises(ValueError):
        store.grant(key.id, 0, "nothing")
