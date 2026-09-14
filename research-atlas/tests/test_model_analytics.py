"""Integration checks for model selection, representations, and retained noise papers."""

import copy
import json

import numpy as np
import pytest

import app.analytics as analytics
from app.embedding_models import SBERT_MODELS, resolve_embedding_model


def options(**changes):
    return {"start_year": 2021, "end_year": 2025, "n_topics": 3, **changes}


def corpus(include_empty=False):
    records = []
    for group, text in enumerate([
        "Quantum qubit error correction superconducting entanglement",
        "Perovskite photovoltaic solar absorber conversion efficiency",
        "Organoid tissue stem cells differentiation culture bioprinting",
    ]):
        for i in range(6):
            records.append({"id": f"{group}-{i}", "title": text, "abstract": text + f" experiment trial {i}",
                            "year": 2021 + i % 5, "keywords": [], "citations": i,
                            "authors": [{"id": f"author-{group}", "name": f"Author {group}"}],
                            "citation_history": {}})
    if include_empty:
        records.append({"id": "z-empty", "title": "the and", "abstract": "", "keywords": [],
                        "year": 2025, "citations": None, "authors": [], "citation_history": {}})
    return records


@pytest.mark.parametrize("method", ["nmf", "lda"])
def test_actual_topic_models_keep_every_paper_and_use_model_words_and_map(method, monkeypatch):
    records = corpus(include_empty=True)
    before = copy.deepcopy(records)
    actual_fit = analytics.fit_topic_model
    actual_map = analytics._build_map
    captured = {}

    def fit(*args):
        captured["fit"] = actual_fit(*args)
        return captured["fit"]

    def build_map(papers, labels, matrix, representation):
        captured["map"] = matrix.copy()
        captured["map_input"] = representation
        return actual_map(papers, labels, matrix, representation)

    monkeypatch.setattr(analytics, "fit_topic_model", fit)
    monkeypatch.setattr(analytics, "_build_map", build_map)
    result = analytics.analyze(records, options(topic_model=method))

    assert records == before
    assert {p["id"] for p in result["papers"]} == {p["id"] for p in records}
    assert result["summary"]["papers"] == sum(t["count"] for t in result["topics"]) == 19
    assert result["summary"]["topics"] == 3
    assert result["meta"]["unclassified_papers"] == 1
    assert result["meta"]["embedding"] == "tfidf"
    assert result["meta"]["embedding_model"] == ("CountVectorizer / integer unigrams + bigrams"
                                                if method == "lda" else "TF-IDF / unigrams + bigrams")
    if method == "lda":
        assert result["meta"]["embedding_details"]["keyword_representation"] == "TF-IDF / unigrams + bigrams"
    assert result["meta"]["topic_model"] == method
    assert result["meta"]["map_representation"] == f"{method}_topic_distribution"
    assert captured["map_input"] == method
    np.testing.assert_allclose(captured["map"], captured["fit"]["map_matrix"])
    assert method.upper() in result["map"]["method"]
    assert "Sentence Transformer" not in result["map"]["method"]
    assert "t-SNE" in result["map"]["method"]
    model_terms = captured["fit"]["topic_keywords"]
    for topic in result["topics"]:
        if topic["is_outlier"]:
            assert topic["label"] == "情報不足"
            assert topic["score"] == 0 and topic["forecast"] == []
        else:
            label = int(topic["id"].split("-")[-1]) - 1
            assert topic["keywords"] == model_terms[label]
            assert topic["label"] == " · ".join(model_terms[label][:2])
            assert topic["keywords"] and "情報不足" not in topic["keywords"]
    json.dumps(result, allow_nan=False)


def test_explicit_kmeans_tfidf_preserves_legacy_default_result():
    records = corpus()
    implicit = analytics.analyze(records, options())
    explicit = analytics.analyze(records, options(embedding="tfidf", topic_model="kmeans"))
    assert implicit == explicit
    assert implicit["meta"]["map_representation"] == "tfidf"
    assert implicit["summary"]["topics"] == len(implicit["topics"]) == 3


@pytest.mark.parametrize("method", ["nmf", "lda"])
def test_all_uninformative_lexical_papers_remain_visible_without_forecasts(method):
    record = {"id": "missing", "year": 2025, "title": "the and", "abstract": "", "keywords": [],
              "authors": [], "citations": None, "citation_history": {}}
    result = analytics.analyze([record], options(topic_model=method))
    assert result["summary"]["papers"] == 1 and result["summary"]["topics"] == 0
    assert result["meta"]["unclassified_papers"] == 1
    assert result["topics"][0]["label"] == "情報不足"
    assert result["topics"][0]["forecast"] == []
    assert result["map"]["nodes"][0]["x"] == 0.5
    assert result["frontiers"]["monthly"]["topics"][0]["status"] == "insufficient"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("preset", ["multilingual_minilm", "mpnet", None])
