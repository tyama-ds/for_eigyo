import copy
import json

import numpy as np
import pytest
from scipy import sparse

from app.foresight import (ALPHA, BETA, GAMMA, MIN_ANCHOR_SIMILARITY, _rocchio,
                           _fragments, build_assessment, refine_assessment)


def paper(identifier, topic="steel", year=2024, text=None, authors=None, **extra):
    return {"id": identifier, "title": "Steel tensile strength " + identifier,
            "abstract": text if text is not None else "Steel tensile strength improved to 900 MPa. However, fatigue lifetime decreased.",
            "year": year, "topic_id": topic, "authors": authors or [], "keywords": ["steel", "tensile strength"],
            "doi": "", "citations": 0, "citation_history": {}, **extra}


def result(papers=None, **meta):
    return {"id": "r-one", "dataset_id": "ds-one", "dataset_name": "Fixture",
            "meta": {"years": [2021, 2022, 2023, 2024], "topic_model": "nmf", **meta},
            "topics": [{"id": "steel", "label": "Steel tensile strength", "keywords": ["steel", "tensile strength"],
                        "count": 999, "score": 100, "forecast": [{"year": 2030, "value": 999}]},
                       {"id": "solar", "label": "Solar photovoltaic", "keywords": ["solar", "photovoltaic"]},
                       {"id": "noise", "label": "Unclassified", "is_outlier": True}],
            "papers": papers or [paper("a", year=2021), paper("b", year=2022), paper("c", year=2023),
                                  paper("d", year=2024), paper("e", "solar", text="Solar photovoltaic efficiency improved.")],
            "map": {"nodes": []}}


def candidate(value, identifier="steel"):
    return next(c for c in value["candidates"] if c["id"] == identifier)


def test_build_preserves_inputs_and_full_paper_counts_and_excludes_outliers():
    raw = result([paper("a", year=2021), paper("b", year=2024), paper("noise", "noise")])
    before = copy.deepcopy(raw)
    assessment = build_assessment(raw)
    assert raw == before
    assert assessment["result_id"] == "r-one"
    assert assessment["dataset_id"] == "ds-one"
    assert len(assessment["papers"]) == 3
    assert len(assessment["candidates"]) == 1
    focus = candidate(assessment)
    assert focus["count"] == focus["seed_count"] == 2
    assert sum(row["count"] for row in focus["growth"]["series"]) == 2
    assert focus["growth"]["forecast"] == []
    assert focus["growth"]["forecast_available"] is False
    assert focus["readiness"]["stage"] == "unassessed"
    json.dumps(assessment, allow_nan=False)


