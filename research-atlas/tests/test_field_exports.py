"""CSV provenance preserves the facts needed to audit paper-level comparisons."""

from copy import deepcopy
import csv
import io
import json

from app.field_exports import papers_csv


def fixture():
    paper = {"id": "europepmc:MED:123", "topic_id": "focus", "title": "Auditable steel paper",
             "year": 2025, "publication_date": "2025-03", "date_precision": "month",
             "date_source": "crossref:published", "abstract": "Complete abstract,\nwith a second line.",
             "authors": [{"id": "orcid:0000-0000-0000-0001", "name": "Jane A",
                          "aliases": ["scopus:123", "name:jane-a"], "affiliations": ["Institute A, Tokyo"]}],
             "affiliations": ["Institute A, Tokyo"], "keywords": ["steel"], "doi": "10.1234/steel",
             "citations": None, "citation_source": "crossref", "citation_history": {"2025": 0},
             "citation_snapshots": [{"provider": "europepmc", "count": 4, "retrieved_at": "2026-09-12T00:00:00Z"}],
             "citation_history_snapshots": [{"provider": "crossref", "history": {"2025": 0}, "retrieved_at": "2026-09-12T00:00:00Z"}],
             "aliases": {"ids": ["pmid:123"], "eids": ["2-s2.0-123"], "dois": ["10.1234/steel"]},
             "references": [{"id": "pmid:456", "doi": "10.1234/referenced"}], "references_status": "provided",
             "topic_weights": {"focus": .6, "neighbor": .4}, "providers": ["crossref", "europepmc"],
             "source": "Journal of Steel", "retrieved_at": "2026-09-12T00:00:00Z",
             "external_url": "https://doi.org/10.1234/steel", "provenance": "public-api:crossref:v1",
             "provenances": ["public-api:crossref:v1", "public-api:europepmc:v1"], "is_outlier": False,
             "future_metadata": {"source_precision": None, "warnings": []}}
    report = {"id": "report", "focus": {"id": "focus"}, "neighbor": {"id": "neighbor"}}
    result = {"id": "analysis", "topics": [{"id": "focus", "label": "Steel"}, {"id": "neighbor", "label": "Welding"}],
              "papers": [paper]}
    return report, result


def rows(value):
    return list(csv.DictReader(io.StringIO(value.lstrip("\ufeff"))))


def test_papers_csv_preserves_dates_citation_missingness_aliases_and_all_extra_metadata():
    report, result = fixture()
    before = deepcopy(result)
    content = papers_csv(report, result)
    row = rows(content)[0]
    original = result["papers"][0]
    assert content.startswith("\ufeff") and result == before
    assert row["Date precision"] == "month" and row["Date source"] == "crossref:published"
    assert row["Source title"] == "Journal of Steel" and row["Retrieved at"] == original["retrieved_at"]
    assert row["Cumulative citations"] == ""
    assert json.loads(row["Citation history (JSON)"]) == {"2025": 0}
    assert "2024" not in json.loads(row["Citation history (JSON)"])
    for column, field in [("Citation snapshots (JSON)", "citation_snapshots"),
                          ("Citation history snapshots (JSON)", "citation_history_snapshots"),
                          ("Aliases (JSON)", "aliases"), ("Provenances (JSON)", "provenances"),
                          ("References (JSON)", "references")]:
        assert json.loads(row[column]) == original[field]
    assert json.loads(row["Authors (JSON)"]) == original["authors"]
    assert row["Provenance"] == original["provenance"]
    assert json.loads(row["Is outlier (JSON)"]) is False
    extra = json.loads(row["Additional paper metadata (JSON)"])
    assert extra["future_metadata"] == original["future_metadata"]
    assert extra["date_precision"] == "month" and extra["citation_history"] == {"2025": 0}
    assert row["Abstract"] == original["abstract"]


def test_new_metadata_columns_are_appended_and_old_results_keep_unknowns():
    report, result = fixture()
    result["papers"] = [{"id": "legacy", "topic_id": "focus", "title": "Legacy", "year": 2021}]
    row = rows(papers_csv(report, result))[0]
    assert list(row)[:20] == ["Report ID", "Analysis ID", "Paper ID", "Topic ID", "Topic", "Title", "Year",
        "Publication date", "Abstract", "Authors (JSON)", "Affiliations (JSON)", "Keywords (JSON)",
        "DOI", "Cumulative citations", "Citation source", "References (JSON)", "References status",
        "Topic weights (JSON)", "Providers (JSON)", "External URL"]
    assert row["Date precision"] == "" and row["Date source"] == ""
    assert json.loads(row["Citation history (JSON)"]) is None
    assert json.loads(row["Citation snapshots (JSON)"]) is None
    assert json.loads(row["Aliases (JSON)"]) is None
    assert json.loads(row["Additional paper metadata (JSON)"]) == {}


def test_added_scalar_metadata_cannot_be_executed_as_spreadsheet_formulas():
    report, result = fixture()
    paper = result["papers"][0]
    paper.update(date_source="=HYPERLINK(unsafe)", source="+external", provenance="@unsafe")
    paper["aliases"]["ids"].append("=formula-like-id")
    row = rows(papers_csv(report, result))[0]
    assert row["Date source"].startswith("'=") and row["Source title"].startswith("'+")
    assert row["Provenance"].startswith("'@")
    assert json.loads(row["Aliases (JSON)"])["ids"][-1] == "=formula-like-id"
    assert json.loads(row["Additional paper metadata (JSON)"])["date_source"] == "=HYPERLINK(unsafe)"
