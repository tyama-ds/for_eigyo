import copy
import builtins
from datetime import date
import json

import numpy as np
import pytest
from scipy import sparse

import app.analytics as analytics
from app.analytics import _count_prediction, _forecast, _represent, _windowed_embeddings, analyze


def paper(identifier, year, title="Quantum computing error correction", **kwargs):
    result = {"id": identifier, "year": year, "title": title, "abstract": title,
              "authors": [{"id": "author-a", "name": "Author A"}],
              "keywords": ["quantum computing"], "citations": 3,
              "citation_history": {}, "doi": "", "source": "Journal"}
    result.update(kwargs)
    return result


def options(**kwargs):
    return {"start_year": 2021, "end_year": 2025, "n_topics": 2, **kwargs}


@pytest.mark.parametrize("topic_model", ["kmeans", "nmf", "lda"])
def test_test_summary_metadata_is_excluded_without_losing_original_abstract(topic_model):
    marked = "[TEST SUMMARY; TITLE-BASED; JA] 電池の性能を評価したテスト要約。"
    papers = [paper("a", 2025, "Battery electrolyte cells", abstract=marked),
              paper("b", 2025, "Quantum computing qubits", abstract="[TEST SUMMARY; TITLE-BASED; JA] 量子回路のテスト要約。")]
    before = copy.deepcopy(papers)
    documents, _, terms = analytics._tfidf(papers, [])
    assert all("TEST SUMMARY" not in text for text in documents)
    assert not {"ja", "summary", "title-based"} & set(terms)
    result = analyze(papers, options(start_year=2025, topic_model=topic_model))
    assert result["papers"][0]["abstract"] == marked
    assert papers == before
    assert any("TEST SUMMARY" in warning for warning in result["meta"]["warnings"])
    assert all(topic["forecast"] == [] and topic["growth_pct"] is None for topic in result["topics"])
    assert all(keyword["growth_pct"] is None for keyword in result["keywords"])
    assert result["summary"]["emerging_topics"] == 0
    assert not result["frontiers"]["monthly"]["available"]


def test_legitimate_summary_words_are_not_globally_filtered():
    documents, _, terms = analytics._tfidf([paper("a", 2025, "Summary evaluation", abstract="A summary of methods.")], [])
    assert "summary" in terms
    assert "A summary of methods." in documents[0]


def test_default_window_is_last_five_completed_years():
    this_year = date.today().year
    result = analyze([paper("old", this_year - 6), paper("included", this_year - 1), paper("unfinished", this_year)])
    assert result["meta"]["years"] == list(range(this_year - 5, this_year))
    assert [p["id"] for p in result["papers"]] == ["included"]


def test_snapshot_citations_never_become_annual_history():
    result = analyze([paper("a", 2021, citations=150), paper("b", 2025, citations=30)], options())
    assert result["summary"]["citations"] == 180
    assert result["timeline"][0]["citations"] == 150
    assert result["timeline"][-1]["citations"] == 30
    assert all(row["annual_citations"] is None for row in result["timeline"])
    assert result["meta"]["citation_history_coverage"] == 0
    assert result["topics"][0]["citation_growth_pct"] is None


def test_annual_events_preserve_unknowns_and_real_zero():
    result = analyze([
        paper("a", 2021, citation_history={"2022": 0, "2024": 4, "2025": 5}),
        paper("b", 2023, citation_history={"2025": 3}),
    ], options())
    rows = {row["year"]: row for row in result["timeline"]}
    assert rows[2021]["annual_citations"] is None
    assert rows[2022]["annual_citations"] == 0
    assert rows[2022]["annual_coverage"] == 100
    assert rows[2024]["annual_citations"] == 4
    assert rows[2024]["annual_coverage"] == 50
    assert rows[2025]["annual_citations"] == 8
    assert rows[2025]["annual_coverage"] == 100
    # Missing 2024 for b prevents misleading growth from an expanding observation set.
    assert result["topics"][0]["citation_growth_pct"] is None


def test_citation_growth_compares_fixed_existing_cohort():
    result = analyze([
        paper("a", 2021, citation_history={"2024": 4, "2025": 9}),
        paper("new", 2025, citation_history={"2025": 500}),
    ], options())
    assert result["topics"][0]["citation_growth_pct"] == 100
    assert result["topics"][0]["score_components"]["citation_mode"] == "annual_fixed_cohort"


