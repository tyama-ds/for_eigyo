"""Scopus-sized batches, accumulation boundaries, and reimport integrity."""

import csv
import io
from copy import deepcopy

import pytest

from app.ingest import parse_scopus_csv
from app.limits import MAX_DATASET_PAPERS, MAX_IMPORT_ROWS
from app.merge import merge_papers


def export(rows, headers=("EID", "Title", "Year", "DOI")):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def test_actual_20000_row_scopus_batch_is_accepted_without_truncation():
    content = export((f"2-s2.0-{index}", f"Article {index}", 2000 + index % 26, f"10.1234/p{index}")
                     for index in range(20_000))
    papers, report = parse_scopus_csv(content)
    assert MAX_IMPORT_ROWS == 20_000 and MAX_DATASET_PAPERS is None
    assert len(papers) == report["imported_count"] == 20_000
    assert papers[-1]["id"] == "2-s2.0-19999"
    assert report["invalid_rows"] == report["duplicates_removed"] == 0
    assert {paper["year"] for paper in papers} == set(range(2000, 2026))


def test_20001_rows_reject_even_when_the_extra_rows_are_duplicates():
    content = export(("2-s2.0-1", "Repeated article", 2024, "10.1234/one") for _ in range(20_001))
    with pytest.raises(ValueError, match="20,000"):
        parse_scopus_csv(content)


def test_batch_limit_counts_malformed_rows_and_not_physical_abstract_lines(monkeypatch):
    monkeypatch.setattr("app.ingest.MAX_IMPORT_ROWS", 2)
    multiline = "line\n" * 10
    papers, _ = parse_scopus_csv(export([("one", 2024, multiline), ("two", 2025, multiline)],
                                       ("Title", "Year", "Abstract")))
    assert len(papers) == 2 and papers[0]["abstract"] == multiline.strip()
    with pytest.raises(ValueError, match="2 行"):
        parse_scopus_csv(b"Title,Year\nValid,2024\nBroken,2024,extra\nAnother,2024\n")


def test_long_abstract_and_reference_fields_are_read_in_full():
    abstract = "Quoted, measured result; with Unicode 日本語.\n" * 4_000
    references = "Long bibliographic prose without an identifier. " * 4_000 + "DOI:10.1234/source"
    papers, report = parse_scopus_csv(export([("Long article", 2024, abstract, references)],
        ("Title", "Year", "Abstract", "References")))
    assert papers[0]["abstract"] == abstract.strip()
    assert papers[0]["references"] == [{"doi": "10.1234/source"}]
    assert report["invalid_rows"] == 0


def test_csv_title_cannot_join_different_dois_and_missing_doi_is_ambiguous():
    papers, report = parse_scopus_csv(export([
        ("a", "Same title", 2024, "10.1234/a"),
        ("b", "Same title", 2024, "10.1234/b"),
        ("c", "Same title", 2024, ""),
    ]))
    assert len(papers) == 3 and report["duplicates_removed"] == 0
    assert any("DOI" in warning for warning in report["warnings"])


def test_csv_missing_doi_bridge_does_not_merge_conflicting_explicit_doi():
    papers, _ = parse_scopus_csv(export([
        ("a", "Original A", 2024, "10.1234/a"),
        ("b", "Original B", 2024, "10.1234/b"),
        ("a", "Original B", 2024, ""),
    ]))
    assert len(papers) == 2
    assert {paper["doi"] for paper in papers} == {"10.1234/a", "10.1234/b"}


def test_csv_explicit_eid_bridge_retains_doi_aliases_and_reports_conflict():
    papers, report = parse_scopus_csv(export([
        ("same-id", "First", 2024, "10.1234/a"),
        ("same-id", "Second", 2024, "10.1234/b"),
    ]))
    assert len(papers) == 1 and papers[0]["doi"] == "10.1234/a"
    assert papers[0]["aliases"]["dois"] == ["10.1234/a", "10.1234/b"]
    assert any("共通 EID" in warning for warning in report["warnings"])


def test_ten_batches_reimport_boundary_citations_aliases_and_parent_immutability(monkeypatch):
    # Use the real ten-step workflow with a small injected corpus limit.
    monkeypatch.setattr("app.merge.MAX_DATASET_PAPERS", 20)
    accumulated = []
    first_batch = None
    for batch in range(10):
        incoming, _ = parse_scopus_csv(export([
            (f"2-s2.0-{index}", f"Article {index}", 2000 + index, f"10.1234/p{index}", index,
             "10.1234/reference", "University A")
            for index in range(batch * 2, batch * 2 + 2)
        ], ("EID", "Title", "Year", "DOI", "Cited by", "References", "Affiliations")))
        if first_batch is None:
            first_batch = deepcopy(incoming)
        before = deepcopy(accumulated)
        accumulated, report = merge_papers(accumulated, incoming)
        assert report["added_count"] == 2 and report["imported_count"] == (batch + 1) * 2
        assert all(p["id"] == old["id"] for p, old in zip(accumulated, before))
        assert [p["citations"] for p in accumulated] == list(range((batch + 1) * 2))
    parent = deepcopy(accumulated)
    again, report = merge_papers(accumulated, first_batch)
    assert again == parent and accumulated == parent
    assert report["added_count"] == 0 and report["matched_incoming_count"] == 2
    assert all(p["references"] == [{"doi": "10.1234/reference"}] for p in again)
    assert all(p["affiliations"] == ["University A"] for p in again)
    again[0]["aliases"]["eids"].append("edited")
    again[0]["citation_snapshots"][0]["count"] = 999
    assert accumulated == parent
    one_more, _ = parse_scopus_csv(export([("2-s2.0-21", "Extra paper", 2024, "10.1234/extra")]))
    with pytest.raises(ValueError, match="20 件"):
        merge_papers(accumulated, one_more)
    assert accumulated == parent


def test_unknown_nested_source_metadata_remains_independent_after_merge():
    incoming, _ = parse_scopus_csv(export([("id", "Original", 2024, "10.1234/a")]))
    incoming[0]["custom_source_data"] = {"raw": [{"text": "keep"}]}
    original = deepcopy(incoming)
    merged, _ = merge_papers([], incoming)
    merged, _ = merge_papers(merged, incoming)
    merged[0]["custom_source_data"]["raw"][0]["text"] = "changed"
    assert incoming == original
