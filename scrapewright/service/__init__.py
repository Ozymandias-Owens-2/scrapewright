"""HTTP service around the scrapewright core: API keys, quotas, metering, jobs.

Install with ``pip install "scrapewright[service]"`` and run
``scrapewright serve``. Billing is deliberately a pluggable seam — see
:mod:`scrapewright.service.billing`.
"""

from .billing import BillingProvider, NoopBilling
from .jobs import JobRegistry
from .metering import Meter, metered_scrapewright
from .plans import PLANS, Plan, get_plan
from .store import ApiKey, Store, Usage

__all__ = [
    "ApiKey", "Store", "Usage",
    "Plan", "PLANS", "get_plan",
    "Meter", "metered_scrapewright",
    "JobRegistry",
    "BillingProvider", "NoopBilling",
]
