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
from scrapewright.service.credits import (  # noqa: E402
    CREDITS_PER_SYNTHESIS,
    FREE_MONTHLY_CREDITS,
)
from scrapewright.service.plans import get_tier  # noqa: E402
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
    raw, key = store.create_key(label="test", plan="metered")
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
def test_running_out_of_credits_refuses_before_any_work(client, stub_core):
    c, store, key, _ = client
    # Drain the free allowance the first request would have granted.
    store.ensure_free_allowance(key.id, FREE_MONTHLY_CREDITS)
    store.spend(key.id, FREE_MONTHLY_CREDITS, "earlier jobs")

    built_before = len(stub_core["built"])
    r = c.post("/v1/extract", json={"url": "https://x/1"})
    assert r.status_code == 402                      # payment required
    assert "out of credits" in r.json()["detail"]
    # Refused means not charged: no pipeline was ever constructed.
    assert len(stub_core["built"]) == built_before


def test_daily_synthesis_quota_message_mentions_compiled_sites(client):
    c, store, key, _ = client
    store.record(key.id, syntheses=get_tier("metered").daily_syntheses)
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


def test_crawl_is_capped_by_the_credits_on_hand(client):
    """A record costs one credit, so a caller cannot start a job bigger than
    their balance -- the cap replaces an overdraft."""
    c, *_ = client
    r = c.post("/v1/crawl", json={"url": "https://x/shop", "max_items": 100_000})
    body = r.json()
    assert body["max_items"] == FREE_MONTHLY_CREDITS
    assert body["credits_available"] == FREE_MONTHLY_CREDITS


def test_crawl_is_also_capped_by_the_tier(client):
    c, store, key, _ = client
    store.grant(key.id, 500_000, "big top-up")
    r = c.post("/v1/crawl", json={"url": "https://x/shop", "max_items": 100_000})
    assert r.json()["max_items"] == get_tier("metered").max_items_per_job


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
def test_usage_endpoint_reports_the_balance_and_the_ledger(client):
    c, *_ = client
    c.post("/v1/extract", json={"url": "https://x/1"})
    body = c.get("/v1/usage").json()
    assert body["tier"] == "metered"
    assert body["month"]["records"] == 1
    # One record delivered = one credit off the free allowance.
    assert body["credits"]["balance"] == FREE_MONTHLY_CREDITS - 1
    assert body["credits"]["free_monthly_allowance"] == FREE_MONTHLY_CREDITS
    assert body["credits"]["cost_per_unit"]["new_site"] == CREDITS_PER_SYNTHESIS
    assert body["records_delivered"] == 1
    assert body["sites_compiled"] == 0
    assert any("free allowance" in e["reason"] for e in body["recent_ledger"])


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


# ── credits ──────────────────────────────────────────────────────────────────
def test_extract_reports_what_it_spent_and_what_is_left(client):
    c, *_ = client
    body = c.post("/v1/extract", json={"url": "https://x/1"}).json()
    assert body["credits_spent"] == 1                      # one record
    assert body["credits_left"] == FREE_MONTHLY_CREDITS - 1


def test_a_render_costs_more_credits_than_a_plain_record(client):
    c, store, key, _ = client
    plain = c.post("/v1/extract", json={"url": "https://x/1"}).json()
    rendered = c.post("/v1/extract", json={"url": "https://x/2", "js": True}).json()
    assert rendered["credits_spent"] > plain["credits_spent"]


def test_a_failed_extract_still_charges_work_that_actually_happened(client, stub_core):
    """No record delivered, so no record credit -- but a render that ran is
    real cost and is charged."""
    c, store, key, _ = client
    store.ensure_free_allowance(key.id, FREE_MONTHLY_CREDITS)
    stub_core["record"] = None
    before = store.balance(key.id)
    c.post("/v1/extract", json={"url": "https://x/1", "js": True})   # 422
    after = store.balance(key.id)
    assert before - after == 5          # the render, not a record


def test_topping_up_restores_service(client, stub_core):
    c, store, key, _ = client
    store.ensure_free_allowance(key.id, FREE_MONTHLY_CREDITS)
    store.spend(key.id, FREE_MONTHLY_CREDITS, "earlier jobs")
    assert c.post("/v1/extract", json={"url": "https://x/1"}).status_code == 402

    store.grant(key.id, 10_000, "pack: starter", idempotency_key="pay_1")
    assert c.post("/v1/extract", json={"url": "https://x/1"}).status_code == 200


def test_unlimited_tier_is_metered_but_never_refused(client, tmp_path):
    """Self-hosting: usage is still visible, but there is nobody to bill."""
    c, store, _, _ = client
    raw, key = store.create_key(label="self-host", plan="unlimited")
    other = TestClient(c.app)
    other.headers.update({"X-API-Key": raw})

    store.spend(key.id, 5_000, "pretend overdraft")
    r = other.post("/v1/extract", json={"url": "https://x/1"})
    assert r.status_code == 200
    assert store.usage_for_month(key.id).records == 1     # still counted


def test_health_reports_what_a_watchdog_needs(tmp_path):
    """A process that serves 200s while its database is unreachable is the
    worst outage there is: monitoring says fine, customers say otherwise."""
    from fastapi.testclient import TestClient

    from scrapewright.service.app import create_app
    from scrapewright.service.jobs import JobRegistry
    from scrapewright.service.store import Store

    client = TestClient(create_app(store=Store(tmp_path / "h.db"), jobs=JobRegistry()))

    body = client.get("/health").json()

    assert body["ok"] is True
    assert body["database"] is True
    assert set(body) == {"ok", "version", "js", "database", "payments"}


def test_health_says_no_when_the_database_is_gone(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from scrapewright.service.app import create_app
    from scrapewright.service.jobs import JobRegistry
    from scrapewright.service.store import Store

    store = Store(tmp_path / "h.db")
    client = TestClient(create_app(store=store, jobs=JobRegistry()))

    def broken(*a, **kw):
        raise RuntimeError("disk is gone")

    monkeypatch.setattr(store, "count_events", broken)

    body = client.get("/health").json()

    assert body["ok"] is False
    assert body["database"] is False


def test_the_pages_a_payment_processor_asks_for_are_served(tmp_path):
    """Stripe will not approve a live account without these, and a customer
    should not have to ask what happens to their money or their data."""
    from fastapi.testclient import TestClient

    from scrapewright.service.app import create_app
    from scrapewright.service.jobs import JobRegistry
    from scrapewright.service.store import Store

    client = TestClient(create_app(store=Store(tmp_path / "l.db"), jobs=JobRegistry()))

    for path in ("/terms", "/refunds", "/privacy"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"]
        # Each must reach the others and the stylesheet, or they are dead ends.
        assert "/static/legal.css" in r.text, path
        assert "mailto:" in r.text, path

    assert client.get("/static/legal.css").status_code == 200


def test_the_landing_page_links_to_them(tmp_path):
    """A policy nobody can find is not a policy."""
    from fastapi.testclient import TestClient

    from scrapewright.service.app import create_app
    from scrapewright.service.jobs import JobRegistry
    from scrapewright.service.store import Store

    client = TestClient(create_app(store=Store(tmp_path / "l.db"), jobs=JobRegistry()))

    home = client.get("/").text

    for path in ('href="/terms"', 'href="/refunds"', 'href="/privacy"'):
        assert path in home
