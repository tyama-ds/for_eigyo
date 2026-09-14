import copy
import json

import numpy as np
import pytest

from app.analytics import analyze
from app.field_analysis import build_field_report, store_nmf_geometry


def paper(identifier, topic, year, authors=None, **extras):
    return {"id": identifier, "title": f"Research {identifier}", "abstract": "", "year": year,
            "topic_id": topic, "authors": authors or [], "affiliations": [], "doi": "",
            "keywords": [], "references": [], "references_status": "not_provided", **extras}


def author(identifier="orcid:0000-0001-2345-6789", name="Researcher A", **extra):
    return {"id": identifier, "name": name, **extra}


def result(papers=None, **extras):
    return {"id": "result-a", "dataset_name": "Materials", "meta": {
        "years": [2021, 2022, 2023, 2024, 2025], "topic_model": "nmf", "sampled": True,
        "providers": ["scopus_csv"], "is_demo": False},
        "topics": [
            {"id": "topic-1", "label": "Steel microstructure", "keywords": ["steel", "grain"], "count": 999,
             "forecast": [{"year": 2026, "value": 3, "lower": 0, "upper": 8}, {"year": 2028, "value": 4}, {"year": 2030, "value": 99}],
             "backtest": {"folds": 2, "mae": 1, "baseline_mae": 2}},
            {"id": "topic-2", "label": "Additive manufacturing", "keywords": ["steel", "laser"], "count": 888,
             "forecast": [], "backtest": {"folds": 0}},
            {"id": "topic-3", "label": "Solar energy", "keywords": ["solar"], "count": 1,
             "forecast": [], "backtest": {}},
            {"id": "topic-4", "label": "Unclassified", "is_outlier": True, "keywords": [], "count": 1}],
        "papers": papers or [paper("a", "topic-1", 2021, [author()]), paper("b", "topic-2", 2023, [author()]),
                              paper("c", "topic-3", 2025), paper("d", "topic-4", 2025)],
        "map": {"nodes": [{"id": "a", "x": 0, "y": 0}, {"id": "b", "x": 1, "y": 1}]}, **extras}


def test_counts_use_every_actual_paper_and_never_stale_topic_counts_or_map_sample():
    records = [paper(f"a-{i}", "topic-1", 2021 + i % 5) for i in range(405)]
    records += [paper("b", "topic-2", 2025), paper("other", "topic-3", 2025)]
    value = result(records)
    before = copy.deepcopy(value)
    report = build_field_report(value, "topic-1", "topic-2")
    assert report["focus"]["count"] == 405
    assert report["neighbor"]["count"] == 1
    assert sum(row["focus_count"] for row in report["annual"]) == 405
    assert report["scope"]["corpus_papers"] == 407
    assert report["evidence_papers_total"] == 406
    assert len(report["evidence_papers"]) == report["evidence_display_limit"] == 40
    assert report["scope"]["sampled"] is True
    assert any("標本" in text for text in report["limitations"])
    assert value == before
    json.dumps(report, allow_nan=False)


def test_nmf_geometry_and_weights_preserve_components_and_do_not_use_map_coordinates():
    records = [paper("a", "topic-1", 2021), paper("b", "topic-1", 2022), paper("c", "topic-2", 2023)]
    value = result(records)
    weights = np.array([[0.7, 0.2, 0.1], [0.6, 0.1, 0.3], [0.1, 0.8, 0.1]])
    geometry = store_nmf_geometry(records, value["topics"], weights, {"label_to_component": {"0": 0, "1": 1}})
    value["field_geometry"] = geometry
    first, second = weights[:2].mean(axis=0), weights[2]
    expected = np.dot(first, second) / (np.linalg.norm(first) * np.linalg.norm(second))
    report = build_field_report(value, "topic-1", "topic-2")
    neighbor = next(item for item in report["focus"]["neighbors"] if item["topic_id"] == "topic-2")
    assert neighbor["method"] == "nmf_centroid_cosine"
    assert neighbor["similarity"] == pytest.approx(expected, abs=1e-6)
    assert records[0]["topic_weights"] == {"topic-1": 0.7, "topic-2": 0.2}
    assert records[0]["topic_weights_unassigned_mass"] == 0.1
    assert geometry["latent_dimensions"] == 3
    value["map"]["nodes"] = []
    assert build_field_report(value, "topic-1", "topic-2")["focus"]["neighbors"] == report["focus"]["neighbors"]
    assert report["connections"]["bridge_papers_total"] == 3


