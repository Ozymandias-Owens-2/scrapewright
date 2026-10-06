"""Where the browser ceiling is taken, and where it is not.

A live Sheetwatch run failed about half its rows with "all 1 browser slots
are busy" -- including plain Shopify pages that never open a browser. The
slot was taken around the whole request, so one `js=true` extract held the
only one through its static fetch and through a sixty-second model call,
and the second row in flight waited twenty seconds and gave up.

The ceiling exists to stop two Chromiums running at once. It was stopping
two requests running at once, which is a different and much more expensive
thing.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from scrapewright.models import Record
from scrapewright.service import browser_pool, metering
from scrapewright.service.app import create_app
from scrapewright.service.browser_pool import BrowserPool, NoBrowserSlot
from scrapewright.service.jobs import JobRegistry
from scrapewright.service.store import Store

URL = "https://shop.test/p/1"


@pytest.fixture(autouse=True)
def one_slot():
    before = browser_pool._pool
    browser_pool.set_pool(BrowserPool(slots=1, wait=0.3))
    yield browser_pool.get_pool()
    browser_pool.set_pool(before)


class _Watcher:
    """A stand-in browser that records how many slots were held as it ran."""

    def __init__(self, pool, delay: float = 0.0):
        self.pool, self.delay, self.seen = pool, delay, []

    def fetch(self, url):
        self.seen.append(self.pool.in_use)
        time.sleep(self.delay)
        return "<html><body>rendered</body></html>"

    def close(self):
        pass


# ── the slot is held around the render, and only around the render ──────────
def test_the_slot_is_held_while_rendering(one_slot, monkeypatch):
    watcher = _Watcher(one_slot)
    monkeypatch.setattr(one_slot, "fetcher", lambda **kw: watcher)

    sw, meter = metering.metered_scrapewright(js=True)

    assert one_slot.in_use == 0          # building the pipeline takes nothing
    sw._browser_fetch(URL)
    assert watcher.seen == [1]           # ...and the render itself takes one
    assert one_slot.in_use == 0          # given straight back
    assert meter.renders == 1


def test_no_slot_is_held_while_the_model_thinks(one_slot, monkeypatch):
    """The sixty seconds that broke the live run.

    Synthesis happens between fetches, and nothing may be held across it.
    """
    monkeypatch.setattr(one_slot, "fetcher", lambda **kw: _Watcher(one_slot))

    sw, meter = metering.metered_scrapewright(js=True)
    during = []

    class _Slow:
        def synthesize(self, html, url, schema=None, **kw):
            during.append(one_slot.in_use)
            return None

    sw.llm = metering._CountingLlm(_Slow(), meter)
    sw.llm.synthesize("<html>", URL)
    assert during == [0]


def test_a_render_that_cannot_get_a_slot_says_so(one_slot, monkeypatch):
    monkeypatch.setattr(one_slot, "fetcher", lambda **kw: _Watcher(one_slot))
    sw, _ = metering.metered_scrapewright(js=True)

    with one_slot.slot():
        with pytest.raises(NoBrowserSlot):
            sw._browser_fetch(URL)


def test_a_render_that_never_happened_is_not_counted(one_slot, monkeypatch):
    """Slot outside, counter inside: nobody pays five credits for a 503."""
    monkeypatch.setattr(one_slot, "fetcher", lambda **kw: _Watcher(one_slot))
    sw, meter = metering.metered_scrapewright(js=True)

    with one_slot.slot():
        with pytest.raises(NoBrowserSlot):
            sw._browser_fetch(URL)
    assert meter.renders == 0


def test_a_crawl_waits_for_its_turn_rather_than_being_refused(one_slot, monkeypatch):
    """The long wait moved from around the job to around each render. If it
    had not, a job holding the only slot would have blocked its own
    renders."""
    monkeypatch.setattr(one_slot, "fetcher", lambda **kw: _Watcher(one_slot))
    sw, _ = metering.metered_scrapewright(js=True, slot_wait=10)

    held = threading.Event()
    done = threading.Event()

    def occupy():
        with one_slot.slot():
            held.set()
            time.sleep(0.6)

    threading.Thread(target=occupy, daemon=True).start()
    assert held.wait(1)

    def render():
        sw._browser_fetch(URL)           # waits, does not raise
        done.set()

    threading.Thread(target=render, daemon=True).start()
    assert done.wait(5), "a crawl's render should wait rather than be refused"


# ── through the actual endpoint ──────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A service whose extraction is slow but never touches a browser.

    This is the Sheetwatch case exactly: `js=true` sent on every row so the
    server can decide, against pages that turn out to be plain Shopify.
    """
    class _Slow:
        def extract(self, url, schema, **kw):
            time.sleep(0.6)             # a model call, as far as timing goes
            return Record(url=url, schema_name=schema.name,
                          data={"title": "A", "price": "10"},
                          source_platform="selector")

        def close(self):
            pass

    monkeypatch.setattr("scrapewright.service.app.metered_scrapewright",
                        lambda **kw: (_Slow(), metering.Meter()))
    store = Store(str(tmp_path / "s.db"))
    raw, key = store.create_key(label="t", plan="metered")
    with TestClient(create_app(store=store, jobs=JobRegistry())) as c:
        c.headers.update({"X-API-Key": raw})
        yield c


def test_two_rows_at_once_both_go_through(client):
    """Both ask for js=true; neither needs a browser. With the ceiling
    around the request, the second waited and got a 503 -- which is what
    failed half a live run."""
    codes = []

    def row():
        codes.append(client.post("/v1/extract",
                                 json={"url": URL, "js": True}).status_code)

    threads = [threading.Thread(target=row) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert codes == [200, 200]


def test_a_request_holds_no_slot_at_all_while_it_runs(client, one_slot):
    """The slot stays free for whoever actually needs a browser."""
    free = []

    def watch():
        time.sleep(0.2)
        free.append(one_slot.in_use)

    watcher = threading.Thread(target=watch)
    watcher.start()
    client.post("/v1/extract", json={"url": URL, "js": True})
    watcher.join(5)

    assert free == [0]
