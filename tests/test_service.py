"""The HTTP service: auth, quotas, metering, and jobs.

Real requests through FastAPI's TestClient, with the scraping core stubbed —
so these test the service's own contract (who may call, what it costs, what
gets refused) rather than re-testing extraction.
"""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from scrapewright.models import Record  # noqa: E402
from scrapewright.service import metering  # noqa: E402
from scrapewright.service.app import create_app  # noqa: E402
from scrapewright.service.jobs import JobRegistry  # noqa: E402
from scrapewright.service.plans import get_plan  # noqa: E402
from scrapewright.service.store import Store, hash_key  # noqa: E402


class _StubPipeline:
    """Stands in for Scrapewright; records what it was asked for."""

    def __init__(self, meter, record=None, records=None):
        self.meter = meter
        self._record = record
        self._records = records or []
        self.closed = False

    def extract(self, url, schema, **kw):
        self.meter.pages += 1
        return self._record

    def crawl_records(self, url, schema, **kw):
        self.meter.pages += len(self._records)
        return iter(self._records)

    def close(self):
        self.closed = True


@pytest.fixture
def stub_core(monkeypatch):
    """Replace the metered pipeline factory with a stub, keeping the meter."""
    state = {"record": Record(url="https://x/1", schema_name="product",
                              data={"title": "A", "price": "10"},
                              source_platform="selector"),
             "records": [], "built": []}

    def fake_factory(*, js=False, meter=None, **kwargs):
        m = meter or metering.Meter()
        if js:
            m.renders += 1
        pipeline = _StubPipeline(m, state["record"], state["records"])
        state["built"].append({"js": js, "pipeline": pipeline})
        return pipeline, m

    monkeypatch.setattr("scrapewright.service.app.metered_scrapewright", fake_factory)
    return state


@pytest.fixture
def client(tmp_path, stub_core):
    store = Store(tmp_path / "svc.db")
    raw, key = store.create_key(label="test", plan="free")
    app = create_app(store=store, jobs=JobRegistry(max_workers=1))
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": raw})
        yield c, store, key, raw


# ── auth ─────────────────────────────────────────────────────────────────────
def test_health_needs_no_key(client):
    c, *_ = client
    r = TestClient(c.app).get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_missing_key_is_rejected(client):
    c, *_ = client
    r = TestClient(c.app).post("/v1/extract", json={"url": "https://x/1"})
    assert r.status_code == 401


def test_revoked_key_stops_working(client):
    c, store, key, raw = client
    assert c.post("/v1/extract", json={"url": "https://x/1"}).status_code == 200
    store.revoke(key.id)
    assert c.post("/v1/extract", json={"url": "https://x/1"}).status_code == 401


def test_keys_are_stored_hashed_never_in_the_clear(client):
    c, store, key, raw = client
    with store._conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM api_keys").fetchall()]
    assert all(raw not in str(r.values()) for r in rows)
    assert rows[0]["key_hash"] == hash_key(raw)


# ── extraction ───────────────────────────────────────────────────────────────
def test_extract_returns_a_record_and_its_usage(client):
    c, *_ = client
    r = c.post("/v1/extract", json={"url": "https://x/1"})
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["title"] == "A"
    assert body["complete"] is True
    assert body["usage"]["pages"] == 1


def test_extract_reports_completeness_against_the_custom_schema(client, stub_core):
    """`complete` tracks the schema's REQUIRED fields (the first one, by
    default) — not merely whether every declared field came back."""
    c, *_ = client
    fields = ["title", "salary:number"]

    stub_core["record"] = Record(url="https://x/1", schema_name="custom",
                                 data={"title": "A"}, source_platform="selector")
    assert c.post("/v1/extract", json={"url": "https://x/1",
                                       "fields": fields}).json()["complete"] is True

    stub_core["record"] = Record(url="https://x/1", schema_name="custom",
                                 data={"salary": "100"}, source_platform="selector")
    assert c.post("/v1/extract", json={"url": "https://x/1",
                                       "fields": fields}).json()["complete"] is False


def test_nothing_extracted_is_a_422_not_a_500(client, stub_core):
    c, *_ = client
    stub_core["record"] = None
    r = c.post("/v1/extract", json={"url": "https://x/1"})
    assert r.status_code == 422
    assert "js=true" in r.json()["detail"]


def test_js_request_is_metered_as_a_render(client):
    c, store, key, _ = client
    c.post("/v1/extract", json={"url": "https://x/1", "js": True})
    assert store.usage_for_month(key.id).renders == 1


def test_pipeline_is_always_closed(client, stub_core):
    c, *_ = client
    c.post("/v1/extract", json={"url": "https://x/1"})
    assert stub_core["built"][-1]["pipeline"].closed


# ── quotas ───────────────────────────────────────────────────────────────────
def test_quota_refusal_happens_before_any_work(client, stub_core):
    c, store, key, _ = client
    plan = get_plan("free")
    store.record(key.id, records=plan.monthly_records)

    built_before = len(stub_core["built"])
    r = c.post("/v1/extract", json={"url": "https://x/1"})
    assert r.status_code == 429
    assert "record quota" in r.json()["detail"]
    # Refused means not charged: no pipeline was ever constructed.
    assert len(stub_core["built"]) == built_before


def test_daily_synthesis_quota_message_mentions_compiled_sites(client):
    c, store, key, _ = client
    store.record(key.id, syntheses=get_plan("free").daily_syntheses)
    r = c.post("/v1/extract", json={"url": "https://x/1"})
    assert r.status_code == 429
    assert "already compiled still work" in r.json()["detail"]


