import copy
import csv
import io
from pathlib import Path

import pytest

from app.ingest import SYNTHETIC_PROVENANCE, attach_citation_history, demo_papers, parse_scopus_csv


def csv_bytes(headers, rows, delimiter=",", encoding="utf-8-sig"):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode(encoding)


@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
def test_scopus_quoted_multiline_abstract_and_author_alignment(delimiter):
    abstract = 'A measured result, with a "quoted" observation; and a second line.\n追加の説明。'
    content = csv_bytes(
        ["Authors", "Author full names", "Author(s) ID", "Title", "Year", "Cited by", "Abstract", "Author Keywords", "Index Keywords", "EID", "DOI", "Source title"],
        [["Smith, J.; Aster, N.", "Smith, Jane (123456789); Aster, Nora (987654321)", "123456789;987654321;", "A title; with comma, punctuation", "2024", "1,234", abstract, "battery; ion transport", "Battery; interface", "2-s2.0-123", "https://doi.org/10.1234/ABC", "Test Journal"]],
        delimiter=delimiter,
    )
    papers, meta = parse_scopus_csv(content)
    assert len(papers) == 1
    assert papers[0]["abstract"] == abstract
    assert papers[0]["authors"] == [{"id": "scopus:123456789", "name": "Smith, Jane"}, {"id": "scopus:987654321", "name": "Aster, Nora"}]
    assert papers[0]["keywords"] == ["battery", "ion transport", "interface"]
    assert papers[0]["citations"] == 1234
    assert papers[0]["doi"] == "10.1234/abc"
    assert meta["delimiter"] == delimiter
    assert meta["invalid_rows"] == 0


def test_cp932_japanese_headers_and_fullwidth_numbers():
    papers, meta = parse_scopus_csv(csv_bytes(
        ["論文名", "出版年", "著者", "著者ID", "抄録", "被引用回数", "キーワード"],
        [["次世代電池の研究", "２０２３", "青空 太郎;白雲 花子", "100001;100002", "固体電解質の評価", "０", "電池;固体電解質"]],
        encoding="cp932",
    ))
    assert meta["encoding"] == "cp932"
    assert papers[0]["year"] == 2023
    assert papers[0]["citations"] == 0
    assert [author["name"] for author in papers[0]["authors"]] == ["青空 太郎", "白雲 花子"]


def test_author_comma_is_not_blindly_split_and_old_scopus_style_supported():
    papers, _ = parse_scopus_csv(csv_bytes(
        ["Title", "Year", "Authors", "Author(s) ID"],
        [["Full name", 2024, "Smith, Jane", "123456"],
         ["Single initials", 2024, "Jones, A.", ""],
         ["Old style", 2024, "Smith J., Jones A.", "111111;222222"],
         ["Paired old style", 2024, "Smith, J., Jones, A.", "111111;222222"]],
    ))
    assert [a["name"] for a in papers[0]["authors"]] == ["Smith, Jane"]
    assert [a["name"] for a in papers[1]["authors"]] == ["Jones, A."]
    assert [a["id"] for a in papers[2]["authors"]] == ["scopus:111111", "scopus:222222"]
    assert [a["name"] for a in papers[3]["authors"]] == ["Smith, J.", "Jones, A."]


def test_misaligned_author_ids_are_not_assigned_by_position():
    papers, meta = parse_scopus_csv(csv_bytes(
        ["Title", "Year", "Authors", "Author(s) ID"],
        [["Alignment", 2023, "Aster, Nora; Boreal, Eli", "123456"]],
    ))
    assert all(author["id"].startswith("name:") for author in papers[0]["authors"])
    assert any("件数が異なる" in warning for warning in meta["warnings"])


