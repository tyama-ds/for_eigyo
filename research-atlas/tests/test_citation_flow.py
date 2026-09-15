import json
import math

import numpy as np
import pytest
from sklearn.decomposition import PCA

from app import citation_flow, corpus_landscape, large_storage, storage
from app.analytics import _normalize_positions


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    yield
    citation_flow._build.cache_clear()
    citation_flow._projection.cache_clear()
    corpus_landscape._load.cache_clear()


def paper(identifier, year, vector, refs=None, topic="topic-source", **extra):
    return {"id": identifier, "doi": "10.1234/" + identifier, "title": "Research " + identifier,
            "abstract": "Observed research content", "year": year, "topic_id": topic,
            "publication_date": str(year) if year else "", "date_precision": "year",
            "landscape_vector": vector, "references": refs or [],
            "references_status": "provided" if refs is not None else "not_provided", **extra}


def save(papers):
    result = {"id": storage.new_id(), "meta": {"landscape_representation": {
                  "basis_scope": "full_corpus", "embedding": "fixture", "dimensions": len(papers[0]["landscape_vector"]) if papers else 1}},
              "papers": papers, "topics": [{"id": "topic-source", "label": "Current", "color": "#112233"},
                                            {"id": "topic-target", "label": "Foundations", "color": "#aabbcc"}]}
    storage.save("results", result)
    return result


def group(flow, period):
    return next(row for row in flow["centroids"] if row["period_id"] == period)


def ref(identifier):
    return {"doi": "10.1234/" + identifier}


def test_equal_citing_paper_weight_and_paired_source_population():
    papers = [paper("a", 2020, [1, 0, 0], topic="topic-target"),
              paper("b", 2020, [0, 1, 0], topic="topic-target"),
              paper("c", 2020, [0, 1, 0], topic="topic-target"),
              paper("s1", 2024, [1, 0, 0], [ref("a")]),
              paper("s2", 2024, [1, 0, 0], [ref("b"), ref("c")]),
              paper("unmatched", 2024, [0, 0, 1], [ref("outside")])]
    result = save(papers)
    flow = citation_flow.build_citation_flow(result["id"])
    row = group(flow, "2024")
    assert row["current"]["count"] == 3
    assert row["paired_current"]["count"] == row["foundation"]["count"] == 2
    assert row["cosine_distance"] == pytest.approx(1 - 1 / math.sqrt(2))
    assert row["coverage"]["matched_target_papers"] == 3
    assert row["coverage"]["semantic_edges"] == 3
    assert row["coverage"]["exclusions"]["unresolved_in_analysis"] == 1
    # The affine projection preserves the same nested mean and common basis.
    vectors = np.asarray([p["landscape_vector"] for p in papers], dtype=np.float32)
    expected = _normalize_positions(PCA(n_components=2, svd_solver="full").fit_transform(vectors))
    foundation = (expected[0] + expected[[1, 2]].mean(axis=0)) / 2
    np.testing.assert_allclose([row["foundation"]["x"], row["foundation"]["y"]], foundation, atol=1e-7)
    np.testing.assert_allclose([row["current"]["x"], row["current"]["y"]], expected[3:].mean(axis=0), atol=1e-7)
    np.testing.assert_allclose([row["paired_current"]["x"], row["paired_current"]["y"]], expected[[3, 4]].mean(axis=0), atol=1e-7)
    assert set(row["foundation"]["evidence_ids"]) == {"a", "b", "c"}
    assert all(edge["source_id"].startswith("s") and edge["target_id"] in {"a", "b", "c"} for edge in flow["edges"])
    assert flow["meta"]["projection"]["fit_papers"] == len(papers)
    json.dumps(flow, allow_nan=False)


