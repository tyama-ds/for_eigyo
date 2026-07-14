"""Phase 1-2: CSV・表抽出の列意味ベース化の回帰テスト。"""

from __future__ import annotations

from fermiscope.domain.enums import DocumentType, SearchPurpose
from fermiscope.domain.models import ParameterEstimate
from fermiscope.evidence.extractor import extract_evidence
from fermiscope.research.fetcher import FetchedDocument


def _doc(text: str, doc_type: DocumentType, tables=None) -> FetchedDocument:
    return FetchedDocument(
        url="https://example.jp/data",
        final_url="https://example.jp/data",
        content_type="text/csv" if doc_type == DocumentType.CSV else "text/html",
        doc_type=doc_type,
        status_code=200,
        text=text,
        tables=tables or [],
    )


def _pop_param() -> ParameterEstimate:
    return ParameterEstimate(
        id="population",
        name="東京都の人口",
        unit="person",
        definition="東京都の総人口",
        target_geography="東京都",
        search_terms_ja=["人口"],
        search_terms_en=["population"],
    )


def test_csv_value_column_not_year_column():
    """year,metric,value から人口として 14000000 を抽出し、2025 を抽出しない。"""
    doc = _doc("year,metric,value\n2025,Tokyo population,14000000\n", DocumentType.CSV)
    items = extract_evidence(doc, _pop_param(), "人口", SearchPurpose.DIRECT_VALUE)
    assert items, "抽出できていない"
    assert items[0].extracted_value == 14000000.0
    assert items[0].extracted_value != 2025
    # 年は時点として付随情報へ回る(値としては採らない)
    assert "2025" in (items[0].time_period or "") or items[0].time_period == "2025"
    # 列名・行が監査可能
    assert "列" in items[0].locator and "value" in items[0].locator


def test_html_table_header_unit_value_cell_no_unit():
    """『人口』ヘッダーの下に 14000000 だけの表から抽出できる。"""
    tables = [[["人口"], ["14000000"]]]
    doc = _doc("", DocumentType.HTML, tables=tables)
    items = extract_evidence(doc, _pop_param(), "人口", SearchPurpose.DIRECT_VALUE)
    assert items
    assert items[0].extracted_value == 14000000.0
    assert items[0].unit == "person"  # ヘッダーから単位を推定


def test_csv_first_numeric_in_matching_row_not_blindly_taken():
    """一致行の最初の数値(年)を無条件に採らない。"""
    doc = _doc("年,指標,値\n2020,東京都の人口,13900000\n", DocumentType.CSV)
    items = extract_evidence(doc, _pop_param(), "人口", SearchPurpose.DIRECT_VALUE)
    assert items
    assert items[0].extracted_value == 13900000.0


def test_excel_grid_via_tables_path():
    """XLSX 経由(doc.tables)でも列意味ベースで抽出する。"""
    tables = [[["地域", "人口"], ["大阪府", "8800000"]]]
    doc = _doc("", DocumentType.XLSX, tables=tables)
    doc.content_type = ""
    items = extract_evidence(doc, _pop_param(), "人口", SearchPurpose.DIRECT_VALUE)
    assert items
    assert items[0].extracted_value == 8800000.0
    assert items[0].geography == "大阪府"  # 地域列は付随情報へ