# ── crawl jobs ───────────────────────────────────────────────────────────────
def test_crawl_returns_a_job_then_completes(client, stub_core):
    c, *_ = client
    stub_core["records"] = [
        Record(url=f"https://x/{i}", schema_name="product",
               data={"title": f"P{i}", "price": "5"}, source_platform="selector")
        for i in range(3)
    ]
    r = c.post("/v1/crawl", json={"url": "https://x/shop", "max_items": 3})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    for _ in range(50):
        got = c.get(f"/v1/jobs/{job_id}").json()
        if got["status"] in ("done", "error"):
            break
    assert got["status"] == "done", got
    assert got["result"]["count"] == 3
    assert got["usage"]["pages"] == 3


def test_crawl_max_items_is_capped_by_the_plan(client):
    c, *_ = client
    r = c.post("/v1/crawl", json={"url": "https://x/shop", "max_items": 100_000})
    assert r.json()["max_items"] == get_plan("free").max_items_per_job


def test_a_job_id_is_not_a_capability(client, tmp_path):
    """Another key must not be able to read someone else's job."""
    c, store, key, _ = client
    job_id = c.post("/v1/crawl", json={"url": "https://x/shop"}).json()["job_id"]

    other_raw, _ = store.create_key(label="other")
    other = TestClient(c.app)
    other.headers.update({"X-API-Key": other_raw})
    assert other.get(f"/v1/jobs/{job_id}").status_code == 404


def test_failed_job_is_reported_not_raised(client, monkeypatch):
    c, *_ = client

    def boom(*a, **kw):
        raise RuntimeError("site exploded")

    monkeypatch.setattr("scrapewright.service.app.metered_scrapewright", boom)
    job_id = c.post("/v1/crawl", json={"url": "https://x/shop"}).json()["job_id"]
    for _ in range(50):
        got = c.get(f"/v1/jobs/{job_id}").json()
        if got["status"] in ("done", "error"):
            break
    assert got["status"] == "error"
    assert "site exploded" in got["error"]


# ── usage reporting ──────────────────────────────────────────────────────────
def test_usage_endpoint_reports_plan_and_counters(client):
    c, *_ = client
    c.post("/v1/extract", json={"url": "https://x/1"})
    body = c.get("/v1/usage").json()
    assert body["plan"] == "free"
    assert body["month"]["records"] == 1
    assert body["limits"]["monthly_records"] == get_plan("free").monthly_records
    assert body["bill"]["total_usd"] == 0          # free plan, within quota
    assert body["records_delivered"] == 1
    assert body["sites_compiled"] == 0


def test_detect_is_charged_a_single_page(client, monkeypatch):
    c, store, key, _ = client

    class _Det:
        base, kind, strategy = "https://x", "shopify", "catalog"
        catalog_endpoint, note, matched = "https://x/products.json", "ok", ["shopify"]
        has_catalog_api, likely_needs_js = True, False

    monkeypatch.setattr("scrapewright.service.app.detect", lambda url: _Det())
    before = store.usage_for_month(key.id).pages
    r = c.post("/v1/detect", json={"url": "https://x"})
    assert r.status_code == 200 and r.json()["platform"] == "shopify"
    assert store.usage_for_month(key.id).pages == before + 1


def test_catalog_crawls_are_metered_by_records_delivered(client, stub_core):
    """A platform catalog returns many products in few requests. Quotas must
    still reflect what the caller received, or a catalog drains for free."""
    c, store, key, _ = client
    stub_core["records"] = [
        Record(url=f"https://x/{i}", schema_name="product",
               data={"title": f"P{i}", "price": "5"}, source_platform="shopify")
        for i in range(7)
    ]
    # The stub counts one page per record; force the catalog case where the
    # fetcher is bypassed entirely and the raw count would be zero.
    stub_core["records_meter_pages"] = 0
    job_id = c.post("/v1/crawl", json={"url": "https://x/shop",
                                       "max_items": 7}).json()["job_id"]
    for _ in range(50):
        got = c.get(f"/v1/jobs/{job_id}").json()
        if got["status"] in ("done", "error"):
            break
    assert got["status"] == "done"
    assert got["usage"]["pages"] >= 7


# ── pricing: quotas follow value, caps follow cost ───────────────────────────
def test_extract_charges_a_record_only_when_one_is_delivered(client, stub_core):
    c, store, key, _ = client
    c.post("/v1/extract", json={"url": "https://x/1"})
    assert store.usage_for_month(key.id).records == 1

    stub_core["record"] = None
    c.post("/v1/extract", json={"url": "https://x/2"})   # 422, nothing found
    assert store.usage_for_month(key.id).records == 1    # unchanged: no charge


def test_catalog_crawl_charges_every_record_it_delivered(client, stub_core):
    """The gap this closes: a platform catalog returns hundreds of products in
    two JSON requests, so fetch counts describe our effort, not the customer's
    benefit. Quotas must track what they received."""
    c, store, key, _ = client
    stub_core["records"] = [
        Record(url=f"https://x/{i}", schema_name="product",
               data={"title": f"P{i}", "price": "5"}, source_platform="shopify")
        for i in range(40)
    ]
    job_id = c.post("/v1/crawl", json={"url": "https://x/shop",
                                       "max_items": 50}).json()["job_id"]
    for _ in range(50):
        got = c.get(f"/v1/jobs/{job_id}").json()
        if got["status"] in ("done", "error"):
            break
    assert got["status"] == "done"
    assert got["usage"]["records"] == 40
    assert store.usage_for_month(key.id).records == 40