def test_zero_vectors_missing_references_and_cancellation_are_not_zero_distance():
    papers = [paper("old", 2020, [0, 1]), paper("zero-old", 2020, [0, 0]),
              paper("zero-source", 2024, [0, 0], [ref("old")]),
              paper("bad-target", 2024, [1, 0], [ref("zero-old")]),
              paper("missing", 2024, [1, 0]), paper("supplied-empty", 2024, [1, 0], [])]
    flow = citation_flow.build_citation_flow(save(papers)["id"])
    row = group(flow, "2024")
    assert row["current"]["count"] == 3
    assert row["foundation"] is row["paired_current"] is row["cosine_distance"] is None
    assert row["coverage"]["papers_with_references"] == 3
    assert row["coverage"]["valid_edges"] == 2
    assert row["coverage"]["semantic_edges"] == 0
    assert row["coverage"]["exclusions"]["zero_source_vector"] == 1
    assert row["coverage"]["exclusions"]["zero_target_vector"] == 1
    assert not flow["edges"]
    cancelled = save([paper("a", 2020, [1, 0]), paper("b", 2020, [-1, 0]),
                      paper("source", 2024, [0, 1], [ref("a"), ref("b")])])
    row = group(citation_flow.build_citation_flow(cancelled["id"]), "2024")
    assert row["foundation"]["count"] == 1
    assert row["cosine_distance"] is None
    assert row["comparison_reason"] == "zero_centroid_norm"


def test_exact_aliases_duplicates_ambiguity_and_conflicting_identifiers():
    papers = [paper("a", 2020, [1, 0], aliases={"ids": ["PMID:42"], "dois": ["10.1234/alias"]}),
              paper("b", 2020, [0, 1], aliases={"ids": ["PMID:43"]}),
              paper("c", 2020, [0, 1], doi="10.1234/ambiguous", aliases={"ids": ["PMID:44"]}),
              paper("d", 2020, [0, 1], doi="10.1234/ambiguous"),
              paper("source", 2024, [1, 1], [
                  {"doi": "HTTPS://DOI.ORG/10.1234/ALIAS.", "id": "https://pubmed.ncbi.nlm.nih.gov/42/"},
                  {"id": "pmid:42"}, ref("a"),
                  {"doi": "10.1234/a", "id": "pmid:43"},
                  {"doi": "10.1234/nonexistent", "id": "pmid:42"},
                  {"doi": "10.1234/ambiguous", "id": "pmid:44"},
                  "Reference DOI 10.1234/a PMID:43",
                  {"id": "a"}, {"title": "Research a"}, ref("absent")])]
    flow = citation_flow.build_citation_flow(save(papers)["id"])
    assert [(edge["source_id"], edge["target_id"]) for edge in flow["edges"]] == [("source", "a")]
    excluded = group(flow, "2024")["coverage"]["exclusions"]
    assert excluded == {"duplicate_source_target": 2, "conflicting_identifiers": 3,
                        "ambiguous_identifier": 1, "unrecognized_identifier": 2, "unresolved_in_analysis": 1}


def test_chronology_uses_date_uncertainty_and_never_invents_months():
    dated = {"date_precision": "day"}
    papers = [paper("before", 2024, [1, 0], publication_date="2024-06-09", **dated),
              paper("same", 2024, [1, 0], publication_date="2024-06-10", **dated),
              paper("month-before", 2024, [1, 0], publication_date="2024-05", date_precision="month"),
              paper("year-only", 2024, [1, 0]),
              paper("future-target", 2024, [1, 0], publication_date="2024-07-01", **dated),
              paper("missing-year", None, [1, 0]),
              paper("date-conflict", 2023, [1, 0], publication_date="2024-01", date_precision="month"),
              paper("source", 2024, [0, 1], [ref(key) for key in ("before", "same", "month-before", "year-only", "future-target", "missing-year", "date-conflict", "source")],
                    publication_date="2024-06-10", **dated)]
    result = save(papers)
    yearly = citation_flow.build_citation_flow(result["id"])
    monthly = citation_flow.build_citation_flow(result["id"], interval="month")
    quarterly = citation_flow.build_citation_flow(result["id"], interval="quarter")
    for flow in (yearly, monthly, quarterly):
        assert {edge["target_id"] for edge in flow["edges"]} == {"before", "month-before"}
        assert flow["meta"]["projection"]["projection_id"] == yearly["meta"]["projection"]["projection_id"]
    assert group(yearly, "2024")["coverage"]["exclusions"]["chronology_not_strictly_older"] == 2
    assert group(yearly, "2024")["coverage"]["exclusions"]["future_target"] == 1
    assert group(yearly, "2024")["coverage"]["exclusions"]["self_reference"] == 1
    assert monthly["coverage"]["exclusions"]["source_period_unknown"] == 3
    assert all(node["id"] != "year-only" for node in monthly["nodes"])
    xy = {node["id"]: (node["x"], node["y"]) for node in yearly["nodes"]}
    assert all((node["x"], node["y"]) == xy[node["id"]] for node in monthly["nodes"])


