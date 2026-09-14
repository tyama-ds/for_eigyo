import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import analytics, field_exports, field_llm, storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with TestClient(app) as value:
        yield value


@pytest.fixture
def result(client):
    papers = []
    for i in range(16):
        title = "stainless steel tensile microstructure" if i % 2 else "laser welding steel fatigue"
        papers.append({"id": f"p{i}", "title": title + f" study {i}", "abstract": title + " tensile testing using SEM and finite element modeling.",
                       "year": 2021 + i % 5, "authors": [{"id": "orcid:0000-0001-1234-1234", "name": "Shared Researcher", "affiliations": ["Shared Institute"]}],
                       "affiliations": ["Institute A" if i % 2 else "Institute B"],
                       "references": [{"doi": f"10.1234/p{i-1}"}] if i else [], "references_status": "provided",
                       "keywords": [], "citations": None, "source": "Test journal", "doi": f"10.1234/p{i}", "citation_history": {}})
    result = analytics.analyze(papers, {"start_year": 2021, "end_year": 2025, "n_topics": 2, "topic_model": "nmf"})
    result.update(id=storage.new_id(), dataset_id=storage.new_id(), dataset_name="Field report fixture", created_at=storage.now())
    return storage.save("results", result)


def run(client, result, **options):
    body = {"result_id": result["id"], "topic_id": result["topics"][0]["id"],
            "neighbor_id": result["topics"][1]["id"], **options}
    response = client.post("/api/field-reports", json=body)
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + response.json()["job_id"]).json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(.02)
    pytest.fail("Report job did not complete")


def test_full_report_is_persisted_without_llm_and_exports_all_data(client, result, monkeypatch):
    monkeypatch.setattr(field_llm, "generate", lambda *args, **kwargs: pytest.fail("LLM must be opt-in"))
    before = json.dumps(result)
    job = run(client, result)
    assert job["status"] == "completed", job
    response = client.get("/api/field-reports/" + job["field_report_id"])
    report = response.json()
    assert report["narrative"]["mode"] == "deterministic"
    assert report["result_id"] == result["id"]
    assert sum(row["focus_count"] + row["neighbor_count"] for row in report["annual"]) == 16
    assert report["connections"]["shared_authors_total"] == 1
    assert report["institutions"]["focus_coverage_pct"] == 100
    assert report["methods"]["rows"]
    for kind in ("report", "data", "papers"):
        exported = client.get(f"/api/field-reports/{report['id']}/export?kind={kind}")
        assert exported.status_code == 200
        assert exported.content.startswith(b"\xef\xbb\xbf")
        assert "attachment" in exported.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
        assert rows
        if kind == "papers":
            assert len(rows) == 16
            assert all("Shared Institute" in row["Authors (JSON)"] for row in rows)
            assert all(row["Abstract"] for row in rows)
        if kind == "data":
            assert sum(row["Section"] == "annual" for row in rows) == 10
            assert any(row["Item / JSON Pointer"].startswith("/export_data/") for row in rows)
    assert json.dumps(storage.read("results", result["id"])) == before


def test_invalid_field_comparisons_are_rejected(client, result):
    topic = result["topics"][0]["id"]
    for options in ({"neighbor_id": topic}, {"topic_id": "unknown"}, {"provider": "remote_custom"}):
        response = client.post("/api/field-reports", json={"result_id": result["id"], "topic_id": topic, **options})
        assert response.status_code == 422
    assert client.get("/api/field-reports/" + "f" * 32).status_code == 404
    response = client.post("/api/field-reports", json={"result_id": result["id"], "topic_id": topic}, headers={"Origin": "https://other.example"})
    assert response.status_code == 403


def test_llm_failure_preserves_comparison_and_downloads(client, result, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("ローカルLLMに接続できません。")
    monkeypatch.setattr(field_llm, "generate", fail)
    job = run(client, result, provider="local", model="local-model")
    assert job["status"] == "failed"
    report = client.get("/api/field-reports/" + job["field_report_id"]).json()
    assert report["narrative"]["mode"] == "deterministic"
    assert report["llm_error"] == job["error"]
    exported = client.get(f"/api/field-reports/{report['id']}/export?kind=report")
    assert exported.status_code == 200 and "LLM生成エラー" in exported.text


def test_generated_report_text_and_evidence_are_exported(client, result, monkeypatch):
    narrative = {"mode": "local_llm", "model": "local-model", "headline": "総合比較", "sections": [
        {"title": "検討すべき仮説", "text": "=HYPERLINK(unsafe)\n仮説の改行", "evidence_ids": ["p1"]}], "caveats": ["検証が必要"]}
    monkeypatch.setattr(field_llm, "generate", lambda *args, **kwargs: narrative)
    job = run(client, result, provider="local")
    report = client.get("/api/field-reports/" + job["field_report_id"]).json()
    rows = list(csv.DictReader(io.StringIO(field_exports.report_csv(report).lstrip("\ufeff"))))
    section = next(row for row in rows if row["Section"] == "検討すべき仮説")
    assert section["Text"].startswith("'=HYPERLINK")
    assert "\n仮説の改行" in section["Text"]
    assert json.loads(section["Evidence paper IDs (JSON)"]) == ["p1"]
    assert section["Report mode"] == "local_llm"


def test_missing_neighbor_counts_are_null_in_csv():
    report = {"id": "r", "result_id": "a", "focus": {"id": "t", "label": "field"}, "neighbor": None,
              "annual": [{"year": 2025, "focus_count": 2, "neighbor_count": None}]}
    rows = list(csv.DictReader(io.StringIO(field_exports.data_csv(report).lstrip("\ufeff"))))
    missing = next(row for row in rows if row["Section"] == "annual" and row["Item / JSON Pointer"] == "neighbor")
    assert missing["Value"] == "" and missing["Value type"] == "null"


@pytest.mark.parametrize("successful_saves", [0, 1])
def test_storage_failure_always_terminates_report_job(client, result, monkeypatch, successful_saves):
    original = storage.save
    calls = 0
    def save(kind, value):
        nonlocal calls
        if kind == "field_reports":
            calls += 1
            if calls > successful_saves:
                raise OSError("Disk unavailable with private path")
        return original(kind, value)
    def fail(*args, **kwargs):
        raise RuntimeError("LLM unavailable")
    monkeypatch.setattr(storage, "save", save)
    monkeypatch.setattr(field_llm, "generate", fail)
    job = run(client, result, provider="local")
    assert job["status"] == "failed"
    assert "private path" not in job["error"]
    if successful_saves:
        saved = client.get("/api/field-reports/" + job["field_report_id"]).json()
        assert saved["narrative"]["mode"] == "deterministic"
        assert saved["annual"]
    else:
        assert "field_report_id" not in job