def test_rocchio_numerically_uses_means_and_only_explicit_negatives():
    docs = sparse.csr_matrix(np.array([[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]))
    anchor = np.array([1., 0., 0.])
    feedback = [{"paper_id": "p", "source": "user", "relevance": "relevant", "stance": "counter"},
                {"paper_id": "n", "source": "user", "relevance": "irrelevant", "stance": "support"},
                {"paper_id": "x", "source": "pseudo", "relevance": "irrelevant"}]
    query, detail = _rocchio(docs, anchor, feedback, {"p": 0, "n": 1, "x": 2}, [], False)
    expected = ALPHA * anchor + BETA * np.array([1., 0., 0.]) - GAMMA * np.array([0., 1., 0.])
    expected /= np.linalg.norm(expected)
    assert np.allclose(query, expected)
    assert detail["positive_ids"] == ["p"]
    assert detail["negative_ids"] == ["n"]


def test_rocchio_anchor_guard_limits_off_topic_feedback():
    matrix = np.array([[0., 1.], [1., 0.]])
    feedback = [{"paper_id": "p", "source": "user", "relevance": "relevant"},
                {"paper_id": "n", "source": "user", "relevance": "irrelevant"}]
    query, details = _rocchio(matrix, np.array([1., 0.]), feedback, {"p": 0, "n": 1}, [], False)
    assert details["drift_limited"] is True
    assert np.dot(query, np.array([1., 0.])) >= MIN_ANCHOR_SIMILARITY
    assert details["feedback_scale"] < 1


def test_support_counter_labels_do_not_change_relevance_vector():
    base = build_assessment(result())
    support = refine_assessment(base, "steel", [{"paper_id": "a", "relevance": "relevant", "stance": "support"}])
    counter = refine_assessment(base, "steel", [{"paper_id": "a", "relevance": "relevant", "stance": "counter"}])
    left, right = candidate(support), candidate(counter)
    assert left["terms"] == right["terms"]
    assert [(r["paper_id"], r["relevance"]) for r in left["recommendations"]] == [(r["paper_id"], r["relevance"]) for r in right["recommendations"]]
    assert counter["rounds"][-1]["positive_ids"] == ["a"]
    assert counter["rounds"][-1]["negative_ids"] == []
    assert any(r["paper_id"] == "a" and r["feedback_stance"] == "counter" for r in right["recommendations"])


def test_explicit_negative_is_excluded_and_pseudo_negative_does_not_subtract():
    base = build_assessment(result())
    updated = refine_assessment(base, "steel", [{"paper_id": "a", "relevance": "irrelevant", "stance": "counter"},
                                               {"paper_id": "b", "relevance": "irrelevant", "source": "pseudo"}])
    assert "a" not in candidate(updated)["paper_ids"]
    assert "a" not in [r["paper_id"] for r in candidate(updated)["recommendations"]]
    assert updated["rounds"][-1]["negative_ids"] == ["a"]
    assert len(updated["feedback"]) == 1


def test_cumulative_feedback_deduplicates_latest_label_and_cannot_be_overwritten_by_pseudo():
    base = build_assessment(result())
    original = candidate(base)["original_query"]
    first = refine_assessment(base, "steel", [{"paper_id": "a", "relevance": "relevant"}] * 5)
    assert len(first["feedback"]) == 1
    second = refine_assessment(first, "steel", [{"paper_id": "a", "relevance": "irrelevant", "stance": "counter"},
                                              {"paper_id": "a", "relevance": "relevant", "source": "pseudo"},
                                              {"paper_id": "b", "relevance": "unknown"}])
    assert len(second["feedback"]) == 2
    assert second["feedback"][0]["relevance"] == "irrelevant"
    assert second["feedback"][1]["relevance"] == "unjudged"
    assert candidate(second)["original_query"] == original
    assert second["retrieval_space"] == base["retrieval_space"]
    assert first["feedback"][0]["relevance"] == "relevant"


def test_generated_abstract_and_missing_abstract_do_not_become_evidence():
    records = [paper("test", text="[TEST SUMMARY; TITLE-BASED; JA] Strength improved; cost decreased; prototype demonstrated."),
               paper("empty", text="", year=2022),
               paper("generated", text="Strength improved with prototype manufacture.", abstract_kind="generated"),
               paper("real", text="The tensile strength increased to 900 MPa. Fatigue durability remains a challenge.", year=2021)]
    assessment = build_assessment(result(records))
    focus = candidate(assessment)
    assert focus["readiness"]["eligible_papers"] == 1
    assert focus["readiness"]["generated_papers"] == 2
    assert focus["readiness"]["missing_abstract_papers"] == 1
    assert {f["paper_id"] for f in focus["evidence"]} == {"real"}
    assert focus["readiness"]["evidence_coverage_score"] == pytest.approx(33.33)
    assert next(d for d in focus["readiness"]["dimensions"] if d["id"] == "cost")["status"] == "unknown"
    assert focus["growth"]["available"] is False
    assert all(f["attribution"] == "not_verified" for f in focus["evidence"])
    source = records[-1]["abstract"]
    for fact in focus["evidence"]:
        assert source[fact["start"]:fact["end"]] == fact["text"]
    assert any("900 MPa" in f["numeric_mentions"] for f in focus["evidence"])


def test_readiness_with_no_real_abstracts_is_unknown_not_zero_maturity():
    assessment = build_assessment(result([paper("missing", text="")]))
    profile = candidate(assessment)["readiness"]
    assert profile["evidence_coverage_score"] is None
    assert profile["stage"] == "unassessed"
    assert all(row["status"] == "unknown" for row in profile["dimensions"])


def test_descriptive_growth_uses_share_and_actual_full_corpus_denominators():
    records = [paper("a", year=2021), paper("b", year=2022), paper("c", year=2023), paper("d", year=2024)]
    records += [paper(f"solar-{i}", "solar", year=2024) for i in range(6)]
    value = candidate(build_assessment(result(records)))["growth"]
    assert value["available"] is True
    assert value["growth_pct"] == 0
    assert value["share_change_pp"] == -75
    assert value["descriptive_only"] is True
    assert not value["forecast_available"]


@pytest.mark.parametrize("metadata", [{"sampled": True}, {"source_reports": [{"truncated": True}]}, {"is_demo": True}, {"annual_comparison_available": False}])
def test_invalid_comparison_scope_abstains_but_keeps_counts(metadata):
    focus = candidate(build_assessment(result(**metadata)))
    assert focus["growth"]["available"] is False
    assert focus["growth"]["growth_pct"] is None
    assert focus["growth"]["share_change_pp"] is None
    assert sum(row["count"] for row in focus["growth"]["series"]) == 4


def test_expanding_selected_years_does_not_invent_single_year_growth():
    focus = candidate(build_assessment(result([paper("a"), paper("b")], years=list(range(2015, 2026)))))
    assert focus["growth"]["reason_code"] == "single_year"
    assert focus["growth"]["available"] is False


def test_adding_papers_preserves_initial_time_series_and_invalidates_all_candidate_growth():
    base = build_assessment(result())
    before = copy.deepcopy(base)
    base["narrative"] = {"summary": "Old generated prose"}
    candidate(base)["content_facts"] = [{"old": True}]
    incoming = [paper("new", "", year=2025, doi="10.5555/new"), paper("a", year=2021)]
    incoming_before = copy.deepcopy(incoming)
    updated = refine_assessment(base, "steel", [], incoming)
    assert updated["rounds"][-1]["added_papers"] == 1
    assert updated["meta"]["adaptive_collection"] is True
    assert candidate(updated)["growth"]["series"] == candidate(before)["growth"]["series"]
    assert all(not c["growth"]["available"] for c in updated["candidates"])
    assert "narrative" not in updated and "content_facts" not in candidate(updated)
    assert "narrative" in base
    assert incoming == incoming_before
    assert updated["retrieval_space"] == before["retrieval_space"]
    assert len(updated["papers"]) == 6


def test_duplicate_incoming_doi_does_not_inflate_paper_counts():
    raw = result()
    raw["papers"][0]["doi"] = "10.5555/existing"
    base = build_assessment(raw)
    incoming = [paper("new-id", year=2021, doi="https://doi.org/10.5555/EXISTING")]
    updated = refine_assessment(base, "steel", [], incoming)
    assert len(updated["papers"]) == len(base["papers"])
    assert updated["rounds"][-1]["added_papers"] == 0
    assert updated["meta"]["adaptive_collection"] is False


def test_same_author_concentration_gets_penalty_and_identical_input_order_is_stable():
    records = [paper("a", authors=[{"id": "same", "name": "A"}]),
               paper("b", authors=[{"id": "same", "name": "A"}]),
               paper("c", authors=[{"id": "independent", "name": "B"}])]
    a = candidate(build_assessment(result(records)))["recommendations"]
    b = candidate(build_assessment(result(list(reversed(records)))))["recommendations"]
    assert a == b
    assert [r["paper_id"] for r in a] == ["a", "c", "b"]
    assert a[-1]["diversity_factor"] < 1


def test_empty_vocabulary_does_not_fabricate_similarities_or_lose_papers():
    raw = result([paper("zero", text="", title="!!!", keywords=[])])
    raw["topics"][0].update(label="???", keywords=[])
    assessment = build_assessment(raw)
    assert len(assessment["papers"]) == 1
    assert candidate(assessment)["recommendations"] == []
    refined = refine_assessment(assessment, "steel", [{"paper_id": "zero", "relevance": "relevant"}])
    assert candidate(refined)["recommendations"] == []
    assert not refined["rounds"][-1]["original_query_available"]
    json.dumps(refined, allow_nan=False)


def test_japanese_lexical_queries_match_without_transformer_download():
    raw = result([paper("ja", text="ステンレス鋼の引張強度が向上した。疲労寿命には課題が残る。", title="ステンレス鋼の試験", keywords=["引張強度"])])
    raw["topics"][0].update(label="引張強度", keywords=["引張強度"])
    focus = candidate(build_assessment(raw))
    assert focus["recommendations"][0]["paper_id"] == "ja"
    assert {f["dimension"] for f in focus["evidence"]} == {"performance", "durability"}


def test_sbert_path_uses_supplied_embedding_vectors_and_keeps_signed_dimensions(monkeypatch):
    base = build_assessment(result([paper("a", year=2021), paper("b")]))
    seen = []
    def fake(documents, meta, embedding):
        seen.append((documents, embedding))
        return np.array([[1., 0.], [0.8, -0.6], [1., 0.]]), {"model_id": "test-only"}
    monkeypatch.setattr("app.foresight._sbert_matrix", fake)
    updated = refine_assessment(base, "steel", [{"paper_id": "b", "relevance": "relevant"}], embedding="sbert")
    assert seen[0][1] == "sbert"
    assert updated["rounds"][-1]["embedding_details"]["model_id"] == "test-only"
    assert len(candidate(updated)["recommendations"]) == 2


def test_missing_sbert_does_not_silently_fall_back(monkeypatch):
    def fail(*args):
        raise RuntimeError("SBERT unavailable")
    monkeypatch.setattr("app.foresight._sbert_matrix", fail)
    with pytest.raises(RuntimeError, match="SBERT unavailable"):
        refine_assessment(build_assessment(result()), "steel", [], embedding="sbert")


def test_generated_abstract_quality_survives_duplicate_missing_text_fill():
    raw = result([paper("base", text="", doi="10.5555/missing")])
    base = build_assessment(raw)
    incoming = [paper("other", text="Steel strength improved to 900 MPa.", doi="10.5555/missing", abstract_kind="generated")]
    value = refine_assessment(base, "steel", [], incoming)
    assert len(value["papers"]) == 1
    assert value["papers"][0]["abstract_kind"] == "generated"
    assert candidate(value)["readiness"]["eligible_papers"] == 0
    assert candidate(value)["evidence"] == []


def test_sbert_nonfinite_vectors_are_rejected(monkeypatch):
    base = build_assessment(result([paper("one")]))
    monkeypatch.setattr("app.foresight._sbert_matrix", lambda *args: (np.array([[1., 0.], [float("nan"), 1.]]), {}))
    with pytest.raises(RuntimeError, match="有効な文書埋め込み"):
        refine_assessment(base, "steel", [], embedding="sbert")


def test_unknown_feedback_references_and_invalid_modes_are_rejected():
    base = build_assessment(result())
    for feedback in [[{"paper_id": "absent", "relevance": "relevant"}], [{"paper_id": "a", "relevance": "good"}]]:
        with pytest.raises(ValueError):
            refine_assessment(base, "steel", feedback)
    with pytest.raises(ValueError):
        refine_assessment(base, "absent", [])
    with pytest.raises(ValueError):
        refine_assessment(base, "steel", [], embedding="invented")


def test_all_noise_has_no_recommendations_but_retains_corpus():
    value = build_assessment(result([paper("noise", "noise")]))
    assert value["candidates"] == []
    assert len(value["papers"]) == 1


def test_display_caps_do_not_change_profile_counts_and_nonfinite_meta_is_safe():
    records = [paper(f"p-{i:03d}", year=2021 + i % 4) for i in range(160)]
    base = build_assessment(result(records, legacy_score=float("nan")))
    focus = candidate(base)
    assert focus["count"] == 160
    assert len(focus["recommendations"]) <= 30
    assert len(focus["evidence"]) <= 120
    assert focus["readiness"]["evidence_total"] > len(focus["evidence"])
    assert next(d for d in focus["readiness"]["dimensions"] if d["id"] == "performance")["paper_count"] == 160
    assert sum(row["count"] for row in focus["growth"]["series"]) == 160
    json.dumps(base, allow_nan=False)


def test_evidence_windows_complete_english_words_and_preserve_exact_offsets():
    text = "microstructural " * 40 + "strength increased to 901.5 MPa " + "characterization " * 40
    facts = _fragments(paper("window", text=text))
    assert facts
    for fact in facts:
        assert fact["text"] == text[fact["start"]:fact["end"]]
        assert not fact["text"].startswith("…") and not fact["text"].endswith("…")
        assert not fact["start"] or not (text[fact["start"] - 1].isalnum() and text[fact["start"]].isalnum())
        assert fact["end"] == len(text) or not (text[fact["end"] - 1].isalnum() and text[fact["end"]].isalnum())
        assert len(fact["text"]) < 500
        assert fact["prefix_omitted"] and fact["suffix_omitted"]
    assert any("901.5 MPa" in fact["text"] for fact in facts)


def test_evidence_prefers_japanese_sentence_boundaries_without_invented_punctuation():
    text = "背景を調べた。実験では引張強度が向上した。次の条件を比較する。"
    facts = _fragments(paper("ja-boundary", text=text))
    assert facts
    assert all(fact["text"] == "実験では引張強度が向上した。" for fact in facts)
    assert all(fact["text"] == text[fact["start"]:fact["end"]] for fact in facts)


def test_unbroken_surrounding_tokens_do_not_create_unbounded_excerpts():
    text = "x" * 5000 + " strength " + "y" * 5000
    fact = _fragments(paper("ocr", text=text))[0]
    assert fact["text"] == "strength"
    assert text[fact["start"]:fact["end"]] == fact["text"]