@pytest.mark.parametrize("method", ["nmf", "lda", "kmeans", "bertopic"])
def test_old_and_non_nmf_results_use_explicit_keyword_fallback(method):
    value = result()
    value["meta"]["topic_model"] = method
    report = build_field_report(value, "topic-1", "topic-2")
    nearest = report["focus"]["neighbors"][0]
    assert nearest["topic_id"] == "topic-2"
    assert nearest["similarity"] == pytest.approx(1 / 3, abs=1e-6)
    assert nearest["method"] == "representative_keyword_jaccard"
    assert report["connections"]["bridge_papers"] == []
    assert any("Jaccard" in text for text in report["connections"]["basis_notes"])


def test_shared_authors_resolve_declared_aliases_and_first_year_direction():
    first = author("scopus:42", "A", aliases=["orcid:0000-0000-0000-0042"])
    alias = author("orcid:0000-0000-0000-0042", "A")
    same = author("name:same", "Same")
    reverse = author("orcid:reverse", "Reverse")
    records = [paper("a-first", "topic-1", 2021, [first]), paper("a-last", "topic-1", 2025, [first]),
               paper("b-first", "topic-2", 2023, [alias]),
               paper("same-a", "topic-1", 2024, [same]), paper("same-b", "topic-2", 2024, [same]),
               paper("reverse-a", "topic-1", 2025, [reverse]), paper("reverse-b", "topic-2", 2022, [reverse])]
    report = build_field_report(result(records), "topic-1", "topic-2")
    connections = report["connections"]
    assert connections["shared_authors_total"] == 3
    assert connections["transition_counts"] == {"focus_to_neighbor": 1, "neighbor_to_focus": 1, "same_year": 1}
    a = next(row for row in connections["shared_authors"] if row["name"] == "A")
    assert a["id"] == "orcid:0000-0000-0000-0042" and a["identity_basis"] == "orcid"
    assert a["focus_years"] == [2021, 2025] and a["neighbor_years"] == [2023]
    transition = next(row for row in connections["transitions"] if row["name"] == "A")
    assert transition["from_year"] == 2021 and transition["to_year"] == 2023
    assert transition["evidence_ids"] == ["a-first", "b-first"]
    assert next(row for row in connections["shared_authors"] if row["name"] == "Same")["identity_basis"] == "name_estimate"


def test_ambiguous_name_alias_does_not_merge_two_explicit_author_ids():
    records = [paper("a", "topic-1", 2021, [author("orcid:a", "Same", aliases=["name:shared"])]),
               paper("b", "topic-2", 2022, [author("orcid:b", "Same", aliases=["name:shared"])])]
    report = build_field_report(result(records), "topic-1", "topic-2")
    assert report["connections"]["shared_authors_total"] == 0
    assert any("複数の明示ID" in text for text in report["connections"]["basis_notes"])


def test_name_alias_link_to_known_author_stays_explicitly_estimated():
    records = [paper("a", "topic-1", 2021, [author("orcid:a", "A", aliases=["name:a"])]),
               paper("b", "topic-2", 2022, [author("name:a", "A")])]
    shared = build_field_report(result(records), "topic-1", "topic-2")["connections"]["shared_authors"]
    assert len(shared) == 1 and shared[0]["identity_basis"] == "name_alias_estimate"


