import builtins
import json

import numpy as np
import pytest
from scipy import sparse

from app import analytics, landscape, large_storage, storage


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))


def result_fixture(*, shifted=True, dates=True, group_size=12):
    rng = np.random.default_rng(72)
    papers, vectors, nodes = [], [], []
    for period in range(2):
        for index in range(group_size):
            identifier = f"p-{period}-{index:03d}"
            year = 2023 + period
            term = "catalyst electrolyte" if period and shifted else "steel fatigue"
            vector = np.array([.03, .97, .1]) if period and shifted else np.array([.97, .03, .1])
            vector = vector + rng.uniform(0, .005, 3)
            vector /= np.linalg.norm(vector)
            papers.append({"id": identifier, "title": f"{term} investigation", "abstract": term,
                           "year": year, "topic_id": "topic-1", "keywords": term.split(),
                           "publication_date": f"{year}-01" if dates else "",
                           "date_precision": "month" if dates else "year"})
            nodes.append({**papers[-1], "label": papers[-1]["title"], "x": .2 + .6 * period, "y": .2 + index * .03})
            vectors.append(vector.tolist())
    result = {"id": storage.new_id(), "papers": papers,
              "topics": [{"id": "topic-1", "label": "Materials", "color": "#42e8cf"}],
              "meta": {"topic_model": "nmf", "paper_count": len(papers)}, "summary": {"papers": len(papers)},
              "map": {"nodes": nodes, "edges": [], "projection": {"algorithm": "tsne", "requested_method": "auto"},
                      "projection_inputs": {"paper_ids": [p["id"] for p in papers], "vectors": vectors,
                          "embedding": "nmf", "source": "saved_analysis_representation"}}}
    storage.save("results", result)
    return result


def test_intervals_keep_exact_shared_coordinates_and_year_only_is_not_january():
    result = result_fixture()
    result["papers"][0].update(publication_date="", date_precision="year")
    storage.save("results", result)
    yearly = landscape.build_landscape(result["id"])
    monthly = landscape.build_landscape(result["id"], interval="month")
    quarterly = landscape.build_landscape(result["id"], interval="quarter")
    assert yearly["projection_id"] == monthly["projection_id"] == quarterly["projection_id"]
    coordinates = lambda value: [(n["id"], n["x"], n["y"]) for n in value["map"]["nodes"]]
    assert coordinates(yearly) == coordinates(monthly) == coordinates(quarterly)
    assert monthly["meta"]["excluded_date_count"] == 1
    assert monthly["periods"][0]["count"] == 11
    assert len(monthly["periods"]) == 13
    assert not monthly["periods"][1]["observed"]
    assert monthly["movements"][0]["gap_periods"] == 11
    assert monthly["movements"][0]["status"] == "insufficient"
    assert monthly["movements"][0]["p_value"] is None


def test_shift_in_latent_representation_detected_with_evidence_and_terms():
    result = result_fixture()
    value = landscape.build_landscape(result["id"])
    move = value["movements"][0]
    assert move["status"] == "shift"
    assert move["q_value"] <= .05
    assert move["cosine_distance"] > .5
    assert "fatigue" in {t["term"] for t in move["from_terms"]}
    assert "catalyst" in {t["term"] for t in move["to_terms"]}
    assert len(move["evidence_before"]) == len(move["evidence_after"]) == 6
    assert move["from_count"] == 12
    assert "研究者の移動" in move["explanation"]
    assert value["meta"]["count_scope"] == "display_sample"
    json.dumps(value, allow_nan=False)


def test_no_detected_shift_does_not_claim_proof_of_stability():
    result = result_fixture(shifted=False)
    movement = landscape.build_landscape(result["id"])["movements"][0]
    assert movement["status"] == "stable"
    assert movement["cosine_distance"] < .03
    assert "証明ではありません" in movement["explanation"]


def test_publication_years_are_not_content_shift_terms_and_material_codes_survive():
    result = result_fixture(shifted=False)
    for paper in result["papers"]:
        paper["title"] = f"316L 17-4PH fatigue monitoring {paper['year']}"
        paper["abstract"] = f"Study number 001 fatigue monitoring {paper['year']}"
    storage.save("results", result)
    move = landscape.build_landscape(result["id"])["movements"][0]
    terms = {row["term"] for key in ("from_terms", "to_terms") for row in move[key]}
    assert not any("2023" in term or "2024" in term or term.isdecimal() for term in terms)
    assert "316l" in terms
    assert "17-4ph" in terms


def test_small_periods_and_undefined_vectors_are_insufficient():
    result = result_fixture(group_size=4)
    movement = landscape.build_landscape(result["id"])["movements"][0]
    assert movement["status"] == "insufficient"
    assert movement["p_value"] is None
    result["map"]["projection_inputs"]["vectors"] = [[0, 0, 0] for p in result["papers"]]
    storage.save("results", result)
    movement = landscape.build_landscape(result["id"])["movements"][0]
    assert movement["cosine_distance"] is None


def test_zero_vectors_cannot_satisfy_minimum_group_count():
    first = np.array([[1., 0.]] * 4 + [[0., 0.]])
    second = np.array([[0., 1.]] * 4 + [[0., 0.]])
    distance, p_value = landscape._test_shift(first, second, 42)
    assert distance == 1
    assert p_value is None


def test_zero_vectors_are_excluded_from_permutation_distribution():
    first = np.array([[1., 0.]] * 5)
    second = np.array([[0., 1.]] * 5)
    original = landscape._test_shift(first, second, 42)
    padded = landscape._test_shift(np.vstack((first, np.zeros((6, 2)))), np.vstack((second, np.zeros((3, 2)))), 42)
    assert padded == original


