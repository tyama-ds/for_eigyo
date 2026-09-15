import csv
import io
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import insights, reports
from app import analytics
from app.embedding_models import SBERT_MODELS
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    with TestClient(app) as value:
        yield value


def dataset(client):
    content = io.StringIO(newline="")
    writer = csv.writer(content)
    writer.writerow(["EID", "Title", "Year", "Publication date", "Abstract"])
    for i in range(24):
        text = "stainless steel grain refinement tensile strength" if i % 2 else "lithium battery electrolyte ion transport"
        year = 2023 + i % 3
        writer.writerow([f"model-{i}", f"{text} process {i}", year, f"{year}-06-15", text])
    response = client.post("/api/import", files={"file": ("model-fixture.csv", content.getvalue().encode(), "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()["dataset"]["id"]


def run(client, identifier, method, **options):
    response = client.post("/api/analyze", json={"dataset_id": identifier, "start_year": 2023,
        "end_year": 2025, "topic_model": method, "embedding": "tfidf", "n_topics": 2, **options})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + response.json()["job_id"]).json()
        if job["status"] in {"completed", "failed"}:
            assert job["status"] == "completed", job
            return client.get("/api/results/" + job["result_id"]).json()
        time.sleep(0.05)
    pytest.fail("Model analysis timed out")


@pytest.mark.parametrize("method", ["nmf", "lda"])
def test_lexical_topic_models_work_through_api_and_exports(client, method):
    result = run(client, dataset(client), method)
    assert result["meta"]["topic_model"] == method
    assert result["meta"]["embedding_model"] == ("CountVectorizer / integer unigrams + bigrams"
                                                if method == "lda" else "TF-IDF / unigrams + bigrams")
    assert result["meta"]["map_representation"] == method + "_topic_distribution"
    assert result["summary"]["papers"] == 24
    assert sum(t["count"] for t in result["topics"]) == 24
    assert len(result["map"]["nodes"]) == 24
    assert all(p["topic_id"] in {t["id"] for t in result["topics"]} for p in result["papers"])
    assert all(t["keywords"] for t in result["topics"])
    assert json.loads(json.dumps(result, allow_nan=False))["meta"]["topic_model"] == method
    rows = list(csv.DictReader(io.StringIO(reports.topic_csv(result).lstrip("\ufeff"))))
    assert all(row["Topic model"] == method for row in rows)
    assert all(row["Map representation"] == method + "_topic_distribution" for row in rows)
    assert f"テーマ抽出: {method}" in reports.report_html(result)
    assert insights.build_evidence(result, None, "")["topic_model"] == method


def test_invalid_model_combinations_are_rejected_before_work(client):
    for options in [
        {"topic_model": "unknown"}, {"embedding": "sbert", "sbert_model": "remote/arbitrary-model"},
        {"topic_model": "lda", "embedding": "sbert"}, {"topic_model": "nmf", "embedding": "transformer"},
        {"topic_model": "bertopic", "embedding": "tfidf"}, {"min_topic_size": 1}, {"min_topic_size": 101}]:
        response = client.post("/api/analyze", json={"dataset_id": "a" * 32, **options})
        assert response.status_code == 422, response.text


def test_model_catalog_exposes_available_choices(client):
    status = client.get("/api/status").json()
    assert {r["id"] for r in status["topic_models"]} == {
        "kmeans", "kmeans_pp", "minibatch_kmeans", "xmeans", "knn_graph",
        "dbscan", "gmm", "birch", "agglomerative", "nmf", "lda", "bertopic",
    }
    assert {r["id"] for r in status["sbert_models"]} == {"mpnet", "multilingual_minilm"}
    assert all(r["available"] for r in status["topic_models"] if r["id"] != "bertopic")
    assert all(r["model_id"].startswith("sentence-transformers/") for r in status["sbert_models"])


@pytest.mark.parametrize("requested,expected_id,expected_preset", [
    ({"embedding": "transformer"}, "legacy/custom-model", "legacy_custom"),
    ({"embedding": "transformer", "sbert_model": None}, "legacy/custom-model", "legacy_custom"),
    ({"embedding": "sbert"}, SBERT_MODELS["mpnet"], "mpnet"),
    ({"embedding": "sbert", "sbert_model": None}, SBERT_MODELS["mpnet"], "mpnet"),
    ({"embedding": "sbert", "sbert_model": "multilingual_minilm"},
     SBERT_MODELS["multilingual_minilm"], "multilingual_minilm"),
])
def test_api_distinguishes_omitted_sbert_preset_from_legacy_transformer(client, monkeypatch, requested,
                                                                     expected_id, expected_preset):
    monkeypatch.setenv("ATLAS_EMBEDDING_MODEL", "legacy/custom-model")
    loaded = []
    monkeypatch.setattr(analytics, "_load_transformer", lambda name: loaded.append(name) or object())
    monkeypatch.setattr(analytics, "_windowed_embeddings", lambda model, documents: (
        np.random.default_rng(42).normal(size=(len(documents), 8)), {"capped_documents": 0}))
    result = run(client, dataset(client), "kmeans", **requested)
    assert result["options"]["sbert_model"] == requested.get("sbert_model")
    assert loaded == [expected_id]
    assert result["meta"]["embedding"] == requested["embedding"]
    assert result["meta"]["embedding_model"] == expected_id
    assert result["meta"]["embedding_model_preset"] == expected_preset
    assert result["meta"]["embedding_details"]["legacy_environment_override"] == (requested["embedding"] == "transformer")


def test_unclassified_group_is_not_an_insight_or_forecast(client):
    result = run(client, dataset(client), "nmf")
    for topic in result["topics"]:
        topic.update(is_outlier=True, status="unclassified", forecast=[], score=0)
    result["meta"]["unclassified_papers"] = 24
    local = insights.local_insights(result)
    assert local["hypotheses"] == []
    assert local["evidence_ids"] == []
    assert any("分類された研究テーマがありません" in finding for finding in local["findings"])
    assert insights.build_evidence(result, None, "")["topics"] == []
    assert "未分類: 24 論文" in reports.report_html(result)
    with pytest.raises(ValueError):
        insights.local_insights(result, result["topics"][0]["id"])
