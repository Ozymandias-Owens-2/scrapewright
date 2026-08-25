"""Tiers — what a key is allowed to do, now that credits decide how much.

With prepaid credits there is no subscription to model: the balance answers
"how much", so a tier only answers "what". Two of them:

* **metered** — the default. Every job is paid for in credits; the small free
  monthly allowance is just a grant like any other.
* **unlimited** — self-hosting. Still metered so the operator can see usage,
  never refused, because there is nobody to bill.

The limits that remain are not pricing. They are abuse and blast-radius
guards: a per-job item cap so one request cannot run for an hour, and a daily
new-site ceiling so a runaway loop cannot burn a whole balance overnight
before anyone notices.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    name: str
    max_items_per_job: int
    daily_syntheses: int
    js_allowed: bool = True
    metered: bool = True

    def daily_synthesis_limit_hit(self, syntheses_today: int) -> str | None:
        if syntheses_today >= self.daily_syntheses:
            return (f"daily new-site limit reached ({self.daily_syntheses}); "
                    f"sites already compiled still work, and cost no credits")
        return None


TIERS: dict[str, Tier] = {
    "metered": Tier(name="metered", max_items_per_job=5_000, daily_syntheses=50),
    "unlimited": Tier(name="unlimited", max_items_per_job=100_000,
                      daily_syntheses=10**9, metered=False),
}

DEFAULT_TIER = "metered"


def get_tier(name: str) -> Tier:
    """Resolve a key's tier. Keys issued before credits (free/starter/pro) are
    all metered now — only an explicit 'unlimited' bypasses the balance."""
    return TIERS.get(name, TIERS[DEFAULT_TIER])


# Backwards-compatible aliases: the CLI and older callers say "plan".
PLANS = TIERS
get_plan = get_tier
