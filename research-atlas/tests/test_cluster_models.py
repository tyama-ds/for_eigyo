"""Behavioral regression tests for full-corpus clustering and resource failures."""

import json

import numpy as np
import pytest
from scipy import sparse
from sklearn.cluster import DBSCAN
from sklearn.metrics import adjusted_rand_score
from sklearn.preprocessing import normalize

import app.cluster_models as clustering
from app.cluster_models import ClusterModelError, fit_cluster_model


def fixture_vectors(include_missing=True):
    rng = np.random.default_rng(129)
    centers = np.eye(3)
    values = np.concatenate([center + rng.normal(0, .018, (24, 3)) for center in centers])
    return np.vstack([values, np.zeros((2, 3))]) if include_missing else values


@pytest.mark.parametrize("method", list(clustering.CLUSTER_MODELS))
def test_algorithms_keep_all_rows_and_identify_separate_document_groups(method):
    values = fixture_vectors()
    before = values.copy()
    result = fit_cluster_model(values, method, 3, min_cluster_size=3,
                               options={"n_neighbors": 8, "eps": .12, "max_clusters": 3})
    np.testing.assert_array_equal(values, before)
    assert result["labels"].shape == (74,)
    assert result["map_matrix"].shape == (74, 3)
    assert np.isfinite(result["map_matrix"]).all()
    assert result["details"]["effective_clusters"] >= 3
    assert len(set(result["labels"][:24])) == 1
    assert len(set(result["labels"][24:48])) == 1
    assert len(set(result["labels"][48:72])) == 1
    assert len(set(result["labels"][[0, 24, 48]])) == 3
    assert result["outlier_label"] is not None
    assert (result["labels"][-2:] == result["outlier_label"]).all()
    assert result["details"]["information_insufficient_count"] == 2
    assert result["details"]["training_papers"] == 72
    assert result["details"]["transform_papers"] == 74
    assert result["details"]["sampling"] == "none"
    assert result["details"]["representation"]["representation_fit_papers"] == 74
    assert result["topic_keywords"] is None
    json.dumps(result["details"], allow_nan=False)


@pytest.mark.parametrize("method", list(clustering.CLUSTER_MODELS))
def test_all_zero_and_single_document_inputs_are_retained(method):
    empty = fit_cluster_model(sparse.csr_matrix((7, 100)), method, 3)
    assert empty["details"]["effective_clusters"] == 0
    assert empty["details"]["information_insufficient_count"] == 7
    assert empty["outlier_label"] == 0
    assert empty["labels"].tolist() == [0] * 7
    singleton = fit_cluster_model(np.ones((1, 100)), method, 3, min_cluster_size=1)
    assert singleton["labels"].tolist() == [0]
    assert singleton["details"]["effective_clusters"] == 1
    assert singleton["details"]["transform_papers"] == 1
    json.dumps(singleton["details"], allow_nan=False)


@pytest.mark.parametrize("method", list(clustering.CLUSTER_MODELS))
def test_same_input_produces_reproducible_assignments(method):
    args = (fixture_vectors(), method, 3)
    left = fit_cluster_model(*args)
    right = fit_cluster_model(*args)
    np.testing.assert_array_equal(left["labels"], right["labels"])
    np.testing.assert_allclose(left["map_matrix"], right["map_matrix"])


def test_random_and_plus_plus_initializations_are_explicitly_distinct():
    for method, expected in [("kmeans", "random"), ("kmeans_pp", "k-means++")]:
        result = fit_cluster_model(fixture_vectors(), method, 3)
        assert result["details"]["initialization"] == expected
        assert result["details"]["n_init"] == 10


def test_sparse_svd_uses_every_row_and_reduces_columns_without_mutating_input():
    rng = np.random.default_rng(42)
    values = sparse.random(135, 240, density=.03, random_state=rng, dtype=np.float32, format="csr")
    values = sparse.vstack([values, sparse.csr_matrix((1, 240))], format="csr")
    before = values.copy()
    result = fit_cluster_model(values, "kmeans_pp", 4)
    assert result["map_matrix"].shape == (136, 50)
    assert (values != before).nnz == 0
    assert result["details"]["representation"]["representation_fit_papers"] == 136
    assert result["details"]["representation"]["reduction"] == "TruncatedSVD (fit on all papers)"
    assert result["labels"][-1] == result["outlier_label"]


