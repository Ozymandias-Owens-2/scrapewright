"""A typo in a hostname is the caller's mistake: 4xx, never a 500."""
import pytest
from fastapi.testclient import TestClient

from scrapewright.service.app import create_app
from scrapewright.service.jobs import JobRegistry
from scrapewright.service.store import Store

BAD = "https://www." + "a" * 70 + ".nl/"


@pytest.fixture()
def client(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    raw, _ = store.create_key(label="t", plan="metered")
    with TestClient(create_app(store=store, jobs=JobRegistry())) as c:
        c.headers.update({"X-API-Key": raw})
        yield c, store


def test_extract_says_400_not_500(client):
    c, _ = client
    r = c.post("/v1/extract", json={"url": BAD})
    assert r.status_code == 400
    assert "hostname" in r.json()["detail"]


def test_crawl_says_400_before_it_starts_a_job(client):
    c, _ = client
    r = c.post("/v1/crawl", json={"url": BAD})
    assert r.status_code == 400
    assert "job_id" not in r.json()


def test_a_refused_url_costs_nothing(client):
    c, store = client
    before = store.balance(c.get("/v1/usage").json()["key_id"])
    c.post("/v1/extract", json={"url": BAD})
    assert store.balance(c.get("/v1/usage").json()["key_id"]) == before


def test_detect_still_answers_200_with_a_note(client):
    """Routing advice degrades to a note rather than an error, by design."""
    c, _ = client
    r = c.post("/v1/detect", json={"url": BAD})
    assert r.status_code == 200
    assert r.json()["note"]


def test_the_mcp_tool_reports_it_as_an_error_field(client):
    """Over MCP an HTTPException becomes a returned error, not a crash."""
    c, _ = client
    r = c.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "extract_page", "arguments": {"url": BAD}}})
    assert r.status_code == 200
    body = r.json()["result"]["structuredContent"]
    assert body["status"] == 400


BLOCKED = "https://walled.test/lst?sort=standard"


@pytest.fixture()
def robots_says_no():
    """A site whose robots.txt forbids the path the caller typed."""
    from scrapewright import robots
    from scrapewright.robots import RobotsPolicy

    class _Response:
        status_code = 200
        text = "User-agent: *\nDisallow: /lst?\n"

    class _Session:
        def get(self, url, **kw): return _Response()

    before = robots.get_policy()
    robots.set_policy(RobotsPolicy(session=_Session()))
    yield
    robots.set_policy(before)


def test_a_disallowed_url_says_so_instead_of_returning_nothing(client, robots_says_no):
    """"Nothing came back, try another page" sent the caller off to debug
    their selectors for a decision robots.txt made before the first fetch."""
    c, _ = client
    r = c.post("/v1/crawl", json={"url": BLOCKED, "mode": "rows"})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "robots.txt" in detail and "obey" in detail


def test_a_disallowed_url_costs_nothing(client, robots_says_no):
    c, store = client
    key_id = c.get("/v1/usage").json()["key_id"]
    before = store.balance(key_id)
    c.post("/v1/extract", json={"url": BLOCKED})
    assert store.balance(key_id) == before
