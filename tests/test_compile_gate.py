"""One compile per site, however many requests arrive at once.

A spreadsheet sends two rows together and they are almost always two pages
of the same shop. If that shop is new, both of them paid a model to read
it: 300 credits twice for one answer, and two of the day's fifty compiles.
Four shops did exactly that in one live run -- sanjorge.cafe,
cosmos-cafe.es, salem.cafe, tegernseer-kaffeeroesterei.de -- because their
products happened to sit next to each other in the sheet.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from scrapewright.cache import RecipeCache
from scrapewright.extract.base import SelectorRecipe
from scrapewright.pipeline import CompileBusy, Scrapewright, compile_gate
from scrapewright.schema import PRODUCT_SCHEMA
from scrapewright.service.app import create_app
from scrapewright.service.jobs import JobRegistry
from scrapewright.service.store import Store

PAGE = """<html><body>
  <h1 class="product__title">A Thing</h1>
  <p class="price">&euro;19,90</p>
</body></html>"""

RECIPE = SelectorRecipe(fields={"title": ".product__title", "price": ".price"},
                        schema_name="product")


class _Session:
    def get(self, url, **kw):
        class _R:
            text, status_code = PAGE, 200

            def raise_for_status(self):
                pass
        return _R()


class _SlowLlm:
    """A model call that takes long enough for a second request to arrive."""

    def __init__(self, delay=0.4):
        self.delay, self.calls = delay, 0
        self._lock = threading.Lock()

    def synthesize(self, html, url, schema=None, **kw):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return RECIPE.model_copy(deep=True)


def _pipeline(tmp_path, llm, name="recipes.json"):
    sw = Scrapewright(session=_Session(),
                      cache=RecipeCache(tmp_path / name))
    sw.llm = llm
    return sw


def _both(target, args_a, args_b):
    out = {}

    def run(tag, args):
        try:
            out[tag] = target(*args)
        except Exception as e:                   # recorded, asserted on
            out[tag] = e

    threads = [threading.Thread(target=run, args=("a", args_a)),
               threading.Thread(target=run, args=("b", args_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return out


def test_one_new_site_two_pages_is_one_compile(tmp_path):
    """The whole point. Both rows come back; only one of them paid."""
    llm = _SlowLlm()
    sw = _pipeline(tmp_path, llm)

    out = _both(lambda url: sw.extract(url, PRODUCT_SCHEMA),
                ("https://shop.test/p/1",), ("https://shop.test/p/2",))

    assert llm.calls == 1
    assert out["a"] is not None and out["b"] is not None
    assert out["a"].data["title"] == "A Thing"
    assert out["b"].data["title"] == "A Thing"


def test_two_different_sites_compile_at_the_same_time(tmp_path):
    """Sites must not queue behind each other: the gate is per site."""
    llm = _SlowLlm(delay=0.5)
    sw = _pipeline(tmp_path, llm)

    start = time.perf_counter()
    out = _both(lambda url: sw.extract(url, PRODUCT_SCHEMA),
                ("https://one.test/p/1",), ("https://two.test/p/1",))
    elapsed = time.perf_counter() - start

    assert llm.calls == 2
    assert out["a"] is not None and out["b"] is not None
    assert elapsed < 0.9, "two sites were compiled one after the other"


def test_the_waiter_replays_rather_than_recompiling(tmp_path):
    """The second request's own page proves the recipe before it is kept.

    Waiting is only worth it if what you get at the end is usable here, so
    the fresh recipe is replayed against this request's own HTML.
    """
    llm = _SlowLlm()
    sw = _pipeline(tmp_path, llm)
    sw.extract("https://shop.test/p/1", PRODUCT_SCHEMA)
    assert llm.calls == 1

    sw.extract("https://shop.test/p/2", PRODUCT_SCHEMA)
    assert llm.calls == 1          # cached, no gate needed, no model call


def test_waiting_too_long_is_an_honest_refusal():
    with compile_gate("shop.test", wait=5):
        with pytest.raises(CompileBusy):
            with compile_gate("shop.test", wait=0.1):
                pass


# ── and what the caller sees ────────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scrapewright.service import metering

    class _Busy:
        def extract(self, url, schema, **kw):
            raise CompileBusy("another request is already learning shop.test")

        def close(self):
            pass

    monkeypatch.setattr("scrapewright.service.app.metered_scrapewright",
                        lambda **kw: (_Busy(), metering.Meter()))
    store = Store(str(tmp_path / "s.db"))
    raw, key = store.create_key(label="t", plan="metered")
    with TestClient(create_app(store=store, jobs=JobRegistry())) as c:
        c.headers.update({"X-API-Key": raw})
        yield c, store, key


def test_a_request_that_gave_up_waiting_is_503_and_free(client):
    c, store, key = client
    c.get("/v1/usage")                      # the allowance lands here
    before = store.balance(key.id)

    r = c.post("/v1/extract", json={"url": "https://shop.test/p/1"})

    assert r.status_code == 503
    assert r.headers["Retry-After"]
    assert store.balance(key.id) == before