def test_streamed_dbscan_matches_sklearn_including_noise_and_duplicates():
    values = fixture_vectors(False)
    values = np.vstack([values, values[:6], -np.ones((1, 3))])
    result = fit_cluster_model(values, "dbscan", 3, min_cluster_size=4, options={"eps": .12})
    expected = DBSCAN(eps=.12, min_samples=4).fit_predict(normalize(values))
    actual = result["labels"].copy()
    actual[actual == result["outlier_label"]] = -1
    assert adjusted_rand_score(expected, actual) == 1
    np.testing.assert_array_equal(expected == -1, actual == -1)
    assert result["details"]["noise_count"] == 1
    assert result["details"]["core_papers"] == len(values) - 1


def test_dbscan_border_shared_between_clusters_joins_first_core_component():
    # Work directly on Euclidean values to make border reachability explicit.
    # Each dense group has 4 points, the middle point reaches one from each
    # group but is not core with min_samples=4.
    values = np.array([[-1.0], [-1.01], [-1.02], [-1.03], [1.0], [1.01], [1.02], [1.03], [0.0]])
    actual, details = clustering._dbscan(values, 1.0, 4, None)
    expected = DBSCAN(eps=1.0, min_samples=4).fit_predict(values)
    np.testing.assert_array_equal(actual, expected)
    assert actual[-1] == actual[0]


def test_dbscan_density_guard_is_readable_and_does_not_choose_another_algorithm(monkeypatch):
    monkeypatch.setattr(clustering, "MAX_GRAPH_EDGES", 50)
    with pytest.raises(ClusterModelError, match="eps.*MiniBatch"):
        fit_cluster_model(np.ones((30, 2)), "dbscan", 2, options={"eps": .8})


def test_knn_graph_small_components_are_retained_as_noise():
    values = np.vstack([fixture_vectors(False), [[-1, 0, 0], [0, -1, 0]]])
    result = fit_cluster_model(values, "knn_graph", 3, min_cluster_size=3, options={"n_neighbors": 8})
    assert result["details"]["supervised"] is False
    assert result["details"]["effective_clusters"] == 3
    assert result["details"]["noise_count"] == 2
    assert (result["labels"][-2:] == result["outlier_label"]).all()


def test_knn_edge_guard_checked_before_query(monkeypatch):
    monkeypatch.setattr(clustering, "MAX_GRAPH_EDGES", 50)
    with pytest.raises(ClusterModelError, match="近傍数 k"):
        fit_cluster_model(fixture_vectors(), "knn_graph", 3, options={"n_neighbors": 8})


def test_minibatch_visits_every_row_each_pass_and_assigns_late_groups(monkeypatch):
    # Final 23 rows represent a late-arriving topic. They must be trained and
    # inferred, even when they do not fill a batch.
    values = np.vstack([np.tile([1., 0., 0.], (1024, 1)), np.tile([0., 1., 0.], (1024, 1)),
                        np.tile([0., 0., 1.], (23, 1))])
    rows_seen = []
    real = clustering.MiniBatchKMeans.partial_fit
    def capture(self, batch, *args, **kwargs):
        rows_seen.append(len(batch))
        return real(self, batch, *args, **kwargs)
    monkeypatch.setattr(clustering.MiniBatchKMeans, "partial_fit", capture)
    result = fit_cluster_model(values, "minibatch_kmeans", 3)
    assert sum(rows_seen) == len(values) * 3
    assert len(rows_seen) == 6
    assert result["details"]["training_passes"] == 3
    assert len(set(result["labels"][[0, 1024, -1]])) == 3


def test_gmm_probabilities_correspond_to_compacted_labels_and_zero_rows():
    result = fit_cluster_model(fixture_vectors(), "gmm", 3)
    membership = result["membership"]
    np.testing.assert_allclose(membership[:72].sum(axis=1), 1, atol=1e-6)
    np.testing.assert_array_equal(membership[-2:], 0)
    for index in range(72):
        component = result["details"]["label_to_component"][str(result["labels"][index])]
        assert membership[index].argmax() == component
    assert result["details"]["covariance_type"] == "diag"


def test_gmm_memory_guard_does_not_sample(monkeypatch):
    monkeypatch.setattr(clustering, "MAX_GMM_RESPONSIBILITY_BYTES", 100)
    with pytest.raises(ClusterModelError, match="所属確率行列"):
        fit_cluster_model(fixture_vectors(), "gmm", 3)


