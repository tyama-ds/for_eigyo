"""Selection controls must change actual evidence, never measured movements."""
from copy import deepcopy
import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import corpus_landscape, field_llm, landscape, landscape_reports, storage
from app.main import app


@pytest.fixture
def selected_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    papers = []
    for year in (2023, 2024):
        for index in range(25):
            papers.append({"id": f"p{year}-{index:02d}", "title": "Steel microstructure study", "topic_id": "steel",
                           "year": year, "publication_date": f"{year}-06-{index + 1:02d}", "date_precision": "day",
                           "abstract": "Laser heating changed the steel microstructure.", "citations": index,
                           "landscape_vector": [1., index / 30.]})
    result = {"id": storage.new_id(), "papers": papers, "topics": [{"id": "steel", "label": "Steel"}],
              "meta": {"landscape_representation": {"basis_scope": "full_corpus", "embedding": "nmf", "dimensions": 2}},
              "map": {"nodes": [{"id": p["id"], "topic_id": "steel", "x": .5, "y": .5} for p in papers],
                      "projection_inputs": {"paper_ids": [p["id"] for p in papers],
                                            "vectors": [p["landscape_vector"] for p in papers],
                                            "source": "saved_analysis_representation", "embedding": "nmf"}}}
    storage.save("results", result)
    movement = {"id": "move", "topic_id": "steel", "from_period": "2023", "to_period": "2024",
                "from_count": 25, "to_count": 25, "cosine_distance": .23, "distance_2d": .74,
                "p_value": .02, "q_value": .04, "status": "shift", "from_terms": [], "to_terms": [],
                "evidence_before": [p["id"] for p in papers[:6]], "evidence_after": [p["id"] for p in papers[25:31]]}
    def build(identifier, *, projection="auto", interval="year", scope="sample"):
        assert identifier == result["id"]
        return {"result_id": identifier, "projection_id": "fixed", "topics": result["topics"],
                "movements": [deepcopy(movement)], "meta": {"scope": scope, "interval": interval}}
    monkeypatch.setattr(landscape, "build_landscape", build)
    yield result, movement
    corpus_landscape._load.cache_clear()


def prepare(saved, **options):
    return landscape_reports.prepare_report(saved[0]["id"], "pca", "year", "move", **options)


@pytest.mark.parametrize("scope", ["sample", "full"])
@pytest.mark.parametrize("method", ["centroid", "diverse", "cited", "recent"])
def test_twenty_per_period_selected_from_original_candidates_without_metric_changes(selected_result, scope, method):
    original = deepcopy(selected_result[1])
    report = prepare(selected_result, scope=scope, papers_per_period=20, selection_method=method, abstract_only=True)
    assert len(report["evidence_papers"]) == 40
    assert report["selection"]["before"]["candidate_count"] == 25
    assert report["selection"]["after"]["eligible_count"] == 25
    assert {k: v for k, v in report["movement"].items() if not k.startswith("evidence_")} == {
        k: v for k, v in original.items() if not k.startswith("evidence_")}
    assert selected_result[1] == original
    for side, year, alias in (("before", 2023, "B"), ("after", 2024, "A")):
        rows = [p for p in report["evidence_papers"] if p["side"] == side]
        assert [p["id"] for p in rows] == report["selection"][side]["selected_ids"] == report["movement"]["evidence_" + side]
        assert [p["selection_rank"] for p in rows] == list(range(1, 21))
        assert [p["citation_id"] for p in rows] == [f"{alias}{i}" for i in range(1, 21)]
        assert all(p["year"] == year and p["abstract"] for p in rows)
        assert any(p["id"] not in original["evidence_" + side] for p in rows)
        if method in {"cited", "recent"}:
            assert [p["id"] for p in rows] == [f"p{year}-{i:02d}" for i in range(24, 4, -1)]
    assert "最大20件" in report["input_summary"]["selection"]
    assert "最大6本" not in report["input_summary"]["selection"]
    rows = list(csv.DictReader(io.StringIO(landscape_reports.export_csv(report).lstrip("\ufeff"))))
    audit = next(row for row in rows if row["Section"] == "selection")
    assert json.loads(audit["Value"]) == report["selection"]


def test_selected_priority_and_measured_citations_are_passed_to_llm(selected_result, monkeypatch):
    report = prepare(selected_result, papers_per_period=2, selection_method="cited")
    def generate(payload, *args, **kwargs):
        assert [p["id"] for p in payload["papers"]] == ["p2023-24", "p2023-23", "p2024-24", "p2024-23"]
        assert payload["papers"][0]["citations"] == 24
        assert "selected_records" not in json.dumps(payload)
        return {"headline": "文献比較", "sections": [{"title": "引用と内容", "text": "この論文の被引用数は24です。両期間にはレーザー加熱の研究が含まれます。",
                "evidence_ids": ["B1", "A1"]}], "caveats": []}, "local_llm", "test-model"
    monkeypatch.setattr(field_llm, "structured_output", generate)
    narrative = landscape_reports.generate(report, "local")
    assert narrative["validation"]["status"] == "passed"
    assert narrative["sections"][0]["evidence_ids"] == ["p2023-24", "p2024-24"]


def wait_report(client, saved, **options):
    response = client.post("/api/landscape-reports", json={"result_id": saved[0]["id"], "movement_id": "move", **options})
    assert response.status_code == 200, response.text
    for _ in range(300):
        job = client.get("/api/jobs/" + response.json()["job_id"]).json()
        if job["status"] in {"failed", "completed"}:
            assert job["status"] == "completed", job
            return client.get("/api/landscape-reports/" + job["landscape_report_id"]).json()
        time.sleep(.01)
    pytest.fail("Report job did not finish")


def test_api_preserves_selection_and_rejects_invalid_or_ignored_options(selected_result):
    with TestClient(app) as client:
        report = wait_report(client, selected_result, papers_per_period=20, selection_method="recent", abstract_only=True)
        assert report["selection"]["papers_per_period"] == 20
        assert report["selection"]["selection_method"] == "recent"
        assert report["input_summary"]["paper_count"] == 40
        for options in ({"papers_per_period": 0}, {"papers_per_period": 21}, {"papers_per_period": True},
                        {"papers_per_period": "6"}, {"papers_per_period": 1.5}, {"selection_method": "random"},
                        {"abstract_only": "false"}, {"kind": "centroid", "topic_id": "steel", "period_id": "2023", "papers_per_period": 20}):
            response = client.post("/api/landscape-reports", json={"result_id": selected_result[0]["id"], "movement_id": "move", **options})
            assert response.status_code == 422


def test_empty_side_keeps_metrics_and_reports_source_problem_without_llm_call(selected_result, monkeypatch):
    for paper in selected_result[0]["papers"]:
        if paper["year"] == 2023:
            paper["abstract"] = ""
    storage.save("results", selected_result[0])
    monkeypatch.setattr(field_llm, "structured_output", lambda *a, **k: pytest.fail("One-sided comparison must not call LLM"))
    with TestClient(app) as client:
        report = wait_report(client, selected_result, provider="local", abstract_only=True)
    assert report["generation_status"] == "failed"
    assert report["generation_error_kind"] == "selection_missing_period"
    assert report["selection"]["before"]["selected_count"] == 0
    assert report["selection"]["after"]["selected_count"] == 6
    assert report["movement"]["from_count"] == 25 and report["movement"]["cosine_distance"] == .23
    assert report["narrative"]["mode"] == "deterministic"
    assert "選び方" in report["llm_error"]
