"""The hosted MCP endpoint is the REST API in a different envelope: same key,
same credits. Exercised through the real SDK client, offline."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from scrapewright.service.app import create_app
from scrapewright.service.jobs import JobRegistry
from scrapewright.service.store import Store

pytest.importorskip("mcp")


def _call(client: TestClient, tool: str, args: dict, key: str | None):
    """One stateless JSON-RPC call, the way an HTTP client with no SDK does it."""
    headers = {"Accept": "application/json, text/event-stream"}
    if key:
        headers["X-API-Key"] = key
    r = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_tools_list_and_account(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    raw, _ = store.create_key(label="t", plan="metered")
    with TestClient(create_app(store=store, jobs=JobRegistry())) as client:
        r = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert {"detect_site", "extract_page", "crawl_site", "crawl_status", "account"} <= names

        out = _call(client, "account", {}, raw)
        assert out["structuredContent"]["credits"]["balance"] == 1000

        out = _call(client, "account", {}, "sw_bogus")
        assert out["structuredContent"]["status"] == 401