def test_xmeans_discovers_more_groups_within_explicit_bound():
    values = fixture_vectors(False)
    result = fit_cluster_model(values, "xmeans", 1, options={"max_clusters": 3})
    assert result["details"]["initial_clusters"] == 1
    assert result["details"]["effective_clusters"] == 3
    assert result["details"]["accepted_splits"] == 2
    assert any("分散補正" in warning for warning in result["warnings"])


def test_bic_favors_two_separated_clouds_but_penalizes_spurious_split():
    rng = np.random.default_rng(7)
    one = rng.normal(0, .25, (250, 3))
    separated = np.vstack([one - 4, one + 4])
    one_fit = clustering._kmeans(separated, 1)
    two_fit = clustering._kmeans(separated, 2)
    assert clustering._bic(separated, two_fit.labels_, two_fit.cluster_centers_) > clustering._bic(separated, one_fit.labels_, one_fit.cluster_centers_)
    one_fit = clustering._kmeans(one, 1)
    two_fit = clustering._kmeans(one, 2)
    assert clustering._bic(one, two_fit.labels_, two_fit.cluster_centers_) < clustering._bic(one, one_fit.labels_, one_fit.cluster_centers_)


def test_birch_subcluster_guard_prevents_unbounded_ward_matrix(monkeypatch):
    monkeypatch.setattr(clustering, "MAX_BIRCH_SUBCLUSTERS", 2)
    with pytest.raises(ClusterModelError, match="部分クラスタ"):
        fit_cluster_model(fixture_vectors(), "birch", 3, options={"birch_threshold": .1})


def test_agglomerative_rejects_too_many_papers_without_fitting_subset(monkeypatch):
    monkeypatch.setattr(clustering, "MAX_AGGLOMERATIVE_PAPERS", 20)
    with pytest.raises(ClusterModelError, match="Ward.*20 件"):
        fit_cluster_model(fixture_vectors(), "agglomerative", 3)


def test_representation_memory_guard_precedes_dense_conversion(monkeypatch):
    monkeypatch.setattr(clustering, "MAX_VECTOR_BYTES", 20)
    with pytest.raises(ClusterModelError, match="作業メモリ上限"):
        fit_cluster_model(sparse.csr_matrix((100, 5000)), "kmeans_pp", 3)


def test_finite_large_vectors_are_normalized_without_becoming_missing_information():
    values = np.array([[1e30, 0], [0, 1e30]])
    result = fit_cluster_model(values, "kmeans_pp", 2)
    assert result["details"]["information_insufficient_count"] == 0
    np.testing.assert_allclose(result["map_matrix"], np.eye(2))


def test_many_density_components_raise_instead_of_dropping_or_merging_papers():
    angles = np.linspace(0, 2 * np.pi, 101, endpoint=False)
    values = np.repeat(np.column_stack([np.sin(angles), np.cos(angles)]), 3, axis=0)
    with pytest.raises(ClusterModelError, match="100 個.*自動で削除・統合"):
        fit_cluster_model(values, "dbscan", 3, min_cluster_size=2, options={"eps": .001})


@pytest.mark.parametrize("options", [{"eps": 0}, {"eps": float("nan")}, {"eps": 2.1},
    {"n_neighbors": 0}, {"n_neighbors": 1.5}, {"n_neighbors": True}, {"max_clusters": 101},
    {"min_samples": -2}, {"birch_threshold": float("inf")}, {"unknown": 2}])
def test_invalid_options_rejected_even_for_zero_information_input(options):
    with pytest.raises(ClusterModelError):
        fit_cluster_model(np.zeros((2, 2)), "kmeans_pp", 3, options=options)


@pytest.mark.parametrize("values", [np.array([[np.nan]]), np.array([[np.inf]]), np.array([[1j]]),
                                    np.zeros((0, 3)), np.zeros((3, 0)), np.ones(3)])
def test_invalid_document_representations_rejected(values):
    with pytest.raises(ClusterModelError):
        fit_cluster_model(values, "kmeans_pp", 3)


def test_xmeans_bounds_are_consistent():
    with pytest.raises(ClusterModelError, match="最大クラスタ数は初期"):
        fit_cluster_model(fixture_vectors(), "xmeans", 4, options={"max_clusters": 3})


def test_catalog_is_json_and_callers_cannot_mutate_it():
    catalog = clustering.cluster_model_catalog()
    assert len(catalog) == 9
    assert {entry["id"] for entry in catalog} == set(clustering.CLUSTER_MODELS)
    json.dumps(catalog, ensure_ascii=False)
    catalog[0]["label"] = "changed"
    assert clustering.cluster_model_catalog()[0]["label"] != "changed"
