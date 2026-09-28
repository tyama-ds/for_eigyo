"""テスト用の資料ファイル（docx / xlsx / pptx / eml / pdf など）を最小構成で作る。"""
from __future__ import annotations

import zipfile
from email.message import EmailMessage
from pathlib import Path

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(path: Path) -> Path:
    body = (
        f'<w:document {W_NS}><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>見積書</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>A社向け 生産管理システム更改の概算です。</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        '<w:r><w:t>初期費用 1,200万円</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>項目</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>金額</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>保守</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>月額 30万円</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", body)
    return path


def make_xlsx(path: Path) -> Path:
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml",
                   f'<workbook {ns} {rns}><sheets><sheet name="案件一覧" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml",
                   f'<sst {ns}><si><t>顧客</t></si><si><t>確度</t></si><si><t>A社</t></si><si><t>B社</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet {ns}><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                   '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>60</v></c></row>'
                   '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><v>30</v></c></row>'
                   '</sheetData></worksheet>')
    return path


def make_pptx(path: Path) -> Path:
    ns = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
    slide = (f'<p:sld {ns}><p:cSld><p:spTree>'
             '<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
             '<p:txBody><a:p><a:r><a:t>提案のポイント</a:t></a:r></a:p></p:txBody></p:sp>'
             '<p:sp><p:nvSpPr><p:nvPr/></p:nvSpPr><p:txBody>'
             '<a:p><a:r><a:t>段階導入でライン停止ゼロ</a:t></a:r></a:p></p:txBody></p:sp>'
             '</p:spTree></p:cSld></p:sld>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("ppt/slides/slide1.xml", slide)
    return path


def make_eml(path: Path) -> Path:
    msg = EmailMessage()
    msg["Subject"] = "Re: A社 見積の件"
    msg["From"] = "田中 <tanaka@example.co.jp>"
    msg["To"] = "sales@example.co.jp"
    msg["Date"] = "Fri, 25 Sep 2026 10:00:00 +0900"
    msg.set_content("見積ありがとうございます。稼働率の根拠を追加してください。")
    msg.add_attachment(b"dummy", maintype="application", subtype="octet-stream", filename="資料.pdf")
    path.write_bytes(bytes(msg))
    return path


def make_pdf(path: Path, text: str = "Mycel PDF sample text") -> Path:
    """テキスト 1 行だけの最小 PDF（英数字のみ）。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def make_all(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "docx": make_docx(folder / "見積書.docx"),
        "xlsx": make_xlsx(folder / "案件一覧.xlsx"),
        "pptx": make_pptx(folder / "提案資料.pptx"),
        "eml": make_eml(folder / "田中さんからの返信.eml"),
        "pdf": make_pdf(folder / "カタログ.pdf"),
    }
    (folder / "議事メモ.txt").write_bytes("10/1 打ち合わせ\n稼働率99%が必須条件".encode("cp932"))
    files["txt"] = folder / "議事メモ.txt"
    (folder / "売上.csv").write_text("月,売上\n4月,120\n5月,150\n", encoding="utf-8")
    files["csv"] = folder / "売上.csv"
    (folder / "ページ.html").write_text(
        "<html><head><title>製品紹介</title><style>x{}</style></head><body><h2>特長</h2>"
        "<ul><li>在庫の一元化</li></ul><script>alert(1)</script></body></html>", encoding="utf-8")
    files["html"] = folder / "ページ.html"
    (folder / "写真.png").write_bytes(b"\x89PNG")
    return files
