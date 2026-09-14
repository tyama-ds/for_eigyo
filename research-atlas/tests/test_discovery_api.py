import copy
import time

import pytest
from fastapi.testclient import TestClient

from app import insights, storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    with TestClient(app) as value:
        yield value


def wait_job(client, identifier):
    end = time.monotonic() + 30
    while time.monotonic() < end:
        result = client.get("/api/jobs/" + identifier).json()
        if result["status"] in {"failed", "completed"}:
            return result
        time.sleep(0.05)
    pytest.fail("Job did not finish")


def paper(index=1, provider="crossref"):
    return {"id": f"{provider}:{index}", "title": f"Battery electrolyte transport study {index}",
        "year": 2020 + index, "abstract": "Lithium battery electrolyte ion transport performance.",
        "authors": [{"id": "name:test-researcher", "name": "Test Researcher"}],
        "keywords": ["battery", "electrolyte"], "citations": 3 * index, "doi": f"10.1234/p{index}",
        "source": "Journal of Test Fixtures", "citation_history": {}, "providers": [provider],
        "citation_source": provider, "external_url": f"https://doi.org/10.1234/p{index}",
        "citation_snapshots": [{"provider": provider, "count": 3 * index, "retrieved_at": "2026-09-11T00:00:00Z"}]}


def fake_discover(**options):
    options["progress"]("検索中のテスト")
    return [paper(i) for i in range(1, 6)], {
        "provider": "crossref", "warnings": ["各年の関連度上位を取得しています。"], "truncated": True,
        "year_coverage": [{"year": 2020 + i, "total": 100, "imported": 1} for i in range(1, 6)]}


def discover(client):
    response = client.post("/api/discover", json={"provider": "crossref", "query": "battery", "start_year": 2021, "end_year": 2025, "limit": 20})
    assert response.status_code == 200, response.text
    result = wait_job(client, response.json()["job_id"])
    assert result["status"] == "completed", result
    return result


def test_discovery_requires_no_scopus_and_preserves_scope(client, monkeypatch):
    monkeypatch.setattr("app.sources.discover", fake_discover)
    catalog = client.get("/api/sources").json()["providers"]
    assert {p["id"] for p in catalog} == {"crossref", "arxiv", "europepmc"}
    imported = discover(client)
    assert imported["dataset"]["paper_count"] == 5
    assert imported["dataset"]["is_demo"] is False
    assert imported["report"]["abstract_coverage"] == 100
    result = wait_job(client, client.post("/api/analyze", json={"dataset_id": imported["dataset"]["id"], "start_year": 2021, "end_year": 2025, "n_topics": 2}).json()["job_id"])
    assert result["status"] == "completed", result
    analysis = client.get("/api/results/" + result["result_id"]).json()
    assert analysis["meta"]["sampled"] is True
    assert analysis["meta"]["providers"] == ["crossref"]
    assert analysis["meta"]["citation_sources"] == ["crossref"]
    assert analysis["papers"][0]["external_url"].startswith("https://doi.org/")
    assert analysis["papers"][0]["citation_snapshots"][0]["provider"] == "crossref"
    assert insights.build_evidence(analysis, None, "")["corpus_scope"]["sampled_top_ranked_per_year"] is True
    exported = client.get(f"/api/results/{result['result_id']}/export?format=csv").text
    assert "Sampled corpus (not population growth)" in exported


def test_identical_search_reuses_local_result(client, monkeypatch):
    calls = []
    def counted(**options):
        calls.append(options["query"])
        return fake_discover(**options)
    monkeypatch.setattr("app.sources.discover", counted)
    first, second = discover(client), discover(client)
    assert first["dataset"]["id"] == second["dataset"]["id"]
    assert calls == ["battery"]


def test_add_optional_scopus_csv_without_double_counting(client, monkeypatch):
    monkeypatch.setattr("app.sources.discover", fake_discover)
    imported = discover(client)
    base_id = imported["dataset"]["id"]
    before = storage.read("datasets", base_id)
    csv = b"EID,Title,Year,Abstract,DOI,Cited by\n2-s2.0-p1,Battery electrolyte transport study 1,2021,,10.1234/p1,99\n"
    added = client.post("/api/import", files={"file": ("scopus.csv", csv, "text/csv")}).json()["dataset"]
    response = client.post("/api/datasets/merge", json={"base_dataset_id": base_id, "additional_dataset_id": added["id"]})
    assert response.status_code == 200, response.text
    merged = storage.read("datasets", response.json()["dataset"]["id"])
    assert len(merged["papers"]) == 5
    matched = next(p for p in merged["papers"] if p["doi"] == "10.1234/p1")
    assert matched["citations"] == 3
    assert set(matched["providers"]) == {"scopus_csv", "crossref"}
    assert {s["count"] for s in matched["citation_snapshots"]} == {3, 99}
    assert merged["report"]["source_reports"][0]["truncated"] is True
    assert storage.read("datasets", base_id) == before


def test_generic_csv_source_and_reject_demo_mixing(client):
    uploaded = client.post("/api/import", data={"provider": "csv"}, files={"file": ("records.csv", b"Title,Year\nResearch,2024\n", "text/csv")}).json()["dataset"]
    saved = storage.read("datasets", uploaded["id"])
    assert saved["papers"][0]["providers"] == ["csv"]
    demo = client.post("/api/demo").json()["dataset"]
    response = client.post("/api/datasets/merge", json={"base_dataset_id": uploaded["id"], "additional_dataset_id": demo["id"]})
    assert response.status_code == 422
    assert "合成デモ" in response.json()["detail"]


def test_discovery_errors_and_validation(client, monkeypatch):
    for change in [{"provider": "unknown"}, {"query": " "}, {"limit": 1001}, {"end_year": 2100}, {"start_year": 2026, "end_year": 2025}]:
        body = {"provider": "crossref", "query": "battery", "start_year": 2021, "end_year": 2025, **change}
        assert client.post("/api/discover", json=body).status_code == 422
    def failure(**options):
        raise RuntimeError("公開サービスの利用上限です。時間をおいて再試行してください。")
    monkeypatch.setattr("app.sources.discover", failure)
    result = wait_job(client, client.post("/api/discover", json={"provider": "crossref", "query": "battery"}).json()["job_id"])
    assert result["status"] == "failed"
    assert "利用上限" in result["error"]
    assert storage.list_datasets() == []


def test_citation_revision_keeps_source_scope(client, monkeypatch):
    monkeypatch.setattr("app.sources.discover", fake_discover)
    imported = discover(client)
    content = b"DOI,Year,Citations\n10.1234/p1,2025,1\n"
    response = client.post(f"/api/datasets/{imported['dataset']['id']}/citations", files={"file": ("history.csv", content, "text/csv")})
    assert response.status_code == 200, response.text
    revision = storage.read("datasets", response.json()["dataset"]["id"])
    assert revision["report"]["source_reports"][0]["truncated"] is True
    assert revision["papers"][0]["citation_source"] == "crossref"
