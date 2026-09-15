from copy import deepcopy
import json

import pytest

from app.ingest import SYNTHETIC_PROVENANCE
from app.merge import merge_papers


def test_merge_fills_same_year_missing_publication_month_and_preserves_base_month():
    first = paper("a", publication_date="", date_precision="year", date_source="csv:Year")
    second = paper("b", publication_date="2024-03-12", date_precision="day", date_source="crossref:published")
    result, report = merge_papers([first], [second])
    assert result[0]["publication_date"] == "2024-03-12"
    assert result[0]["date_source"] == "crossref:published"
    assert report["date_usable_count"] == 1 and report["date_pipeline_version"] == 2
    first.update(publication_date="2024-03", date_precision="month")
    result, _ = merge_papers([first], [second])
    assert result[0]["publication_date"] == "2024-03" and result[0]["date_precision"] == "month"


def test_merge_date_conflicts_keep_existing_date_and_report_disagreement():
    first = paper("a", publication_date="2024-03", date_precision="month", date_source="csv:Date")
    second = paper("b", publication_date="2024-04-02", date_precision="day", date_source="crossref:published")
    result, report = merge_papers([first], [second])
    assert result[0]["publication_date"] == "2024-03"
    assert report["date_conflicts"] == 1
    result, report = merge_papers([paper("a", doi="10.1234/a")], [paper("b", year=2025, doi="10.1234/a", publication_date="2025-03", date_precision="month")])
    assert result[0]["year"] == 2024 and result[0]["publication_date"] == ""
    assert report["date_conflicts"] == 1


def test_merge_invalid_date_keeps_annual_record_and_does_not_mutate_inputs():
    first = paper("a", publication_date="2023-03-01", date_precision="day", date_source="legacy")
    before = deepcopy(first)
    result, report = merge_papers([first], [])
    assert first == before
    assert result[0]["year"] == 2024 and result[0]["publication_date"] == "" and result[0]["date_precision"] == "year"
    assert report["date_invalid_records"] == 1


def paper(identifier, title="Novel battery interfaces", **changes):
    value = {"id": identifier, "title": title, "year": 2024, "abstract": "",
             "authors": [], "keywords": [], "doi": "", "source": "Journal of Batteries",
             "citations": None, "citation_history": {}, "providers": ["crossref"],
             "citation_source": "crossref", "citation_snapshots": [], "retrieved_at": "2026-09-11T00:00:00Z",
             "external_url": ""}
    value.update(changes)
    return value


def test_doi_url_case_merge_preserves_existing_id_and_never_sums_or_maxes_citations():
    first = paper("existing", doi="https://doi.org/10.1234/ABC", citations=7)
    second = paper("incoming", doi="DOI:10.1234/abc", abstract="New abstract", citations=90,
                   providers=["europepmc"], citation_source="europepmc")
    result, report = merge_papers([first], [second])
    assert len(result) == 1
    assert result[0]["id"] == "existing"
    assert result[0]["doi"] == "10.1234/abc"
    assert result[0]["abstract"] == "New abstract"
    assert result[0]["citations"] == 7
    assert result[0]["citation_source"] == "crossref"
    assert {(row["provider"], row["count"]) for row in result[0]["citation_snapshots"]} == {("crossref", 7), ("europepmc", 90)}
    assert report["imported_count"] == 1
    assert report["duplicates_removed"] == report["merged_count"] == 1
    assert report["citation_count_conflicts"] == 1


def test_percent_encoded_doi_url_matches_plain_doi():
    result, _ = merge_papers([paper("a", doi="https://dx.doi.org/10.1234%2FABCD?tracking=x")],
                             [paper("b", title="A revised title", doi="10.1234/abcd")])
    assert len(result) == 1
    assert result[0]["doi"] == "10.1234/abcd"


def test_same_title_different_explicit_dois_remain_separate():
    result, report = merge_papers([paper("a", doi="10.1234/a")], [paper("b", doi="10.1234/b")])
    assert len(result) == 2
    assert report["duplicates_removed"] == 0
    assert report["identity_conflicts"] == 1
    assert report["warnings"]


def test_normalized_title_and_year_fill_missing_metadata():
    result, _ = merge_papers([paper("a", title="Novel-AI: Systems", source="", keywords=["AI"])],
                             [paper("b", title=" novel ai systems ", doi="10.1234/new",
                                    source="A Real Journal", keywords=["ai", "Robotics"], external_url="https://example.org/paper")])
    assert len(result) == 1
    assert result[0]["id"] == "a"
    assert result[0]["doi"] == "10.1234/new"
    assert result[0]["keywords"] == ["AI", "Robotics"]
    assert result[0]["source"] == "A Real Journal"
    assert result[0]["external_url"] == "https://example.org/paper"