def test_citations_require_observed_identifiers_and_preserve_direction():
    records = [paper("a", "topic-1", 2025, doi="10.1234/A", citations=100,
                     references=[{"doi": "https://doi.org/10.1234/B"}, {"id": "pmid:42"}, {"id": "unknown"}], references_status="provided"),
               paper("b", "topic-2", 2023, doi="10.1234/b", aliases={"ids": ["pmid:42"]}, citations=500),
               paper("c", "topic-2", 2024, references=[], references_status="provided")]
    connections = build_field_report(result(records), "topic-1", "topic-2")["connections"]
    assert connections["citation_links"] == [{"source_id": "a", "target_id": "b", "source_topic_id": "topic-1", "target_topic_id": "topic-2"}]
    coverage = connections["citation_coverage"]
    assert coverage["available"] is True
    assert coverage["papers_with_references"] == 2
    assert coverage["coverage_pct"] == 66.67
    assert coverage["references_total"] == 3
    assert coverage["matched_references"] == 2


def test_cumulative_citation_counts_never_become_citation_edges():
    value = result()
    for record in value["papers"]:
        record["citations"] = 999
    connections = build_field_report(value, "topic-1", "topic-2")["connections"]
    assert connections["citation_links"] == []
    assert connections["citation_coverage"]["available"] is False
    assert connections["citation_coverage"]["reason"] == "references_not_provided"


def test_duplicate_reference_identifier_is_not_guessed():
    records = [paper("a", "topic-1", 2025, references=[{"doi": "10.1234/duplicate"}]),
               paper("b", "topic-2", 2023, doi="10.1234/duplicate"),
               paper("c", "topic-2", 2024, doi="10.1234/duplicate")]
    connections = build_field_report(result(records), "topic-1", "topic-2")["connections"]
    assert connections["citation_links"] == []
    assert connections["citation_coverage"]["ambiguous_references"] == 1


def test_institutions_count_papers_once_and_do_not_infer_personal_affiliation():
    records = [paper("a", "topic-1", 2021, [author(affiliations=["University A, Tokyo"])] ,
                     affiliations=["University A, Tokyo", "University A, Tokyo"]),
               paper("missing", "topic-1", 2022),
               paper("b", "topic-2", 2023, affiliations=["university a, tokyo", "Institute B"])]
    institutions = build_field_report(result(records), "topic-1", "topic-2")["institutions"]
    assert institutions["focus_coverage_pct"] == 50
    assert institutions["neighbor_coverage_pct"] == 100
    university = next(row for row in institutions["rows"] if row["name"].casefold().startswith("university"))
    assert university["focus_count"] == university["neighbor_count"] == 1
    assert university["name"] == "University A, Tokyo"
    assert any("論文単位" in text for text in institutions["notes"])


def test_method_detection_counts_abstract_mentions_and_keeps_negation_evidence():
    records = [paper("a", "topic-1", 2021, abstract="We did not use finite element analysis. Finite element results from prior work were discussed."),
               paper("title-only", "topic-1", 2022, title="Finite element analysis", abstract=""),
               paper("b", "topic-2", 2023, abstract="Tensile tests and finite element analysis were compared.")]
    methods = build_field_report(result(records), "topic-1", "topic-2")["methods"]
    fem = next(row for row in methods["rows"] if row["id"] == "fem")
    assert fem["focus_count"] == fem["neighbor_count"] == 1
    assert "did not use" in fem["mentions"][0]["snippet"]
    assert {row["paper_id"] for row in fem["mentions"]} == {"a", "b"}
    assert methods["focus_abstract_coverage_pct"] == 50
    assert any("実験で使用" in text for text in methods["notes"])


@pytest.mark.parametrize("method, phrase", [
    ("pcr", "quantitative PCR"), ("rna_seq", "RNA sequencing"), ("crispr", "CRISPR-Cas9"),
    ("rct", "randomized controlled trial"), ("review", "systematic review and meta-analysis"),
    ("interview_survey", "structured interviews"), ("regression", "logistic regression"),
    ("monte_carlo", "Monte Carlo sampling"),
])
def test_method_vocabulary_includes_non_materials_fields(method, phrase):
    report = build_field_report(result([paper("a", "topic-1", 2021, abstract=phrase),
                                        paper("b", "topic-2", 2022)]), "topic-1", "topic-2")
    row = next(item for item in report["methods"]["rows"] if item["id"] == method)
    assert row["focus_count"] == 1 and row["neighbor_count"] == 0
    assert row["mentions"][0]["snippet"] == phrase


