"""抽出器のテスト（DOCX / PPTX / XLSX / PDF / CSV / HTML / Markdown / テキスト）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docvault.extract import ExtractError, decode_text, extract  # noqa: E402
from docvault.samples import make_docx, make_pdf, make_pptx, make_xlsx  # noqa: E402

try:
    import pypdf  # noqa: F401
    HAS_PDF = True
except ImportError:
    try:
        import fitz  # noqa: F401
        HAS_PDF = True
    except ImportError:
        HAS_PDF = False


class TestExtract(unittest.TestCase):
    def test_docx_headings_tables_lists(self):
        data = make_docx("仕様書", [
            ("h1", "第1章 概要"), ("p", "本書は試験装置の仕様を定める。"),
            ("h2", "1.1 構成"), ("table", [["部品", "数量"], ["センサー", "2"]]),
            ("li", "保証は2年"),
            ("h1", "第2章 運用"), ("p", "点検は月1回。"),
        ])
        r = extract(data, "spec.docx")
        self.assertEqual(r["title"], "仕様書")
        heads = [s["heading"] for s in r["sections"]]
        self.assertEqual(heads, ["第1章 概要", "第1章 概要 > 1.1 構成", "第2章 運用"])
        self.assertIn("センサー | 2", r["sections"][1]["text"])
        self.assertIn("・保証は2年", r["sections"][1]["text"])
        self.assertEqual(r["sections"][1]["locator"], "1.1 構成")

    def test_pptx_order_title_notes(self):
        data = make_pptx([("はじめに", ["背景", "目的"], "話す内容のメモ"), ("結論", ["採用する"], "")])
        r = extract(data, "deck.pptx")
        self.assertEqual([s["locator"] for s in r["sections"]], ["スライド1", "スライド2"])
        self.assertEqual(r["sections"][0]["heading"], "はじめに")
        self.assertIn("(ノート)話す内容のメモ", r["sections"][0]["text"])  # NFKC で全角括弧は半角に
        self.assertEqual(r["pages"], 2)

    def test_xlsx_header_rows(self):
        rows = [["品番", "名称", "単価"]] + [[f"A{i}", f"部品{i}", i * 100] for i in range(1, 41)]
        r = extract(make_xlsx({"価格表": rows, "空": []}), "price.xlsx")
        self.assertEqual(len(r["sections"]), 2)                    # 30行ずつ
        first = r["sections"][0]
        self.assertTrue(first["locator"].startswith("価格表!行2-"))
        self.assertIn("品番: A1 / 名称: 部品1 / 単価: 100", first["text"])
        self.assertIn("列: 品番 | 名称 | 単価", first["text"])

    @unittest.skipUnless(HAS_PDF, "pypdf / PyMuPDF が無い")
    def test_pdf_pages(self):
        r = extract(make_pdf(["Quarterly report for the plant.", "Second page: defects decreased."]),
                    "report.pdf")
        self.assertEqual([s["locator"] for s in r["sections"]], ["p.1", "p.2"])
        self.assertEqual(r["sections"][1]["page"], 2)
        self.assertIn("defects", r["sections"][1]["text"])

    @unittest.skipUnless(HAS_PDF, "pypdf / PyMuPDF が無い")
    def test_pdf_without_text_is_error(self):
        with self.assertRaises(ExtractError) as cm:
            extract(make_pdf(["", ""]), "scan.pdf")
        self.assertIn("OCR", str(cm.exception))

    def test_markdown_sections(self):
        r = extract("# 手順書\n\n準備する。\n\n## 手順1\n電源を入れる。\n```\n# コード内は見出しでない\n```\n".encode(),
                    "proc.md")
        self.assertEqual(r["title"], "手順書")
        self.assertEqual([s["locator"] for s in r["sections"]], ["手順書", "手順1"])
        self.assertIn("# コード内は見出しでない", r["sections"][1]["text"])

    def test_text_encodings(self):
        text = "品質保証部の連絡先一覧"
        for enc in ("utf-8", "cp932", "euc-jp"):
            self.assertEqual(decode_text(text.encode(enc)), text)
        r = extract(text.encode("cp932"), "memo.txt")
        self.assertIn("品質保証部", r["sections"][0]["text"])

    def test_csv_and_html(self):
        r = extract("名前,部署\n佐藤,品質保証部\n".encode("cp932"), "list.csv")
        self.assertIn("名前: 佐藤 / 部署: 品質保証部", r["sections"][0]["text"])
        r = extract("<html><head><title>社内ポータル</title><script>x()</script></head>"
                    "<body><h1>お知らせ</h1><p>点検は<b>明日</b>です</p></body></html>".encode(), "a.html")
        self.assertEqual(r["title"], "社内ポータル")
        self.assertIn("点検は明日です", r["sections"][0]["text"])
        self.assertNotIn("x()", r["sections"][0]["text"])

    def test_errors(self):
        with self.assertRaises(ExtractError):
            extract(b"x", "a.exe")
        with self.assertRaises(ExtractError) as cm:
            extract(b"not a zip", "a.docx")
        self.assertIn("壊れて", str(cm.exception))
        with self.assertRaises(ExtractError):
            extract(b"   \n  ", "empty.txt")


if __name__ == "__main__":
    unittest.main()
