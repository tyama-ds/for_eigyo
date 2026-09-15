import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.analytics import analyze
from app.cluster_models import CLUSTER_MODELS
from app.main import app, AnalyzeRequest


def corpus():
    return [{"id": f"p{group}-{index}", "title": text, "abstract": text,
             "year": 2023 + index % 2, "citations": 0, "authors": [], "keywords": []}
            for group, text in enumerate(["Steel tensile grain hardening", "Solar photovoltaic perovskite", "Quantum qubit superconducting"])
            for index in range(8)]


@pytest.mark.parametrize("method", list(CLUSTER_MODELS))
def test_full_pipeline_keeps_rows_and_common_vectors(method):
    records = corpus()
    result = analyze(records, {"topic_model": method, "n_topics": 3, "min_topic_size": 3,
        "start_year": 2023, "end_year": 2024, "map_projection": "pca",
        "cluster_options": {"eps": .2, "n_neighbors": 7, "max_clusters": 3}})
    assert {p["id"] for p in result["papers"]} == {p["id"] for p in records}
    assert result["summary"]["papers"] == 24
    assert result["meta"]["topic_model"] == method
    assert result["meta"]["landscape_representation"]["basis_scope"] == "full_corpus"
    vectors = np.asarray([p["landscape_vector"] for p in result["papers"]])
    assert vectors.shape[0] == 24 and vectors.shape[1] <= 50 and np.isfinite(vectors).all()
    assert not any("landscape_vector" in p for p in records)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("method", ["kmeans", "dbscan", "gmm"])
def test_empty_information_is_not_invented_as_a_technology(method):
    papers = [{"id": str(i), "title": "the and", "abstract": "", "year": 2024} for i in range(5)]
    result = analyze(papers, {"topic_model": method, "start_year": 2024, "end_year": 2024})
    assert result["summary"]["topics"] == 0 and result["summary"]["unclassified_papers"] == 5


def test_algorithms_are_available_through_api_contract():
    with TestClient(app) as client:
        available = {row["id"] for row in client.get("/api/status").json()["topic_models"]}
    assert available >= set(CLUSTER_MODELS)
    for method in CLUSTER_MODELS:
        assert AnalyzeRequest(dataset_id="a" * 32, topic_model=method).topic_model == method
