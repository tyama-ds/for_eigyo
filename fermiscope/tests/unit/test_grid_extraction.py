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


def test_header_scale_thousands_applied():
    """列ヘッダー「人口(千人)」のスケールを反映する(14100 → 14,100,000)。"""
    tables = [[["地域", "人口(千人)"], ["東京都", "14100"]]]
    doc = _doc("", DocumentType.HTML, tables=tables)
    items = extract_evidence(doc, _pop_param(), "人口", SearchPurpose.DIRECT_VALUE)
    assert items
    assert items[0].extracted_value == 14_100_000.0
    assert items[0].unit == "person"
    assert "千" in items[0].locator  # スケール適用が監査可能


def test_header_scale_millions_yen_applied():
    """列ヘッダー「(百万円)」のスケールを反映する。"""
    param = ParameterEstimate(
        id="sales", name="売上", unit="JPY", definition="年間売上",
        target_geography="日本", search_terms_ja=["売上"], search_terms_en=["sales"],
    )
    tables = [[["年", "売上(百万円)"], ["2025", "1200"]]]
    doc = _doc("", DocumentType.HTML, tables=tables)
    items = extract_evidence(doc, param, "売上", SearchPurpose.DIRECT_VALUE)
    assert items
    assert items[0].extracted_value == 1_200_000_000.0  # 1200百万円 = 12億円


def test_header_scale_not_double_applied_when_cell_has_scale_word():
    """セル自身がスケール語(万)を含む場合、列スケールを二重適用しない。"""
    tables = [[["地域", "人口(万人)"], ["東京都", "1410万"]]]
    doc = _doc("", DocumentType.HTML, tables=tables)
    items = extract_evidence(doc, _pop_param(), "人口", SearchPurpose.DIRECT_VALUE)
    assert items
    # 「1410万」= 14,100,000。列の「万」を二重適用して 1.41e11 にしない。
    assert items[0].extracted_value == 14_100_000.0
