"""Self-service signup — mostly a test of what it will not give away.

The endpoint has no API key, because it is where a stranger becomes a customer.
That makes the free allowance the asset: anyone who can mint keys can mint free
credits, and at ~$0.20 of real cost per allowance, a thousand fake accounts is
a real bill. These tests pin the two guards that stop it.
"""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from scrapewright.service.app import (  # noqa: E402
    MAX_SIGNUPS_PER_DAY,
    create_app,
)
from scrapewright.service.credits import FREE_MONTHLY_CREDITS  # noqa: E402
from scrapewright.service.jobs import JobRegistry  # noqa: E402
from scrapewright.service.store import Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "signup.db")


@pytest.fixture
def client(store):
    return TestClient(create_app(store=store, jobs=JobRegistry()))


def test_signup_returns_a_working_key(client, store):
    r = client.post("/v1/signup", json={"email": "alice@example.com"})

    assert r.status_code == 201
    body = r.json()
    assert body["api_key"].startswith("sw_")
    assert body["credits"] == FREE_MONTHLY_CREDITS
    # The key must actually authenticate, not just look like one.
    assert store.resolve(body["api_key"]) is not None


def test_only_the_hash_of_the_key_is_kept(client, store):
    raw = client.post("/v1/signup", json={"email": "alice@example.com"}).json()["api_key"]

    with store._conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM api_keys")]

    assert all(raw not in str(row.values()) for row in rows)


def test_the_email_is_not_stored_in_the_clear(client, store):
    client.post("/v1/signup", json={"email": "alice@example.com"})

    with store._conn() as conn:
        row = dict(conn.execute("SELECT * FROM api_keys").fetchone())

    assert "alice@example.com" not in str(row.get("email_hash"))
    assert row["email_hash"] and len(row["email_hash"]) == 64


def test_a_second_signup_at_one_address_gets_no_second_allowance(client, store):
    """The whole point. Otherwise signup is a free-credit printer."""
    first = client.post("/v1/signup", json={"email": "alice@example.com"}).json()
    second = client.post("/v1/signup", json={"email": "ALICE@example.com "}).json()

    assert first["credits"] == FREE_MONTHLY_CREDITS
    assert second["credits"] == 0
    # Both keys work; only the allowance is withheld.
    assert store.resolve(second["api_key"]) is not None


def test_one_address_cannot_take_unlimited_keys(client):
    for i in range(MAX_SIGNUPS_PER_DAY):
        ok = client.post("/v1/signup", json={"email": f"user{i}@example.com"})
        assert ok.status_code == 201

    refused = client.post("/v1/signup", json={"email": "onemore@example.com"})

    assert refused.status_code == 429


def test_a_malformed_address_is_refused(client):
    r = client.post("/v1/signup", json={"email": "not-an-email"})

    assert r.status_code == 400


def test_signup_does_not_leak_other_keys(client):
    client.post("/v1/signup", json={"email": "alice@example.com"})
    body = client.post("/v1/signup", json={"email": "bob@example.com"}).json()

    assert set(body) == {"api_key", "key_id", "credits", "note"}
