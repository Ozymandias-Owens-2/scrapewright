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
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..detect import detect
from ..robots import RobotsDisallowed
from ..robots import check as check_robots
from ..safeurl import UnsafeUrl, check_syntax
from ..pipeline import Scrapewright
from ..export import write_any
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
    mode: str = Field(
        default="links",
        description="How to find the items. 'links': follow links from a "
                    "listing into item pages. 'rows': the listing's own cards "
                    "are the rows, walking its pagination. 'like': the URL is "
                    "one item page; find every other page shaped like it.")
    rows: bool = Field(
        default=False, json_schema_extra={"deprecated": True},
        # Marked deprecated in the schema rather than with pydantic's
        # `deprecated=True`, which warns on every read -- once per crawl, in
        # the service log, for a field the caller may not even have sent.
        description="Shipped before `mode` existed; true means mode='rows'.")
    scroll: int = Field(
        0, ge=0, le=50,
        description="For listings that load more as you scroll: how many times "
                    "to scroll before reading. Needs js=true. Scrolling stops "
                    "early when the page stops growing, so a generous number "
                    "costs nothing on a page that does not need it.")


class SignupRequest(BaseModel):
    email: str = Field(..., description="where to reach you about this key")


class CheckoutRequest(BaseModel):
    pack: str = Field(description="starter | growth | scale")


def _reject_unusable_url(url: str) -> None:
    """A malformed URL is the caller's mistake, so say 400 and say why.

    Without this a hostname the IDNA codec cannot encode -- a label over 63
    characters, a typo leaving an empty one -- surfaced as a 502 from the
    generic failure handler, or as a failed job the caller had to poll for.

    Only the syntax is checked here. Whether the address is one we are willing
    to reach (SSRF) needs DNS, which would put a lookup in front of every
    request for an answer the fetch is about to work out anyway; that check
    stays where the fetching happens.
    """
    try:
        check_syntax(url)
    except UnsafeUrl as e:
        raise HTTPException(400, str(e)) from e

    # Say when the site itself is the one refusing. Without this a crawl of a
    # disallowed URL came back as "nothing came back -- try a page that lists
    # items", which sends the caller off to debug their selectors for a
    # decision robots.txt made before we fetched anything.
    try:
        check_robots(url)
    except RobotsDisallowed as e:
        raise HTTPException(
            403, f"{e}. This is the site's own rule, not ours, and we obey it.") from e
    except Exception:
        # robots itself being unreachable is the fetch's problem to report.
        return


def _schema_for(fields: list[str] | None) -> Schema:
    return Schema.from_names(fields, name="custom") if fields else PRODUCT_SCHEMA


