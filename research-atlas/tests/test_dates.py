from copy import deepcopy

import pytest

from app.dates import merge_publication_dates, normalize_paper_date, normalize_publication_date


@pytest.mark.parametrize("raw,expected,precision", [
    ("2024-02-29", "2024-02-29", "day"),
    ("2024-03", "2024-03", "month"),
    ("2024/3/12", "2024-03-12", "day"),
    ("12-Mar-2024", "2024-03-12", "day"),
    ("March 12, 2024", "2024-03-12", "day"),
    ("2024 March 12th", "2024-03-12", "day"),
    ("September 2024", "2024-09", "month"),
    ("2024 Sept.", "2024-09", "month"),
    ("2024年３月１２日", "2024-03-12", "day"),
    ("2024年3月", "2024-03", "month"),
    ("03/2024", "2024-03", "month"),
    ("15/03/2024", "2024-03-15", "day"),
    ("03/15/2024", "2024-03-15", "day"),
    ("2024-03-12T12:30:00Z", "2024-03-12", "day"),
    ([2024, 3], "2024-03", "month"),
    ([2024, 3, 12], "2024-03-12", "day"),
    ([2024], "", "year"),
    ("2024", "", "year"),
    (None, "", "year"),
])
def test_supported_dates_preserve_precision(raw, expected, precision):
    result = normalize_publication_date(raw, 2024)
    assert result == {"publication_date": expected, "date_precision": precision, "warnings": []}


@pytest.mark.parametrize("raw", ["2024-13-01", "2024-02-30", "2024-00", "03/04/2024", "not a date", [2024, 3, 0], [2024, True], [2024, 3, 4, 5]])
def test_invalid_or_ambiguous_dates_keep_known_year_without_inventing_month(raw):
    result = normalize_publication_date(raw, 2024)
    assert result["publication_date"] == ""
    assert result["date_precision"] == "year"
    assert result["warnings"]


def test_year_conflict_is_a_warning_not_a_replacement_of_the_year():
    result = normalize_publication_date("2023-12-31", 2024)
    assert result["publication_date"] == ""
    assert result["date_precision"] == "year"
    assert "矛盾" in result["warnings"][0]
    assert normalize_publication_date(None)["date_precision"] == "unknown"


def test_declared_coarse_precision_is_not_upgraded_by_padded_dates():
    paper = {"year": 2024, "publication_date": "2024-01-01", "date_precision": "year", "date_source": "legacy"}
    assert normalize_paper_date(paper)["publication_date"] == ""
    paper["date_precision"] = "month"
    normalized = normalize_paper_date(paper)
    assert normalized["publication_date"] == "2024-01" and normalized["date_precision"] == "month"
    assert normalized["date_source"] == "legacy"


def test_merge_fills_same_year_month_but_keeps_explicit_base_precision():
    first = {"year": 2024, "publication_date": "", "date_precision": "year", "date_source": "csv:Year"}
    second = {"year": 2024, "publication_date": "2024-03-12", "date_precision": "day", "date_source": "crossref:published"}
    before = deepcopy((first, second))
    fields, warnings = merge_publication_dates(first, second)
    assert fields["publication_date"] == "2024-03-12" and not warnings
    assert fields["date_source"] == "crossref:published"
    first.update(publication_date="2024-03", date_precision="month")
    fields, warnings = merge_publication_dates(first, second)
    assert fields["publication_date"] == "2024-03" and fields["date_precision"] == "month"
    assert not warnings
    assert second == before[1]


def test_merge_keeps_existing_value_when_month_or_year_conflicts():
    first = {"year": 2024, "publication_date": "2024-03", "date_precision": "month", "date_source": "csv:Date"}
    for incoming in ["2024-04-02", "2025-03-02"]:
        second = {"year": int(incoming[:4]), "publication_date": incoming, "date_precision": "day", "date_source": "crossref:published"}
        fields, warnings = merge_publication_dates(first, second)
        assert fields["publication_date"] == "2024-03"
        assert fields["date_source"] == "csv:Date"
        assert warnings
    fields, warnings = merge_publication_dates(first, {"year": 2025})
    assert fields["publication_date"] == "2024-03" and warnings
