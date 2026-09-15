"""Real CSV parsing and mocked official-schema ingestion of supplied metadata."""

import copy
import csv
import io
import json

import httpx
import pytest

from app import sources
from app.bibliography import (attach_author_affiliations, normalize_affiliations,
                              normalize_references, normalize_reference_id)
from app.ingest import parse_scopus_csv
from app.merge import merge_papers


def csv_bytes(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def test_long_reference_list_deduplicates_and_preserves_input_order():
    references = [{"doi": f"10.1234/reference-{index}"} for index in range(2_000)]
    original = copy.deepcopy(references)
    assert normalize_references(references + references) == original
    assert references == original


def test_reference_index_does_not_join_conflicting_dois_or_discarded_ids():
    assert normalize_references([
        {"doi": "10.1234/a", "id": "pmid:1"},
        {"doi": "10.1234/b", "id": "pmid:1"},
        {"id": "pmid:1"},
    ]) == [{"doi": "10.1234/a", "id": "pmid:1"}, {"doi": "10.1234/b", "id": "pmid:1"}]
    assert normalize_references([
        {"doi": "10.1234/a", "id": "pmid:1"},
        {"doi": "10.1234/a", "id": "pmid:2"},
        {"id": "pmid:2"},
    ]) == [{"doi": "10.1234/a", "id": "pmid:1"}, {"id": "pmid:2"}]


def test_csv_preserves_institution_addresses_without_inventing_author_mapping():
    content = csv_bytes(["EID", "Title", "Year", "Authors", "Affilations", "Cited by"], [
        ["2-s2.0-123", "Battery interfaces", 2025, "Smith J.;Jones K.",
         "Institute A, Tokyo, Japan; University B, Osaka, Japan", 999]])
    papers, report = parse_scopus_csv(content)
    paper = papers[0]
    assert paper["affiliations"] == ["Institute A, Tokyo, Japan", "University B, Osaka, Japan"]
    assert all(not author.get("affiliations") for author in paper["authors"])
    assert paper["references"] == [] and paper["references_status"] == "not_provided"
    assert report["affiliation_papers"] == 1 and report["reference_metadata_papers"] == 0
    assert report["metadata_pipeline_version"] == 3


def test_scopus_named_author_affiliations_attach_only_to_the_named_author():
    content = csv_bytes(["Title", "Year", "Author full names", "Author(s) ID", "Authors with affiliations"], [
        ["Grain refinement", 2025, "Smith, Jane;Boreal, John", "10001;10002",
         "Smith, Jane, Institute A, Tokyo; Boreal, John, University B, Osaka"]])
    papers, _ = parse_scopus_csv(content)
    assert papers[0]["authors"] == [
        {"id": "scopus:10001", "name": "Smith, Jane", "affiliations": ["Institute A, Tokyo"]},
        {"id": "scopus:10002", "name": "Boreal, John", "affiliations": ["University B, Osaka"]}]
    assert papers[0]["affiliations"] == ["Institute A, Tokyo", "University B, Osaka"]


def test_positional_affiliation_list_does_not_count_as_explicit_correspondence():
    authors = [{"id": "one", "name": "Jane A"}, {"id": "two", "name": "John B"}]
    before = copy.deepcopy(authors)
    result, unmatched = attach_author_affiliations(authors, "Institute A, Tokyo;Institute B, Osaka")
    assert result == before == authors and unmatched == 2
    ambiguous, unmatched = attach_author_affiliations([
        {"id": "orcid:one", "name": "Jane A"}, {"id": "orcid:two", "name": "Jane A"}],
        {"Jane A": ["Institute A"]})
    assert all(not author.get("affiliations") for author in ambiguous) and unmatched == 1


def test_mismatched_explicit_affiliation_id_does_not_fall_back_to_same_name():
    authors = [{"id": "scopus:10001", "name": "Alice Ito", "aliases": ["orcid:known"]}]
    original = copy.deepcopy(authors)
    result, unmatched = attach_author_affiliations(authors, [{
        "id": "scopus:other", "name": "Alice Ito", "affiliations": ["Other institute"]}])
    assert result == authors == original and unmatched == 1
    result, unmatched = attach_author_affiliations(authors, [{
        "id": "orcid:known", "name": "Another supplied name", "affiliations": ["Verified institute"]}])
    assert result[0]["affiliations"] == ["Verified institute"] and unmatched == 0
    assert authors == original


def test_name_keyed_affiliation_shorthand_remains_supported_with_unique_name():
    authors = [{"id": "scopus:10001", "name": "Alice Ito"}]
    by_name, unmatched = attach_author_affiliations(authors, {"Alice Ito": ["Institute A, Tokyo"]})
    assert by_name[0]["affiliations"] == ["Institute A, Tokyo"] and unmatched == 0
    by_id, unmatched = attach_author_affiliations(authors, {"scopus:10001": ["Institute B, Osaka"]})
    assert by_id[0]["affiliations"] == ["Institute B, Osaka"] and unmatched == 0
    bad_id, unmatched = attach_author_affiliations(authors, {"scopus:other": ["Wrong institute"]})
    assert bad_id == authors and unmatched == 1


def test_json_author_mapping_and_reference_round_trip_in_csv():
    refs = [{"doi": "10.1234/target", "id": "2-s2.0-555"}, {"id": "arxiv:2501.12345"}]
    content = csv_bytes(["Title", "Year", "Author full names", "Author(s) ID", "所属", "Author affiliations", "References", "References status"], [
        ["Evidence fixture", 2025, "Smith, Jane;Boreal, John", "10001;10002", "共同研究所, 東京",
         json.dumps([{"id": "scopus:10002", "affiliations": ["University B, Osaka"]}]), json.dumps(refs), "provided"]])
    papers, report = parse_scopus_csv(content)
    assert papers[0]["references"] == refs
    assert not papers[0]["authors"][0].get("affiliations")
    assert papers[0]["authors"][1]["affiliations"] == ["University B, Osaka"]
    assert papers[0]["affiliations"] == ["共同研究所, 東京", "University B, Osaka"]
    assert report["reference_metadata_papers"] == report["reference_identifier_papers"] == 1


def test_reference_extraction_requires_real_identifiers_and_keeps_unknown_status():
    refs = normalize_references("Smith (2020), Unknown article; DOI:10.1234/ABC.; PMID: 123456; arXiv:2501.12345v2; EID:2-s2.0-900")
    assert {tuple(row.items()) for row in refs} == {
        (("doi", "10.1234/abc"),), (("id", "pmid:123456"),),
        (("id", "arxiv:2501.12345"),), (("id", "2-s2.0-900"),)}
    assert normalize_references([{"id": "ref1", "key": "10.1234/not-a-doi-field", "year": "2020"}, 250, None]) == []
    assert normalize_references([{"DOI": "https://doi.org/10.1234/ABC?tracking=1"}, {"doi": "10.1234/abc"}]) == [{"doi": "10.1234/abc"}]
    assert normalize_reference_id("2025") == ""
    assert normalize_reference_id("PMC123456") == "pmcid:PMC123456"


def test_csv_exported_empty_reference_array_preserves_unretrieved_state():
    papers, report = parse_scopus_csv(csv_bytes(["Title", "Year", "References", "References status"], [
        ["Unknown references", 2025, "[]", "not_provided"],
        ["Supplied empty list", 2025, "[]", "provided"]]))
    assert [paper["references_status"] for paper in papers] == ["not_provided", "provided"]
    assert report["reference_metadata_papers"] == 1


def test_duplicate_csv_rows_union_reference_and_explicit_author_affiliations():
    papers, report = parse_scopus_csv(csv_bytes(["EID", "Title", "Year", "Authors", "Author affiliations", "Reference DOIs"], [
        ["2-s2.0-1", "Steel grains", 2025, "Jane A", '{"Jane A":["Institute A, Tokyo"]}', "10.1234/first"],
        ["2-s2.0-1", "Steel grains", 2025, "Jane A", '{"Jane A":["Institute B, Osaka"]}', "10.1234/second"]]))
    assert len(papers) == 1 and report["duplicates_removed"] == 1
    assert papers[0]["authors"][0]["affiliations"] == ["Institute A, Tokyo", "Institute B, Osaka"]
    assert papers[0]["references"] == [{"doi": "10.1234/first"}, {"doi": "10.1234/second"}]


def test_affiliation_html_and_case_duplicates_are_cleaned_without_address_split():
    assert normalize_affiliations('["<i>Institute A</i>, Tokyo", "institute a, Tokyo"]') == ["Institute A, Tokyo"]
    assert normalize_affiliations("Institute B, Osaka<script>unsafe()</script>; N/A") == ["Institute B, Osaka"]


def base_paper(**changes):
    return {"id": "base", "title": "Grain refinement", "year": 2025, "abstract": "Steel grains",
            "doi": "10.1234/grain", "authors": [{"id": "orcid:one", "name": "Jane A"}],
            "keywords": [], "citations": 3, "citation_history": {}, "citation_source": "crossref", **changes}


def test_merge_unions_metadata_immutably_without_changing_citation_rules():
    first = base_paper(affiliations=["Institute A, Tokyo"], references=[{"doi": "10.1234/first"}],
                       authors=[{"id": "orcid:one", "name": "Jane A", "affiliations": ["Institute A, Tokyo"]}])
    second = base_paper(id="incoming", affiliations=["Institute B, Osaka"],
                        authors=[{"id": "orcid:one", "name": "Jane A", "affiliations": ["Institute B, Osaka"]},
                                 {"id": "orcid:two", "name": "John B"}],
                        references=[{"DOI": "10.1234/FIRST"}, {"id": "pmid:123456"}], citations=500,
                        citation_source="europepmc")
    before = copy.deepcopy([first, second])
    papers, report = merge_papers([first], [second])
    assert [first, second] == before
    result = papers[0]
    assert result["id"] == "base" and result["citations"] == 3
    assert result["affiliations"] == ["Institute A, Tokyo", "Institute B, Osaka"]
    assert result["authors"][0]["affiliations"] == result["affiliations"]
    assert not result["authors"][1].get("affiliations")
    assert result["references"] == [{"doi": "10.1234/first"}, {"id": "pmid:123456"}]
    assert result["references_status"] == "provided"
    assert report["reference_identifier_papers"] == 1
    legacy, _ = merge_papers([base_paper()], [])
    assert legacy[0]["references"] == [] and legacy[0]["references_status"] == "not_provided"


def mock_provider(monkeypatch, payload, xml=False):
    monkeypatch.setattr(sources, "_make_client", lambda *args: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=payload) if xml else httpx.Response(200, json=payload))))