def test_title_matches_require_same_publication_year():
    result, _ = merge_papers([paper("a", year=2023)], [paper("b", year=2024)])
    assert len(result) == 2


def test_eid_alias_match_preserves_all_ids_for_later_merges():
    first = paper("stable", title="Original title", aliases={"eids": ["2-s2.0-42"], "dois": []})
    second = paper("remote", title="Updated title", aliases={"eids": ["2-s2.0-42"], "dois": []})
    result, _ = merge_papers([first], [second])
    again, _ = merge_papers(result, [paper("remote", title="Updated again", abstract="Completeness")])
    assert len(again) == 1
    assert again[0]["id"] == "stable"
    assert set(again[0]["aliases"]["ids"]) == {"stable", "remote"}
    assert again[0]["abstract"] == "Completeness"


def test_older_existing_known_zero_citations_is_a_measurement():
    result, _ = merge_papers([paper("a", citations=0)], [paper("b", citations=99)])
    assert result[0]["citations"] == 0


def test_unknown_existing_citations_take_incoming_known_value():
    result, _ = merge_papers([paper("a")], [paper("b", citations=11)])
    assert result[0]["citations"] == 11


def test_same_source_annual_histories_fill_gaps_and_preserve_conflicting_existing_values():
    result, report = merge_papers([paper("a", citation_history={"2023": 2})],
                                  [paper("b", citation_history={"2023": 9, "2024": 4})])
    assert result[0]["citation_history"] == {"2023": 2, "2024": 4}
    assert report["citation_history_conflicts"] == 1
    assert {snapshot["history"]["2023"] for snapshot in result[0]["citation_history_snapshots"] if "2023" in snapshot["history"]} == {2, 9}


def test_different_sources_never_mix_annual_histories():
    result, report = merge_papers([paper("a", citations=30, citation_history={"2023": 2})],
                                  [paper("b", citations=80, citation_history={"2024": 4},
                                         providers=["europepmc"], citation_source="europepmc")])
    assert result[0]["citation_history"] == {"2023": 2}
    assert result[0]["citations"] == 30
    assert result[0]["citation_source"] == "crossref"
    assert {row["provider"] for row in result[0]["citation_history_snapshots"]} == {"crossref", "europepmc"}
    assert report["citation_source_conflicts"] == 1


def test_entirely_unmeasured_existing_record_can_adopt_incoming_source_bundle():
    result, _ = merge_papers([paper("a", providers=["arxiv"], citation_source="arxiv")],
                             [paper("b", citations=7, citation_history={"2025": 7}, citation_source="europepmc", providers=["europepmc"])])
    assert result[0]["citation_source"] == "europepmc"
    assert result[0]["citations"] == 7
    assert result[0]["citation_history"] == {"2025": 7}
    assert result[0]["providers"] == ["arxiv", "europepmc"]


def test_empty_incoming_measurements_do_not_relabel_existing_citations():
    result, _ = merge_papers([paper("a", citations=11)], [paper("b", providers=["arxiv"], citation_source="arxiv")])
    assert result[0]["citation_source"] == "crossref"
    assert result[0]["citations"] == 11


def test_existing_annual_source_is_not_mislabeled_when_other_source_supplies_total():
    result, report = merge_papers([paper("a", citation_history={"2024": 4})],
                                  [paper("b", citations=30, providers=["europepmc"], citation_source="europepmc")])
    assert result[0]["citations"] is None
    assert result[0]["citation_source"] == "crossref"
    assert result[0]["citation_history"] == {"2024": 4}
    assert result[0]["citation_snapshots"][0]["count"] == 30
    assert report["citation_source_conflicts"] == 1


def test_unknown_sources_do_not_prove_comparable_annual_measurements():
    result, report = merge_papers([paper("a", providers=[], citation_source="", citation_history={"2023": 1})],
                                  [paper("b", providers=[], citation_source="", citation_history={"2024": 3})])
    assert result[0]["citation_history"] == {"2023": 1}
    assert report["citation_source_conflicts"] == 1


