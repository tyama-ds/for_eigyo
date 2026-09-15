"""Large result contracts: full measurements with bounded browser payloads."""
import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import main, storage, large_storage, reports
from app.analytics import analyze
from app.ingest import demo_papers


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 2)
    main.JOBS.clear()
    with TestClient(main.app) as client:
        yield client
    main.JOBS.clear()


@pytest.fixture
def saved(client):
    papers = demo_papers()[:24]
    result = analyze(papers, {"start_year": 2021, "end_year": 2025, "n_topics": 2})
    result.update(id=storage.new_id(), dataset_id=storage.new_id(), dataset_name="Indexed fixture", created_at=storage.now())
    # Map/evidence deliberately omit some papers, as happens above the map limit.
    result["map"]["nodes"] = result["map"]["nodes"][:2]
    for topic in result["topics"]:
        topic["evidence_ids"] = topic["evidence_ids"][:1]
    result["papers"][-1]["id"] = "doi:10.1234/steel-test"
    storage.save("results", result)
    return result


def test_capacity_and_multiyear_validation(client):
    limits = client.get("/api/status").json()["limits"]
    assert limits == {"max_import_rows": 20000, "max_dataset_papers": 200000,
                      "max_upload_bytes": None, "max_analysis_years": 50, "paper_page_limit": 200}
    body = {"dataset_id": "a" * 32, "start_year": 1976, "end_year": 2025}
    # Missing dataset is checked only after 50-year input validation succeeds.
    assert client.post("/api/analyze", json=body).status_code == 404
    body["start_year"] = 1975
    rejected = client.post("/api/analyze", json=body)
    assert rejected.status_code == 422 and "50" in rejected.text


def test_summary_is_bounded_and_keeps_full_aggregates(client, saved, monkeypatch):
    original = storage.read
    reads = []
    def read(kind, identifier, **kwargs):
        reads.append(kwargs.get("include_papers", True))
        return original(kind, identifier, **kwargs)
    monkeypatch.setattr(storage, "read", read)
    response = client.get(f"/api/results/{saved['id']}?view=summary")
    assert response.status_code == 200, response.text
    summary = response.json()
    assert reads == [False]
    assert summary["meta"]["papers_total"] == 24
    assert summary["meta"]["papers_truncated"]
    assert summary["meta"]["papers_loaded"] == len(summary["papers"]) < 24
    assert summary["topics"] == saved["topics"]
    assert summary["timeline"] == saved["timeline"]
    assert summary["summary"] == saved["summary"]
    assert summary["top_cited_papers"] == saved["top_cited_papers"]
    assert "_large_store" not in summary and "export_data" not in summary["network"]
    full = client.get(f"/api/results/{saved['id']}").json()
    assert full == saved


def test_indexed_pages_and_lazy_paper_detail(client, saved):
    url = f"/api/results/{saved['id']}/papers"
    first = client.get(url, params={"limit": 5, "sort": "citations"}).json()
    second = client.get(url, params={"limit": 5, "offset": 5, "sort": "citations"}).json()
    assert first["total"] == second["total"] == 24
    assert len(first["items"]) == len(second["items"]) == 5
    assert not {p["id"] for p in first["items"]} & {p["id"] for p in second["items"]}
    pid = "doi:10.1234/steel-test"
    assert client.get(url + "/" + quote(pid, safe="")).json()["id"] == pid
    assert client.get(url + "/absent").status_code == 404
    assert client.get(url, params={"query": "not-a-paper-fragment"}).json()["total"] == 0
    for params in ({"limit": 201}, {"offset": -1}, {"sort": "payload"}, {"query": "x" * 501}):
        assert client.get(url, params=params).status_code == 422


def test_summary_insights_and_terrain_do_not_restore_entire_corpus(client, saved, monkeypatch):
    original = storage.read
    def read(kind, identifier, **kwargs):
        assert kwargs.get("include_papers") is False
        return original(kind, identifier, **kwargs)
    monkeypatch.setattr(storage, "read", read)
    response = client.post("/api/insights", json={"result_id": saved["id"]})
    assert response.status_code == 200 and response.json()["mode"] == "local"
    assert client.get(f"/api/results/{saved['id']}/terrain").status_code == 200


def test_json_export_restores_every_paper_and_network(client, saved):
    response = client.get(f"/api/results/{saved['id']}/export?format=json")
    assert response.status_code == 200, response.text
    assert json.loads(response.content) == saved
    assert "attachment" in response.headers["content-disposition"]
    for format in ("csv", "html"):
        response = client.get(f"/api/results/{saved['id']}/export?format={format}")
        assert response.status_code == 200, response.text
        expected = reports.topic_csv(saved) if format == "csv" else reports.report_html(saved)
        assert response.text == expected


def test_foresight_limit_checked_before_full_result_read(client, monkeypatch):
    identifier = storage.new_id()
    storage.save("results", {"id": identifier, "meta": {"paper_count": 200000}, "papers": []})
    original = storage.read
    def read(kind, identifier, **kwargs):
        assert kwargs.get("include_papers") is False
        return original(kind, identifier, **kwargs)
    monkeypatch.setattr(storage, "read", read)
    response = client.post("/api/assessments", json={"result_id": identifier})
    assert response.status_code == 422 and "10,000" in response.text
    assert not main.JOBS


def test_analysis_queues_identifier_and_prevents_parallel_corpus_loading(client, monkeypatch):
    dataset = storage.create_dataset([{"id": "p", "year": 2025}], "Fixture")
    submitted = []
    monkeypatch.setattr(main, "submit_with_context", lambda *args: submitted.append(args))
    body = {"dataset_id": dataset["id"], "start_year": 2000, "end_year": 2025}
    response = client.post("/api/analyze", json=body)
    assert response.status_code == 200
    assert submitted[0][3] == dataset["id"]
    assert client.post("/api/analyze", json=body).status_code == 409
    assert len(submitted) == 1


def test_import_accepts_a_file_larger_than_old_twenty_mb_cap(client):
    # A real parse, not a mocked parser: whitespace padding keeps one valid row.
    content = b"EID,Title,Year,Abstract\np1,Steel tensile strength,2025," + b" " * (21 * 1024 * 1024) + b"Steel\n"
    response = client.post("/api/import", files={"file": ("long.csv", content, "text/csv")})
    assert response.status_code == 200, response.text
    assert response.json()["dataset"]["paper_count"] == 1
