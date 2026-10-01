"""The outage of 1 October, as tests.

A client sent eight concurrent renders. Each request started its own
Chromium; the machine has a gigabyte; the kernel killed one, which happened
to be the Playwright driver, and every later render answered "Connection
closed while reading from the driver". The health check ran in the same
thread pool, so the monitor saw nothing at all. One client, fifteen minutes,
everyone.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from scrapewright.service import browser_pool
from scrapewright.service.app import create_app
from scrapewright.service.browser_pool import BrowserPool, NoBrowserSlot
from scrapewright.service.jobs import JobRegistry
from scrapewright.service.store import Store


@pytest.fixture(autouse=True)
def one_slot():
    before = browser_pool._pool
    browser_pool.set_pool(BrowserPool(slots=1, wait=0.2))
    yield browser_pool.get_pool()
    browser_pool.set_pool(before)


# ── the ceiling ──────────────────────────────────────────────────────────────
def test_only_one_render_runs_at_a_time(one_slot):
    started = threading.Event()
    release = threading.Event()

    def hold():
        with one_slot.slot():
            started.set()
            release.wait(2)

    threading.Thread(target=hold, daemon=True).start()
    assert started.wait(1)

    with pytest.raises(NoBrowserSlot):
        with one_slot.slot():
            pass
    release.set()


def test_the_slot_comes_back_afterwards(one_slot):
    with one_slot.slot():
        assert one_slot.in_use == 1
    assert one_slot.in_use == 0
    with one_slot.slot():                 # free again
        pass


def test_a_crawl_waits_instead_of_being_refused(one_slot):
    done = threading.Event()

    def hold():
        with one_slot.slot():
            time.sleep(0.3)

    threading.Thread(target=hold, daemon=True).start()
    time.sleep(0.05)

    with one_slot.slot(wait=5):           # a job is patient
        done.set()
    assert done.is_set()


# ── a dead browser is replaced, not kept ─────────────────────────────────────
class _Corpse:
    """A fetcher whose browser has died, as happens after an OOM kill."""
    alive = False
    built = 0

    def __init__(self, **kwargs):
        type(self).built += 1

    def close(self): pass


def test_a_dead_browser_is_rebuilt_on_the_next_request(monkeypatch, one_slot):
    monkeypatch.setattr("scrapewright.fetch.BrowserFetcher", _Corpse)
    _Corpse.built = 0

    one_slot.fetcher()
    one_slot.fetcher()
    assert _Corpse.built == 2          # the corpse was thrown away


def test_a_live_browser_is_reused(monkeypatch, one_slot):
    class _Live(_Corpse):
        alive = True

    monkeypatch.setattr("scrapewright.fetch.BrowserFetcher", _Live)
    _Live.built = 0

    one_slot.fetcher()
    one_slot.fetcher()
    assert _Live.built == 1            # one Chromium, not one per request


# ── what the API does about it ───────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    raw, key = store.create_key(label="t", plan="metered")
    with TestClient(create_app(store=store, jobs=JobRegistry())) as c:
        c.headers.update({"X-API-Key": raw})
        yield c, store, key


def test_a_second_render_is_turned_away_and_costs_nothing(client, one_slot):
    c, store, key = client
    c.get("/v1/usage")                 # the monthly allowance lands here
    before = store.balance(key.id)

    with one_slot.slot():              # the only slot is taken
        r = c.post("/v1/extract", json={"url": "https://x.test/p", "js": True})

    assert r.status_code == 503
    assert r.headers["Retry-After"]
    assert store.balance(key.id) == before


def test_a_static_request_is_unaffected_while_a_render_runs(client, one_slot):
    c, *_ = client
    with one_slot.slot():
        r = c.post("/v1/extract", json={"url": "https://x.test/p"})
    assert r.status_code != 503


def test_health_answers_while_every_browser_is_busy(client, one_slot):
    c, *_ = client
    with one_slot.slot():
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["browsers_busy"] == 1
    assert r.json()["browser_slots"] == 1


# ── one client must not take the machine ─────────────────────────────────────
def test_a_key_may_only_have_so_many_requests_running(client, one_slot):
    """Eight at once from one client is what started the outage."""
    from scrapewright.service.app import MAX_IN_FLIGHT_PER_KEY

    c, _, key = client
    held = []
    limiter = c.app.state.in_flight
    for _ in range(MAX_IN_FLIGHT_PER_KEY):
        ctx = limiter.hold(key.id)
        ctx.__enter__()
        held.append(ctx)

    r = c.post("/v1/extract", json={"url": "https://x.test/p"})
    assert r.status_code == 429
    assert r.headers["Retry-After"]

    for ctx in held:
        ctx.__exit__(None, None, None)
    assert c.post("/v1/extract", json={"url": "https://x.test/p"}).status_code != 429


def test_the_count_comes_back_down(client):
    c, _, key = client
    limiter = c.app.state.in_flight
    with limiter.hold(key.id):
        pass
    for _ in range(10):                # would raise if it leaked
        with limiter.hold(key.id):
            pass


def test_one_key_filling_up_does_not_block_another(client, tmp_path):
    c, store, key = client
    other_raw, other = store.create_key(label="other", plan="metered")
    limiter = c.app.state.in_flight

    with limiter.hold(key.id), limiter.hold(key.id):
        r = c.post("/v1/extract", headers={"X-API-Key": other_raw},
                   json={"url": "https://x.test/p"})
    assert r.status_code != 429


def test_health_reports_the_browser_it_actually_has(client):
    """Playwright's sync API refuses to run inside an asyncio loop. The async
    health check was its first caller, the refusal was swallowed as "no
    browser", and lru_cache made that permanent: a deployment that could
    render perfectly well reported js=false until it was restarted."""
    from scrapewright.service.app import browser_available

    c, *_ = client
    assert c.get("/health").json()["js"] == browser_available()


def test_the_probe_is_primed_before_any_request(tmp_path):
    from scrapewright.service.app import browser_available, create_app

    browser_available.cache_clear()
    create_app(store=Store(str(tmp_path / "s.db")), jobs=JobRegistry())
    assert browser_available.cache_info().currsize == 1