def test_full_corpus_sampling_preserves_aggregates_and_both_endpoints(monkeypatch):
    papers = [paper(f"old-{i}", 2020, [1, i % 3, .2], topic="topic-target") for i in range(300)]
    papers += [paper(f"new-{i}", 2024, [.2, 1, i % 3], [ref(f"old-{j}") for j in range(i % 300, min(i % 300 + 5, 300))]) for i in range(300)]
    result = save(papers)
    full = citation_flow.build_citation_flow(result["id"])
    assert len(full["nodes"]) <= 240 and len(full["edges"]) <= 480
    assert full["coverage"]["valid_edges"] > 480
    assert group(full, "2024")["current"]["count"] == 300
    shown = {node["id"] for node in full["nodes"]}
    assert all({edge["source_id"], edge["target_id"]} <= shown for edge in full["edges"])
    monkeypatch.setattr(citation_flow, "NODE_LIMIT", 8)
    monkeypatch.setattr(citation_flow, "EDGE_LIMIT", 4)
    small = citation_flow.build_citation_flow(result["id"])
    assert len(small["nodes"]) <= 8 and len(small["edges"]) <= 4
    assert small["centroids"] == full["centroids"]
    assert small["coverage"]["valid_edges"] == full["coverage"]["valid_edges"]
    # Returned responses are isolated from both the result and the caches.
    small["centroids"][0]["current"]["x"] = -999
    again = citation_flow.build_citation_flow(result["id"])
    assert again["centroids"] == full["centroids"]
    assert again["nodes"] == citation_flow.build_citation_flow(result["id"])["nodes"]


def test_topic_filter_retains_cross_topic_targets_and_full_basis():
    result = save([paper("old", 2020, [1, 0], topic="topic-target"),
                   paper("source", 2024, [0, 1], [ref("old")]),
                   paper("other", 2024, [1, 1], topic="topic-target")])
    all_topics = citation_flow.build_citation_flow(result["id"], topic_id="all")
    selected = citation_flow.build_citation_flow(result["id"], topic_id="topic-source")
    assert selected["coverage"]["papers_total"] == 1
    assert selected["coverage"]["matched_target_papers"] == 1
    assert {node["id"] for node in selected["nodes"]} == {"source", "old"}
    assert [p["id"] for p in selected["periods"]] == ["2020", "2024"]
    assert [p["index"] for p in selected["periods"]] == [0, 1]
    assert selected["meta"]["projection"] == all_topics["meta"]["projection"]
    assert selected["centroids"][0]["topic_id"] == "topic-source"
    with pytest.raises(ValueError):
        citation_flow.build_citation_flow(result["id"], topic_id="not-a-topic")
    with pytest.raises(ValueError):
        citation_flow.build_citation_flow(result["id"], interval="decade")


def test_empty_single_and_one_dimensional_inputs_have_finite_coordinates():
    for papers in ([], [paper("single", 2024, [0])],
                   [paper("old", 2020, [-1]), paper("new", 2024, [1], [ref("old")])]):
        flow = citation_flow.build_citation_flow(save(papers)["id"])
        json.dumps(flow, allow_nan=False)
        assert flow["coverage"]["analysis_papers"] == len(papers)
        if len(papers) == 2:
            assert flow["nodes"][0]["x"] != flow["nodes"][1]["x"]
            assert group(flow, "2024")["cosine_distance"] == pytest.approx(2)


def test_sidecar_corpus_preserves_provenance_and_streams_all_papers(monkeypatch):
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 1)
    result = save([paper("old", 2020, [1, 0]), paper("new", 2024, [0, 1], [ref("old")])])
    result.update(dataset_id="fixture-dataset", dataset_name="Synthetic validation")
    result["meta"].update(is_demo=True, providers=["synthetic"], sampled=True)
    storage.save("results", result)
    manifest = storage.read("results", result["id"], include_papers=False)
    assert manifest["_large_store"]["count"] == 2
    assert manifest["papers"] == []
    flow = citation_flow.build_citation_flow(result["id"])
    assert flow["coverage"]["valid_edges"] == 1
    assert flow["meta"]["is_demo"] is True
    assert flow["meta"]["dataset_id"] == "fixture-dataset"
    assert flow["meta"]["provenance"]["providers"] == ["synthetic"]
    assert flow["meta"]["provenance"]["sampled"] is True
