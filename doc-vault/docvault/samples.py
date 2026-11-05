"""サンプル文書と、Office 文書（DOCX / XLSX / PPTX）・PDF の最小生成器。

「サンプルを読み込む」ボタンとテストで使う。生成する DOCX / XLSX は Word / Excel で開ける
最小構成。PPTX と PDF はテスト用（抽出器の検証用の最小構成）。
"""
from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def make_docx(title: str, blocks: list) -> bytes:
    """blocks: ("h1"|"h2"|"p"|"li", テキスト) または ("table", [[セル…]…])。"""
    body = []
    for kind, content in blocks:
        if kind == "table":
            rows = "".join(
                "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{escape(str(c))}</w:t></w:r></w:p></w:tc>"
                                   for c in row) + "</w:tr>" for row in content)
            body.append(f"<w:tbl><w:tblPr><w:tblStyle w:val=\"TableGrid\"/></w:tblPr>{rows}</w:tbl>")
            continue
        ppr = ""
        if kind in ("h1", "h2"):
            ppr = f"<w:pPr><w:pStyle w:val=\"{1 if kind == 'h1' else 2}\"/></w:pPr>"
        elif kind == "li":
            ppr = "<w:pPr><w:numPr><w:ilvl w:val=\"0\"/><w:numId w:val=\"1\"/></w:numPr></w:pPr>"
        body.append(f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">{escape(content)}</w:t></w:r></w:p>")
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{_W}"><w:body>{"".join(body)}</w:body></w:document>')
    # 日本語版 Word と同じく、見出しのスタイル ID は "1" "2"（名前が heading 1/2）
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{_W}">'
              '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/>'
              '<w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="2"><w:name w:val="heading 2"/>'
              '<w:pPr><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
              '</w:styles>')
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title>{escape(title)}</dc:title></cp:coreProperties>')
    return _zip({
        "[Content_Types].xml": (
            f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="{_CT}">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PR}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '</Relationships>'),
        "word/_rels/document.xml.rels": (
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PR}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>'),
        "word/document.xml": document,
        "word/styles.xml": styles,
        "docProps/core.xml": core,
    })