def _jsonable(value):
    """A Decimal is not JSON, and str() would undo the parsing we just did.

    Fields declared ``number`` arrive here as Decimal. Serialising them the
    old way -- anything not a builtin becomes str() -- would hand the caller
    back "52.15" in quotes, which is the string they asked us not to send.
    JSON has only doubles, so a double is what they get.
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, str, bool, int, float)) or value is None:
        return value
    return str(value)


def _record_payload(record: Record) -> dict[str, Any]:
    return {"url": record.url, "schema": record.schema_name,
            "source": record.source_platform,
            "data": {k: _jsonable(v) for k, v in record.data.items()}}


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The hosted MCP transport keeps a session manager that must be running
        # for the duration of the process; mounting alone does not start it.
        manager = getattr(app.state, "mcp_session_manager", None)
        if manager is None:
            yield
        else:
            async with manager.run():
                yield

    app = FastAPI(
        lifespan=lifespan,
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
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # Without a Cache-Control header browsers guess a lifetime from
    # Last-Modified and keep serving yesterday's page after a deploy; the
    # first person that bit was the owner. `no-cache` means revalidate, not
    # never store -- the ETag makes an unchanged page cost one small 304.
    #
    # The CSP is for our own pages only: they load nothing from anywhere else,
    # so the policy can say so. `unsafe-inline` because the scripts are inline;
    # `form-action` because the buy buttons end at Stripe. /docs is left alone
    # -- Swagger loads from a CDN and is not where a stolen key would go.
    PAGE_HEADERS = {
        "Cache-Control": "no-cache",
        "Content-Security-Policy":
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self' https://checkout.stripe.com; "
            "frame-ancestors 'none'; base-uri 'self'",
    }

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy",
                                    "camera=(), microphone=(), geolocation=()")
        return response

    @app.get("/", include_in_schema=False)
    def landing() -> FileResponse:
        """The page a human lands on. Everything else here answers to machines."""
        return FileResponse(STATIC / "index.html", media_type="text/html",
                            headers=PAGE_HEADERS)

    # Terms, refunds and privacy. A payment processor will not approve a live
    # account without them, and a customer should not have to ask what happens
    # to their money or their data.
    def _page(name: str):
        def serve() -> FileResponse:
            return FileResponse(STATIC / name, media_type="text/html",
                                headers=PAGE_HEADERS)
        return serve

    for path, filename in (("/terms", "terms.html"),
                           ("/refunds", "refunds.html"),
                           ("/privacy", "privacy.html"),
                           ("/account", "account.html")):
        app.get(path, include_in_schema=False)(_page(filename))

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Whether this instance can actually do its job, not just answer.

        A process that serves 200s while its database is unreachable is the
        worst kind of outage: monitoring says fine, customers say otherwise. So
        the database is touched for real, and the answers a watchdog needs --
        can we render, can we sell -- are stated rather than assumed.
        """
        try:
            store.count_events("healthcheck", "never", hours=1)
            database = True
        except Exception:
            log.exception("health check could not reach the database")
            database = False

        # Which Stripe world this instance is wired to. Answering it here means
        # nobody has to fight shell quoting over an ssh one-liner to find out,
        # and "live" is the single fact an operator most wants confirmed before
        # believing a payment went anywhere real.
        secret = os.environ.get("STRIPE_SECRET_KEY", "")
        stripe_mode = ("live" if secret.startswith("sk_live_")
                       else "test" if secret else "none")

        return {
            "ok": database,
            "version": __version__,
            "js": browser_available(),
            "database": database,
            "payments": hasattr(billing, "checkout_session"),
            "stripe": stripe_mode,
        }

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
        _reject_unusable_url(req.url)
        enforce_quota(key)
        schema = _schema_for(req.fields)
        sw, meter = metered_scrapewright(js=req.js)
        try:
            record = sw.extract(req.url, schema)
        except Exception as e:
            # Nothing is charged. The caller cannot act on our failure, and
            # billing for a request that errored is how a service loses the
            # benefit of the doubt it only gets once.
            log.exception("extract failed for %s", req.url)
            raise HTTPException(502, f"could not read that page: {e}") from e
        finally:
            sw.close()

        # Charge for what was delivered, not for the attempt: a page that yields
        # nothing costs no record credits. A render or synthesis it did consume
        # is still charged -- that work really happened.
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
        _reject_unusable_url(req.url)
        balance = enforce_quota(key)
        # `rows: true` shipped a day before `mode` did, and somebody's script
        # may still send it.
        mode = "rows" if req.rows else req.mode
        if mode not in ("links", "rows", "like"):
            raise HTTPException(400, "mode must be links, rows or like")
        tier = get_tier(billing.plan_for(key))
        # A record costs one credit, so the balance is itself an item cap: the
        # job stops at what the caller can pay for instead of overdrawing.
        max_items = min(req.max_items, tier.max_items_per_job,
                        MAX_ITEMS_HARD_CAP, balance)
        schema = _schema_for(req.fields)

        def work() -> tuple[Any, dict[str, int]]:
            sw, meter = metered_scrapewright(js=req.js,
                                             max_scrolls=req.scroll)
            # Looked up by name, not by building a dict of bound methods:
            # that evaluates all three, and a pipeline double that implements
            # only the one under test dies on the other two.
            walk = getattr(sw, {"rows": "crawl_rows", "like": "crawl_like",
                                "links": "crawl_records"}[mode])
            try:
                records = list(walk(req.url, schema, max_items=max_items))
            finally:
                sw.close()
            # Only a job that finished is billed. A crawl that died partway may
            # have cost us real renders, and we eat those: a failed job that
            # still takes credits is worse for us than the renders are.
            #
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

    @app.get("/v1/jobs/{job_id}/download")
    def job_download(job_id: str, format: str = "csv",
                     key: ApiKey = Depends(require_key)) -> FileResponse:
        """A finished crawl as a file, so the rows can leave as a spreadsheet.

        The JSON at ``/v1/jobs/{id}`` is what a program wants. A person wants
        a file they can open, and until this existed the only way to get one
        was to run the CLI yourself -- which left anyone who bought credits
        and opened a browser with no way to collect what they paid for.
        """
        suffix = {"csv": ".csv", "xlsx": ".xlsx", "jsonl": ".jsonl"}.get(format)
        if suffix is None:
            raise HTTPException(400, "format must be csv, xlsx or jsonl")

        job = jobs.get(job_id, key_id=key.id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
        if job.status != "done":
            raise HTTPException(409, f"that job is {job.status}, not done")

        rows = (job.result or {}).get("records") or []
        if not rows:
            raise HTTPException(404, "that job found nothing to download")

        # The job holds payload dicts, not models: rebuild just enough Record
        # for the writer, which knows how to lay out an unknown field set.
        records = [Record(url=r.get("url", ""),
                          schema_name=r.get("schema", "custom"),
                          data=r.get("data") or {},
                          source_platform=r.get("source"))
                   for r in rows]
        # Written to a file rather than streamed: openpyxl builds a zip, and
        # the result is small -- a job is capped at what the caller can pay for.
        path = Path(tempfile.gettempdir()) / f"scrapewright-{job_id}{suffix}"
        try:
            write_any(records, path)
        except ImportError as e:      # xlsx without openpyxl in the image
            raise HTTPException(503, f"{format} export is unavailable here: {e}") from e

        stamp = time.strftime("%Y-%m-%d")
        return FileResponse(path, filename=f"scrapewright-{stamp}{suffix}",
                            headers={"Cache-Control": "no-store"})

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

    # ── the same tools, for agents that connect over HTTP ──────────────────
    from .mcp_http import mount_hosted_mcp

    mount_hosted_mcp(app, require_key=require_key, detect=detect_endpoint,
                     extract=extract_endpoint, crawl=crawl_endpoint,
                     job=job_endpoint, usage=usage_endpoint)
    return app
