import builtins
import importlib.util
import json

import numpy as np
import pytest
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer

import app.topic_models as topic_models
from app.topic_models import TopicModelError, fit_topic_model


def tfidf_for(documents):
    vectorizer = TfidfVectorizer(stop_words=topic_models.STOP_WORDS, ngram_range=(1, 2),
                                 strip_accents="unicode", token_pattern=topic_models.TOKEN_PATTERN)
    matrix = vectorizer.fit_transform(documents)
    return matrix, vectorizer.get_feature_names_out()


def fit(documents, method="nmf", n_topics=2, **kwargs):
    matrix, terms = tfidf_for(documents)
    return fit_topic_model(documents, matrix, terms, kwargs.pop("embeddings", None),
                           method, n_topics, **kwargs)


def assert_valid(result, n):
    labels = result["labels"]
    assert labels.shape == (n,)
    assert np.issubdtype(labels.dtype, np.integer)
    assert set(labels) == set(range(len(set(labels))))
    assert result["map_matrix"].shape[0] == n
    assert np.isfinite(result["map_matrix"]).all()
    json.dumps(result["details"], allow_nan=False)
    assert sum(np.sum(labels == label) for label in set(labels)) == n


@pytest.mark.parametrize("method", ["nmf", "lda"])
def test_real_models_separate_two_clear_vocabularies_and_use_model_words(method):
    documents = ["quantum qubit quantum computing error correction" for _ in range(5)]
    documents += ["solar photovoltaic solar panel energy conversion" for _ in range(5)]
    result = fit(documents, method)
    assert_valid(result, 10)
    assert len(set(result["labels"][:5])) == 1
    assert len(set(result["labels"][5:])) == 1
    assert result["labels"][0] != result["labels"][5]
    assert any("quantum" in word for word in result["topic_keywords"][int(result["labels"][0])])
    assert any("solar" in word for word in result["topic_keywords"][int(result["labels"][5])])
    assert result["outlier_label"] is None
    np.testing.assert_allclose(result["membership"].sum(axis=1), 1)
    assert result["details"]["iterations"] <= (300 if method == "nmf" else 20)


@pytest.mark.parametrize("method", ["nmf", "lda"])
def test_lexical_fits_are_reproducible_and_keep_the_full_document_mixture(method):
    documents = ["quantum qubit computation", "quantum qubit correction", "quantum silicon qubit",
                 "solar panel energy", "solar silicon panel", "solar energy conversion"]
    result = fit(documents, method)
    repeated = fit(documents, method)
    np.testing.assert_array_equal(result["labels"], repeated["labels"])
    np.testing.assert_allclose(result["membership"], repeated["membership"])
    assert result["topic_keywords"] == repeated["topic_keywords"]
    for label, component in result["details"]["label_to_component"].items():
        rows = result["labels"] == int(label)
        assert (result["membership"][rows].argmax(axis=1) == component).all()


@pytest.mark.parametrize("method", ["nmf", "lda"])
def test_identical_corpus_and_one_paper_reduce_component_count(method):
    for documents in [["steel microstructure"] * 10, ["steel microstructure"]]:
        result = fit(documents, method, n_topics=8)
        assert_valid(result, len(documents))
        assert result["details"]["fitted_components"] == 1
        assert set(result["labels"]) == {0}


@pytest.mark.parametrize("method", ["nmf", "lda"])
def test_empty_vocabulary_has_no_fabricated_topic_words(method):
    documents = ["the and", "", "using paper results"]
    sentinel = sparse.csr_matrix(np.ones((3, 1)))
    result = fit_topic_model(documents, sentinel, ["情報不足"], None, method, 3)
    assert_valid(result, 3)
    assert result["outlier_label"] == 0
    assert result["details"]["information_insufficient_label"] == 0
    assert result["details"]["information_insufficient_count"] == 3
    assert result["details"]["fitted_components"] == 0
    assert result["topic_keywords"] == {0: []}
    assert result["membership"] is None


@pytest.mark.parametrize("method", ["nmf", "lda"])
def test_zero_rows_are_retained_without_lda_prior_becoming_a_fake_assignment(method):
    documents = ["steel grain microstructure", "steel grain strength", "the and", ""]
    matrix, terms = tfidf_for(documents)
    # The production analyzer adds a sentinel indicator; neither model may fit it.
    matrix = sparse.hstack([matrix, sparse.csr_matrix([[0], [0], [1], [1]])], format="csr")
    terms = np.append(terms, "情報不足")
    result = fit_topic_model(documents, matrix, terms, None, method, 2)
    assert_valid(result, 4)
    assert list(result["labels"][-2:]) == [result["outlier_label"]] * 2
    np.testing.assert_array_equal(result["membership"][-2:], 0)
    assert result["details"]["information_insufficient_count"] == 2
    assert all("情報不足" not in words for words in result["topic_keywords"].values())


