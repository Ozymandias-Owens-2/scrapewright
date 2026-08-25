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

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .. import __version__
from ..detect import detect
from ..models import Record
from ..schema import PRODUCT_SCHEMA, Schema
from .billing import BillingProvider, NoopBilling
from .jobs import JobRegistry
from .metering import metered_scrapewright
from .plans import get_plan
from .pricing import bill_for
from .store import ApiKey, Store

MAX_ITEMS_HARD_CAP = 1000


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


def _schema_for(fields: list[str] | None) -> Schema:
    return Schema.from_names(fields, name="custom") if fields else PRODUCT_SCHEMA


def _record_payload(record: Record) -> dict[str, Any]:
    return {"url": record.url, "schema": record.schema_name,
            "source": record.source_platform,
            "data": {k: (v if isinstance(v, (list, str, bool, int, float)) else str(v))
                     for k, v in record.data.items()}}


# ── app factory ──────────────────────────────────────────────────────────────
def create_app(store: Store | None = None,
               billing: BillingProvider | None = None,
               jobs: JobRegistry | None = None) -> FastAPI:
    store = store or Store(os.environ.get("SCRAPEWRIGHT_DB", "scrapewright_service.db"))
    billing = billing or NoopBilling()
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

    def enforce_quota(key: ApiKey) -> None:
        plan = get_plan(billing.plan_for(key))
        breach = plan.exceeded(store.usage_for_month(key.id),
                               store.usage_for_day(key.id))
        if breach:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, breach)

    def charge(key: ApiKey, usage: dict[str, int]) -> None:
        store.record(key.id, **usage)
        billing.report_usage(key, usage)

    # ── endpoints ────────────────────────────────────────────────────────────
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.post("/v1/detect")
    def detect_endpoint(req: DetectRequest,
                        key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        """What platform is this, and which strategy fits? One cheap request."""
        det = detect(req.url)
        charge(key, {"pages": 1})
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
        plan = get_plan(billing.plan_for(key))
        if req.js and not plan.js_allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"browser rendering is not available on the "
                                f"'{plan.name}' plan")

        schema = _schema_for(req.fields)
        sw, meter = metered_scrapewright(js=req.js)
        record = None
        try:
            record = sw.extract(req.url, schema)
        finally:
            sw.close()
            # Charge for what was delivered, not for the attempt: a page that
            # yields nothing costs the customer no records.
            charge(key, {**meter.as_dict(), "records": 1 if record else 0})

        if record is None:
            raise HTTPException(
                422,  # named constant differs across Starlette versions
                "nothing extracted; try js=true if the page renders client-side")
        payload = _record_payload(record)
        payload["complete"] = schema.is_satisfied_by(record.data)
        payload["usage"] = meter.as_dict()
        return payload

    @app.post("/v1/crawl", status_code=status.HTTP_202_ACCEPTED)
    def crawl_endpoint(req: CrawlRequest,
                       key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        """Walk a whole site. Returns a job id — crawls outlive a request."""
        enforce_quota(key)
        plan = get_plan(billing.plan_for(key))
        max_items = min(req.max_items, plan.max_items_per_job, MAX_ITEMS_HARD_CAP)
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
                charge(key, usage)
            return ({"count": len(records),
                     "records": [_record_payload(r) for r in records]}, usage)

        job = jobs.submit(key.id, "crawl", work)
        return {**job.as_dict(), "max_items": max_items,
                "poll": f"/v1/jobs/{job.id}"}

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

    @app.get("/v1/usage")
    def usage_endpoint(key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        plan = get_plan(billing.plan_for(key))
        month = store.usage_for_month(key.id)
        today = store.usage_for_day(key.id)
        return {
            "key_id": key.id,
            "plan": plan.name,
            "month": month.as_dict(),
            "today": today.as_dict(),
            "limits": {"monthly_records": plan.monthly_records,
                       "monthly_syntheses": plan.monthly_syntheses,
                       "daily_syntheses": plan.daily_syntheses,
                       "monthly_renders": plan.monthly_renders,
                       "max_items_per_job": plan.max_items_per_job},
            "bill": bill_for(plan, month),
            # The product's own argument, made visible: records climb while
            # sites_compiled stays flat, because each site is compiled once.
            "records_delivered": month.records,
            "sites_compiled": month.syntheses,
        }

    return app
