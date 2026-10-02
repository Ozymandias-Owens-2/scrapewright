"""The browser on a machine that holds nothing worth stealing.

Chromium is the part most likely to be broken into: the delivery mechanism
for a renderer exploit is a URL, and this product accepts URLs for money.
Beside the API, an escape lands on the Stripe key, the model key, the backup
credentials and write access to the database. On its own machine it lands on
a container that can fetch web pages.
"""
import pytest
from fastapi.testclient import TestClient

from scrapewright.service.browser_pool import BrowserPool
from scrapewright.service.remote_browser import RemoteBrowserFetcher
from scrapewright.service.render import create_render_app

TOKEN = "a-shared-secret"
PAGE = "<html><body><h1>rendered</h1></body></html>"


class _Browser:
    """Stands in for Chromium: no driver started in these tests."""
    alive = True

    def __init__(self, **kwargs):
        self.asked = []

    def fetch(self, url):
        self.asked.append(url)
        return None if "blocked" in url else PAGE

    def close(self): pass


@pytest.fixture()
def renderer(monkeypatch):
    monkeypatch.setenv("RENDER_TOKEN", TOKEN)
    monkeypatch.setattr("scrapewright.fetch.BrowserFetcher", _Browser)
    pool = BrowserPool(slots=1, wait=0.2)
    return TestClient(create_render_app(pool)), pool


# ── it answers only to us ────────────────────────────────────────────────────
def test_a_caller_without_the_token_is_refused(renderer):
    client, _ = renderer
    r = client.post("/render", json={"url": "https://x.test/p"})
    assert r.status_code == 401


def test_a_wrong_token_is_refused(renderer):
    client, _ = renderer
    r = client.post("/render", json={"url": "https://x.test/p"},
                    headers={"X-Render-Token": "nearly-right"})
    assert r.status_code == 401


def test_without_a_configured_token_it_answers_nobody(monkeypatch):
    """Private networking is not authentication: anything in the same
    organisation can reach this address."""
    monkeypatch.delenv("RENDER_TOKEN", raising=False)
    client = TestClient(create_render_app(BrowserPool(slots=1)))
    r = client.post("/render", json={"url": "https://x.test/p"},
                    headers={"X-Render-Token": ""})
    assert r.status_code == 503


# ── it renders ───────────────────────────────────────────────────────────────
def test_it_returns_the_html(renderer):
    client, _ = renderer
    r = client.post("/render", json={"url": "https://x.test/p"},
                    headers={"X-Render-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["html"] == PAGE


def test_a_page_it_may_not_read_comes_back_empty(renderer):
    """robots and the address rules travel with the browser: it runs the
    same fetcher, so moving it does not move it outside the rules."""
    client, _ = renderer
    r = client.post("/render", json={"url": "https://x.test/blocked"},
                    headers={"X-Render-Token": TOKEN})
    assert r.status_code == 200 and r.json()["html"] is None


def test_its_slots_are_its_own(renderer):
    client, pool = renderer
    with pool.slot():
        r = client.post("/render", json={"url": "https://x.test/p"},
                        headers={"X-Render-Token": TOKEN})
    assert r.status_code == 503
    assert r.headers["Retry-After"]


def test_health_needs_no_token_and_no_browser(renderer):
    client, _ = renderer
    body = client.get("/health").json()
    assert body["ok"] and body["browser_slots"] == 1


# ── the client side ──────────────────────────────────────────────────────────
class _Session:
    def __init__(self, behaviour): self.behaviour, self.calls = behaviour, 0

    def post(self, url, **kwargs):
        self.calls += 1
        return self.behaviour(url, **kwargs)


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code, self._payload = status_code, payload
    def json(self): return self._payload


def test_the_caller_sends_the_token_and_gets_the_html():
    seen = {}

    def behaviour(url, **kwargs):
        seen.update(kwargs["headers"])
        return _Response(200, {"html": PAGE})

    fetcher = RemoteBrowserFetcher("http://render.internal:8080", TOKEN,
                                   session=_Session(behaviour))
    assert fetcher.fetch("https://x.test/p") == PAGE
    assert seen["X-Render-Token"] == TOKEN


def test_an_unreachable_renderer_falls_back_here(caplog):
    import requests

    def behaviour(url, **kwargs):
        raise requests.ConnectionError("no route to host")

    local = _Browser()
    fetcher = RemoteBrowserFetcher("http://render.internal:8080", TOKEN,
                                   session=_Session(behaviour),
                                   local_fallback=local)
    assert fetcher.fetch("https://x.test/p") == PAGE
    assert fetcher.used_fallback
    assert local.asked == ["https://x.test/p"]


def test_the_fallback_is_loud():
    """It puts a browser back beside the secrets, so it must never become
    the normal state quietly."""
    import logging
    import requests

    def behaviour(url, **kwargs):
        raise requests.ConnectionError("no route")

    logger = logging.getLogger("scrapewright.service")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        RemoteBrowserFetcher("http://render.internal:8080", TOKEN,
                             session=_Session(behaviour),
                             local_fallback=_Browser()).fetch("https://x.test/p")
    finally:
        logger.removeHandler(handler)

    assert any(r.levelno >= logging.ERROR for r in records)


def test_a_busy_renderer_is_not_a_reason_to_render_here():
    """Falling back on a full queue would defeat the slot limit that keeps
    the machine alive."""
    local = _Browser()
    fetcher = RemoteBrowserFetcher(
        "http://render.internal:8080", TOKEN,
        session=_Session(lambda url, **kw: _Response(503)),
        local_fallback=local)

    assert fetcher.fetch("https://x.test/p") is None
    assert local.asked == []


# ── which one the pool hands out ─────────────────────────────────────────────
def test_the_pool_uses_the_renderer_when_one_is_configured(monkeypatch):
    monkeypatch.setenv("RENDER_URL", "http://render.internal:8080")
    monkeypatch.setenv("RENDER_TOKEN", TOKEN)
    monkeypatch.setattr("scrapewright.fetch.BrowserFetcher", _Browser)

    fetcher = BrowserPool(slots=1).fetcher()
    assert isinstance(fetcher, RemoteBrowserFetcher)
    assert fetcher.token == TOKEN


def test_without_one_it_renders_here(monkeypatch):
    monkeypatch.delenv("RENDER_URL", raising=False)
    monkeypatch.setattr("scrapewright.fetch.BrowserFetcher", _Browser)

    assert isinstance(BrowserPool(slots=1).fetcher(), _Browser)