def test_lda_fits_integer_term_counts_instead_of_tfidf(monkeypatch):
    real_estimator = topic_models.LatentDirichletAllocation
    observed = {}

    def recording_estimator(**kwargs):
        estimator = real_estimator(**kwargs)
        real_fit = estimator.fit_transform

        def record(matrix):
            observed["matrix"] = matrix.copy()
            return real_fit(matrix)

        estimator.fit_transform = record
        return estimator

    monkeypatch.setattr(topic_models, "LatentDirichletAllocation", recording_estimator)
    documents = ["steel steel steel grain", "solar solar panel"]
    result = fit(documents, "lda")
    count_matrix = observed["matrix"]
    assert count_matrix.dtype.kind in "iu"
    assert count_matrix.max() == 3
    expected = topic_models._count_vectorizer().fit_transform(documents)
    np.testing.assert_array_equal(count_matrix.toarray(), expected.toarray())
    assert result["details"]["input"] == "integer_term_counts"


def test_input_documents_and_sparse_matrix_are_not_modified():
    documents = ["steel grain microstructure", "solar photovoltaic panel"]
    original_documents = list(documents)
    matrix, terms = tfidf_for(documents)
    original_matrix = matrix.copy()
    original_terms = terms.copy()
    fit_topic_model(documents, matrix, terms, None, "nmf", 2)
    assert documents == original_documents
    np.testing.assert_array_equal(matrix.toarray(), original_matrix.toarray())
    np.testing.assert_array_equal(terms, original_terms)


def mock_bertopic(monkeypatch, raw_labels, reduced_labels=None):
    state = {}

    class Reducer:
        def __init__(self, **kwargs):
            state["umap"] = kwargs

    class Clusterer:
        def __init__(self, **kwargs):
            state["hdbscan"] = kwargs

    class FakeBERTopic:
        def __init__(self, **kwargs):
            state["bertopic"] = kwargs
            self.topics_ = list(raw_labels)

        def fit_transform(self, documents, embeddings):
            state["documents"] = documents
            state["embeddings"] = embeddings.copy()
            return self.topics_, None

        def reduce_topics(self, documents, nr_topics):
            state["reduced_to"] = nr_topics
            self.topics_ = list(reduced_labels)

        def get_topic(self, topic):
            return [(f"material{topic}", 0.8), ("grain", 0.2)]

    monkeypatch.setattr(topic_models, "_bertopic_dependencies",
                        lambda: (FakeBERTopic, Reducer, Clusterer, object))
    return state


def semantic_documents(n):
    return [f"steel alloy grain microstructure condition{number}" for number in range(n)]


def semantic_vectors(n):
    return np.random.default_rng(42).normal(size=(n, 8))


def test_bertopic_uses_precomputed_embeddings_and_keeps_hdbscan_noise(monkeypatch):
    raw_labels = [1, 1, 1, 0, 0, -1, -1, 0]
    state = mock_bertopic(monkeypatch, raw_labels)
    documents = semantic_documents(8)
    vectors = semantic_vectors(8)
    original = vectors.copy()
    result = fit(documents, "bertopic", embeddings=vectors)
    assert_valid(result, 8)
    assert result["outlier_label"] == 2
    assert result["labels"][5] == result["labels"][6] == 2
    assert result["details"]["outlier_count"] == 2
    assert result["topic_keywords"][2] == []
    assert result["membership"] is None
    assert state["bertopic"]["embedding_model"] is None
    assert state["bertopic"]["calculate_probabilities"] is False
    assert state["hdbscan"]["prediction_data"] is False
    assert state["umap"]["random_state"] == 42
    assert state["umap"]["n_jobs"] == 1
    np.testing.assert_allclose(np.linalg.norm(state["embeddings"], axis=1), 1)
    np.testing.assert_array_equal(vectors, original)
    np.testing.assert_array_equal(result["map_matrix"], original)


def test_bertopic_all_noise_is_not_force_clustered(monkeypatch):
    state = mock_bertopic(monkeypatch, [-1] * 8)
    result = fit(semantic_documents(8), "bertopic", embeddings=semantic_vectors(8))
    assert_valid(result, 8)
    assert set(result["labels"]) == {0}
    assert result["outlier_label"] == 0
    assert result["details"]["effective_topics"] == 0
    assert result["details"]["status"] == "all_noise"
    assert result["topic_keywords"] == {0: []}
    assert "reduced_to" not in state