def test_citation_snapshot_proxy_is_labelled_and_age_adjusted():
    result = analyze([paper("a", 2021, citations=60)], options())
    topic = result["topics"][0]
    assert topic["score_components"]["citation_mode"] == "age_adjusted_snapshot"
    assert topic["citation_age_proxy"] == round(60 / (date.today().year - 2021 + 1), 3)
    assert topic["citation_growth_pct"] is None


def test_no_citation_measurements_mean_no_citation_score_component():
    result = analyze([paper("a", 2021, citations=None)], options())
    components = result["topics"][0]["score_components"]
    assert components["citation_mode"] == "unavailable"
    assert components["citation_signal"] is None


def test_evidence_interleaves_recent_work_with_highly_cited_work():
    papers = [paper(f"old-{i}", 2021, citations=1000 - i) for i in range(5)]
    papers += [paper("recent-a", 2025, citations=1), paper("recent-b", 2025, citations=0)]
    result = analyze(papers, options(n_topics=1))
    assert result["topics"][0]["evidence_ids"] == ["old-0", "recent-a", "old-1", "recent-b", "old-2"]


def test_identical_corpus_has_one_reproducible_topic_and_finite_json():
    papers = [paper(str(i), 2021 + i % 5) for i in range(10)]
    result = analyze(papers, options(n_topics=6))
    again = analyze(list(reversed(papers)), options(n_topics=6))
    assert len(result["topics"]) == 1
    assert result["map"] == again["map"]
    assert result["network"] == again["network"]
    json.dumps(result, allow_nan=False)


def test_distinct_technologies_form_coherent_clusters():
    papers = [paper(f"quantum-{i}", 2021 + i % 5) for i in range(5)]
    papers += [paper(f"solar-{i}", 2021 + i % 5,
                     title="Perovskite photovoltaics solar energy conversion",
                     abstract="Perovskite photovoltaics solar cells energy conversion efficiency",
                     keywords=["perovskite photovoltaics"]) for i in range(5)]
    result = analyze(papers, options())
    assert len(result["topics"]) == 2
    assignments = {item["id"]: item["topic_id"] for item in result["papers"]}
    assert len({assignments[f"quantum-{i}"] for i in range(5)}) == 1
    assert len({assignments[f"solar-{i}"] for i in range(5)}) == 1
    assert assignments["quantum-0"] != assignments["solar-0"]


def test_empty_abstract_and_empty_vocabulary_are_valid_but_disclosed():
    result = analyze([paper("a", 2021, title="the and of", abstract="", keywords=[], citations=None)], options())
    assert result["meta"]["abstract_coverage"] == 0
    assert result["topics"][0]["label"] == "情報不足"
    assert result["summary"]["citations"] == 0
    assert result["keywords"] == []
    assert result["map"]["nodes"][0]["x"] == 0.5
    json.dumps(result, allow_nan=False)


def test_missing_year_invalid_history_and_nan_are_not_fabricated():
    result = analyze([paper("missing", None), paper("valid", 2024, citations=float("nan"),
                     citation_history={"2020": 4, "2024": -5, "2025": float("nan")})], options())
    assert result["summary"]["papers"] == 1
    assert result["meta"]["citation_history_coverage"] == 0
    assert result["papers"][0]["citations"] is None
    assert len(result["meta"]["warnings"]) >= 3
    json.dumps(result, allow_nan=False)


def test_does_not_modify_imported_source():
    papers = [paper("a", 2022, authors=[{"name": "A"}], keywords=["Quantum", "Quantum"])]
    before = copy.deepcopy(papers)
    analyze(papers, options())
    assert papers == before


def test_analysis_retains_provider_and_measurement_provenance_without_mutating_it():
    metadata = {"providers": ["crossref", "europepmc"], "citation_source": "crossref",
                "citation_snapshots": [{"provider": "crossref", "count": 3, "retrieved_at": "2026-09-11"},
                                       {"provider": "europepmc", "count": 7, "retrieved_at": "2026-09-11"}],
                "external_url": "https://doi.org/10.1234/example", "retrieved_at": "2026-09-11",
                "aliases": {"ids": ["a", "remote-a"]}, "provenance": "synthetic:research-atlas:v1"}
    original = paper("a", 2024, **metadata)
    result = analyze([original], options())
    analyzed = result["papers"][0]
    for field, value in metadata.items():
        assert analyzed[field] == value
    assert analyzed["citations"] == 3
    analyzed["citation_snapshots"][0]["count"] = 500
    assert original["citation_snapshots"][0]["count"] == 3