def test_journal_title_is_not_used_as_citation_source():
    result, _ = merge_papers([paper("a", providers=[], citation_source="", source="Crossref Journal", citations=2)], [])
    assert result[0]["citation_source"] == ""
    assert result[0]["citation_snapshots"][0]["provider"] == "unknown"


def test_author_name_order_matches_and_real_id_replaces_name_hash():
    result, _ = merge_papers([paper("a", authors=[{"id": "name:abc", "name": "Smith, Jane"}])],
                             [paper("b", authors=[{"id": "scopus:123456", "name": "Jane Smith"}])])
    assert len(result[0]["authors"]) == 1
    assert result[0]["authors"][0]["id"] == "scopus:123456"
    assert set(result[0]["authors"][0]["aliases"]) == {"name:abc", "scopus:123456"}


def test_same_name_different_persistent_ids_stay_separate_without_explicit_aliases():
    result, _ = merge_papers([paper("a", authors=[{"id": "scopus:123456", "name": "Jane Smith"}])],
                             [paper("b", authors=[{"id": "orcid:0000-0001-0002-0003", "name": "Smith, Jane"}])])
    assert result[0]["authors"][0]["id"] == "scopus:123456"
    assert len(result[0]["authors"]) == 2
    assert result[0]["authors"][0]["aliases"] == ["scopus:123456"]
    assert result[0]["authors"][1]["aliases"] == ["orcid:0000-0001-0002-0003"]


def test_explicit_persistent_aliases_can_link_different_ids_and_names():
    first = paper("a", authors=[{"id": "scopus:123456", "name": "Jane Smith", "affiliations": ["Institute A"]}])
    second = paper("b", authors=[{"id": "orcid:0000-0001-0002-0003", "name": "Jane Brown",
        "aliases": ["scopus:123456"], "affiliations": ["Institute B"]}])
    before = deepcopy([first, second])
    result, _ = merge_papers([first], [second])
    assert [first, second] == before
    assert len(result[0]["authors"]) == 1
    assert result[0]["authors"][0]["id"] == "scopus:123456"
    assert set(result[0]["authors"][0]["aliases"]) == {"scopus:123456", "orcid:0000-0001-0002-0003"}
    assert result[0]["authors"][0]["affiliations"] == ["Institute A", "Institute B"]


def test_same_name_explicit_coauthors_keep_separate_affiliations_and_weak_aliases():
    authors = [{"id": "scopus:101", "name": "Alex Sora", "aliases": ["name:shared"], "affiliations": ["Institute A"]},
               {"id": "scopus:202", "name": "Alex Sora", "aliases": ["name:shared"], "affiliations": ["Institute B"]}]
    first = paper("a", authors=authors)
    before = deepcopy(first)
    result, report = merge_papers([], [first])
    assert first == before
    assert [a["id"] for a in result[0]["authors"]] == ["scopus:101", "scopus:202"]
    assert [a["affiliations"] for a in result[0]["authors"]] == [["Institute A"], ["Institute B"]]
    assert "scopus:202" not in result[0]["authors"][0]["aliases"]
    assert any("同名" in warning for warning in report["warnings"])


@pytest.mark.parametrize("order", [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)])
def test_missing_id_name_fallback_does_not_choose_between_multiple_persistent_ids(order):
    authors = [{"id": "name:shared", "name": "Alex Sora", "affiliations": ["Unresolved institute"]},
               {"id": "scopus:101", "name": "Alex Sora", "affiliations": ["Institute A"]},
               {"id": "scopus:202", "name": "Alex Sora", "affiliations": ["Institute B"]}]
    result, _ = merge_papers([], [paper("a", authors=[authors[i] for i in order])])
    actual = {a["id"]: a for a in result[0]["authors"]}
    assert set(actual) == {"name:shared", "scopus:101", "scopus:202"}
    assert actual["scopus:101"]["affiliations"] == ["Institute A"]
    assert actual["scopus:202"]["affiliations"] == ["Institute B"]


def test_same_author_id_can_fill_placeholder_name_but_initials_do_not_guess_full_names():
    result, _ = merge_papers([paper("a", authors=[{"id": "scopus:123456", "name": "Author 123456"}])],
                             [paper("b", authors=[{"id": "scopus:123456", "name": "Jane Smith"}, {"id": "name:other", "name": "J Smith"}])])
    assert result[0]["authors"][0]["name"] == "Jane Smith"
    assert len(result[0]["authors"]) == 2


