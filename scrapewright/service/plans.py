"""Tiers — what a key is allowed to do, now that credits decide how much.

With prepaid credits there is no subscription to model: the balance answers
"how much", so a tier only answers "what". Two of them:

* **free** — an account nobody has paid for. It gets the deterministic side
  of the tool in full: catalogue APIs, and every site already compiled, at no
  cost to anyone. What it barely gets is the two things that cost us real
  money the moment they run -- compiling a new site (model tokens) and
  rendering in a browser (our CPU). An account is free to create and email
  addresses are free to invent, so anything a free account can do at our
  expense can be done a thousand times.
* **metered** — a key that has been topped up. Every job is paid for in
  credits; the free monthly allowance is a grant like any other.
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
    # Renders are the other thing that costs us rather than the caller: a
    # browser page is seconds of CPU on a machine shared by everyone.
    daily_renders: int = 10**9
    metered: bool = True

    def daily_render_limit_hit(self, renders_today: int) -> str | None:
        if renders_today >= self.daily_renders:
            return (f"daily rendering limit reached ({self.daily_renders} pages). "
                    f"Static pages and already-compiled sites still work; "
                    f"topping up lifts this.")
        return None

    def daily_synthesis_limit_hit(self, syntheses_today: int) -> str | None:
        if syntheses_today >= self.daily_syntheses:
            return (f"daily new-site limit reached ({self.daily_syntheses}); "
                    f"sites already compiled still work, and cost no credits")
        return None


TIERS: dict[str, Tier] = {
    "free": Tier(name="free", max_items_per_job=500, daily_syntheses=1,
                 daily_renders=20),
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