def test_crossref_reference_ids_and_author_affiliations_are_used_only_if_supplied(monkeypatch):
    record = {"DOI": "10.1234/source", "title": ["Steel structure"], "published": {"date-parts": [[2025]]},
              "author": [{"given": "Jane", "family": "A", "affiliation": [{"name": "Institute A, Tokyo"}]},
                         {"given": "John", "family": "B"}], "is-referenced-by-count": 900,
              "reference": [{"key": "ref1", "DOI": "10.1234/Target"}, {"id": "local-ref-2"},
                            {"unstructured": "A study, DOI:10.2345/Actual"}]}
    mock_provider(monkeypatch, {"message": {"total-results": 1, "items": [record]}})
    papers, report = sources.discover("crossref", "steel", 2025, 2025, 1)
    paper = papers[0]
    assert paper["affiliations"] == ["Institute A, Tokyo"]
    assert paper["authors"][0]["affiliations"] == ["Institute A, Tokyo"]
    assert not paper["authors"][1].get("affiliations")
    assert paper["references"] == [{"doi": "10.1234/target"}, {"doi": "10.2345/actual"}]
    assert paper["citation_history"] == {} and report["metadata_pipeline_version"] == 3
    del record["reference"]
    papers, _ = sources.discover("crossref", "steel", 2025, 2025, 1)
    assert papers[0]["references"] == [] and papers[0]["references_status"] == "not_provided"


