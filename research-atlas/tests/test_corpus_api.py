"""Validation and export contract of whole-corpus report endpoints."""
import json

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app import corpus_reporting as reports
from test_corpus_reporting import ledger, finish, paper, result


@pytest.mark.parametrize("options", [{"batch_size": 0}, {"batch_size": 20001}, {"batch_size": True}, {"batch_size": "100"},
    {"run_all": "true"}, {"provider": "none"}, {"model": "bad model"}, {"result_id": "../outside"}, {"unknown": True}])
def test_bad_create_options_do_not_start_a_worker(ledger, options):
    saved = result([paper("a")])
    response = TestClient(app).post("/api/corpus-reports", json={"result_id": saved["id"], **options})
    assert response.status_code == 422 and not reports._ACTIVE


def test_start_detail_list_pages_resume_and_all_data_export(ledger):
    saved = result([paper("a"), paper("b"), paper("c", abstract="")])
    client = TestClient(app)
    response = client.post("/api/corpus-reports", json={"result_id": saved["id"], "batch_size": 1})
    assert response.status_code == 200
    report = finish(response.json()["id"])
    assert client.get("/api/corpus-reports", params={"result_id": saved["id"]}).json()["reports"][0]["id"] == report["id"]
    route = "/api/corpus-reports/" + report["id"]
    pending = client.get(route + "/papers?status=pending&limit=1").json()
    assert pending["total"] == 1 and pending["items"][0]["id"] == "b"
    assert client.get(route + "/papers?limit=101").status_code == 422
    assert client.post(route + "/resume", json={"retry_failed": "true"}).status_code == 422
    assert client.post(route + "/resume", json={"run_all": True}).status_code == 200
    finish(report["id"])
    export = client.get(route + "/export?format=json").json()
    assert len(export["papers"]) == 3 and export["report"]["counts"]["missing"] == 1
    csv = client.get(route + "/export?format=csv")
    assert csv.status_code == 200 and "abstract" in csv.text and "missing" in csv.text
    assert client.post(route + "/synthesize", json={}).status_code == 200
    assert finish(report["id"])["synthesis_status"] == "partial"


def test_missing_report_returns_404_without_path_or_provider_error(ledger):
    client = TestClient(app)
    response = client.get("/api/corpus-reports/" + "f" * 32)
    assert response.status_code == 404
    assert "traceback" not in response.text.lower()