def test_constant_forecast_is_constant_and_backtest_has_expected_origins():
    forecast, backtest = _forecast([8, 8, 8, 8, 8], 2025, 3)
    assert [row["value"] for row in forecast] == [8, 8, 8]
    assert backtest["mae"] == 0
    assert backtest["baseline_mae"] == 0
    assert backtest["folds"] == 2
    assert [row["year"] for row in backtest["fold_details"]] == [2024, 2025]
    assert [row["year"] for row in forecast] == [2026, 2027, 2028]


def test_backtest_first_origin_cannot_see_later_counts():
    _, original = _forecast([1, 2, 3, 4, 5], 2025, 3)
    _, altered = _forecast([1, 2, 3, 4, 9999], 2025, 3)
    assert original["fold_details"][0] == altered["fold_details"][0]
    assert original["fold_details"][1]["prediction"] == altered["fold_details"][1]["prediction"]


def test_forecast_and_backtest_ignore_current_citation_totals():
    papers = [paper(str(i), 2021 + i % 5, citations=0) for i in range(17)]
    before = analyze(papers, options())
    for index, item in enumerate(papers):
        item["citations"] = index * 100000
    after = analyze(papers, options())
    assert before["topics"][0]["forecast"] == after["topics"][0]["forecast"]
    assert before["topics"][0]["backtest"] == after["topics"][0]["backtest"]


@pytest.mark.parametrize("counts", [[0], [0, 0, 0, 0], [0, 0, 0, 1000], [1000, 1, 0, 0], [1, 2, 5, 20, 100]])
def test_extrapolation_is_bounded_and_bands_ordered(counts):
    forecast = _count_prediction(counts, 3)
    for step, row in enumerate(forecast, 1):
        assert 0 <= row["lower"] <= row["value"] <= row["upper"]
        assert row["upper"] <= max(counts[-3:]) * (1 + 0.6 * step) + 2 * step
        assert np.isfinite(list(row.values())).all()


def test_tiny_period_has_no_fake_backtest():
    result = analyze([paper("a", 2025)], options(start_year=2025))
    assert result["topics"][0]["backtest"]["folds"] == 0
    assert result["topics"][0]["backtest"]["mae"] is None
    assert result["summary"]["growth_pct"] is None


@pytest.mark.parametrize("topic_model", ["kmeans", "nmf", "lda"])
@pytest.mark.parametrize("observed_year", [2023, 2025])
def test_single_year_corpus_cannot_gain_growth_or_forecasts_by_expanding_selected_period(topic_model, observed_year):
    papers = [paper("a", observed_year, "Battery electrolyte ion transport"),
              paper("b", observed_year, "Lithium battery electrolyte interfaces"),
              paper("c", observed_year, "Quantum qubit error correction"),
              paper("d", observed_year, "Quantum circuit logical qubits")]
    before = copy.deepcopy(papers)
    expanded = analyze(papers, options(topic_model=topic_model, single_year_corpus=True))
    actual = analyze(papers, options(start_year=observed_year, end_year=observed_year,
                                    topic_model=topic_model, single_year_corpus=True))
    assert papers == before
    assert expanded["meta"]["single_year_corpus"] is True
    assert expanded["meta"]["annual_comparison_available"] is False
    assert [row["papers"] for row in expanded["timeline"]] == [4 if y == observed_year else 0 for y in range(2021, 2026)]
    assert all(t["growth_pct"] is None and t["forecast"] == [] for t in expanded["topics"])
    assert all(t["backtest"]["model"] == "insufficient_history" and t["backtest"]["folds"] == 0 for t in expanded["topics"])
    assert all(k["growth_pct"] is None for k in expanded["keywords"])
    assert expanded["summary"]["growth_pct"] is None and expanded["summary"]["emerging_topics"] == 0
    assert all(t["status"] not in {"emerging", "declining"} for t in expanded["topics"])
    assert {t["id"]: t["score"] for t in expanded["topics"]} == {t["id"]: t["score"] for t in actual["topics"]}
    assert all("年次増減は比較できず" in t["explanation"] for t in expanded["topics"])
    assert any("収集範囲が未宣言" in warning for warning in expanded["meta"]["warnings"])