def test_partial_scopus_full_names_do_not_drop_authors_or_guess_missing_affiliations():
    content = (Path(__file__).parent / "fixtures" / "scopus_partial_full_names.csv").read_bytes()
    papers, report = parse_scopus_csv(content)
    assert len(papers) == 2 and report["encoding"] == "utf-8-sig" and report["invalid_rows"] == 0
    assert papers[0]["authors"] == [
        {"id": "scopus:91001", "name": "K.V.N.S."},
        {"id": "scopus:91002", "name": "Lumen, Mina", "affiliations": ["Synthetic Aster University, Tokyo, Japan"]},
    ]
    assert papers[0]["affiliations"] == ["Synthetic Aster University, Tokyo, Japan"]
    assert papers[0]["source"] == "Synthetic Fixture Journal" and papers[0]["citations"] == 0
    assert [a["id"] for a in papers[1]["authors"]] == [f"scopus:9200{i}" for i in range(1, 7)]
    assert [a["name"] for a in papers[1]["authors"]] == ["Sora, Alex", "Y.", "Lumen, Mina", "Y.", "Nova, Rei", "Comet, Noa"]
    assert all(not a.get("affiliations") for a in papers[1]["authors"])
    assert papers[1]["citations"] is None and papers[1]["citation_history"] == {}
    assert report["is_demo"] and report["synthetic_count"] == 2
    assert sum("フルネーム列が不完全" in message for message in report["warnings"]) == 2


def test_partial_full_names_use_embedded_ids_instead_of_full_name_positions():
    papers, _ = parse_scopus_csv(csv_bytes(["Title", "Year", "Authors", "Author full names", "Author(s) ID"], [
        ["Explicit identity order", 2025, "Sora A.;Lumen M.;Nova R.",
         "Nova, Rei (91003);Sora, Alex (91001)", "91001;91002;91003"]]))
    assert papers[0]["authors"] == [{"id": "scopus:91001", "name": "Sora, Alex"},
                                   {"id": "scopus:91002", "name": "Lumen M."},
                                   {"id": "scopus:91003", "name": "Nova, Rei"}]


def test_partial_full_names_without_embedded_ids_keep_complete_abbreviated_author_list():
    papers, _ = parse_scopus_csv(csv_bytes(["Title", "Year", "Authors", "Author full names", "Author(s) ID"], [
        ["No explicit full-name matching", 2025, "Sora A.;Lumen M.;Nova R.",
         "Nova, Rei;Sora, Alex", "91001;91002;91003"]]))
    assert [a["name"] for a in papers[0]["authors"]] == ["Sora A.", "Lumen M.", "Nova R."]


@pytest.mark.parametrize("supplied,expected", [
    ("123456", "scopus:123456"), ("scopus:123456", "scopus:123456"),
    ("SCOPUS: 123456", "scopus:123456"),
    ("orcid:0000-0002-1825-0097", "orcid:0000-0002-1825-0097"),
    ("https://orcid.org/0000-0002-1825-0097", "orcid:0000-0002-1825-0097"),
    ("https://orcid.org/0000-0002-1694-233x/", "orcid:0000-0002-1694-233X"),
    ("name:provided-hash", "name:provided-hash"), ("custom:original-id", "custom:original-id"),
])
def test_provided_author_namespace_survives_csv_reimport(supplied, expected):
    papers, _ = parse_scopus_csv(csv_bytes(["Title", "Year", "Authors", "Author(s) ID"], [
        ["Identity round trip", 2025, "Alice Ito", supplied]]))
    assert papers[0]["authors"] == [{"id": expected, "name": "Alice Ito"}]
    again, _ = parse_scopus_csv(csv_bytes(["Title", "Year", "Authors", "Author(s) ID"], [
        ["Identity round trip", 2025, "Alice Ito", expected]]))
    assert again[0]["authors"] == papers[0]["authors"]


def test_provided_author_ids_without_names_normalize_without_double_prefix():
    papers, _ = parse_scopus_csv(csv_bytes(["Title", "Year", "Author(s) ID"], [[
        "IDs only", 2025, "123456;scopus:123456;https://orcid.org/0000-0002-1825-0097"]]))
    assert [a["id"] for a in papers[0]["authors"]] == ["scopus:123456", "orcid:0000-0002-1825-0097"]


def test_unknown_citation_count_remains_none_and_invalid_rows_are_reported():
    papers, meta = parse_scopus_csv(csv_bytes(
        ["Title", "Year", "Cited by"],
        [["Unknown", 2023, ""], ["Observed zero", 2024, 0], ["Negative", 2024, -1], ["Fractional", 2024, 1.5], ["Bad year", "unknown", 2], ["", 2024, 2]],
    ))
    assert [paper["citations"] for paper in papers] == [None, 0]
    assert meta["invalid_rows"] == 4
    assert {error["row"] for error in meta["row_errors"]} == {4, 5, 6, 7}
    assert any("不明" in warning for warning in meta["warnings"])


