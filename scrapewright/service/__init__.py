"""HTTP service around the scrapewright core: API keys, quotas, metering, jobs.

Install with ``pip install "scrapewright[service]"`` and run
``scrapewright serve``. Billing is deliberately a pluggable seam — see
:mod:`scrapewright.service.billing`.
"""

from .billing import BillingProvider, NoopBilling
from .jobs import JobRegistry
from .metering import Meter, metered_scrapewright
from .credits import PACKS, CreditPack, credits_for
from .plans import TIERS, Tier, get_tier
from .store import ApiKey, Store, Usage

__all__ = [
    "ApiKey", "Store", "Usage",
    "Tier", "TIERS", "get_tier",
    "CreditPack", "PACKS", "credits_for",
    "Meter", "metered_scrapewright",
    "JobRegistry",
    "BillingProvider", "NoopBilling",
]
