"""File-backed CSV parsing without a whole-file read or fixed byte ceiling."""

import csv
import gc
import io

import pytest

from app.ingest import _read_csv, attach_citation_history, parse_scopus_csv


class BoundedReadFile(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        assert 0 <= size <= 1024 * 1024, "CSV parsing must request bounded reads"
        self.read_sizes.append(size)
        return super().read(size)


def csv_text(headers, rows, delimiter=","):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
def test_seekable_binary_upload_matches_bytes_and_retains_quoted_newlines(delimiter):
    content = csv_text(["EID", "Title", "Year", "Abstract", "References"], [
        ["one", "An article", 2024, 'Measured, "quoted" result.\n日本語; second line.', "10.1234/source"],
        ["two", "Another article", 2025, "Evidence", ""],
    ], delimiter).encode("utf-8-sig")
    source = BoundedReadFile(content)
    source.seek(10)  # Parsing always starts at the beginning of the upload.
    assert parse_scopus_csv(source) == parse_scopus_csv(content)
    assert not source.closed and source.read_sizes


def test_cp932_character_after_multiple_chunks_selects_fallback_before_reading_rows():
    abstract = "a" * (2 * 1024 * 1024) + "ステンレス鋼の評価"
    source = BoundedReadFile(csv_text(["Title", "Year", "Abstract"], [["Late Japanese", 2024, abstract]]).encode("cp932"))
    papers, report = parse_scopus_csv(source)
    assert report["encoding"] == "cp932"
    assert papers[0]["abstract"] == abstract
    assert not source.closed


def test_utf8_character_split_at_validation_chunk_boundary_is_preserved():
    prefix = b"Title,Year,Abstract\nBoundary,2024,"
    abstract = b"a" * (1024 * 1024 - 1 - len(prefix)) + "鋼".encode("utf-8")
    source = BoundedReadFile(prefix + abstract + b"\n")
    papers, report = parse_scopus_csv(source)
    assert report["encoding"] == "utf-8-sig"
    assert papers[0]["abstract"] == abstract.decode("utf-8")


@pytest.mark.parametrize("encoding,bom", [("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")])
def test_utf16_bom_blank_prefix_and_excel_preamble(encoding, bom):
    abstract = "強度測定\r\n二行目; quoted separator"
    text = "\ufeff\r\n\nsep=;\r\n" + csv_text(["Title", "Year", "Abstract"], [["鋼の研究", 2024, abstract]], ";")
    source = BoundedReadFile(bom + text.encode(encoding))
    papers, report = parse_scopus_csv(source)
    assert report["encoding"] == "utf-16" and report["delimiter"] == ";"
    assert papers[0]["abstract"] == abstract
    assert not source.closed


def test_long_blank_prefix_does_not_become_a_header_or_an_unbounded_read():
    source = BoundedReadFile(b"\r\n" * 40_000 + b"sep=;\nTitle;Year\nExample;2024\n")
    papers, report = parse_scopus_csv(source)
    assert papers[0]["title"] == "Example" and report["delimiter"] == ";"


@pytest.mark.parametrize("tail", [b"\x00", b"\x81"])
def test_invalid_bytes_late_in_file_are_not_silently_accepted(tail):
    source = BoundedReadFile(b"Title,Year,Abstract\nValid,2024,OK\nLate,2024," + b"a" * (1024 * 1024 + 100) + tail)
    with pytest.raises(ValueError, match="文字コード"):
        parse_scopus_csv(source)
    assert not source.closed


@pytest.mark.parametrize("content", [b"", b" \n\t", b"Other,Header\n1,2\n", b'Title,Year\n"unclosed,2024'])
def test_error_paths_do_not_close_caller_owned_file(content):
    source = BoundedReadFile(content)
    with pytest.raises(ValueError):
        parse_scopus_csv(source)
    gc.collect()
    assert not source.closed
    source.seek(0)
    assert source.read(1) == content[:1]


def test_streamed_malformed_records_keep_original_row_numbers():
    content = b'Title,Year,Abstract\nGood,2024,"line one\nline two"\nBroken,2024,extra,column\nSecond,2024,OK\nBad,2024,"unclosed\n'
    papers, report = parse_scopus_csv(BoundedReadFile(content))
    assert [paper["title"] for paper in papers] == ["Good", "Second"]
    assert report["invalid_rows"] == 2
    assert [error["row"] for error in report["row_errors"]] == [4, 6]


def test_streamed_row_limit_counts_records_and_keeps_file_open(monkeypatch):
    monkeypatch.setattr("app.ingest.MAX_IMPORT_ROWS", 2)
    source = BoundedReadFile(b'Title,Year,Abstract\nOne,2024,"line\nline"\nTwo,2024,OK\nThree,2024,OK\n')
    with pytest.raises(ValueError, match="2 行"):
        parse_scopus_csv(source)
    assert not source.closed


def test_annual_citations_accept_binary_stream_without_changing_parent():
    papers, _ = parse_scopus_csv(b"EID,Title,Year\none,Example,2024\n")
    source = BoundedReadFile(b"EID,Year,Citations\none,2024,12\none,2025,30\n")
    updated, report = attach_citation_history(papers, source)
    assert updated[0]["citation_history"] == {"2024": 12, "2025": 30}
    assert papers[0]["citation_history"] == {}
    assert report["annual_values_imported"] == 2 and not source.closed


def test_annual_citation_stream_still_rejects_more_than_100000_rows():
    papers, _ = parse_scopus_csv(b"EID,Title,Year\none,Example,2024\n")
    source = BoundedReadFile(b"EID,Year,Citations\n" + b"one,2024,1\n" * 100_001)
    with pytest.raises(ValueError, match="100,000"):
        attach_citation_history(papers, source)
    assert not source.closed


def test_csv_larger_than_20_mib_is_read_from_disk_without_upload_size_rejection(tmp_path):
    path = tmp_path / "large.csv"
    block = b"long reference prose " * 52_429
    with path.open("wb") as target:
        target.write(b"Title,Year,External notes\nExample,2024,")
        for _ in range(21):
            target.write(block)
        target.write(b"\n")
    assert path.stat().st_size > 20 * 1024 * 1024
    with path.open("rb") as source:
        papers, report = parse_scopus_csv(source)
        assert not source.closed
    assert len(papers) == report["imported_count"] == 1
    # Neither the former upload cap nor the former field cap constrains a field.
    assert csv.field_size_limit() > 256 * 1024 * 1024


def test_closing_partial_csv_iterator_does_not_close_upload():
    source = BoundedReadFile(b"Title,Year\nOne,2024\nTwo,2025\n")
    rows, *_ = _read_csv(source, max_rows=20_000, label="CSV")
    assert next(rows) == (2, ["One", "2024"])
    rows.close()
    assert not source.closed
