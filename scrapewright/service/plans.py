"""Plans and quotas — priced in the units that cost us money.

The three meters are not arbitrary. A page fetch is close to free, a browser
render costs memory and seconds, and an LLM synthesis costs real tokens. Quotas
are set per meter so a customer who scrapes a million pages off ten already-
compiled sites is cheap to serve, while one who points us at a thousand new
sites is not — which is exactly the difference a flat "requests per month" cap
would hide.

Nothing here charges anyone. This is the metering and entitlement layer; a
payment provider plugs in above it (see :mod:`scrapewright.service.billing`).
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import Usage


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_pages: int
    monthly_renders: int
    monthly_syntheses: int
    daily_syntheses: int
    max_items_per_job: int
    js_allowed: bool = True

    def exceeded(self, month: Usage, today: Usage) -> str | None:
        """The first quota this usage breaks, or None. Returned as a message
        the caller can hand straight to the customer."""
        if month.pages >= self.monthly_pages:
            return (f"monthly page quota reached ({self.monthly_pages}) on the "
                    f"'{self.name}' plan")
        if month.renders >= self.monthly_renders:
            return (f"monthly browser-render quota reached ({self.monthly_renders}) "
                    f"on the '{self.name}' plan")
        if month.syntheses >= self.monthly_syntheses:
            return (f"monthly new-site quota reached ({self.monthly_syntheses}); "
                    f"already-compiled sites still work on the '{self.name}' plan")
        if today.syntheses >= self.daily_syntheses:
            return (f"daily new-site quota reached ({self.daily_syntheses}); "
                    f"already-compiled sites still work")
        return None


PLANS: dict[str, Plan] = {
    # Generous enough to be genuinely useful, small enough that abuse is bounded.
    "free": Plan(name="free", monthly_pages=500, monthly_renders=50,
                 monthly_syntheses=10, daily_syntheses=5,
                 max_items_per_job=50),
    "pro": Plan(name="pro", monthly_pages=50_000, monthly_renders=5_000,
                monthly_syntheses=500, daily_syntheses=100,
                max_items_per_job=1_000),
    # For local runs and self-hosting: metered, never blocked.
    "unlimited": Plan(name="unlimited", monthly_pages=10**9, monthly_renders=10**9,
                      monthly_syntheses=10**9, daily_syntheses=10**9,
                      max_items_per_job=10_000),
}

DEFAULT_PLAN = "free"


def get_plan(name: str) -> Plan:
    return PLANS.get(name, PLANS[DEFAULT_PLAN])
