"""The public demo — the one endpoint with no key at all.

It exists to convert strangers, so it must be free for us to serve and boring
to abuse. Both of those are pinned here.
"""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from scrapewright.service import app as app_module  # noqa: E402
from scrapewright.service.app import MAX_DEMOS_PER_DAY, create_app  # noqa: E402
from scrapewright.service.jobs import JobRegistry  # noqa: E402
from scrapewright.service.store import Store  # noqa: E402


class FakeDetection:
    def __init__(self, kind: str, strategy: str):
        self.kind, self.strategy = kind, strategy


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "demo.db")


@pytest.fixture
def client(store):
    return TestClient(create_app(store=store, jobs=JobRegistry()))


def test_a_site_needing_compilation_is_turned_away(client, monkeypatch):
    """The demo must never trigger a model call: that is our money, unguarded."""
    monkeypatch.setattr(app_module, "detect",
                        lambda url, **kw: FakeDetection("unknown", "crawl"))

    r = client.post("/v1/demo", json={"url": "https://custom.test"})

    assert r.status_code == 422
    assert "catalogue" in r.json()["detail"]


def test_a_blocked_site_is_reported_plainly(client, monkeypatch):
    monkeypatch.setattr(app_module, "detect",
                        lambda url, **kw: FakeDetection("blocked", "blocked"))

    r = client.post("/v1/demo", json={"url": "https://fortress.test"})

    assert r.status_code == 422
    assert "refuses" in r.json()["detail"]


def test_the_demo_is_rate_limited(client, store, monkeypatch):
    monkeypatch.setattr(app_module, "detect",
                        lambda url, **kw: FakeDetection("shopify", "catalog"))
    monkeypatch.setattr(app_module, "Scrapewright", _stub_pipeline())

    for _ in range(MAX_DEMOS_PER_DAY):
        assert client.post("/v1/demo", json={"url": "https://shop.test"}).status_code == 200

    refused = client.post("/v1/demo", json={"url": "https://shop.test"})

    assert refused.status_code == 429


def test_a_refused_site_does_not_burn_the_allowance(client, store, monkeypatch):
    """Turning someone away should not count against their few free runs."""
    monkeypatch.setattr(app_module, "detect",
                        lambda url, **kw: FakeDetection("unknown", "crawl"))

    client.post("/v1/demo", json={"url": "https://custom.test"})

    assert store.count_events("testclient", "demo") == 0


def test_the_demo_returns_rows_and_says_where_it_stops(client, monkeypatch):
    monkeypatch.setattr(app_module, "detect",
                        lambda url, **kw: FakeDetection("shopify", "catalog"))
    monkeypatch.setattr(app_module, "Scrapewright", _stub_pipeline())

    body = client.post("/v1/demo", json={"url": "https://shop.test"}).json()

    assert body["platform"] == "shopify"
    assert body["count"] == 2
    assert body["records"][0]["data"]["title"] == "A shirt"
    assert "free key" in body["note"]


def test_the_demo_never_lets_the_model_run(client, monkeypatch):
    """Pinned explicitly: allow_llm must be False on an unauthenticated path."""
    seen = {}
    monkeypatch.setattr(app_module, "detect",
                        lambda url, **kw: FakeDetection("shopify", "catalog"))
    monkeypatch.setattr(app_module, "Scrapewright", _stub_pipeline(seen))

    client.post("/v1/demo", json={"url": "https://shop.test"})

    assert seen["allow_llm"] is False


def _stub_pipeline(seen: dict | None = None):
    from scrapewright.models import Record

    class StubPipeline:
        def __init__(self, *a, **kw):
            pass

        def crawl_records(self, url, **kw):
            if seen is not None:
                seen.update(kw)
            yield Record(url=url + "/1", schema_name="product",
                         data={"title": "A shirt", "price": "10.00"})
            yield Record(url=url + "/2", schema_name="product",
                         data={"title": "A coat", "price": "99.00"})

        def close(self):
            pass

    return StubPipeline