def test_malformed_column_count_is_reported_without_losing_valid_rows():
    papers, meta = parse_scopus_csv(b"Title,Year,Cited by\nGood,2024,3\nBroken,2023,2,extra\nGood two,2022,4\n")
    assert len(papers) == 2
    assert meta["invalid_rows"] == 1
    assert "列数" in meta["row_errors"][0]["reason"]


def test_unclosed_quoted_row_is_reported_after_valid_data():
    papers, meta = parse_scopus_csv(b'Title,Year,Abstract\nGood,2024,OK\nBroken,2024,"unclosed\n')
    assert len(papers) == 1
    assert meta["invalid_rows"] == 1
    assert "引用符" in meta["row_errors"][0]["reason"]


@pytest.mark.parametrize("content", [b"", b"Title,Year\n", b"Other,Columns\n1,2\n", b"Title,Year\nBad,0\n", b"\x81\x00\x00"])
def test_unusable_input_raises_readable_error(content):
    with pytest.raises(ValueError) as error:
        parse_scopus_csv(content)
    assert any("\u3000" <= character <= "\u9fff" for character in str(error.value))


def test_transitive_dedup_keeps_aliases_for_later_history_import():
    papers, meta = parse_scopus_csv(csv_bytes(
        ["Title", "Year", "EID", "DOI", "Abstract", "Cited by"],
        [["Original title", 2023, "E-1", "", "", ""],
         ["Updated title", 2023, "E-2", "10.1234/one", "Useful abstract", 5],
         ["Original title", 2023, "E-1", "https://doi.org/10.1234/ONE", "", ""],
         ["Updated title", 2023, "E-3", "", "", ""]],
    ))
    assert len(papers) == 1
    assert meta["duplicates_removed"] == 3
    assert papers[0]["abstract"] == "Useful abstract"
    assert papers[0]["citations"] == 5
    assert set(papers[0]["aliases"]["eids"]) == {"E-1", "E-2", "E-3"}
    updated, history_meta = attach_citation_history(papers, csv_bytes(["EID", "Year", "Citations"], [["E-3", 2024, 3]]))
    assert updated[0]["citation_history"] == {"2024": 3}
    assert history_meta["updated_papers"] == 1


def test_wide_annual_citations_preserve_gaps_and_reject_prepublication_values():
    papers, meta = parse_scopus_csv(csv_bytes(
        ["Title", "Year", "Cited by", "Citations 2022", "Citations 2023", "Citations 2024"],
        [["Observed", 2022, 10, 0, "", 4], ["Too early", 2023, 2, 0, 1, 1]],
    ))
    assert papers[0]["citation_history"] == {"2022": 0, "2024": 4}
    assert papers[0]["citations"] == 10
    assert meta["invalid_rows"] == 1


def test_history_conflicts_reject_all_conflicting_observations_without_mutation():
    papers, _ = parse_scopus_csv(csv_bytes(
        ["Title", "Year", "EID", "Cited by", "Citations 2023"],
        [["First paper", 2022, "E1", 30, 5], ["Second paper", 2023, "E2", "", ""]],
    ))
    before = copy.deepcopy(papers)
    updated, meta = attach_citation_history(papers, csv_bytes(
        ["EID", "Year", "Citations"],
        [["E1", 2023, 8], ["E1", 2024, 3], ["E1", 2024, 9], ["E2", 2024, 0], ["E2", 2024, 0], ["E2", 2025, ""], ["missing", 2024, 2], ["E2", 2022, 0], ["E2", 2025, -1]],
    ))
    assert papers == before
    assert updated[0]["citation_history"] == {"2023": 5}
    assert updated[1]["citation_history"] == {"2024": 0}
    assert updated[1]["citations"] is None
    assert meta["invalid_rows"] == 6
    assert meta["duplicates_removed"] == 1
    assert meta["missing_rows"] == 1
    assert meta["annual_values_imported"] == 1


