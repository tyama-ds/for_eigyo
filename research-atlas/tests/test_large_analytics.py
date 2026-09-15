"""Full-corpus invariants for the bounded lexical pipeline."""

from collections import Counter

import numpy as np
import pytest
from scipy import sparse

import app.analytics as analytics
import app.topic_models as models
from app.limits import MAX_DATASET_PAPERS


def paper(index, title="steel grain strength", year=2025, citations=None):
    return {"id": f"p-{index:06d}", "title": title, "abstract": "", "keywords": [],
            "year": year, "authors": [], "citations": citations}


def test_large_tfidf_uses_all_documents_and_keeps_float32_sparse(monkeypatch):
    monkeypatch.setattr(analytics, "LARGE_CORPUS_THRESHOLD", 2)
    monkeypatch.setattr(analytics, "LEXICAL_BATCH_SIZE", 2)
    papers = [paper(i, "steel grain strength") for i in range(5)]
    papers.append(paper(5, "quantum superconducting qubit"))
    stages = []
    documents, matrix, terms = analytics._tfidf(papers, [], stages.append)
    assert sparse.isspmatrix_csr(matrix)
    assert matrix.dtype == np.float32
    assert matrix.shape[0] == len(papers)
    assert "superconducting" in terms
    assert matrix[-1, list(terms).index("superconducting")] > 0
    # Vocabulary and IDF are learned from the last batch too.
    vectorizer = analytics.TfidfVectorizer(stop_words=analytics.STOP_WORDS, ngram_range=(1, 2),
        max_features=6000, sublinear_tf=True, strip_accents="unicode",
        token_pattern=r"(?u)\b[^\W\d_][\w-]+\b", dtype=np.float32)
    expected = vectorizer.fit_transform(documents)
    np.testing.assert_array_equal(terms, vectorizer.get_feature_names_out())
    np.testing.assert_allclose(matrix.toarray(), expected.toarray(), atol=1e-6)
    assert stages


@pytest.mark.parametrize("method,estimator_name,passes", [
    ("nmf", "MiniBatchNMF", 2), ("lda", "LatentDirichletAllocation", 1)])
def test_minibatch_models_visit_every_paper_and_preserve_missing_rows(monkeypatch, method, estimator_name, passes):
    monkeypatch.setattr(models, "LARGE_CORPUS_THRESHOLD", 2)
    monkeypatch.setattr(models, "LEXICAL_BATCH_SIZE", 7)
    monkeypatch.setattr(models, "NMF_TRAINING_PASSES", passes)
    monkeypatch.setattr(models, "LDA_TRAINING_PASSES", passes)
    base = getattr(models, estimator_name)
    visits, transforms = [], []

    class Recorded(base):
        def partial_fit(self, matrix, *args, **kwargs):
            assert sparse.issparse(matrix)
            assert len(matrix.data) <= 7 * matrix.shape[1]
            visits.extend(tuple(matrix.indices[matrix.indptr[i]:matrix.indptr[i + 1]]) for i in range(matrix.shape[0]))
            return super().partial_fit(matrix, *args, **kwargs)

        def transform(self, matrix, *args, **kwargs):
            transforms.append(matrix.shape[0])
            assert matrix.shape[0] <= 7
            return super().transform(matrix, *args, **kwargs)

    monkeypatch.setattr(models, estimator_name, Recorded)
    papers = ([paper(i, "steel grain strength") for i in range(12)] +
              [paper(i + 12, "solar panel energy") for i in range(12)] +
              [paper(24, "the and")])
    documents, matrix, terms = analytics._tfidf(papers, [])
    stages = []
    result = models.fit_topic_model(documents, matrix, terms, None, method, 2, progress_callback=stages.append)
    assert len(visits) == 24 * passes
    assert sorted(Counter(visits).values()) == [12 * passes, 12 * passes]
    assert sum(transforms) == 24
    assert result["membership"].shape == (25, 2)
    assert result["membership"].dtype == np.float32
    np.testing.assert_allclose(result["membership"][:24].sum(axis=1), 1, atol=1e-6)
    np.testing.assert_array_equal(result["membership"][-1], 0)
    assert result["details"]["corpus_papers"] == 25
    assert result["details"]["training_papers"] == 24
    assert result["details"]["training_passes"] == passes
    assert result["details"]["sampling"] == "none"
    assert stages
    repeated = models.fit_topic_model(documents, matrix, terms, None, method, 2)
    np.testing.assert_array_equal(result["labels"], repeated["labels"])
    np.testing.assert_allclose(result["membership"], repeated["membership"], atol=1e-6)


