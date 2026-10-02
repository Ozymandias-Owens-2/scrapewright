"""Two requests at once must not both compile.

The limit was read before the work and written after it, so two calls
arriving together both saw "none used today" and both called the model. A
free key, whose allowance is one compilation a day, produced two in a live
test on 2 October -- twice the spend the tier exists to cap.
"""
import threading

import pytest
from fastapi.testclient import TestClient

from scrapewright.service import metering
from scrapewright.service.app import create_app
from scrapewright.service.jobs import JobRegistry
from scrapewright.service.plans import get_tier
from scrapewright.service.store import Store

HTML = "<html><body><h1>A thing</h1><b>10.00</b></body></html>"


class _Fetcher:
    def fetch(self, url): return HTML
    def close(self): pass


class _SlowLlm:
    """Compiles slowly, so two requests really do overlap."""

    def __init__(self, barrier=None):
        self.calls = 0
        self._lock = threading.Lock()
        self.barrier = barrier

    def synthesize(self, html, url, schema=None, **kw):
        from scrapewright.extract.base import SelectorRecipe
        with self._lock:
            self.calls += 1
        if self.barrier is not None:
            try:
                self.barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
        return SelectorRecipe(fields={"title": "h1", "price": "b"})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    llm = _SlowLlm()

    def factory(*, js=False, meter=None, **kwargs):
        m = meter or metering.Meter()
        from scrapewright.cache import RecipeCache
        from scrapewright.pipeline import Scrapewright
        sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm,
                          fetcher=_Fetcher())
        sw.llm = metering._CountingLlm(llm, m)
        return sw, m

    monkeypatch.setattr("scrapewright.service.app.metered_scrapewright", factory)
    store = Store(str(tmp_path / "s.db"))
    raw, key = store.create_key(label="free", plan="metered")
    app = create_app(store=store, jobs=JobRegistry(max_workers=4))
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": raw})
        yield c, store, key, llm


def test_the_free_tier_allows_one_compile_a_day():
    assert get_tier("free").daily_syntheses == 1


def test_two_at_once_produce_one_compile_and_one_429(client):
    c, store, key, llm = client
    llm.barrier = threading.Barrier(2)       # hold the first inside the model
    results = []

    def go(i):
        results.append(c.post("/v1/extract",
                              json={"url": f"https://shop.test/p{i}"}).status_code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert sorted(results) == [200, 429], results
    assert llm.calls == 1, f"the model was called {llm.calls} times"


def test_a_refused_request_costs_no_credits(client):
    c, store, key, llm = client
    c.get("/v1/usage")                        # the allowance lands
    c.post("/v1/extract", json={"url": "https://shop.test/first"})
    before = store.balance(key.id)

    r = c.post("/v1/extract", json={"url": "https://other.test/p"})
    assert r.status_code == 429
    assert store.balance(key.id) == before


def test_the_reservation_is_handed_back_when_nothing_was_compiled(client):
    """An allowance must not be spent by a request that never called a
    model -- a cached site should not use up the day."""
    c, store, key, llm = client
    assert c.post("/v1/extract", json={"url": "https://shop.test/a"}).status_code == 200
    assert llm.calls == 1
    assert c.app.state.synthesis.held(key.id) == 0

    # Same site again: replayed from cache, no model, still allowed.
    assert c.post("/v1/extract", json={"url": "https://shop.test/b"}).status_code == 200
    assert llm.calls == 1


def test_a_second_key_is_unaffected(client, tmp_path):
    c, store, _, llm = client
    other_raw, _ = store.create_key(label="another", plan="metered")
    c.post("/v1/extract", json={"url": "https://shop.test/a"})

    r = c.post("/v1/extract", headers={"X-API-Key": other_raw},
               json={"url": "https://elsewhere.test/a"})
    assert r.status_code == 200


def test_a_paid_key_is_not_held_to_one(client, tmp_path):
    c, store, key, llm = client
    store.grant(key.id, 10_000, "pack", idempotency_key="stripe:cs_1")
    for i in range(3):
        r = c.post("/v1/extract", json={"url": f"https://site{i}.test/p"})
        assert r.status_code == 200, r.text
    assert llm.calls == 3
