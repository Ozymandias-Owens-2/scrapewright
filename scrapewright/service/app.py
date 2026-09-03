"""The HTTP service: scrapewright behind an API key.

Endpoints mirror the library's three questions — what is this site, extract one
page, walk a whole site — plus the accounting a hosted service needs:

    POST /v1/detect     what platform, which strategy      (cheap, synchronous)
    POST /v1/extract    one page -> structured record      (synchronous)
    POST /v1/crawl      a whole site -> job id             (asynchronous)
    GET  /v1/jobs/{id}  poll a crawl
    GET  /v1/usage      what this key has consumed

Quotas are enforced *before* work starts and consumption is recorded after, so
a caller can never be charged for a request that was refused.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..detect import detect
from ..pipeline import Scrapewright
from ..models import Record
from ..schema import PRODUCT_SCHEMA, Schema
from .billing import BillingProvider, NoopBilling
from .jobs import JobRegistry
from .metering import metered_scrapewright

log = logging.getLogger("scrapewright.service")
STATIC = Path(__file__).parent / "static"

# Deliberately loose: this is a contact address and a dedupe handle, not an
# authentication factor. Rejecting valid-but-unusual addresses would cost more
# than the little it would buy.
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Enough for a developer trying the service from one office; not enough to farm
# the free tier from one machine.
MAX_SIGNUPS_PER_DAY = 3
# The public demo runs without a key, so it has to be free for us to serve:
# it only accepts sites with a catalogue API, where no model is ever called.
MAX_DEMOS_PER_DAY = 5
DEMO_MAX_RECORDS = 5
from .credits import (FREE_MONTHLY_CREDITS, PACKS, PACKS_BY_NAME,
                      credits_for, describe_costs)
from .plans import DEFAULT_TIER, get_tier
from .pricing import value_of
from .store import ApiKey, Store, Usage

# An absolute rail, deliberately above every tier: a tier limit that can never
# take effect is a lie in a config file. This one only catches a caller asking
# for something no tier allows.
MAX_ITEMS_HARD_CAP = 100_000


# ── request/response models ──────────────────────────────────────────────────
class DetectRequest(BaseModel):
    url: str


class ExtractRequest(BaseModel):
    url: str
    fields: list[str] | None = Field(
        default=None,
        description="Custom schema, e.g. ['title', 'salary:number', 'tags:list']. "
                    "Omit for the built-in product schema.")
    js: bool = Field(default=False, description="Render in a headless browser.")


class CrawlRequest(ExtractRequest):
    max_items: int = 25


class SignupRequest(BaseModel):
    email: str = Field(..., description="where to reach you about this key")


class CheckoutRequest(BaseModel):
    pack: str = Field(description="starter | growth | scale")


def _schema_for(fields: list[str] | None) -> Schema:
    return Schema.from_names(fields, name="custom") if fields else PRODUCT_SCHEMA


def _record_payload(record: Record) -> dict[str, Any]:
    return {"url": record.url, "schema": record.schema_name,
            "source": record.source_platform,
            "data": {k: (v if isinstance(v, (list, str, bool, int, float)) else str(v))
                     for k, v in record.data.items()}}


def browser_available() -> bool:
    """Can this deployment render a client-side page?

    Reported by /health because the answer is a property of the image, not the
    code: the same build with WITH_JS=0 silently cannot serve `js=true`, and
    without this the only way to find out is a crawl that comes back empty.

    Checks that the executable exists rather than launching it -- a health check
    that starts Chromium every thirty seconds is its own outage.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


def _default_billing() -> BillingProvider:
    """Stripe when a key is configured, otherwise nothing is for sale.

    Chosen by environment rather than by flag so the same image runs as a free
    demo, a self-hosted instance, or a paid service without a code change.
    """
    if not os.environ.get("STRIPE_SECRET_KEY"):
        return NoopBilling()
    try:
        from .stripe_billing import StripeBilling

        return StripeBilling()
    except Exception as e:
        # Loud, but not fatal. A service that cannot take new payments is
        # wounded; one that will not boot is dead, and takes with it the
        # customers who already paid for the credits in their balance.
        # Selling stops, serving continues, and the log says which.
        log.error("Stripe is configured but could not be initialised, so "
                  "credits cannot be bought: %s", e)
        return NoopBilling()