def test_projection_change_keeps_high_dimensional_tests_identical():
    result = result_fixture()
    auto = landscape.build_landscape(result["id"])
    pca = landscape.build_landscape(result["id"], projection="pca")
    assert auto["projection_id"] != pca["projection_id"]
    for field in ("cosine_distance", "p_value", "q_value", "status"):
        assert auto["movements"][0][field] == pca["movements"][0][field]
    assert pca["map"]["projection"]["algorithm"] == "pca"


def test_saved_coordinate_revision_changes_projection_identity():
    result = result_fixture()
    before = landscape.build_landscape(result["id"])
    result["map"]["nodes"][0]["x"] += .1
    storage.save("results", result)
    after = landscape.build_landscape(result["id"])
    assert before["projection_id"] != after["projection_id"]


def test_unclassified_topic_is_not_a_shift_candidate():
    result = result_fixture()
    result["topics"][0]["is_outlier"] = True
    storage.save("results", result)
    value = landscape.build_landscape(result["id"])
    assert value["centroids"]
    assert value["movements"][0]["status"] == "insufficient"
    assert value["movements"][0]["p_value"] is None
    assert value["movements"][0]["insufficient_reason"] == "unclassified_topic"


def test_legacy_nmf_uses_saved_weights_and_bounded_paper_lookup(monkeypatch):
    result = result_fixture()
    for paper, row in zip(result["papers"], result["map"]["projection_inputs"]["vectors"]):
        paper["topic_weights"] = {"topic-a": row[0], "topic-b": row[1], "topic-c": row[2]}
    del result["map"]["projection_inputs"]
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 10)
    storage.save("results", result)
    def never_restore(*args, **kwargs):
        raise AssertionError("must not scan or restore the full corpus")
    monkeypatch.setattr(large_storage, "iter_papers", never_restore)
    calls = []
    real_read = storage.read
    def bounded_read(kind, identifier, **kwargs):
        assert kwargs["include_papers"] is False
        return real_read(kind, identifier, **kwargs)
    real_papers = large_storage.papers_by_ids
    def bounded_papers(value, root, identifiers):
        calls.append(len(identifiers))
        return real_papers(value, root, identifiers)
    monkeypatch.setattr(storage, "read", bounded_read)
    monkeypatch.setattr(large_storage, "papers_by_ids", bounded_papers)
    value = landscape.build_landscape(result["id"], projection="pca")
    assert calls == [24]
    assert value["meta"]["representation_source"] == "legacy_saved_nmf_topic_weights"


def test_legacy_missing_inputs_explicitly_labels_tfidf_reconstruction():
    result = result_fixture()
    del result["map"]["projection_inputs"]
    result["meta"]["topic_model"] = "bertopic"
    result["map"]["edges"] = [{"source": "a", "target": "b", "weight": .9}]
    storage.save("results", result)
    value = landscape.build_landscape(result["id"], projection="pca")
    assert value["meta"]["representation_source"] == "legacy_sample_tfidf_reconstruction"
    assert any("元の SBERT" in warning for warning in value["warnings"])
    assert value["map"]["edges"] == []


@pytest.mark.parametrize("method", ["pca", "tsne", "umap"])
def test_requested_projection_is_reproducible(method):
    values = np.random.default_rng(42).normal(size=(24, 9))
    first, _, details = analytics._map_projection(values.copy(), "sbert", method)
    second, _, repeated = analytics._map_projection(values.copy(), "sbert", method)
    assert details["algorithm"] == method
    np.testing.assert_allclose(first, second, atol=1e-7)
    assert repeated == details
    assert np.isfinite(first).all()


def test_explicit_missing_umap_never_silently_becomes_tsne(monkeypatch):
    real_import = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == "umap":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ValueError, match="umap-learn"):
        analytics._map_projection(np.eye(12), "nmf", "umap")


def test_new_map_persists_at_most_fifty_dimensions_even_for_one_long_vector():
    papers = [{"id": "a", "title": "A", "year": 2024, "topic_id": "topic-1", "citations": 0}]
    result = analytics._build_map(papers, np.array([0]), sparse.csr_matrix(np.ones((1, 96))), "tfidf")
    assert len(result["projection_inputs"]["vectors"][0]) <= 50
    assert result["projection_inputs"]["paper_ids"] == ["a"]


def test_empty_result_remains_valid_json():
    identifier = storage.new_id()
    storage.save("results", {"id": identifier})
    value = landscape.build_landscape(identifier)
    assert value["map"]["nodes"] == []
    assert value["periods"] == []
    json.dumps(value, allow_nan=False)


def test_bh_adjustment_is_monotone_and_caps_at_one():
    rows = [{"id": str(i), "p_value": p, "q_value": None} for i, p in enumerate([.005, .04, .2, .9])]
    landscape._adjust_q(rows)
    assert [row["q_value"] for row in rows] == [.02, .08, pytest.approx(.26666667), .9]


@pytest.mark.parametrize("date_value,precision", [("2023-02", "year"), ("2022-05", "month"), ("2023-99", "month")])
def test_invalid_or_coarse_months_are_excluded(date_value, precision):
    result = result_fixture()
    result["papers"][0].update(publication_date=date_value, date_precision=precision)
    storage.save("results", result)
    value = landscape.build_landscape(result["id"], interval="month")
    assert value["meta"]["excluded_date_count"] == 1
