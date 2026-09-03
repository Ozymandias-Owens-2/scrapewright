"""Nobody pays for our failures.

Found in production: two /v1/extract calls died on an upstream 400 and the
customer was charged 600 credits for them -- 300 per synthesis that never
produced a recipe and that we were never billed for either. The suite was green
throughout, because nothing here was tested.
"""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from scrapewright.service import app as app_module  # noqa: E402
from scrapewright.service.app import create_app  # noqa: E402
from scrapewright.service.credits import (  # noqa: E402
    CREDITS_PER_SYNTHESIS,
    FREE_MONTHLY_CREDITS,
)
from scrapewright.service.jobs import JobRegistry  # noqa: E402
from scrapewright.service.metering import Meter, _CountingLlm  # noqa: E402
from scrapewright.service.store import Store  # noqa: E402


class Exploding:
    """An extractor that fails the way the real one did: after being called."""

    def synthesize(self, html, url, schema=None):
        raise RuntimeError("upstream said 400")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "failures.db")


@pytest.fixture
def keyed(store):
    raw, key = store.create_key(label="alice", plan="metered")
    store.grant(key.id, 5_000, "test float", idempotency_key="float")
    # The monthly free credits are granted lazily on the first request. Hand
    # them out now, or "the balance before" is measured before a grant that has
    # not happened yet and every assertion below is off by a thousand.
    store.ensure_free_allowance(key.id, FREE_MONTHLY_CREDITS)
    return raw, key


def test_a_synthesis_that_raised_is_not_counted():
    """The meter used to increment before the call, so a 400 still billed 300."""
    meter = Meter()
    llm = _CountingLlm(inner=Exploding(), meter=meter)

    with pytest.raises(RuntimeError):
        llm.synthesize("<html>", "https://x.test")

    assert meter.syntheses == 0


def test_a_failed_extract_charges_nothing(store, keyed, monkeypatch):
    raw, key = keyed
    before = store.balance(key.id)

    class Failing:
        def __init__(self, *a, **kw): pass
        def extract(self, *a, **kw): raise RuntimeError("boom")
        def close(self): pass

    monkeypatch.setattr(app_module, "metered_scrapewright",
                        lambda **kw: (Failing(), Meter()))
    client = TestClient(create_app(store=store, jobs=JobRegistry()),
                        raise_server_exceptions=False)

    r = client.post("/v1/extract", json={"url": "https://x.test"},
                    headers={"X-API-Key": raw})

    assert r.status_code == 502            # not a 500, and not a charge
    assert store.balance(key.id) == before


def test_a_failed_extract_does_not_bill_a_dead_synthesis(store, keyed, monkeypatch):
    """The exact production shape: the synthesis was attempted and failed."""
    raw, key = keyed
    before = store.balance(key.id)
    meter = Meter()

    class Failing:
        def __init__(self, *a, **kw): pass
        def extract(self, *a, **kw):
            _CountingLlm(inner=Exploding(), meter=meter).synthesize("<h>", "u")
        def close(self): pass

    monkeypatch.setattr(app_module, "metered_scrapewright",
                        lambda **kw: (Failing(), meter))
    client = TestClient(create_app(store=store, jobs=JobRegistry()),
                        raise_server_exceptions=False)

    client.post("/v1/extract", json={"url": "https://x.test"},
                headers={"X-API-Key": raw})

    assert store.balance(key.id) == before
    assert before - store.balance(key.id) != CREDITS_PER_SYNTHESIS


def test_a_failed_crawl_charges_nothing(store, keyed, monkeypatch):
    raw, key = keyed
    before = store.balance(key.id)

    class Failing:
        def __init__(self, *a, **kw): pass
        def crawl_records(self, *a, **kw): raise RuntimeError("boom")
        def close(self): pass

    monkeypatch.setattr(app_module, "metered_scrapewright",
                        lambda **kw: (Failing(), Meter()))
    client = TestClient(create_app(store=store, jobs=JobRegistry()))

    job = client.post("/v1/crawl", json={"url": "https://x.test", "max_items": 5},
                      headers={"X-API-Key": raw}).json()
    for _ in range(50):
        state = client.get(f"/v1/jobs/{job['job_id']}",
                           headers={"X-API-Key": raw}).json()
        if state["status"] in {"done", "error", "failed"}:
            break

    assert store.balance(key.id) == before


def test_work_that_did_happen_is_still_charged(store, keyed, monkeypatch):
    """The fix must not become "nothing is ever billed"."""
    raw, key = keyed
    before = store.balance(key.id)
    from scrapewright.models import Record

    class Working:
        def __init__(self, *a, **kw): pass
        def extract(self, *a, **kw):
            return Record(url="https://x.test/1", schema_name="product",
                          data={"title": "A thing"})
        def close(self): pass

    monkeypatch.setattr(app_module, "metered_scrapewright",
                        lambda **kw: (Working(), Meter()))
    client = TestClient(create_app(store=store, jobs=JobRegistry()))

    r = client.post("/v1/extract", json={"url": "https://x.test"},
                    headers={"X-API-Key": raw})

    assert r.status_code == 200
    assert store.balance(key.id) < before