# ── app factory ──────────────────────────────────────────────────────────────
def create_app(store: Store | None = None,
               billing: BillingProvider | None = None,
               jobs: JobRegistry | None = None) -> FastAPI:
    store = store or Store(os.environ.get("SCRAPEWRIGHT_DB", "scrapewright_service.db"))
    billing = billing or _default_billing()
    jobs = jobs or JobRegistry()

    app = FastAPI(
        title="scrapewright",
        version=__version__,
        description="Give it a URL, it writes the scraper. "
                    "An LLM compiles a site once; every page after that replays free.",
    )
    app.state.store = store
    app.state.billing = billing
    app.state.jobs = jobs

    # ── auth + quota gate ────────────────────────────────────────────────────
    def require_key(x_api_key: str = Header(default="")) -> ApiKey:
        key = store.resolve(x_api_key)
        if key is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                "missing or invalid X-API-Key")
        store.record(key.id, requests=1)
        return key

    def available_credits(key: ApiKey) -> int:
        """Balance, after making sure this month's free allowance was granted."""
        store.ensure_free_allowance(key.id, FREE_MONTHLY_CREDITS)
        return store.balance(key.id)

    def enforce_quota(key: ApiKey) -> int:
        """Refuse before any work happens. Returns the credits available."""
        tier = get_tier(billing.plan_for(key))
        if not tier.metered:
            return 10**9   # self-hosted: metered for visibility, never refused

        breach = tier.daily_synthesis_limit_hit(store.usage_for_day(key.id).syntheses)
        if breach:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, breach)

        balance = available_credits(key)
        if balance <= 0:
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                f"out of credits (balance {balance}). Top up to continue; the "
                f"free allowance of {FREE_MONTHLY_CREDITS:,} credits resets monthly.")
        return balance

    def charge(key: ApiKey, usage: dict[str, int], reason: str) -> int:
        """Record consumption and deduct its credits. Returns credits spent."""
        store.record(key.id, **usage)
        countable = {k: v for k, v in usage.items()
                     if k in Usage.__dataclass_fields__}
        spent = credits_for(Usage(**countable))
        if spent and get_tier(billing.plan_for(key)).metered:
            store.spend(key.id, spent, reason)
        billing.report_usage(key, usage)
        return spent

    # ── endpoints ────────────────────────────────────────────────────────────
    @app.get("/", include_in_schema=False)
    def landing() -> FileResponse:
        """The page a human lands on. Everything else here answers to machines."""
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "js": browser_available()}

    @app.post("/v1/detect")
    def detect_endpoint(req: DetectRequest,
                        key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        """What platform is this, and which strategy fits? One cheap request."""
        det = detect(req.url)
        # Routing advice costs no credits: charging for the question would push
        # callers into guessing, which is worse for both of us.
        store.record(key.id, pages=1)
        return {"base": det.base, "platform": det.kind, "strategy": det.strategy,
                "catalog_endpoint": det.catalog_endpoint,
                "free": det.has_catalog_api, "use_js": det.likely_needs_js,
                "also_matched": [m for m in det.matched if m != det.kind],
                "note": det.note}

    @app.post("/v1/extract")
    def extract_endpoint(req: ExtractRequest,
                         key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        """One page in, one structured record out."""
        enforce_quota(key)
        schema = _schema_for(req.fields)
        sw, meter = metered_scrapewright(js=req.js)
        record = None
        try:
            record = sw.extract(req.url, schema)
        finally:
            sw.close()
            # Charge for what was delivered, not for the attempt: a page that
            # yields nothing costs no record credits. A render or synthesis it
            # did consume is still charged -- that work really happened.
            spent = charge(key, {**meter.as_dict(), "records": 1 if record else 0},
                           f"extract {req.url}")

        if record is None:
            raise HTTPException(
                422,  # named constant differs across Starlette versions
                "nothing extracted; try js=true if the page renders client-side")
        payload = _record_payload(record)
        payload["complete"] = schema.is_satisfied_by(record.data)
        payload["usage"] = meter.as_dict()
        payload["credits_spent"] = spent
        payload["credits_left"] = store.balance(key.id)
        return payload

    @app.post("/v1/crawl", status_code=status.HTTP_202_ACCEPTED)
    def crawl_endpoint(req: CrawlRequest,
                       key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        """Walk a whole site. Returns a job id — crawls outlive a request."""
        balance = enforce_quota(key)
        tier = get_tier(billing.plan_for(key))
        # A record costs one credit, so the balance is itself an item cap: the
        # job stops at what the caller can pay for instead of overdrawing.
        max_items = min(req.max_items, tier.max_items_per_job,
                        MAX_ITEMS_HARD_CAP, balance)
        schema = _schema_for(req.fields)

        def work() -> tuple[Any, dict[str, int]]:
            sw, meter = metered_scrapewright(js=req.js)
            records = []
            try:
                records = list(sw.crawl_records(req.url, schema, max_items=max_items))
            finally:
                sw.close()
                # A platform catalog returns hundreds of products in a couple of
                # JSON requests, so fetch counts describe our effort, not the
                # customer's benefit. `records` is the meter quotas run on.
                usage = {**meter.as_dict(), "records": len(records)}
                usage["credits_spent"] = charge(key, usage, f"crawl {req.url}")
            return ({"count": len(records),
                     "records": [_record_payload(r) for r in records]}, usage)

        job = jobs.submit(key.id, "crawl", work)
        return {**job.as_dict(), "max_items": max_items,
                "credits_available": balance, "poll": f"/v1/jobs/{job.id}"}

    @app.get("/v1/jobs/{job_id}")
    def job_endpoint(job_id: str,
                     key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        job = jobs.get(job_id, key_id=key.id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
        return job.as_dict()

    @app.get("/v1/jobs")
    def jobs_endpoint(key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        return {"jobs": [j.as_dict(include_result=False)
                         for j in jobs.list_for(key.id)]}

    # ── buying credits ───────────────────────────────────────────────────────
    @app.post("/v1/demo")
    def demo_endpoint(req: DetectRequest, request: Request) -> dict[str, Any]:
        """Paste a URL, see real rows. No key, no signup, no card.

        Deliberately narrow, because it is unauthenticated. It runs only where
        the platform hands us a catalogue -- Shopify, WooCommerce -- so serving
        it costs a couple of HTTP requests and never a model call. Sites that
        would need compiling are turned away with an explanation rather than
        quietly billed to the operator.
        """
        client_ip = request.client.host if request.client else "unknown"
        if store.count_events(client_ip, "demo") >= MAX_DEMOS_PER_DAY:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "the demo is limited to a few runs a day; a free key lifts that")

        det = detect(req.url)
        if det.kind in ("blocked",):
            raise HTTPException(422, "that site refuses automated visitors "
                                     "(it answered with a block)")
        if det.strategy != "catalog":
            raise HTTPException(
                422, f"the demo only covers sites with a catalogue API "
                     f"(Shopify, WooCommerce). This one looks like "
                     f"'{det.kind}', which has to be compiled first -- that is "
                     f"what a free key is for.")

        store.record_event(client_ip, "demo")
        # Not metered and not billed: nobody is paying, so nothing is counted.
        # allow_llm=False is belt and braces -- the catalogue path never
        # synthesises, and this makes it impossible for a change to that to
        # quietly start spending money on an endpoint with no key.
        sw = Scrapewright()
        try:
            records = [r.model_dump() for _, r in
                       zip(range(DEMO_MAX_RECORDS),
                           sw.crawl_records(req.url, max_items=DEMO_MAX_RECORDS,
                                            allow_llm=False))]
        except Exception as e:
            log.warning("demo failed for %s: %s", req.url, e)
            raise HTTPException(502, "could not read that site just now") from e
        finally:
            sw.close()

        return {"platform": det.kind, "count": len(records), "records": records,
                "note": f"The demo stops at {DEMO_MAX_RECORDS} rows. "
                        f"A free key gives you 1,000."}

    @app.post("/v1/signup", status_code=status.HTTP_201_CREATED)
    def signup_endpoint(req: SignupRequest, request: Request) -> dict[str, Any]:
        """Take an email, hand back an API key. The door into the service.

        Unauthenticated by necessity -- this is where a stranger becomes a
        customer -- which makes the free allowance the thing to protect. Two
        guards, neither of them proof on its own: the allowance is keyed to the
        email rather than the key, so a second signup at the same address gets
        nothing, and one address can only take a few keys a day.

        Deliberately no email verification yet. It would be the honest third
        guard, and it needs a mail sender this deployment does not have; until
        then the endpoint is cheap to abuse and expensive to abuse *at scale*,
        which is the trade being made knowingly.
        """
        email = req.email.strip()
        if not EMAIL_RE.fullmatch(email):
            raise HTTPException(400, "that does not look like an email address")

        client_ip = request.client.host if request.client else "unknown"
        if store.recent_signups(client_ip) >= MAX_SIGNUPS_PER_DAY:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"this address has taken {MAX_SIGNUPS_PER_DAY} keys today; "
                f"write to the operator if you need more")

        raw_key, key = store.create_key(label=email[:64], plan=DEFAULT_TIER,
                                        email=email)
        store.record_signup(client_ip)
        store.ensure_free_allowance(key.id, FREE_MONTHLY_CREDITS)

        return {
            "api_key": raw_key,          # shown once; only its hash is kept
            "key_id": key.id,
            "credits": store.balance(key.id),
            "note": ("Store this key now -- it cannot be shown again. "
                     "Free credits are granted once per address per month."),
        }

    @app.get("/v1/credits/packs")
    def packs_endpoint() -> dict[str, Any]:
        """The price list. Public: nobody should need a key to read prices."""
        return {"cost_per_unit": describe_costs(),
                "free_monthly_allowance": FREE_MONTHLY_CREDITS,
                "packs": [{"name": p.name, "credits": p.credits,
                           "price_usd": p.price_usd,
                           "usd_per_credit": round(p.usd_per_credit, 5)}
                          for p in PACKS]}

    @app.post("/v1/credits/checkout")
    def checkout_endpoint(req: CheckoutRequest,
                          key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        """Start a purchase. Returns a Stripe Checkout URL to send the customer to."""
        pack = PACKS_BY_NAME.get(req.pack)
        if pack is None:
            raise HTTPException(400, f"unknown pack {req.pack!r}; "
                                     f"choose one of {', '.join(PACKS_BY_NAME)}")
        starter = getattr(billing, "checkout_session", None)
        if starter is None:
            raise HTTPException(
                501, "this deployment has no payment provider configured; "
                     "credits are granted by the operator")
        try:
            return starter(key, pack)
        except Exception as e:
            # A misconfigured key is our problem, not a client error.
            raise HTTPException(503, f"payment provider unavailable: {e}") from e

    @app.post("/v1/webhooks/stripe")
    async def stripe_webhook(request: Request,
                             stripe_signature: str = Header(default="")) -> dict[str, Any]:
        """Payment notifications from Stripe.

        Deliberately unauthenticated by API key -- Stripe is the caller, and the
        signature is the credential. The raw body is passed through untouched,
        because signature verification is over exact bytes.
        """
        handler = getattr(billing, "handle_webhook", None)
        if handler is None:
            raise HTTPException(501, "no payment provider configured")
        body = await request.body()
        try:
            return handler(body, stripe_signature, store)
        except Exception as e:
            if type(e).__name__ == "StripeWebhookError":
                # Say why, in the log. A refusal is either someone probing the
                # endpoint or the service quietly declining real payments, and
                # the response body -- a 400 to Stripe -- is seen by nobody.
                log.warning("refused a webhook: %s (signature header: %s)",
                            e, "present" if stripe_signature else "MISSING")
                raise HTTPException(400, str(e)) from e
            log.exception("webhook could not be processed")
            raise HTTPException(503, f"webhook could not be processed: {e}") from e

    @app.get("/v1/usage")
    def usage_endpoint(key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        tier = get_tier(billing.plan_for(key))
        month = store.usage_for_month(key.id)
        today = store.usage_for_day(key.id)
        balance = available_credits(key)
        return {
            "key_id": key.id,
            "tier": tier.name,
            "credits": {"balance": balance,
                        "approx_usd_value": value_of(balance),
                        "free_monthly_allowance": FREE_MONTHLY_CREDITS,
                        "cost_per_unit": describe_costs()},
            "month": month.as_dict(),
            "today": today.as_dict(),
            "limits": {"daily_syntheses": tier.daily_syntheses,
                       "max_items_per_job": tier.max_items_per_job},
            "recent_ledger": store.ledger(key.id, limit=10),
            # The product's own argument, made visible: records climb while
            # sites_compiled stays flat, because each site is compiled once.
            "records_delivered": month.records,
            "sites_compiled": month.syntheses,
        }

    return app
