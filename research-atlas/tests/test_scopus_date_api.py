from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import scopus_date_api as api, storage
from app.scopus_date_api import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def create(client):
    dataset = storage.create_dataset([{"id": "2-s2.0-123", "title": "Example", "year": 2021, "publication_date": "", "date_precision": "year"}], "Test")
    response = client.post("/api/scopus-date-jobs", json={"dataset_id": dataset["id"]})
    assert response.status_code == 200
    return dataset, response.json()


def test_roundtrip_queue_import_preview_apply_export(client):
    dataset, job = create(client)
    base = f"/api/scopus-date-jobs/{job['id']}"
    queue = client.post(base + "/queue", json={"batch_size": 100}).json()
    assert queue["schema"] == "research-atlas.scopus-date-queue.v1"
    assert queue["entries"][0]["url"].endswith("/123?origin=resultslist")
    payload = {"schema": "research-atlas.scopus-date-results.v1", "job_id": job["id"], "batch_id": queue["batch_id"], "observations": [{**{key: queue["entries"][0][key] for key in ("eid", "url")}, "status": "found", "date_text": "March 2021", "matched_by": "testid", "observed_at": "2026-10-14T00:00:00Z"}]}
    response = client.post(base + "/import", json=payload)
    assert response.status_code == 200 and response.json()["found_eids"] == 1
    preview = client.get(base + "/records").json()
    result = client.post(base + "/apply", json={"snapshot_id": preview["snapshot_id"]})
    assert result.status_code == 200 and result.json()["applied_count"] == 1
    for format in ("json", "csv"):
        response = client.get(base + "/export", params={"format": format, "snapshot_id": preview["snapshot_id"]})
        assert response.status_code == 200 and "attachment" in response.headers["content-disposition"]
        assert "March 2021" in response.text
    assert client.get(f"/api/datasets/{dataset['id']}/scopus-dates").json()["coverage"]["missing_month"] == 1


@pytest.mark.parametrize("path,body", [("/queue", {"batch_size": 501}), ("/queue", {"batch_size": True}), ("/queue", {"retry_errors": "true"}), ("/apply", {"overwrite_existing": "true"}), ("/apply", {"cookies": "secret"}), ("/import", {"schema": "bad", "cookies": "secret"})])
def test_strict_payloads_no_secrets_echo(client, path, body):
    _, job = create(client)
    response = client.post(f"/api/scopus-date-jobs/{job['id']}" + path, json=body)
    assert response.status_code == 422
    assert "secret" not in response.text


def test_bounded_streamed_request_and_missing_ids(client, monkeypatch):
    monkeypatch.setattr(api, "MAX_BODY", 100)
    response = client.post("/api/scopus-date-jobs", content=b"x" * 101, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert client.get("/api/scopus-date-jobs/not-an-id").status_code == 404


def test_snapshot_policy_conflict(client):
    _, job = create(client)
    base = f"/api/scopus-date-jobs/{job['id']}"
    preview = client.get(base + "/records").json()
    response = client.post(base + "/apply", json={"snapshot_id": preview["snapshot_id"], "allow_year_change": True})
    assert response.status_code == 409