def test_europepmc_affiliation_details_preserve_mapping_and_reference_missingness(monkeypatch):
    record = {"id": "123456", "source": "MED", "pmid": "123456", "pmcid": "PMC987654", "title": "Organoid culture",
              "firstPublicationDate": "2025-03-01", "citedByCount": 1000, "hasReferences": "Y",
              "affiliation": "Paper contact institute, Paris",
              "authorList": {"author": [{"firstName": "Jane", "lastName": "A", "authorAffiliationDetailsList": {
                  "authorAffiliation": [{"affiliation": "Author institute, Tokyo"}]}},
                  {"firstName": "John", "lastName": "B"}]}}
    mock_provider(monkeypatch, {"hitCount": 1, "resultList": {"result": [record]}})
    papers, _ = sources.discover("europepmc", "organoid", 2025, 2025, 1)
    paper = papers[0]
    assert paper["affiliations"] == ["Paper contact institute, Paris", "Author institute, Tokyo"]
    assert paper["authors"][0]["affiliations"] == ["Author institute, Tokyo"]
    assert not paper["authors"][1].get("affiliations")
    assert paper["aliases"]["ids"] == ["pmid:123456", "pmcid:PMC987654"]
    assert paper["references"] == [] and paper["references_status"] == "not_provided"


def test_arxiv_author_affiliation_is_not_a_bibliography_reference(monkeypatch):
    monkeypatch.setattr(sources, "_ARXIV_LAST_REQUEST", None)
    payload = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
    <opensearch:totalResults>1</opensearch:totalResults><entry><id>https://arxiv.org/abs/2501.12345v1</id><title>Quantum sensor</title>
    <published>2025-01-01T00:00:00Z</published><summary>Quantum measurement</summary><author><name>Jane A</name>
    <arxiv:affiliation>Institute A, Tokyo</arxiv:affiliation></author><author><name>John B</name></author>
    <arxiv:journal_ref>Journal 2025, DOI:10.1234/PublishedVersion</arxiv:journal_ref></entry></feed>'''
    mock_provider(monkeypatch, payload, xml=True)
    papers, _ = sources.discover("arxiv", "quantum", 2025, 2025, 1)
    assert papers[0]["affiliations"] == ["Institute A, Tokyo"]
    assert papers[0]["authors"][0]["affiliations"] == ["Institute A, Tokyo"]
    assert not papers[0]["authors"][1].get("affiliations")
    assert papers[0]["references"] == [] and papers[0]["references_status"] == "not_provided"