def test_sbert_uses_selected_fixed_id_and_records_preset(preset, monkeypatch):
    monkeypatch.setenv("ATLAS_EMBEDDING_MODEL", "unwanted/environment/model")
    loaded = []
    monkeypatch.setattr(analytics, "_load_transformer", lambda name: loaded.append(name) or object())
    monkeypatch.setattr(analytics, "_windowed_embeddings", lambda model, docs: (
        np.array([[1.0, float(index % 3), 0.5] for index, _ in enumerate(docs)]),
        {"strategy": "test", "capped_documents": 0}))
    request = options(embedding="sbert")
    if preset is not None:
        request["sbert_model"] = preset
    result = analytics.analyze(corpus()[:6], request)
    expected = preset or "mpnet"
    assert loaded == [SBERT_MODELS[expected]]
    assert result["meta"]["embedding_model"] == SBERT_MODELS[expected]
    assert result["meta"]["embedding_model_preset"] == expected
    assert result["meta"]["sbert_model"] == expected
    assert result["meta"]["map_representation"] == "sbert_embeddings"
    assert result["meta"]["embedding_details"]["legacy_environment_override"] is False


def test_legacy_transformer_keeps_only_its_environment_override(monkeypatch):
    monkeypatch.delenv("ATLAS_EMBEDDING_MODEL", raising=False)
    assert resolve_embedding_model("transformer")["model_id"] == SBERT_MODELS["multilingual_minilm"]
    monkeypatch.setenv("ATLAS_EMBEDDING_MODEL", "custom/legacy")
    assert resolve_embedding_model("transformer") == {
        "model_id": "custom/legacy", "preset": "legacy_custom", "legacy_environment_override": True}
    assert resolve_embedding_model("sbert", "mpnet")["model_id"] == SBERT_MODELS["mpnet"]


@pytest.mark.parametrize("changes", [
    {"embedding": "sbert", "sbert_model": "unknown"},
    {"embedding": "sbert", "topic_model": "nmf"},
    {"embedding": "transformer", "topic_model": "lda"},
    {"embedding": "tfidf", "topic_model": "bertopic"},
    {"topic_model": "unknown"}, {"topic_model": "bertopic", "min_topic_size": 1},
])
def test_invalid_combinations_fail_before_loading_models(changes, monkeypatch):
    monkeypatch.setattr(analytics, "_load_transformer", lambda _: pytest.fail("invalid options loaded a model"))
    with pytest.raises(ValueError):
        analytics.analyze(corpus(), options(**changes))


def test_sbert_download_failure_has_no_implicit_tfidf_or_kmeans_fallback(monkeypatch):
    def fail(name):
        raise OSError("secret path or network credentials")

    monkeypatch.setattr(analytics, "_load_transformer", fail)
    monkeypatch.setattr(analytics, "_cluster", lambda *args: pytest.fail("unexpected fallback"))
    with pytest.raises(analytics.TransformerError) as error:
        analytics.analyze(corpus(), options(embedding="sbert"))
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("all_noise", [False, True])
def test_bertopic_noise_stays_in_counts_but_has_no_promising_topic_metrics(all_noise, monkeypatch):
    records = corpus()[:12]
    for index, record in enumerate(records):
        record["year"] = 2025
        record["publication_date"] = "2025-01-10" if index < 3 else "2025-04-10"
        record["date_precision"] = "day"
        record["keywords"] = ["unmapped idea"] if index >= 6 else ["quantum sensor"]
    embeddings = np.random.default_rng(42).normal(size=(12, 8))
    monkeypatch.setattr(analytics, "_load_transformer", lambda _: object())
    monkeypatch.setattr(analytics, "_windowed_embeddings", lambda model, docs: (
        embeddings, {"capped_documents": 0}))

    def fit(documents, tfidf, terms, supplied_embeddings, method, n_topics, min_topic_size):
        assert method == "bertopic" and min_topic_size == 5
        np.testing.assert_allclose(supplied_embeddings, embeddings)
        labels = np.zeros(12, dtype=int) if all_noise else np.array([0] * 6 + [1] * 6)
        return {"labels": labels, "map_matrix": embeddings,
                "topic_keywords": {} if all_noise else {0: ["model representative", "sensor"]},
                "outlier_label": 0 if all_noise else 1, "membership": None,
                "details": {"method": "bertopic", "outlier_count": 12 if all_noise else 6},
                "warnings": ["test model evidence"]}

    monkeypatch.setattr(analytics, "fit_topic_model", fit)
    result = analytics.analyze(records, options(embedding="sbert", topic_model="bertopic", anchor_month="2025-06"))
    noise = next(topic for topic in result["topics"] if topic["is_outlier"])
    assert noise["label"] == "未分類" and noise["status"] == "unclassified"
    assert noise["score"] == 0 and noise["forecast"] == [] and noise["growth_pct"] is None
    assert result["summary"]["papers"] == sum(topic["count"] for topic in result["topics"]) == 12
    assert result["summary"]["topics"] == (0 if all_noise else 1)
    assert result["meta"]["unclassified_papers"] == (12 if all_noise else 6)
    assert result["summary"]["citations"] == sum(p["citations"] for p in records)
    assert len(result["map"]["nodes"]) == 12
    assert result["meta"]["map_representation"] == "sbert_embeddings"
    monthly = result["frontiers"]["monthly"]
    assert monthly["recent_total"] + monthly["baseline_total"] == 12
    noise_month = next(row for row in monthly["topics"] if row["topic_id"] == noise["id"])
    assert noise_month["status"] == "insufficient" and noise_month["growth_pct"] is None
    assert result["frontiers"]["sparse"]["corpus_papers"] == 12
    assert all(row["topic_id"] != noise["id"] for row in result["frontiers"]["sparse"]["terms"])
    if not all_noise:
        classified = next(topic for topic in result["topics"] if not topic["is_outlier"])
        assert classified["label"] == "model representative · sensor"
    json.dumps(result, allow_nan=False)