def test_overview_auto_selects_positive_neighbor_and_reuses_bounded_forecast():
    report = build_field_report(result(), "topic-1")
    assert report["neighbor"]["id"] == "topic-2"
    assert report["scope"]["neighbor_auto_selected"] is True
    assert report["neighbors"] == report["focus"]["neighbors"]
    assert sum(row["neighbor_count"] for row in report["annual"]) == 1
    assert [row["year"] for row in report["focus"]["forecast"]] == [2026, 2028]
    assert report["focus"]["backtest"]["folds"] == 2
    assert any("最大3年" in row["text"] for row in report["observations"])


def test_overview_without_positive_similarity_does_not_invent_a_neighbor():
    value = result()
    value["topics"][1]["keywords"] = ["unrelated"]
    report = build_field_report(value, "topic-1")
    assert report["neighbor"] is None
    assert all(row["neighbor_count"] is None for row in report["annual"])
    assert report["connections"]["transitions"] == []


def test_explicit_neighbor_observation_describes_that_field_even_if_not_most_similar():
    report = build_field_report(result(), "topic-1", "topic-3")
    observation = next(row for row in report["observations"] if row["section"] == "neighbors")
    assert "Solar energy" in observation["text"]
    assert "Additive manufacturing" not in observation["text"]


def test_export_rows_are_complete_even_when_gui_lists_are_capped():
    records = []
    for index in range(85):
        identity = author(f"orcid:{index}", f"Author {index}")
        records += [paper(f"a-{index}", "topic-1", 2021, [identity], affiliations=[f"University {index}"]),
                    paper(f"b-{index}", "topic-2", 2023, [identity])]
    report = build_field_report(result(records), "topic-1", "topic-2")
    assert len(report["connections"]["shared_authors"]) == 80
    assert report["connections"]["shared_authors_total"] == 85
    assert len(report["export_data"]["shared_authors"]) == 85
    assert len(report["export_data"]["transitions"]) == 85
    assert len(report["institutions"]["rows"]) == 40
    assert len(report["export_data"]["institutions"]) == report["institutions"]["total_rows"] == 85


def test_unclassified_and_same_field_comparisons_are_rejected():
    with pytest.raises(ValueError, match="分類済み"):
        build_field_report(result(), "topic-4")
    with pytest.raises(ValueError, match="異なる"):
        build_field_report(result(), "topic-1", "topic-1")
    with pytest.raises(ValueError, match="異なる"):
        build_field_report(result(), "topic-1", "topic-4")


def test_analyze_persists_nmf_geometry_and_metadata_without_mutating_input():
    records = [paper(f"a-{i}", "unused", 2021 + i % 5, [author(affiliations=["University A"])],
                     title="Quantum qubit computing correction", abstract="Quantum qubit computing correction",
                     affiliations=["University A"], references=[{"doi": "10.1234/target"}], references_status="provided") for i in range(4)]
    records += [paper(f"b-{i}", "unused", 2021 + i % 5, title="Solar photovoltaic panel energy",
                      abstract="Solar photovoltaic panel energy") for i in range(4)]
    before = copy.deepcopy(records)
    analyzed = analyze(records, {"start_year": 2021, "end_year": 2025, "n_topics": 2, "topic_model": "nmf"})
    assert records == before
    assert analyzed["field_geometry"]["method"] == "nmf_centroid_cosine"
    assert analyzed["field_geometry"]["paper_count"] == 8
    topic_ids = {topic["id"] for topic in analyzed["topics"] if not topic.get("is_outlier")}
    assert len(analyzed["field_geometry"]["similarities"]) == len(topic_ids) * (len(topic_ids) - 1) // 2
    for record in analyzed["papers"]:
        assert set(record["topic_weights"]) == topic_ids
    first = analyzed["papers"][0]
    assert first["affiliations"] == ["University A"]
    assert first["references"] == [{"doi": "10.1234/target"}]
    assert first["references_status"] == "provided"
    json.dumps(build_field_report(analyzed, "topic-1"), allow_nan=False)