def test_multiyear_nmf_aggregates_entire_corpus_but_bounds_map(monkeypatch):
    monkeypatch.setattr(analytics, "LARGE_CORPUS_THRESHOLD", 20)
    monkeypatch.setattr(models, "LARGE_CORPUS_THRESHOLD", 20)
    monkeypatch.setattr(models, "LEXICAL_BATCH_SIZE", 64)
    monkeypatch.setattr(models, "NMF_TRAINING_PASSES", 2)
    titles = ["steel grain strength tensile", "solar panel energy photovoltaic", "quantum qubit computing"]
    papers = [paper(i, titles[i % 3], 1986 + i % 40, citations=i if i % 5 else None) for i in range(480)]
    stages = []
    result = analytics.analyze(papers, {"start_year": 1986, "end_year": 2025,
        "topic_model": "nmf", "embedding": "tfidf", "n_topics": 3}, progress_callback=stages.append)
    assert result["summary"]["papers"] == 480
    assert sum(topic["count"] for topic in result["topics"]) == 480
    assert sum(row["papers"] for row in result["timeline"]) == 480
    assert len(result["timeline"]) == 40
    assert sum(row["citation_known_count"] for row in result["timeline"]) == 384
    assert [p["citations"] for p in result["top_cited_papers"]] == [479, 478, 477, 476, 474, 473]
    assert len(result["map"]["nodes"]) == analytics.MAP_LIMIT
    assert result["field_geometry"]["paper_count"] == 480
    assert result["meta"]["analysis_scope"] == "full_corpus"
    assert result["meta"]["topic_model_details"]["training_papers"] == 480
    for topic in result["topics"]:
        members = [p for p in result["papers"] if p["topic_id"] == topic["id"]]
        known = [p["citations"] for p in members if p["citations"] is not None]
        assert topic["citation_known_count"] == len(known)
        assert topic["citation_total"] == sum(known)
        assert topic["citation_mean"] == round(sum(known) / len(known), 6)
    assert stages[-1].startswith("代表 400 件")


def test_analysis_rejects_over_capacity_and_semantic_before_model_load(monkeypatch):
    assert MAX_DATASET_PAPERS is None
    monkeypatch.setattr(analytics, "MAX_DATASET_PAPERS", 20)
    with pytest.raises(ValueError, match="20"):
        analytics.analyze([{}] * 21)
    monkeypatch.setattr(analytics, "SEMANTIC_PAPER_LIMIT", 2)
    monkeypatch.setattr(analytics, "_tfidf", lambda *_: pytest.fail("Should reject before vectorizing"))
    with pytest.raises(analytics.TransformerError, match="NMF"):
        analytics.analyze([paper(i) for i in range(3)], {"embedding": "sbert", "start_year": 2025, "end_year": 2025})
    monkeypatch.setattr(models, "SEMANTIC_PAPER_LIMIT", 2)
    monkeypatch.setattr(models, "_bertopic_dependencies", lambda: pytest.fail("Should reject before loading"))
    with pytest.raises(models.TopicModelError, match="BERTopic"):
        models.fit_topic_model(["steel"] * 3, None, None, None, "bertopic", 2)


def test_large_kmeans_trains_and_classifies_every_row(monkeypatch):
    monkeypatch.setattr(analytics, "LARGE_CORPUS_THRESHOLD", 1000)
    monkeypatch.setattr(analytics, "LEXICAL_BATCH_SIZE", 200)
    base = analytics.MiniBatchKMeans
    trained, predicted = [], []

    class Recorded(base):
        def partial_fit(self, matrix, *args, **kwargs):
            trained.append(matrix.shape[0])
            return super().partial_fit(matrix, *args, **kwargs)

        def predict(self, matrix, *args, **kwargs):
            predicted.append(matrix.shape[0])
            return super().predict(matrix, *args, **kwargs)

    monkeypatch.setattr(analytics, "MiniBatchKMeans", Recorded)
    matrix = sparse.csr_matrix(np.eye(2, dtype=np.float32)[np.arange(1501) % 2])
    labels = analytics._cluster(matrix, 2, [])
    assert sum(trained) == 1501 * 2
    assert sum(predicted) == 1501
    assert len(labels) == 1501
    assert len(set(labels[::2])) == 1
    assert len(set(labels[1::2])) == 1
    assert labels[0] != labels[1]


def test_missing_citations_remain_unknown_in_topic_aggregate():
    result = analytics.analyze([paper(0)], {"start_year": 2025, "end_year": 2025, "n_topics": 1})
    topic = result["topics"][0]
    assert topic["citation_total"] is None
    assert topic["citation_mean"] is None
    assert topic["citation_known_count"] == 0