def test_missing_doi_record_cannot_bridge_distinct_dois_through_title():
    existing = [paper("a", doi="10.1234/a"), paper("b", doi="10.1234/b")]
    result, report = merge_papers(existing, [paper("ambiguous")])
    assert len(result) == 3
    assert report["ambiguous_matches"] == 1


def test_existing_id_without_doi_cannot_bridge_two_dois_through_title():
    existing = [paper("anchor", title="Unrelated initial title"), paper("a", doi="10.1234/a"), paper("b", doi="10.1234/b")]
    result, report = merge_papers(existing, [paper("anchor")])
    assert len(result) == 3
    assert report["ambiguous_matches"] == 1
    assert next(item for item in result if item["id"] == "anchor")["doi"] == ""


def test_explicit_identifier_bridge_is_reported_and_keeps_earliest_existing_id():
    existing = [paper("first", title="Title A", doi="10.1234/a"), paper("second", title="Title B", doi="10.1234/b")]
    bridge = paper("incoming", title="Title C", aliases={"eids": ["first", "second"], "dois": []})
    result, report = merge_papers(existing, [bridge])
    assert len(result) == 1
    assert result[0]["id"] == "first"
    assert set(result[0]["aliases"]["dois"]) == {"10.1234/a", "10.1234/b"}
    assert report["identity_conflicts"] >= 1
    assert report["duplicates_removed"] == 2


def test_alternative_titles_remain_available_for_future_merges():
    first, _ = merge_papers([paper("a", title="Title A", doi="10.1234/a")],
                            [paper("b", title="Title B", doi="10.1234/a")])
    result, _ = merge_papers(first, [paper("c", title="Title B", abstract="New abstract")])
    assert len(result) == 1
    assert result[0]["id"] == "a"
    assert result[0]["abstract"] == "New abstract"


def test_synthetic_provenance_survives_duplicate_merge():
    result, report = merge_papers([paper("a", provenance="source:real")],
                                  [paper("b", provenance=SYNTHETIC_PROVENANCE)])
    assert report["is_demo"] is True
    assert report["synthetic_count"] == 1
    assert result[0]["provenance"] == SYNTHETIC_PROVENANCE
    assert set(result[0]["provenances"]) == {"source:real", SYNTHETIC_PROVENANCE}


def test_real_synthetic_data_research_is_not_marked_as_demo():
    _, report = merge_papers([], [paper("a", title="Synthetic data research", abstract="A study of synthetic data.")])
    assert report["is_demo"] is False


def test_merging_is_immutable_and_idempotent_with_nested_provenance():
    existing = [paper("a", citations=3, citation_history={"2024": 2}, retrieved_at="",
                      authors=[{"id": "name:abc", "name": "Jane Smith"}])]
    incoming = [paper("b", citations=9, citation_history={"2025": 4}, providers=["europepmc"], citation_source="europepmc")]
    original_existing, original_incoming = deepcopy(existing), deepcopy(incoming)
    merged, _ = merge_papers(existing, incoming)
    again, _ = merge_papers(merged, [])
    assert merged == again
    assert existing == original_existing and incoming == original_incoming
    assert {row["retrieved_at"] for row in again[0]["citation_snapshots"] if row["provider"] == "crossref"} == {""}
    merged[0]["authors"][0]["aliases"].append("changed")
    merged[0]["citation_history"]["2024"] = 999
    assert existing == original_existing and incoming == original_incoming
    json.dumps(again, allow_nan=False)


def test_empty_inputs_and_added_count_have_defined_results():
    assert merge_papers([], [])[0] == []
    output, report = merge_papers([paper("a", title="A")], [paper("b", title="B"), paper("c", title="A")])
    assert len(output) == 2
    assert report["added_count"] == 1
    assert report["matched_incoming_count"] == 1


def test_output_limit_is_checked_after_deduplication(monkeypatch):
    monkeypatch.setattr("app.merge.MAX_DATASET_PAPERS", 3)
    values = [paper(str(index), title=f"Unique article {index}") for index in range(4)]
    with pytest.raises(ValueError, match="3 件"):
        merge_papers([], values)
    output, report = merge_papers(values[:3], [values[0]])
    assert len(output) == 3 and report["added_count"] == 0
    assert report["matched_incoming_count"] == 1


@pytest.mark.parametrize("existing,incoming", [(None, []), ([], ["bad"]), ([], [{"title": "No ID"}])])
def test_invalid_merge_inputs_raise_actionable_errors(existing, incoming):
    with pytest.raises(ValueError):
        merge_papers(existing, incoming)
