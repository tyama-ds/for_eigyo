from copy import deepcopy
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import centroid_reports, field_llm, landscape, landscape_reports, storage
from app.main import app


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    papers = [{"id": f"p{i}", "title": f"Steel paper {i}", "year": 2024,
               "abstract": "Laser heating increased strength to 900 MPa."} for i in range(9)]
    result = {"id": storage.new_id(), "meta": {}, "topics": [{"id": "t1", "label": "Steel"}], "papers": papers}
    storage.save("results", result)
    center = {"topic_id": "t1", "period_id": "2024", "count": 9, "x": .2, "y": .3,
              "evidence_ids": ["p8", "p4", "p6", "p2", "p1", "p5"], "terms": [{"term": "laser", "count": 9}],
              "valid_vector_count": 9, "period_count": 9, "dispersion": .03, "share_of_period": 1}
    layout = {"projection_id": "full-pca", "topics": result["topics"], "centroids": [center],
              "meta": {"scope": "full", "analysis_papers": 9, "map_displayed_papers": 2}, "warnings": []}
    def build(identifier, projection="auto", interval="year", scope="sample"):
        assert identifier == result["id"]
        return deepcopy(layout)
    monkeypatch.setattr(landscape, "build_landscape", build)
    return result, layout


def prepare(saved):
    return centroid_reports.prepare_report(saved[0]["id"], "pca", "year", "t1", "2024", "full-pca", "full")


def test_exact_ranked_full_evidence_and_individual_guides(saved):
    report = prepare(saved)
    assert report["kind"] == "centroid" and report["scope"] == "full"
    assert [p["id"] for p in report["evidence_papers"]] == ["p8", "p4", "p6", "p2", "p1", "p5"]
    assert report["centroid"]["count"] == 9
    assert all("p0" not in row["evidence_ids"] for row in report["narrative"]["sections"])
    assert len(report["narrative"]["sections"]) == 7
    assert "表示標本です" not in json.dumps(report["limitations"], ensure_ascii=False)
    exported = landscape_reports.export_csv(report)
    assert "centroid" in exported and "full-pca" in exported and "900 MPa" in exported


def test_wrong_scope_coordinate_or_period_rejected(saved):
    with pytest.raises(ValueError, match="版"):
        centroid_reports.prepare_report(saved[0]["id"], "pca", "year", "t1", "2024", "sample-pca", "full")
    with pytest.raises(ValueError, match="重心"):
        centroid_reports.prepare_report(saved[0]["id"], "pca", "year", "t1", "2023", scope="full")
    saved[1]["centroids"][0]["evidence_ids"] = ["foreign"]
    with pytest.raises(ValueError, match="根拠ID"):
        prepare(saved)


def test_llm_centroid_reviews_have_paper_grounding_and_numeric_warnings(saved, monkeypatch):
    report = prepare(saved)
    before = deepcopy(report["centroid"])
    def generate(payload, schema, instructions, provider, model, **kwargs):
        assert payload["kind"] == "centroid" and "ONE short section for EACH" in instructions
        assert schema == field_llm.NarrativeOutput and provider == "local"
        return {"headline": "代表論文の評論", "sections": [{"title": "結果の検討", "text": "強度は123 MPaでした。", "evidence_ids": ["p8"]}], "caveats": []}, "local", "test"
    monkeypatch.setattr(field_llm, "structured_output", generate)
    narrative = landscape_reports.generate(report, "local", "test")
    assert narrative["validation"]["status"] == "warning"
    assert "123 MPa" in narrative["sections"][0]["text"]
    assert any(w["code"] == "representative_not_reviewed" for w in narrative["validation"]["warnings"])
    assert not any(w["code"] == "period_evidence_missing" for w in narrative["validation"]["warnings"])
    assert report["centroid"] == before


def test_centroid_endpoint_validation_queue_and_csv(saved):
    with TestClient(app) as client:
        body = {"result_id": saved[0]["id"], "kind": "centroid", "scope": "full", "projection": "pca", "interval": "year", "provider": "none"}
        assert client.post("/api/landscape-reports", json=body).status_code == 422
        response = client.post("/api/landscape-reports", json={**body, "topic_id": "t1", "period_id": "2024", "projection_id": "full-pca"})
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(.01)
        assert job["status"] == "completed", job
        report = client.get(f"/api/landscape-reports/{job['landscape_report_id']}").json()
        assert report["kind"] == "centroid" and report["scope"] == "full"
        csv = client.get(f"/api/landscape-reports/{report['id']}/export")
        assert csv.status_code == 200 and "centroid" in csv.text