def test_declared_multiyear_observation_window_keeps_comparison_with_single_publication_year():
    result = analyze([paper("a", 2025), paper("b", 2025)], options(single_year_corpus=False))
    assert result["meta"]["annual_comparison_available"] is True
    assert result["topics"][0]["growth_pct"] is not None
    assert len(result["topics"][0]["forecast"]) == 3


def test_coauthor_edges_come_only_from_shared_papers_and_deduplicate_authors():
    a = {"id": "a", "name": "A"}
    b = {"id": "b", "name": "B"}
    c = {"id": "c", "name": "C"}
    result = analyze([paper("p1", 2021, authors=[a, a, b]), paper("p2", 2022, authors=[a, b]),
                      paper("p3", 2023, authors=[c])], options())
    assert result["network"]["edges"] == [{"source": "a", "target": "b", "weight": 2, "paper_ids": ["p1", "p2"]}]
    assert result["summary"]["authors"] == 3
    assert {node["id"]: node["count"] for node in result["network"]["nodes"]} == {"a": 2, "b": 2, "c": 1}


def test_map_limits_and_edge_referential_integrity():
    papers = [paper(str(i), 2021 + i % 5, title=f"Quantum error correction architecture {i}") for i in range(405)]
    result = analyze(papers, options(n_topics=1))
    assert result["map"]["truncated"]
    assert len(result["map"]["nodes"]) == 400
    ids = {node["id"] for node in result["map"]["nodes"]}
    assert all(edge["source"] in ids and edge["target"] in ids for edge in result["map"]["edges"])
    assert result["map"]["projection"]["sample_count"] == 400


def test_tsne_preserves_local_technology_groups_reproducibly():
    generator = np.random.default_rng(42)
    groups = np.repeat(np.arange(6), 8)
    matrix = np.eye(96)[groups] + generator.normal(0, 0.015, size=(len(groups), 96))
    matrix /= np.linalg.norm(matrix, axis=1)[:, None]
    positions, method, details = analytics._map_projection(matrix, "transformer")
    repeated, _, _ = analytics._map_projection(matrix, "transformer")
    assert details["algorithm"] == "tsne"
    assert details["reduced_dimensions"] <= 50
    assert "t-SNE" in method
    np.testing.assert_allclose(positions, repeated, atol=1e-7)
    assert np.isfinite(positions).all()
    assert (positions >= 0).all() and (positions <= 1).all()
    distances = np.linalg.norm(positions[:, None, :] - positions[None, :, :], axis=2)
    matching = groups[:, None] == groups[None, :]
    np.fill_diagonal(matching, False)
    assert distances[matching].mean() < 0.4 * distances[groups[:, None] != groups[None, :]].mean()


@pytest.mark.parametrize("matrix,reason", [
    (sparse.csr_matrix(np.eye(3)), "tiny_sample"),
    (sparse.csr_matrix(np.ones((12, 4))), "insufficient_unique_vectors"),
])
def test_tiny_and_identical_map_samples_use_finite_linear_fallback(matrix, reason):
    positions, _, details = analytics._map_projection(matrix, "tfidf")
    assert details["algorithm"] == "svd"
    assert details["fallback_reason"] == reason
    assert positions.shape == (matrix.shape[0], 2)
    assert np.isfinite(positions).all()


def test_projection_does_not_change_topic_classification_or_forecasts(monkeypatch):
    papers = [paper(str(i), 2021 + i % 5) for i in range(12)]
    original = analyze(papers, options())

    monkeypatch.setattr(analytics, "_map_projection", lambda matrix, embedding:
                        (np.zeros((matrix.shape[0], 2)), "test display", {"algorithm": "test"}))
    changed = analyze(papers, options())
    assert changed["topics"] == original["topics"]
    assert changed["summary"] == original["summary"]
    assert changed["timeline"] == original["timeline"]


@pytest.mark.parametrize("bad", [{"start_year": 2025, "end_year": 2021}, {"n_topics": 0},
                                 {"horizon": 4}, {"embedding": "unknown"}, {"n_topics": 2.5}])
def test_invalid_analysis_options_are_rejected(bad):
    with pytest.raises(ValueError):
        analyze([paper("a", 2025)], options(**bad))


def test_empty_selected_period_raises_actionable_error():
    with pytest.raises(ValueError, match="指定期間"):
        analyze([paper("a", 2010)], options())


