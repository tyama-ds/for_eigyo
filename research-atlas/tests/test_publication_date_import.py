"""CSV links and date-enrichment provenance survive import and analysis."""
import copy
import csv
import io

import pytest

from app import storage
from app.analytics import _normalize_papers
from app.ingest import parse_scopus_csv
from app.main import _import_csv_dataset
from app.merge import merge_papers


def csv_content(rows, link_header="Link"):
    content = io.StringIO(newline="")
    writer = csv.writer(content)
    writer.writerow(["Title", "Year", "DOI", link_header, "EID", "Abstract"])
    writer.writerows(rows)
    return content.getvalue().encode("utf-8-sig")


def import_paper(tmp_path, monkeypatch, doi, link):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    created = _import_csv_dataset(csv_content([
        ["Strength study", 2024, doi, link, "2-s2.0-123", "Measured microstructure."]
    ]), "scopus_csv", "scopus.csv")
    return storage.read("datasets", created["dataset"]["id"])["papers"][0]


def test_doi_import_preserves_original_scopus_link(tmp_path, monkeypatch):
    link = "https://www.scopus.com/record/display.uri?eid=2-s2.0-123&origin=resultslist"
    paper = import_paper(tmp_path, monkeypatch, "https://doi.org/10.1234/ABC", link)
    assert paper["source_link"] == link
    assert paper["external_url"] == "https://doi.org/10.1234/abc"
    assert paper["doi"] == "10.1234/abc"


@pytest.mark.parametrize("header", ["Link", "URL", "Document URL", "External URL", "source_link", "文献リンク"])
def test_csv_link_aliases_keep_source_without_doi(header):
    link = "https://publisher.example/articles/2024/123"
    papers, report = parse_scopus_csv(csv_content([
        ["Source without DOI", 2024, "", link, "p1", "Results."]
    ], link_header=header))
    assert papers[0]["source_link"] == papers[0]["external_url"] == link
    assert papers[0]["doi"] == ""
    assert papers[0]["id"] == "p1"
    assert report["invalid_rows"] == 0


def test_import_keeps_clickable_source_link_without_doi(tmp_path, monkeypatch):
    link = "https://publisher.example/article?id=123"
    paper = import_paper(tmp_path, monkeypatch, "", link)
    assert paper["source_link"] == paper["external_url"] == link


@pytest.mark.parametrize("link", [
    "javascript:alert(1)", "data:text/html,test", "file:///C:/secret.txt",
    "//publisher.example/article", "https://user:password@publisher.example/article",
    "https://publisher.example:bad/article", "https://publisher.example:0/article",
    "https://publisher.example/with space", "https://publisher.example/line\nbreak",
    "https://publisher.example\\@other.example/article", "https://[invalid]/paper",
])
def test_non_web_or_ambiguous_link_remains_inactive_provenance(tmp_path, monkeypatch, link):
    paper = import_paper(tmp_path, monkeypatch, "", link)
    assert paper["source_link"] == link
    assert not paper.get("external_url")


def test_repeated_csv_paper_can_fill_missing_original_link():
    link = "https://www.scopus.com/record/display.uri?eid=2-s2.0-123"
    papers, report = parse_scopus_csv(csv_content([
        ["Same paper", 2024, "10.1234/abc", "", "p1", "Results."],
        ["Same paper", 2024, "10.1234/abc", link, "p1", "Results."],
    ]))
    assert len(papers) == 1
    assert papers[0]["source_link"] == papers[0]["external_url"] == link


def test_analysis_keeps_independent_enrichment_audit_without_mutation():
    papers, _ = parse_scopus_csv(csv_content([
        ["Dated paper", 2024, "10.1234/abc", "https://publisher.example/paper", "p1", "Results."]
    ]))
    papers[0].update(publication_date="2024-05-16", date_precision="day", date_source="crossref:published-print",
        date_enrichment={"job_id": "a" * 32, "observation_id": 1, "provider": "crossref",
                         "kind": "published-print", "original": {"year": 2024, "publication_date": ""}})
    before = copy.deepcopy(papers)
    output = _normalize_papers(papers, 2020, 2025, [])
    assert output[0]["source_link"] == papers[0]["source_link"]
    assert output[0]["date_enrichment"] == papers[0]["date_enrichment"]
    assert output[0]["publication_date"] == "2024-05-16"
    output[0]["date_enrichment"]["original"]["year"] = 2000
    assert papers == before


def test_analysis_ignores_malformed_date_enrichment_audit():
    paper = {"id": "p1", "title": "Study", "year": 2024, "date_enrichment": "unstructured text"}
    assert "date_enrichment" not in _normalize_papers([paper], 2020, 2025, [])[0]


@pytest.mark.parametrize("existing_date", ["", "2024-05-16", "2024-03"])
def test_merge_enrichment_audit_follows_only_adopted_date(existing_date):
    base = {"id": "p1", "title": "Study", "year": 2024, "doi": "10.1234/abc",
            "publication_date": existing_date,
            "date_precision": "year" if not existing_date else "day" if len(existing_date) == 10 else "month",
            "date_source": "csv:Date"}
    if existing_date:
        base["date_enrichment"] = {"job_id": "old", "original": {"year": 2024}}
    incoming = {**base, "id": "p2", "source_link": "https://publisher.example/paper",
                "publication_date": "2024-05-16", "date_precision": "day", "date_source": "crossref:published-print",
                "date_enrichment": {"job_id": "new", "original": {"year": 2024, "publication_date": ""}}}
    originals = copy.deepcopy((base, incoming))
    merged, _ = merge_papers([base], [incoming])
    assert merged[0]["source_link"] == incoming["source_link"]
    assert merged[0]["publication_date"] == (existing_date or "2024-05-16")
    assert merged[0]["date_enrichment"]["job_id"] == ("old" if existing_date else "new")
    merged[0]["date_enrichment"]["original"]["year"] = 2000
    assert (base, incoming) == originals


def test_merge_rejected_cross_year_date_does_not_copy_enrichment_audit():
    base = {"id": "p1", "title": "Study", "year": 2024, "doi": "10.1234/abc", "date_precision": "year"}
    incoming = {**base, "id": "p2", "year": 2025, "publication_date": "2025-01-03", "date_precision": "day",
                "date_source": "crossref:published-print", "date_enrichment": {"job_id": "new"}}
    merged, report = merge_papers([base], [incoming])
    assert merged[0]["year"] == 2024
    assert merged[0]["publication_date"] == ""
    assert "date_enrichment" not in merged[0]
    assert report["date_conflicts"] == 1