def test_ambiguous_eid_and_doi_history_is_rejected():
    papers, _ = parse_scopus_csv(csv_bytes(["Title", "Year", "EID", "DOI"], [["First", 2023, "E1", "10.1234/a"], ["Second", 2023, "E2", "10.1234/b"]]))
    with pytest.raises(ValueError, match="異なる論文"):
        attach_citation_history(papers, csv_bytes(["EID", "DOI", "Year", "Citations"], [["E1", "10.1234/b", 2024, 1]]))


def test_missing_only_history_is_not_converted_to_zero():
    papers, _ = parse_scopus_csv(b"Title,Year,EID\nTest,2023,E1\n")
    with pytest.raises(ValueError, match="取り込める年別引用数"):
        attach_citation_history(papers, b"EID,Year,Citations\nE1,2024,\n")
    assert papers[0]["citation_history"] == {}


def test_demo_is_reproducible_diverse_and_explicitly_synthetic():
    papers = demo_papers()
    assert papers == demo_papers()
    assert len(papers) == 869
    assert len({paper["id"] for paper in papers}) == len(papers)
    assert len({paper["title"] for paper in papers}) == len(papers)
    assert len({paper["source"] for paper in papers}) == 8
    assert len({author["id"] for paper in papers for author in paper["authors"]}) == 128
    assert all(paper["doi"] == "" and paper["id"].startswith("SYNTHETIC-") for paper in papers)
    assert all(paper["provenance"] == SYNTHETIC_PROVENANCE for paper in papers)
    assert all("artificially generated" in paper["abstract"] for paper in papers)
    for paper in papers:
        assert set(paper["citation_history"]) == {str(year) for year in range(paper["year"], 2026)}
        assert paper["citations"] == sum(paper["citation_history"].values())
        assert all(value >= 0 for value in paper["citation_history"].values())
    series = {}
    for paper in papers:
        series.setdefault(paper["source"], [0] * 5)[paper["year"] - 2021] += 1
    assert sum(counts[-1] > counts[0] * 3 for counts in series.values()) >= 3
    assert any(counts[-1] < counts[0] / 2 for counts in series.values())
    papers[0]["authors"][0]["name"] = "mutated"
    assert demo_papers()[0]["authors"][0]["name"] != "mutated"


def test_explicit_provenance_is_preserved_and_mixed_data_is_flagged():
    papers, meta = parse_scopus_csv(csv_bytes(
        ["EID", "Title", "Year", "Data provenance"],
        [["example-1", "Sample data", 2024, SYNTHETIC_PROVENANCE], ["real-1", "Real research", 2024, ""]],
    ))
    assert papers[0]["provenance"] == SYNTHETIC_PROVENANCE
    assert meta["is_demo"] is True
    assert meta["synthetic_count"] == 1
    assert any("混在" in warning for warning in meta["warnings"])
    revised, report = attach_citation_history(papers, csv_bytes(["EID", "Year", "Citations"], [["example-1", 2025, 5]]))
    assert revised[0]["provenance"] == SYNTHETIC_PROVENANCE
    assert report["is_demo"] is True
    assert report["synthetic_count"] == 1


def test_old_demo_export_detected_by_strict_content_signature():
    demo = demo_papers()[0]
    papers, meta = parse_scopus_csv(csv_bytes(
        ["EID", "Title", "Year", "Abstract", "Source title"],
        [[demo["id"], demo["title"], demo["year"], demo["abstract"], demo["source"]]],
    ))
    assert meta["is_demo"] is True
    assert meta["synthetic_count"] == 1
    assert papers[0]["provenance"] == SYNTHETIC_PROVENANCE


def test_real_research_about_synthetic_data_is_not_misclassified():
    papers, meta = parse_scopus_csv(csv_bytes(
        ["EID", "Title", "Year", "Abstract", "Source title", "Data provenance"],
        [["2-s2.0-123", "Synthetic data for battery research", 2024, "We evaluate synthetic training data.", "Real Journal", ""],
         ["SYNTHETIC-00001", "Unrelated experiment", 2024, "A different abstract.", "Synthetic Journal of Solid-state batteries", ""]],
    ))
    assert len(papers) == 2
    assert meta["is_demo"] is False
    assert meta["synthetic_count"] == 0


