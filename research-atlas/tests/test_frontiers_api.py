import csv
import io
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import reports, storage
from app.main import app, observed_months


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    with TestClient(app) as value:
        yield value


def wait_job(client, identifier):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + identifier).json()
        if job["status"] in {"completed", "failed"}:
            assert job["status"] == "completed", job
            return job
        time.sleep(0.05)
    pytest.fail("Analysis did not finish")


def fixture_csv(dates=True):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Title", "Year", "Publication date", "Abstract", "Author Keywords"])
    for month in range(1, 7):
        for material in ("Steel", "Copper"):
            description = "grain refinement tensile strength" if material == "Steel" else "electrical conductivity annealing"
            writer.writerow([f"{material} {description} study {month}", 2025,
                f"2025-{month:02d}-15" if dates else "", description, description])
    return output.getvalue().encode()


def analyze_csv(client, dates=True):
    uploaded = client.post("/api/import", data={"provider": "csv"},
        files={"file": ("months.csv", fixture_csv(dates), "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    identifier = uploaded.json()["dataset"]["id"]
    response = client.post("/api/analyze", json={"dataset_id": identifier,
        "start_year": 2025, "end_year": 2025, "n_topics": 2,
        "anchor_month": "2025-06", "window_months": 3})
    assert response.status_code == 200, response.text
    job = wait_job(client, response.json()["job_id"])
    return client.get("/api/results/" + job["result_id"]).json()


def test_month_dates_survive_import_analysis_and_exports(client):
    result = analyze_csv(client)
    monthly = result["frontiers"]["monthly"]
    assert monthly["available"] is True
    assert monthly["date_coverage"] == 100
    assert monthly["recent_months"] == ["2025-04", "2025-05", "2025-06"]
    assert sum(t["recent_count"] for t in monthly["topics"]) == 6
    assert sum(t["baseline_count"] for t in monthly["topics"]) == 6
    assert all(p["publication_date"].startswith("2025-") for p in result["papers"])
    assert all(p["date_precision"] == "day" for p in result["papers"])
    exported = client.get(f"/api/results/{result['id']}/export?format=csv")
    assert "Monthly anchor" in exported.text
    assert "2025-06" in exported.text
    html = client.get(f"/api/results/{result['id']}/export?format=html").text
    assert "月次の研究活動" in html
    assert "2025-04 / 2025-05 / 2025-06" in html
    assert "Publication date" in reports.papers_csv(result["papers"])


def test_year_only_data_does_not_gain_invented_months(client):
    result = analyze_csv(client, dates=False)
    monthly = result["frontiers"]["monthly"]
    assert monthly["available"] is False
    assert monthly["dated_papers"] == 0
    assert monthly["date_coverage"] == 0
    assert all(not p.get("publication_date") for p in result["papers"])
    assert monthly["reason_label"] in reports.report_html(result)


def test_source_window_missing_baseline_blocks_monthly_claim(client, monkeypatch):
    def source(**options):
        from app.ingest import parse_scopus_csv
        papers, _ = parse_scopus_csv(fixture_csv())
        papers = [p for p in papers if p["publication_date"] >= "2025-04"]
        for p in papers:
            p["providers"] = ["europepmc"]
        papers[0]["providers"].append("csv")
        return papers, {"provider": "europepmc", "query": options["query"],
            "truncated": True, "month_coverage": [
                {"month": f"2025-{m:02d}", "total": 200, "imported": 2} for m in (4, 5, 6)],
            "year_coverage": [{"year": 2025, "total": 600, "imported": 6}]}
    monkeypatch.setattr("app.sources.discover", source)
    response = client.post("/api/discover", json={"provider": "europepmc", "query": "material",
        "start_year": 2025, "end_year": 2025, "start_month": "2025-04", "end_month": "2025-06", "limit": 20})
    job = wait_job(client, response.json()["job_id"])
    response = client.post("/api/analyze", json={"dataset_id": job["dataset"]["id"],
        "start_year": 2025, "end_year": 2025, "anchor_month": "2025-06", "n_topics": 2})
    job = wait_job(client, response.json()["job_id"])
    result = client.get("/api/results/" + job["result_id"]).json()
    assert result["frontiers"]["monthly"]["available"] is False
    assert result["frontiers"]["scope"]["sampled"] is True
    assert any("検索標本" in w for w in result["frontiers"]["monthly"]["warnings"])
    assert result["frontiers"]["monthly"]["has_unknown_collection_scope"] is True
    assert result["frontiers"]["monthly"]["observation_scope_known"] is False


def test_collection_ranges_union_same_search_intersect_different_searches():
    def report(query, months):
        return {"provider": "crossref", "query": query, "month_coverage": [{"month": m} for m in months]}
    dataset = {"report": {"source_reports": [report("steel", ["2025-01", "2025-02"]),
        report("steel", ["2025-03"]), report("copper", ["2025-02", "2025-03", "2025-04"])]}}
    assert observed_months(dataset) == ["2025-02", "2025-03"]
    assert observed_months({"report": {}}) is None


def test_annual_empty_month_table_and_zero_allocation_scope():
    dataset = {"report": {"discovery_request": {"query": "steel"}, "month_coverage": [],
        "year_coverage": [{"year": 2025}]}}
    assert observed_months(dataset) == [f"2025-{month:02d}" for month in range(1, 13)]
    dataset["report"]["month_coverage"] = [
        {"month": "2025-01", "allocated": 0, "total": 50},
        {"month": "2025-02", "allocated": 0, "total": 0},
        {"month": "2025-03", "allocated": 1, "total": 50}]
    assert observed_months(dataset) == ["2025-02", "2025-03"]


def test_invalid_month_options_are_rejected_before_network(client):
    base = {"provider": "crossref", "query": "steel", "start_year": 2025, "end_year": 2025}
    for change in [
        {"start_month": "2025-01"},
        {"start_month": "2025-13", "end_month": "2025-13"},
        {"start_month": "2025-06", "end_month": "2025-01"},
        {"start_month": "2024-01", "end_month": "2025-01"},
        {"start_year": 2023, "start_month": "2023-01", "end_month": "2025-01"},
    ]:
        assert client.post("/api/discover", json={**base, **change}).status_code == 422
    for change in [{"window_months": 2}, {"anchor_month": "2024-12"},
        {"anchor_month": date.today().strftime("%Y-%m"), "end_year": date.today().year}]:
        response = client.post("/api/analyze", json={"dataset_id": "a" * 32,
            "start_year": 2025, "end_year": 2025, **change})
        assert response.status_code == 422


def test_frontier_report_escapes_user_terms():
    result = {"frontiers": {"monthly": {"available": False, "reason_label": "<script>"},
        "sparse": {"candidates": [{"term_a": "<img src=x>", "term_b": "A&B", "count_a": 5,
            "count_b": 6, "observed": 0, "expected": 2.5}]}}}
    value = reports.frontier_html(result)
    assert "<script>" not in value and "<img src=x>" not in value
    assert "&lt;script&gt;" in value and "A&amp;B" in value


def test_saved_monthly_dataset_remembers_queried_period(client):
    dataset = storage.create_dataset([], "Monthly saved search", report={"discovery_request": {"query": "steel"},
        "start_month": "2025-04", "end_month": "2025-09", "start_year": 2025, "end_year": 2025})
    summary = next(row for row in client.get("/api/status").json()["datasets"] if row["id"] == dataset["id"])
    assert summary["analysis_defaults"] == {"start_year": 2025, "end_year": 2025,
        "anchor_month": "2025-09", "window_months": 3}
