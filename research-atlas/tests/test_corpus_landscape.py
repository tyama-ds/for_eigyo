import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sklearn.decomposition import PCA

from app import corpus_landscape, landscape, large_storage, storage
from app.analytics import _normalize_positions
from app.main import app


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    yield
    corpus_landscape._load.cache_clear()
    corpus_landscape._project.cache_clear()
    corpus_landscape._build.cache_clear()


def full_fixture(count_per_year=450, saved_vectors=True):
    papers, visible = [], []
    for year in (2023, 2024):
        for index in range(count_per_year):
            hidden_change = year == 2024 and index >= min(200, count_per_year // 2)
            vector = [0., 1., .03] if hidden_change else [1., 0., .03]
            paper = {"id": f"p-{year}-{index:05d}", "title": "electrolyte catalyst" if hidden_change else "steel fatigue",
                     "abstract": "electrolyte catalyst" if hidden_change else "steel fatigue", "keywords": [],
                     "year": year, "publication_date": f"{year}-01", "date_precision": "month", "topic_id": "topic-1",
                     "landscape_vector": vector,
                     "topic_weights": {"topic-1": vector[0], "topic-2": vector[1], "topic-3": vector[2]}}
            papers.append(paper)
            if index < min(200, count_per_year // 2):
                visible.append({"id": paper["id"], "label": paper["title"], "year": year,
                                "publication_date": paper["publication_date"], "date_precision": "month",
                                "topic_id": "topic-1", "x": .5, "y": .5})
    result = {"id": storage.new_id(), "meta": {"topic_model": "nmf", "paper_count": len(papers)},
              "summary": {"papers": len(papers)}, "papers": papers,
              "topics": [{"id": "topic-1", "label": "Materials", "color": "#42e8cf"}],
              "map": {"nodes": visible, "edges": [], "projection": {"algorithm": "pca", "requested_method": "pca"},
                      "projection_inputs": {"paper_ids": [p["id"] for p in visible],
                          "vectors": [[1., 0., .03]] * len(visible), "embedding": "nmf", "source": "saved_analysis_representation"}}}
    if saved_vectors:
        result["meta"]["landscape_representation"] = {"basis_scope": "full_corpus", "embedding": "nmf", "dimensions": 3,
            "source": "saved_full_corpus_representation", "reduction": "none"}
    storage.save("results", result)
    return result


def test_all_papers_shift_outside_display_and_provide_unseen_evidence():
    result = full_fixture()
    simple = landscape.build_landscape(result["id"], projection="pca", scope="sample")
    full = landscape.build_landscape(result["id"], projection="pca", scope="full")
    assert simple["movements"][0]["status"] == "stable"
    movement = full["movements"][0]
    assert movement["status"] == "shift"
    assert movement["from_count"] == movement["to_count"] == 450
    assert movement["test_from_count"] == movement["test_to_count"] == 200
    assert movement["p_value_scope"] == "bounded_permutation_sample"
    assert full["meta"]["analysis_papers"] == 900
    assert full["meta"]["map_displayed_papers"] == 400
    assert full["meta"]["count_scope"] == "full_corpus"
    assert simple["projection_id"] != full["projection_id"]
    shown = {node["id"] for node in full["map"]["nodes"]}
    assert set(movement["evidence_after"]) - shown
    assert {row["term"]: row["count"] for row in movement["to_terms"]}["catalyst"] == 250
    assert full["periods"][1]["count"] == 450
    assert len(full["periods"][1]["node_ids"]) == 200
    assert full["centroids"][1]["count"] == 450
    assert len(full["centroids"][1]["evidence_ids"]) == 6
    assert full["centroids"][1]["share_of_period"] == 1
    json.dumps(full, allow_nan=False)


def test_pca_centroid_matches_all_projected_papers_and_intervals_share_coordinates():
    result = full_fixture()
    full = landscape.build_landscape(result["id"], projection="pca", scope="full")
    monthly = landscape.build_landscape(result["id"], projection="pca", interval="month", scope="full")
    quarterly = landscape.build_landscape(result["id"], projection="pca", interval="quarter", scope="full")
    assert full["projection_id"] == monthly["projection_id"] == quarterly["projection_id"]
    assert [(n["x"], n["y"]) for n in full["map"]["nodes"]] == [(n["x"], n["y"]) for n in monthly["map"]["nodes"]]
    vectors = np.asarray([p["landscape_vector"] for p in result["papers"]], dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1)[:, None]
    coordinates = _normalize_positions(PCA(n_components=2, svd_solver="full").fit_transform(vectors))
    expected = coordinates[450:].mean(axis=0, dtype=np.float64)
    np.testing.assert_allclose([full["centroids"][1]["x"], full["centroids"][1]["y"]], expected, atol=1e-7)
    assert monthly["movements"][0]["gap_periods"] == 11
    assert monthly["movements"][0]["p_value"] is None


def test_full_dates_and_zero_vectors_are_excluded_from_appropriate_counts():
    result = full_fixture(count_per_year=6)
    for index in (0, 1, 6):
        result["papers"][index]["landscape_vector"] = [0., 0., 0.]
    result["papers"][2].update(publication_date="", date_precision="year")
    result["papers"][3].update(publication_date="2022-01", date_precision="month")
    storage.save("results", result)
    yearly = landscape.build_landscape(result["id"], scope="full")
    assert yearly["movements"][0]["from_count"] == 6
    assert yearly["movements"][0]["from_valid_count"] == 4
    assert yearly["movements"][0]["p_value"] is None
    monthly = landscape.build_landscape(result["id"], interval="month", scope="full")
    assert monthly["meta"]["analysis_papers"] == 12
    assert monthly["meta"]["excluded_date_count"] == 2
    assert monthly["meta"]["eligible_papers"] == 10
    assert sum(row["count"] for row in monthly["periods"]) == 10


def test_legacy_explicit_month_is_usable_without_a_precision_field():
    result = full_fixture(count_per_year=6)
    for paper in result["papers"]:
        paper.pop("date_precision")
    storage.save("results", result)
    full = landscape.build_landscape(result["id"], interval="month", scope="full")
    assert full["meta"]["eligible_papers"] == 12
    assert full["meta"]["excluded_date_count"] == 0


def test_legacy_full_nmf_reads_externalized_all_rows_without_restore(monkeypatch):
    result = full_fixture(saved_vectors=False)
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 500)
    storage.save("results", result)
    original = storage.read
    def read(kind, identifier, **kwargs):
        assert kwargs.get("include_papers") is False
        return original(kind, identifier, **kwargs)
    monkeypatch.setattr(storage, "read", read)
    full = landscape.build_landscape(result["id"], scope="full")
    assert full["meta"]["analysis_papers"] == 900
    assert full["meta"]["representation_source"] == "legacy_full_nmf_topic_weights"
    assert full["movements"][0]["to_count"] == 450


def test_legacy_full_tfidf_reconstruction_includes_unseen_catalyst_text():
    result = full_fixture(saved_vectors=False)
    result["meta"]["topic_model"] = "bertopic"
    storage.save("results", result)
    full = landscape.build_landscape(result["id"], scope="full")
    assert full["meta"]["representation_source"] == "full_corpus_tfidf_reconstruction"
    assert full["meta"]["analysis_papers"] == 900
    assert full["movements"][0]["cosine_distance"] > .1
    assert any("全件" in warning and "TF-IDF" in warning for warning in full["warnings"])


def test_inline_papers_with_network_only_descriptor_keep_all_term_counts():
    result = full_fixture(count_per_year=12)
    result["_large_store"] = {"owner": "a" * 32, "count": 0, "network": "a" * 32 + ".network.json"}
    storage.save("results", result)
    full = landscape.build_landscape(result["id"], scope="full")
    assert {row["term"]: row["count"] for row in full["movements"][0]["to_terms"]}["catalyst"] == 6


def test_tsne_method_limit_does_not_limit_full_pca(monkeypatch):
    result = full_fixture()
    monkeypatch.setattr(corpus_landscape, "FULL_TSNE_LIMIT", 50)
    with pytest.raises(ValueError, match="PCA"):
        landscape.build_landscape(result["id"], projection="tsne", scope="full")
    full = landscape.build_landscape(result["id"], projection="auto", scope="full")
    assert full["meta"]["analysis_papers"] == 900
    assert full["map"]["projection"]["algorithm"] == "pca"


def test_full_umap_transforms_every_paper_and_marks_approximation(monkeypatch):
    import umap
    result = full_fixture(count_per_year=12)
    result["papers"][0]["landscape_vector"] = [0., 0., 1.]
    storage.save("results", result)
    calls = []
    class FakeUMAP:
        def __init__(self, **kwargs):
            pass
        def fit_transform(self, values):
            calls.append(("fit", len(values)))
            return values[:, :2]
        def transform(self, values):
            calls.append(("transform", len(values)))
            return values[:, :2]
    monkeypatch.setattr(umap, "UMAP", FakeUMAP)
    monkeypatch.setattr(corpus_landscape, "UMAP_REFERENCE_LIMIT", 10)
    full = landscape.build_landscape(result["id"], projection="umap", scope="full")
    assert calls == [("fit", 10), ("transform", 24)]
    assert full["map"]["projection"]["approximation"] == "reference_fit_transform_all"
    assert full["map"]["projection"]["transform_papers"] == 24
    assert full["centroids"][1]["count"] == 12


def test_nonfinite_umap_output_is_rejected_before_normalizing(monkeypatch):
    import umap
    result = full_fixture(count_per_year=12)
    result["papers"][0]["landscape_vector"] = [0., 0., 1.]
    storage.save("results", result)
    class BrokenUMAP:
        def __init__(self, **kwargs):
            pass
        def fit_transform(self, values):
            return np.full((len(values), 2), np.nan)
    monkeypatch.setattr(umap, "UMAP", BrokenUMAP)
    with pytest.raises(ValueError, match="非有限値"):
        landscape.build_landscape(result["id"], projection="umap", scope="full")


def test_empty_full_result_and_scope_api_validation():
    result_id = storage.new_id()
    storage.save("results", {"id": result_id})
    with TestClient(app) as client:
        response = client.get(f"/api/results/{result_id}/landscape?scope=full")
        assert response.status_code == 200, response.text
        assert response.json()["meta"]["analysis_papers"] == 0
        assert response.json()["meta"]["scope"] == "full"
        assert client.get(f"/api/results/{result_id}/landscape?scope=bad").status_code == 422


def test_scope_and_file_revision_are_in_cache_identity():
    result = full_fixture()
    original = landscape.build_landscape(result["id"], scope="full")
    result["papers"][-1]["landscape_vector"] = [0., .5, .7]
    storage.save("results", result)
    changed = landscape.build_landscape(result["id"], scope="full")
    assert original["projection_id"] != changed["projection_id"]


def test_undefined_full_centroid_cannot_gain_a_sampled_p_value(monkeypatch):
    monkeypatch.setattr(corpus_landscape, "PERMUTATION_GROUP_LIMIT", 5)
    vectors = np.asarray([[1., 0.]] * 10 + [[-1., 0.]] * 10 + [[0., 1.]] * 10)
    values = corpus_landscape._permutation(np.arange(20), np.arange(20, 30), vectors, 42)
    assert values[0] is None
    assert values[1] is None
    assert values[3:5] == (20, 10)


def test_sparse_term_batches_preserve_exact_all_document_frequencies(monkeypatch):
    result = full_fixture(count_per_year=12)
    monkeypatch.setattr(corpus_landscape, "TERM_COUNT_BATCH_ENTRIES", 5)
    full = landscape.build_landscape(result["id"], scope="full")
    terms = {row["term"]: row["count"] for row in full["movements"][0]["to_terms"]}
    assert terms["catalyst"] == 6
    assert terms["fatigue"] == 6


def test_degenerate_umap_uses_shared_linear_map_without_random_artificial_separation(monkeypatch):
    import umap
    result = full_fixture(count_per_year=12)
    for paper in result["papers"]:
        paper["landscape_vector"] = [1., .1, 0.]
    storage.save("results", result)
    def should_not_fit(**kwargs):
        raise AssertionError("UMAP should not be fitted to identical directions")
    monkeypatch.setattr(umap, "UMAP", should_not_fit)
    full = landscape.build_landscape(result["id"], projection="umap", scope="full")
    assert full["map"]["projection"]["algorithm"] == "pca"
    assert full["map"]["projection"]["requested_method"] == "umap"
    assert full["map"]["projection"]["fallback_reason"] == "insufficient_unique_vectors"
    assert len({(node["x"], node["y"]) for node in full["map"]["nodes"]}) == 1
    assert full["movements"][0]["distance_2d"] == 0
    assert not corpus_landscape._has_nonlinear_variety(np.asarray([[1., 0., 0.], [1., -0., 0.], [1., 0., -0.]]))


@pytest.mark.parametrize("candidate_limit,expected", [(4, ["alpha", "beta"]), (100, ["alpha", "beta", "delta"])])
def test_lexical_ties_do_not_depend_on_token_insertion_order(monkeypatch, candidate_limit, expected):
    result = full_fixture(count_per_year=6)
    words = ["zeta", "gamma", "epsilon", "delta", "beta", "alpha"]
    monkeypatch.setattr(corpus_landscape, "VOCAB_CANDIDATES", candidate_limit)
    monkeypatch.setattr(corpus_landscape, "LEXICAL_FEATURES", 3)
    monkeypatch.setattr(corpus_landscape, "TERM_FEATURES", 3)
    monkeypatch.setattr(corpus_landscape, "_tokens", lambda paper: iter(words))
    first = corpus_landscape._load(corpus_landscape._revision(result["id"]))["term_vocabulary"]
    corpus_landscape._load.cache_clear()
    words.reverse()
    second = corpus_landscape._load(corpus_landscape._revision(result["id"]))["term_vocabulary"]
    assert first == second == expected


def test_identical_reruns_with_different_result_ids_keep_permutation_results():
    result = full_fixture()
    for index, paper in enumerate(result["papers"]):
        paper["landscape_vector"][2] += index * .00001
    storage.save("results", result)
    first = landscape.build_landscape(result["id"], scope="full")["movements"][0]
    result["id"] = storage.new_id()
    storage.save("results", result)
    second = landscape.build_landscape(result["id"], scope="full")["movements"][0]
    assert first["p_value_scope"] == "bounded_permutation_sample"
    for key in ("test_cosine_distance", "cosine_distance", "p_value", "q_value", "status"):
        assert first[key] == second[key]