def make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    """sheets: {シート名: [[セル…]…]}（文字列は inlineStr、数値は数値セル）。"""
    files: dict[str, str] = {}
    sheet_entries, rels, overrides = [], [], []
    for i, (name, rows) in enumerate(sheets.items(), 1):
        xml_rows = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, val in enumerate(row):
                ref = f"{chr(65 + c)}{r}"
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    cells.append(f'<c r="{ref}"><v>{val}</v></c>')
                elif val not in (None, ""):
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(val))}</t></is></c>')
            xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
        files[f"xl/worksheets/sheet{i}.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{_S}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
        sheet_entries.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
        overrides.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    files["xl/workbook.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_S}" xmlns:r="{_R}"><sheets>{"".join(sheet_entries)}</sheets></workbook>')
    files["xl/_rels/workbook.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PR}">{"".join(rels)}</Relationships>')
    files["_rels/.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PR}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>')
    files["[Content_Types].xml"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="{_CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'{"".join(overrides)}</Types>')
    return _zip(files)


def make_pptx(slides: list[tuple[str, list[str], str]]) -> bytes:
    """テスト用の最小 PPTX（抽出器が読む部分だけ）。slides: (タイトル, 本文行, ノート)。"""
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    files: dict[str, str] = {}
    ids, rels = [], []
    # 表示順とファイル名の順をわざと逆にして、presentation.xml の順序に従うことを確かめられるようにする
    for order, (title, lines, notes) in enumerate(slides, 1):
        num = len(slides) - order + 1
        def sp(text, ph=""):
            phx = f'<p:nvSpPr><p:cNvPr id="1" name="t"/><p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr>'
            paras = "".join(f"<a:p><a:r><a:t>{escape(t)}</a:t></a:r></a:p>" for t in text)
            return f"<p:sp>{phx}<p:txBody>{paras}</p:txBody></p:sp>"
        tree = sp([title], '<p:ph type="title"/>') + sp(lines)
        files[f"ppt/slides/slide{num}.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8"?><p:sld xmlns:a="{a}" xmlns:p="{p}" xmlns:r="{_R}">'
            f'<p:cSld><p:spTree>{tree}</p:spTree></p:cSld></p:sld>')
        if notes:
            files[f"ppt/notesSlides/notesSlide{num}.xml"] = (
                f'<?xml version="1.0" encoding="UTF-8"?><p:notes xmlns:a="{a}" xmlns:p="{p}">'
                f'<p:cSld><p:spTree>{sp([notes])}</p:spTree></p:cSld></p:notes>')
            files[f"ppt/slides/_rels/slide{num}.xml.rels"] = (
                f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PR}">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" '
                f'Target="../notesSlides/notesSlide{num}.xml"/></Relationships>')
        ids.append(f'<p:sldId id="{255 + order}" r:id="rId{order}"/>')
        rels.append(f'<Relationship Id="rId{order}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{num}.xml"/>')
    files["ppt/presentation.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><p:presentation xmlns:p="{p}" xmlns:r="{_R}">'
        f'<p:sldIdLst>{"".join(ids)}</p:sldIdLst></p:presentation>')
    files["ppt/_rels/presentation.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PR}">{"".join(rels)}</Relationships>')
    return _zip(files)


def make_pdf(pages: list[str]) -> bytes:
    """テスト用の最小 PDF（ASCII テキスト・Helvetica）。"""
    objs: list[bytes] = []
    kids = []
    n_pages = len(pages)
    # 1: catalog, 2: pages, 3: font, 4..: page/content のペア
    for i, text in enumerate(pages):
        page_no, content_no = 4 + i * 2, 5 + i * 2
        kids.append(f"{page_no} 0 R")
        lines = text.split("\n")
        ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
        for ln in lines:
            safe = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({safe}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs.append((page_no, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                              f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>".encode()))
        objs.append((content_no, b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                     + stream + b"\nendstream"))
    objs.insert(0, (3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    objs.insert(0, (2, f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n_pages} >>".encode()))
    objs.insert(0, (1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objs.sort()
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}
    for num, body in objs:
        offsets[num] = out.tell()
        out.write(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    total = len(objs) + 1
    out.write(f"xref\n0 {total}\n0000000000 65535 f \n".encode())
    for num in range(1, total):
        out.write(f"{offsets[num]:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


# ---------------------------------------------------------------- サンプル文書

COMPANY_MD = """# 青嶺製作所 会社概要

青嶺製作所は 1987 年に長野県松本市で創業した産業機器メーカーである。代表取締役社長は早瀬凛。
従業員は約 420 名で、本社工場のほかに仙台工場と大阪営業所を持つ。

## 事業
祖業のプレス加工部品事業と、2015 年に立ち上げた産業用センサー事業の 2 本柱。
センサー事業は「SkyEdge」ブランドで展開し、売上の約 4 割を占める。主力製品は SkyEdge S-2。

## 技術と体制
センサー事業の立ち上げを主導したのは CTO の柏木周平。無線給電技術 AirFeed を中核に据えた。
品質マネジメントは ISO 9001 の認証を 2004 年に取得しており、品質保証部が維持・運用を担う。

## 主要取引先
制御盤大手の北浜電機、食品機械のむつみ食品機械。北浜電機とは 2021 年に資本業務提携を結んだ。
"""

MINUTES_MD = """# 第 42 回 品質会議 議事録（2026 年 9 月）

出席: 品質保証部（部長 柏木周平 ほか 4 名）、仙台工場 製造課、営業部

## 1. 北浜電機からのクレーム
SkyEdge S-2 の筐体固定ネジの緩みについて、北浜電機から 8 月に 3 件の指摘を受けた。
原因は仙台工場の熱処理炉の温度ばらつきで、ネジの硬度が規格下限を下回っていた。
対策として熱処理炉の温度センサーを 9 月 10 日に交換し、全ロットの硬度検査を 10 月末まで実施する。

## 2. ISO 9001 サーベイランス審査
11 月 18〜19 日に実施予定。品質保証部が是正処置記録を 10 月末までに整備する。
熱処理炉の件は是正処置の事例として報告する。

## 3. 決定事項
- 熱処理炉の温度記録を毎日品質保証部へ共有する（担当: 仙台工場 製造課）
- 北浜電機への最終報告書は 10 月 15 日までに提出（担当: 営業部）
"""


def sample_documents() -> list[dict]:
    """(filename, data, sensitivity, folder, tags)。公開資料と社外秘資料を混ぜてある。"""
    spec = make_docx("SkyEdge S-2 製品仕様書", [
        ("h1", "SkyEdge S-2 製品仕様書"),
        ("p", "SkyEdge S-2 は青嶺製作所の産業用ワイヤレスセンサー。振動・温度・電流を計測し、"
              "モーターの劣化予兆を検知する。"),
        ("h2", "主な仕様"),
        ("table", [["項目", "値"], ["計測項目", "振動・温度・電流"], ["給電方式", "AirFeed（無線給電）"],
                   ["通信距離", "最大 80 m"], ["動作温度", "-20〜70 ℃"], ["保護等級", "IP67"]]),
        ("h2", "製造と品質"),
        ("p", "S-2 は仙台工場で製造し、筐体固定ネジは熱処理炉で焼入れした部品を用いる。"
              "出荷検査は ISO 9001 に基づく手順で品質保証部が行う。"),
        ("li", "保証期間: 納入後 2 年"),
        ("li", "保守サービス: SkyEdge Care（年額制）"),
    ])
    defects = make_xlsx({
        "不具合集計": [
            ["月", "製品", "不具合", "件数", "工場", "対応"],
            ["2026-07", "SkyEdge S-2", "ネジ緩み", 1, "仙台工場", "調査中"],
            ["2026-08", "SkyEdge S-2", "ネジ緩み", 3, "仙台工場", "熱処理炉の温度ばらつきが原因"],
            ["2026-08", "SkyEdge S-1", "通信断", 1, "本社工場", "ファーム更新で解決"],
            ["2026-09", "SkyEdge S-2", "ネジ緩み", 0, "仙台工場", "温度センサー交換後は発生なし"],
        ],
    })
    return [
        {"filename": "01_会社概要.md", "data": COMPANY_MD.encode("utf-8"),
         "sensitivity": "public", "folder": "公開資料", "tags": ["会社"]},
        {"filename": "02_SkyEdge_S-2_製品仕様書.docx", "data": spec,
         "sensitivity": "public", "folder": "公開資料", "tags": ["製品"]},
        {"filename": "03_品質会議_議事録_2026-09.md", "data": MINUTES_MD.encode("utf-8"),
         "sensitivity": "internal", "folder": "社内/品質", "tags": ["品質", "議事録"]},
        {"filename": "04_不具合集計.xlsx", "data": defects,
         "sensitivity": "internal", "folder": "社内/品質", "tags": ["品質"]},
    ]