def test_explicit_title_based_test_summary_is_flagged_without_altering_original_abstract():
    abstract = "[TEST SUMMARY; TITLE-BASED; JA] 電池材料の量子計算を扱う検証用要約。"
    papers, report = parse_scopus_csv(csv_bytes(["Title", "Year", "Abstract", "DOI"], [
        ["Title-based test record", 2025, abstract, "10.1234/identifier-kept"],
        ["No test marker", 2025, "", ""],
    ]))
    assert papers[0]["abstract"] == abstract and papers[0]["doi"] == "10.1234/identifier-kept"
    assert papers[0]["provenance"] == SYNTHETIC_PROVENANCE
    assert papers[1]["provenance"] == ""
    assert report["is_demo"] and report["synthetic_count"] == report["test_summary_count"] == 1
    assert any("タイトル由来" in warning and "書誌情報全体が架空とは判定しません" in warning for warning in report["warnings"])
    assert any("混在" in warning for warning in report["warnings"])


def test_deduplication_does_not_drop_later_synthetic_marker():
    papers, meta = parse_scopus_csv(csv_bytes(
        ["EID", "Title", "Year", "Data provenance"],
        [["P1", "Same title", 2024, ""], ["P1", "Same title", 2024, SYNTHETIC_PROVENANCE]],
    ))
    assert len(papers) == 1
    assert papers[0]["provenance"] == SYNTHETIC_PROVENANCE
    assert meta["is_demo"] is True
    assert meta["synthetic_count"] == 1


@pytest.mark.parametrize("header", ["Publication date", "Cover Date", "Date", "発行日", "出版日"])
def test_publication_date_columns_and_precision(header):
    papers, report = parse_scopus_csv(csv_bytes(["Title", "Year", header], [["Date paper", 2024, "12 March 2024"]]))
    assert papers[0]["publication_date"] == "2024-03-12"
    assert papers[0]["date_precision"] == "day"
    assert papers[0]["date_source"] == "csv:" + header
    assert report["date_pipeline_version"] == 2 and report["date_usable_count"] == 1


@pytest.mark.parametrize("month", [3, "March", "3月", "3.0", "3 月"])
def test_month_column_combined_with_year_has_month_precision(month):
    papers, report = parse_scopus_csv(csv_bytes(["Title", "Year", "Month"], [["Month paper", 2024, month]]))
    assert papers[0]["publication_date"] == "2024-03"
    assert papers[0]["date_precision"] == "month"
    assert report["date_precision_counts"]["month"] == 1


def test_bad_date_and_conflicting_columns_do_not_remove_yearly_records():
    papers, report = parse_scopus_csv(csv_bytes(
        ["Title", "Year", "Publication date", "Month"],
        [["Invalid day", 2024, "2024-02-30", ""], ["Wrong year", 2024, "2023-03-04", ""], ["Conflicting month", 2024, "2024-03-04", "April"]],
    ))
    assert len(papers) == 3 and report["invalid_rows"] == 0
    assert all(p["year"] == 2024 and p["publication_date"] == "" and p["date_precision"] == "year" for p in papers)
    assert report["date_usable_count"] == 0
    assert any("月次" in warning for warning in report["warnings"])


def test_csv_dedup_fills_month_and_keeps_existing_conflicting_date():
    papers, report = parse_scopus_csv(csv_bytes(
        ["EID", "Title", "Year", "Publication date"],
        [["E1", "Same paper", 2024, ""], ["E1", "Same paper", 2024, "2024-03"], ["E1", "Same paper", 2024, "2024-04-01"]],
    ))
    assert len(papers) == 1 and papers[0]["publication_date"] == "2024-03"
    assert papers[0]["date_precision"] == "month"
    assert any("公開月" in warning for warning in report["warnings"])


def test_csv_roundtrip_respects_declared_precision_and_demo_has_no_invented_months():
    papers, _ = parse_scopus_csv(csv_bytes(
        ["Title", "Year", "Publication date", "Date precision", "Date source"],
        [["Coarse record", 2024, "2024-03-01", "month", "crossref:published"], ["Year record", 2024, "2024-01-01", "year", "legacy"]],
    ))
    assert papers[0]["publication_date"] == "2024-03" and papers[0]["date_source"] == "crossref:published"
    assert papers[1]["publication_date"] == ""
    assert all(p["publication_date"] == "" and p["date_precision"] == "year" for p in demo_papers())
