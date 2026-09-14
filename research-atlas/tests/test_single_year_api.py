from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from app import storage
from app.ingest import parse_scopus_csv
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    with TestClient(app) as value:
        yield value


def analyzed(client, dataset_id, **options):
    started = client.post("/api/analyze", json={"dataset_id": dataset_id, "start_year": 2021,
        "end_year": 2025, "n_topics": 2, "topic_model": "nmf", "embedding": "tfidf", **options})
    assert started.status_code == 200, started.text
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + started.json()["job_id"]).json()
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)
    assert job["status"] == "completed", job
    return client.get("/api/results/" + job["result_id"]).json()


def test_imported_single_year_scopus_csv_stays_nonpredictive_in_expanded_analysis_window(client):
    content = (Path(__file__).parent / "fixtures" / "scopus_partial_full_names.csv").read_bytes()
    response = client.post("/api/import", files={"file": ("fixture.csv", content, "text/csv")})
    assert response.status_code == 200, response.text
    dataset = response.json()["dataset"]
    assert dataset["analysis_defaults"] == {"start_year": 2025, "end_year": 2025}
    # The flag is derived by the server, so an extra client field cannot enable
    # trends in years with no declared collection scope.
    result = analyzed(client, dataset["id"], single_year_corpus=False)
    assert result["meta"]["single_year_corpus"] is True
    assert result["options"]["single_year_corpus"] is True
    assert result["meta"]["annual_comparison_available"] is False
    assert result["meta"]["years"] == [2021, 2022, 2023, 2024, 2025]
    assert result["summary"]["papers"] == 2 and result["summary"]["emerging_topics"] == 0
    assert result["summary"]["growth_pct"] is None
    assert all(t["growth_pct"] is None and t["forecast"] == [] and t["backtest"]["model"] == "insufficient_history" for t in result["topics"])
    assert all(k["growth_pct"] is None for k in result["keywords"])
    assert sum(len(p["authors"]) for p in result["papers"]) == 8
    assert any("収集範囲が未宣言" in warning for warning in result["meta"]["warnings"])


def test_declared_public_search_retains_zero_years_and_is_not_flagged_as_single_year_corpus(client):
    papers, _ = parse_scopus_csv(b"Title,Year,Abstract\nBattery electrolyte,2025,Lithium ion battery\nQuantum qubits,2025,Quantum logical circuit\n")
    request = {"provider": "crossref", "query": "battery OR quantum", "start_year": 2021, "end_year": 2025}
    report = {"discovery_request": request, **request, "year_coverage": [
        {"year": year, "total": 2 if year == 2025 else 0, "imported": 2 if year == 2025 else 0}
        for year in range(2021, 2026)]}
    dataset = storage.create_dataset(papers, "Declared public search fixture", report=report)
    result = analyzed(client, dataset["id"])
    assert result["meta"]["single_year_corpus"] is False
    assert result["meta"]["annual_comparison_available"] is True
    assert result["meta"]["source_reports"] == [report]
    assert all(t["growth_pct"] is not None and len(t["forecast"]) == 3 for t in result["topics"])


def test_csv_with_multiple_actual_years_keeps_yearly_comparison(client):
    content = b"Title,Year,Abstract\nBattery electrolyte,2023,Lithium ion battery\nQuantum qubits,2025,Quantum logical circuit\n"
    response = client.post("/api/import", files={"file": ("multiple-years.csv", content, "text/csv")})
    result = analyzed(client, response.json()["dataset"]["id"])
    assert result["meta"]["single_year_corpus"] is False
    assert result["meta"]["annual_comparison_available"] is True
    assert all(len(t["forecast"]) == 3 for t in result["topics"])