class CharacterTokenizer:
    """One character is one token; model limits and weights stay independently checkable."""

    def __init__(self):
        self.inputs = []

    def num_special_tokens_to_add(self, pair=False):
        return 2

    def encode(self, text, **kwargs):
        assert kwargs["add_special_tokens"] is False
        assert kwargs["truncation"] is False
        self.inputs.append(text)
        return [ord(character) for character in text]

    def decode(self, ids, **kwargs):
        return "".join(chr(token) for token in ids)


class TinyTransformer:
    max_seq_length = 6

    def __init__(self):
        self.tokenizer = CharacterTokenizer()
        self.consumed = []

    def encode(self, texts, **kwargs):
        assert kwargs["normalize_embeddings"] is True
        assert all(len(text) <= self.max_seq_length - 2 for text in texts)
        self.consumed.extend(texts)
        vectors = np.array([[text.count("A"), text.count("B")] for text in texts], dtype=float)
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1), 1e-12)[:, None]
        return vectors


def test_transformer_reads_tail_windows_and_uses_token_weighted_document_mean():
    model = TinyTransformer()
    matrix, details = _windowed_embeddings(model, ["AAAAB", "BBBB"])
    # The 4-token leading window and 1-token conclusion each contribute their token weight.
    np.testing.assert_allclose(matrix[0], np.array([4, 1]) / np.sqrt(17))
    np.testing.assert_allclose(matrix[1], [0, 1])
    assert model.consumed == ["AAAA", "B", "BBBB"]
    assert details["encoded_windows"] == 3
    assert details["split_documents"] == 1
    assert details["capped_documents"] == 0


def test_window_cap_keeps_beginning_and_conclusion_and_reports_partial_coverage(monkeypatch):
    monkeypatch.setattr(analytics, "TRANSFORMER_WINDOWS_PER_DOCUMENT", 2)
    model = TinyTransformer()
    monkeypatch.setattr(analytics, "_load_transformer", lambda name: model)
    notices, details = [], {}
    matrix, _ = _represent(["A" * 28 + "BBBB"], None, "transformer", notices, details)
    assert model.consumed == ["AAAA", "BBBB"]
    np.testing.assert_allclose(matrix[0], np.array([1, 1]) / np.sqrt(2))
    assert details["window_capped_documents"] == 1
    assert details["available_windows"] == 8
    assert details["encoded_windows"] == 2
    assert any("部分抽出" in item for item in notices)


def test_character_cap_preserves_final_content_and_is_bounded(monkeypatch):
    monkeypatch.setattr(analytics, "TRANSFORMER_CHARACTER_LIMIT", 12)
    model = TinyTransformer()
    _, details = _windowed_embeddings(model, ["A" * 100 + "BBBB"])
    assert len(model.tokenizer.inputs[0]) == 12
    assert model.tokenizer.inputs[0].endswith("BBBB")
    assert details["character_capped_documents"] == 1


def test_global_window_budget_preserves_each_document(monkeypatch):
    monkeypatch.setattr(analytics, "TRANSFORMER_WINDOW_BUDGET", 6)
    model = TinyTransformer()
    matrix, details = _windowed_embeddings(model, ["A" * 28 + "BBBB"] * 3)
    assert matrix.shape == (3, 2)
    assert details["encoded_windows"] == 6
    assert details["window_capped_documents"] == 3
    assert model.consumed == ["AAAA", "BBBB"] * 3


def test_empty_transformer_document_finishes_with_finite_vector():
    model = TinyTransformer()
    matrix, details = _windowed_embeddings(model, [""])
    assert np.isfinite(matrix).all()
    assert details["encoded_windows"] == 1


def test_invalid_transformer_token_budget_raises_instead_of_stalling():
    model = TinyTransformer()
    model.max_seq_length = 2
    with pytest.raises(RuntimeError, match="最大長"):
        _windowed_embeddings(model, ["AAAAB"])


@pytest.mark.parametrize("failure", [OSError("WinError 1114: c10.dll"), RuntimeError("Torch initialization failed")])
def test_transformer_native_import_failure_is_actionable_and_has_no_fallback(monkeypatch, failure):
    original_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise failure
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    with pytest.raises(RuntimeError, match="PyTorch"):
        analytics._load_transformer.__wrapped__("mock-model")
