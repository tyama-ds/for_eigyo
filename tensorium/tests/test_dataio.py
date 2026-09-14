"""dataio: CSV/XLSX 読み込みと列型推論のテスト（標準ライブラリのみ）。"""
from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tcore import dataio  # noqa: E402

SAMPLES = ROOT / "sample_data"


def make_xlsx_bytes(rows: list[list], date_style_cols: set[int] = frozenset()) -> bytes:
    """openpyxl なしで最小の XLSX を組み立てる（内蔵リーダーの試験用）。"""
    shared: list[str] = []

    def sst(s: str) -> int:
        shared.append(s)
        return len(shared) - 1

    def col_letter(i: int) -> str:
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    sheet_rows = []
    for ri, row in enumerate(rows, start=1):
        cells = []
        for ci, v in enumerate(row):
            ref = f"{col_letter(ci)}{ri}"
            if v is None:
                continue
            if isinstance(v, bool):
                cells.append(f'<c r="{ref}" t="b"><v>{int(v)}</v></c>')
            elif isinstance(v, (int, float)):
                style = ' s="1"' if ci in date_style_cols else ""
                cells.append(f'<c r="{ref}"{style}><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{sst(str(v))}</v></c>')
        sheet_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    files = {
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": f'<workbook {ns} {rns}><sheets><sheet name="データ" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>',
        "xl/sharedStrings.xml": f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>",
        "xl/styles.xml": f'<styleSheet {ns}><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy/mm/dd"/></numFmts>'
                         '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestParse(unittest.TestCase):
    def test_parse_number(self):
        self.assertEqual(dataio.parse_number("1,234"), 1234.0)
        self.assertEqual(dataio.parse_number("¥1,200"), 1200.0)
        self.assertEqual(dataio.parse_number("１２．５"), 12.5)
        self.assertEqual(dataio.parse_number("12%"), 12.0)
        self.assertEqual(dataio.parse_number("(300)"), -300.0)
        self.assertEqual(dataio.parse_number("1.5e3"), 1500.0)
        self.assertIsNone(dataio.parse_number(""))
        self.assertIsNone(dataio.parse_number("N/A"))
        self.assertIsNone(dataio.parse_number("abc"))
        self.assertIsNone(dataio.parse_number("-"))

    def test_missing(self):
        for v in ("", " ", "nan", "NULL", "-", None):
            self.assertTrue(dataio.is_missing(v), v)
        self.assertFalse(dataio.is_missing("0"))


class TestCSV(unittest.TestCase):
    def test_utf8_bom_and_sniff(self):
        raw = "﻿名前,値\nA,1\nB,2\n".encode()
        t = dataio.read_csv_bytes(raw, "x.csv")
        self.assertEqual(t["columns"], ["名前", "値"])
        self.assertEqual(t["rows"], [["A", "1"], ["B", "2"]])
        self.assertEqual(t["encoding"], "utf-8-sig")

    def test_cp932(self):
        raw = "商品,価格\nりんご,120\nみかん,80\n".encode("cp932")
        t = dataio.read_csv_bytes(raw, "x.csv")
        self.assertEqual(t["columns"], ["商品", "価格"])
        self.assertEqual(t["encoding"], "cp932")
        self.assertEqual(t["rows"][0][0], "りんご")

    def test_tsv_and_ragged_rows(self):
        raw = b"a\tb\tc\n1\t2\n3\t4\t5\t6\n"
        t = dataio.read_csv_bytes(raw, "x.tsv")
        self.assertEqual(t["delimiter"], "\t")
        self.assertEqual(t["columns"], ["a", "b", "c", "列4"])
        self.assertEqual(t["rows"][0], ["1", "2", "", ""])

    def test_duplicate_and_blank_headers(self):
        raw = b"x,,x\n1,2,3\n"
        t = dataio.read_csv_bytes(raw, "x.csv")
        self.assertEqual(t["columns"], ["x", "列2", "x_2"])

    def test_empty(self):
        with self.assertRaises(dataio.DataError):
            dataio.read_csv_bytes(b"a,b\n", "x.csv")

    def test_sample_files(self):
        for name in ("reviews_ja.csv", "used_cars_ja.csv"):
            t = dataio.read_table_bytes((SAMPLES / name).read_bytes(), name)
            self.assertGreater(t["n_rows"], 100, name)


class TestXLSX(unittest.TestCase):
    def test_builtin_reader(self):
        rows = [["日付", "名前", "金額", "フラグ"], [45000, "太郎", 1200.5, True], [45001, "花子", 300, False], [None, "", None, None]]
        data = make_xlsx_bytes(rows, date_style_cols={0})
        orig = dataio._read_xlsx_openpyxl
        dataio._read_xlsx_openpyxl = lambda *a, **k: (_ for _ in ()).throw(ImportError())
        try:
            t = dataio.read_xlsx_bytes(data, "t.xlsx")
        finally:
            dataio._read_xlsx_openpyxl = orig
        self.assertEqual(t["reader"], "builtin")
        self.assertEqual(t["sheet"], "データ")
        self.assertEqual(t["columns"], ["日付", "名前", "金額", "フラグ"])
        self.assertEqual(t["rows"][0], ["2023-03-15", "太郎", "1200.5", "TRUE"])
        self.assertEqual(t["rows"][1][3], "FALSE")
        self.assertEqual(t["n_rows"], 2)   # 全空行は除外

    def test_zip_detection_without_extension(self):
        data = make_xlsx_bytes([["a", "b"], [1, 2]])
        t = dataio.read_table_bytes(data, "noext")
        self.assertEqual(t["rows"], [["1", "2"]])

    def test_sample_xlsx_matches_between_readers(self):
        path = SAMPLES / "sales_memo_ja.xlsx"
        data = path.read_bytes()
        orig = dataio._read_xlsx_openpyxl
        dataio._read_xlsx_openpyxl = lambda *a, **k: (_ for _ in ()).throw(ImportError())
        try:
            builtin = dataio.read_xlsx_bytes(data, path.name)
        finally:
            dataio._read_xlsx_openpyxl = orig
        self.assertEqual(builtin["n_rows"], 420)
        self.assertEqual(builtin["columns"][0], "商談日")
        self.assertRegex(builtin["rows"][0][0], r"^\d{4}-\d{2}-\d{2}$")
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return
        via = dataio.read_xlsx_bytes(data, path.name)
        self.assertEqual(via["rows"], builtin["rows"])

    def test_missing_sheet(self):
        data = make_xlsx_bytes([["a"], [1]])
        orig = dataio._read_xlsx_openpyxl
        dataio._read_xlsx_openpyxl = lambda *a, **k: (_ for _ in ()).throw(ImportError())
        try:
            with self.assertRaises(dataio.DataError):
                dataio.read_xlsx_bytes(data, "t.xlsx", sheet="ない")
        finally:
            dataio._read_xlsx_openpyxl = orig

    def test_xls_rejected(self):
        with self.assertRaises(dataio.DataError):
            dataio.read_table_bytes(b"\xd0\xcf\x11\xe0", "old.xls")


class TestAnalysis(unittest.TestCase):
    def test_types(self):
        num = dataio.analyze_column("n", [str(i * 1.5) for i in range(100)])
        self.assertEqual(num["type"], "numeric")
        self.assertIn("hist", num["stats"])
        cat = dataio.analyze_column("c", ["A", "B", "C"] * 40)
        self.assertEqual(cat["type"], "categorical")
        self.assertEqual(cat["stats"]["top"][0]["count"], 40)
        txt = dataio.analyze_column("t", [f"これは{i}番目のレビュー本文です。とても良い商品でした。" for i in range(60)])
        self.assertEqual(txt["type"], "text")
        dt = dataio.analyze_column("d", [f"2024-01-{i:02d}" for i in range(1, 29)])
        self.assertEqual(dt["type"], "datetime")
        ids = dataio.analyze_column("id", [f"ID{i:05d}" for i in range(200)])
        self.assertEqual(ids["type"], "id")
        empty = dataio.analyze_column("e", ["", "", "NA"])
        self.assertEqual(empty["type"], "empty")

    def test_suggest(self):
        t = dataio.read_table_bytes((SAMPLES / "reviews_ja.csv").read_bytes(), "reviews_ja.csv")
        s = dataio.table_summary(t)
        self.assertEqual(s["suggest"]["target"], "評価")
        self.assertEqual(s["suggest"]["task"], "classification")
        self.assertEqual(s["suggest"]["roles"]["レビュー本文"], "text")
        self.assertEqual(s["suggest"]["roles"]["価格"], "numeric")
        self.assertEqual(len(s["preview"]), dataio.MAX_PREVIEW_ROWS)
        t2 = dataio.read_table_bytes((SAMPLES / "used_cars_ja.csv").read_bytes(), "used_cars_ja.csv")
        s2 = dataio.table_summary(t2)
        self.assertEqual(s2["suggest"]["task"], "regression")
        self.assertEqual(s2["suggest"]["target"], "価格_万円")

    def test_histogram(self):
        h = dataio.histogram([1.0, 2.0, 3.0, 4.0], bins=2)
        self.assertEqual(h["counts"], [2, 2])
        self.assertEqual(dataio.histogram([5.0, 5.0])["counts"], [2])
        self.assertEqual(dataio.histogram([])["counts"], [])


if __name__ == "__main__":
    unittest.main()
