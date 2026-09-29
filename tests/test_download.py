"""A finished crawl can leave as a file, and only for the key that ran it."""
import codecs
import csv
import io

import pytest
from fastapi.testclient import TestClient

from scrapewright.service.app import create_app
from scrapewright.service.jobs import JobRegistry
from scrapewright.service.store import Store


@pytest.fixture()
def client_and_keys(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    mine, _ = store.create_key(label="mine", plan="metered")
    theirs, _ = store.create_key(label="theirs", plan="metered")
    jobs = JobRegistry()
    app = create_app(store=store, jobs=jobs)
    return TestClient(app), jobs, mine, theirs


def _finished_job(jobs, key_id):
    rows = [{"url": "https://x.test/1", "schema": "job", "source": "selector",
             "data": {"title": "Backend Engineer", "salary": 72000.0}},
            {"url": "https://x.test/2", "schema": "job", "source": "selector",
             "data": {"title": "Data Engineer", "salary": 65000.0}}]
    job = jobs.submit(key_id, "crawl", lambda: ({"count": 2, "records": rows}, {}))
    while job.status not in ("done", "error"):
        pass
    return job


def test_csv_carries_the_rows(client_and_keys):
    client, jobs, mine, _ = client_and_keys
    job = _finished_job(jobs, client.get("/v1/usage", headers={"X-API-Key": mine}).json()["key_id"])
    r = client.get(f"/v1/jobs/{job.id}/download", headers={"X-API-Key": mine})
    assert r.status_code == 200
    # utf-8-sig: the writer emits a BOM on purpose, so Excel opens a CSV full
    # of accents and currency symbols without mangling them.
    assert r.content.startswith(codecs.BOM_UTF8)
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert [row["title"] for row in rows] == ["Backend Engineer", "Data Engineer"]
    assert "attachment" in r.headers["content-disposition"]


def test_another_key_cannot_download_it(client_and_keys):
    client, jobs, mine, theirs = client_and_keys
    job = _finished_job(jobs, client.get("/v1/usage", headers={"X-API-Key": mine}).json()["key_id"])
    assert client.get(f"/v1/jobs/{job.id}/download",
                      headers={"X-API-Key": theirs}).status_code == 404


def test_an_unknown_format_is_refused(client_and_keys):
    client, jobs, mine, _ = client_and_keys
    job = _finished_job(jobs, client.get("/v1/usage", headers={"X-API-Key": mine}).json()["key_id"])
    assert client.get(f"/v1/jobs/{job.id}/download?format=pdf",
                      headers={"X-API-Key": mine}).status_code == 400


def test_a_download_needs_a_key(client_and_keys):
    client, jobs, mine, _ = client_and_keys
    job = _finished_job(jobs, client.get("/v1/usage", headers={"X-API-Key": mine}).json()["key_id"])
    assert client.get(f"/v1/jobs/{job.id}/download").status_code == 401