def test_bertopic_reduction_reserves_noise_in_addition_to_requested_topics(monkeypatch):
    state = mock_bertopic(monkeypatch, [0, 0, 1, 1, 2, 2, -1, -1],
                          [0, 0, 0, 0, 1, 1, -1, -1])
    result = fit(semantic_documents(8), "bertopic", embeddings=semantic_vectors(8))
    assert state["reduced_to"] == 3  # 2 classified topics + BERTopic's noise group.
    assert result["details"]["initial_topics"] == 3
    assert result["details"]["effective_topics"] == 2
    assert result["details"]["reduced_topics"] is True
    assert result["labels"][-1] == result["outlier_label"] == 2


def test_bertopic_keeps_papers_without_information_outside_fitted_subset(monkeypatch):
    state = mock_bertopic(monkeypatch, [0, 0, 0, 1, 1, 1])
    documents = semantic_documents(6) + ["the and", ""]
    result = fit(documents, "bertopic", embeddings=semantic_vectors(8))
    assert_valid(result, 8)
    assert len(state["documents"]) == 6
    assert result["details"]["information_insufficient_count"] == 2
    assert result["labels"][-1] == result["labels"][-2] == result["outlier_label"]


def test_identical_bertopic_embeddings_cannot_acquire_randomized_semantic_clusters(monkeypatch):
    state = mock_bertopic(monkeypatch, [0] * 8)
    result = fit(semantic_documents(8), "bertopic", embeddings=np.ones((8, 8)))
    assert "bertopic" not in state
    assert result["details"]["status"] == "all_noise"
    assert result["details"]["effective_topics"] == 0


def test_bertopic_missing_dependency_is_readable_and_does_not_fallback(monkeypatch):
    real_import = builtins.__import__

    def missing_import(name, *args, **kwargs):
        if name == "bertopic":
            raise ImportError("missing optional dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_import)
    with pytest.raises(TopicModelError, match="requirements-topics.txt"):
        fit(semantic_documents(8), "bertopic", embeddings=semantic_vectors(8))


def test_small_bertopic_corpus_and_missing_embeddings_have_readable_errors():
    with pytest.raises(TopicModelError, match="少なくとも 6 件"):
        fit(semantic_documents(4), "bertopic", embeddings=semantic_vectors(4))
    with pytest.raises(TopicModelError, match="埋め込みが必要"):
        fit(semantic_documents(8), "bertopic")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_nmf_rejects_nonfinite_or_negative_features(bad):
    with pytest.raises(TopicModelError, match="負数または有限"):
        fit_topic_model(["steel"], sparse.csr_matrix([[bad]]), ["steel"], None, "nmf", 1)


def test_invalid_configuration_and_shapes_are_rejected():
    with pytest.raises(TopicModelError, match="トピックモデル"):
        fit(["steel grain"], method="unknown")
    with pytest.raises(TopicModelError, match="トピック数"):
        fit(["steel grain"], n_topics=True)
    with pytest.raises(TopicModelError, match="論文数"):
        fit_topic_model(["steel"], sparse.csr_matrix([[1], [2]]), ["steel"], None, "nmf", 1)
    with pytest.raises(TopicModelError, match="有限"):
        fit(semantic_documents(8), "bertopic", embeddings=np.full((8, 8), np.nan))


@pytest.mark.skipif(importlib.util.find_spec("bertopic") is None, reason="BERTopic is optional")
def test_actual_bertopic_fit_uses_finite_semantic_representation_without_losing_papers():
    generator = np.random.default_rng(42)
    vectors = np.vstack([np.array([1.0, 0, 0, 0, 0, 0, 0, 0]) + generator.normal(0, 0.03, (12, 8)),
                         np.array([0, 1.0, 0, 0, 0, 0, 0, 0]) + generator.normal(0, 0.03, (12, 8))])
    documents = ["quantum qubit computing correction" for _ in range(12)]
    documents += ["solar photovoltaic energy panel" for _ in range(12)]
    result = fit(documents, "bertopic", embeddings=vectors)
    assert_valid(result, 24)
    assert result["details"]["method"] == "bertopic"
    assert result["details"]["label_representation"] == "class_tfidf"
    assert result["details"]["effective_topics"] == 2
    repeated = fit(documents, "bertopic", embeddings=vectors)
    np.testing.assert_array_equal(result["labels"], repeated["labels"])
    assert result["topic_keywords"] == repeated["topic_keywords"]
    # With this minimum cluster size, the two 12-paper groups cannot qualify.
    # Exercise the library's actual all-noise path, including c-TF-IDF.
    all_noise = fit(documents, "bertopic", embeddings=vectors, min_topic_size=20)
    assert_valid(all_noise, 24)
    assert all_noise["details"]["status"] == "all_noise"
    assert all_noise["details"]["outlier_count"] == 24
