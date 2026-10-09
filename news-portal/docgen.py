# -*- coding: utf-8 -*-
"""docgen — Python 標準ライブラリだけで Word(.docx) / Excel(.xlsx) / PowerPoint(.pptx) / PDF を組み立てる。

server.py（Prism ニュースポータル）から呼ばれる。ローカルLLM が作った内容（Markdown・JSON）を
「文書モデル」に直し、各形式のバイト列を返す。python-docx / openpyxl / python-pptx / reportlab は使わない。

文書モデル（dict）:
  title, subtitle, meta: [str], blocks: [block], sources: [{n,title,source,published,link}],
  header: str（ページ上部）, footer: str, toc: bool
block:
  {"t":"h","level":1-3,"text"} / {"t":"p","text"} / {"t":"ul","items":[str]} / {"t":"ol","items":[str]}
  {"t":"table","header":[str],"rows":[[str]]} / {"t":"hr"}
インライン記法: **太字**、出典番号 [n]
"""
from __future__ import annotations

import io
import os
import re
import struct
import sys
import zipfile
import zlib
from datetime import datetime

# ------------------------------------------------------------------ 共通

_CITE_RE = re.compile(r"\[(\d{1,3})\]")
_INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|\[\d{1,3}\])")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def xml(s) -> str:
    return (str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _clean_xml_text(s: str) -> str:
    """XML 1.0 で許されない制御文字を落とす。"""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(s or ""))


def split_inline(text: str) -> list[tuple[str, str]]:
    """文字列を (種類, 文字列) の並びに。種類は text / bold / cite。"""
    out: list[tuple[str, str]] = []
    last = 0
    for m in _INLINE_RE.finditer(text or ""):
        if m.start() > last:
            out.append(("text", text[last:m.start()]))
        tok = m.group(0)
        out.append(("bold", tok[2:-2]) if tok.startswith("**") else ("cite", tok))
        last = m.end()
    if last < len(text or ""):
        out.append(("text", text[last:]))
    return out


def plain(text: str) -> str:
    """太字記法を外した平文。"""
    return (text or "").replace("**", "")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def md_to_blocks(md: str) -> list[dict]:
    """レポートの Markdown（見出し・箇条書き・番号付き・表・段落）をブロック列に。"""
    blocks: list[dict] = []
    lines = (md or "").replace("\r", "").split("\n")
    i = 0
    para: list[str] = []

    def flush_p():
        if para:
            blocks.append({"t": "p", "text": " ".join(x.strip() for x in para)})
            para.clear()

    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if not s:
            flush_p(); i += 1; continue
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        if m:
            flush_p(); blocks.append({"t": "h", "level": len(m.group(1)), "text": m.group(2).strip()}); i += 1; continue
        if re.match(r"^(-{3,}|\*{3,})$", s):
            flush_p(); blocks.append({"t": "hr"}); i += 1; continue
        if s.startswith("|") and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            flush_p()
            header = _split_row(s)
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                r = _split_row(lines[i])
                rows.append((r + [""] * len(header))[:len(header)])
                i += 1
            blocks.append({"t": "table", "header": header, "rows": rows})
            continue
        if re.match(r"^[-*・]\s+", s):
            flush_p()
            items = []
            while i < len(lines) and re.match(r"^\s*[-*・]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*・]\s+", "", lines[i]).strip()); i += 1
            blocks.append({"t": "ul", "items": items}); continue
        if re.match(r"^\d+[.)]\s+", s):
            flush_p()
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]).strip()); i += 1
            blocks.append({"t": "ol", "items": items}); continue
        para.append(s); i += 1
    flush_p()
    return blocks


def _zip(parts: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data.encode("utf-8") if isinstance(data, str) else data)
    return buf.getvalue()


_XMLDECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_OREL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rels(items: list[tuple[str, str, str]], external: set[str] | None = None) -> str:
    """[(Id, Type, Target)] → .rels XML。external に含まれる Id は TargetMode=External。"""
    ext = external or set()
    body = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{xml(tg)}"'
                   + (' TargetMode="External"' if i in ext else "") + "/>" for i, t, tg in items)
    return f'{_XMLDECL}<Relationships xmlns="{_NS_REL}">{body}</Relationships>'


# ------------------------------------------------------------------ Word (.docx)

_W = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      f'xmlns:r="{_NS_OREL}"')


def _docx_runs(text: str, cite_style: bool = True) -> str:
    out = []
    for kind, part in split_inline(_clean_xml_text(text)):
        if not part:
            continue
        if kind == "bold":
            rpr = "<w:rPr><w:b/></w:rPr>"
        elif kind == "cite" and cite_style:
            rpr = '<w:rPr><w:vertAlign w:val="superscript"/><w:color w:val="2B5C8F"/></w:rPr>'
        else:
            rpr = ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{xml(part)}</w:t></w:r>')
    return "".join(out)


def _docx_p(style: str, text: str, runs: str | None = None) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{runs if runs is not None else _docx_runs(text)}</w:p>'


def _docx_table(header: list[str], rows: list[list[str]]) -> str:
    n = max(1, len(header))
    grid = "".join('<w:gridCol w:w="%d"/>' % int(9000 / n) for _ in range(n))
    def cell(t, head=False):
        shd = '<w:shd w:val="clear" w:color="auto" w:fill="DDE5F0"/>' if head else ""
        runs = _docx_runs(("**" + plain(t) + "**") if head and t else t)
        return (f'<w:tc><w:tcPr><w:tcW w:w="{int(9000 / n)}" w:type="dxa"/>{shd}</w:tcPr>'
                f'<w:p><w:pPr><w:pStyle w:val="TableText"/></w:pPr>{runs}</w:p></w:tc>')
    trs = "<w:tr>" + "".join(cell(h, True) for h in header) + "</w:tr>"
    for r in rows:
        r = (list(r) + [""] * n)[:n]
        trs += "<w:tr>" + "".join(cell(c) for c in r) + "</w:tr>"
    return ('<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
            '<w:tblBorders>' + "".join(f'<w:{e} w:val="single" w:sz="4" w:space="0" w:color="BBBBBB"/>'
                                      for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
            + '</w:tblBorders><w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr>'
            f'<w:tblGrid>{grid}</w:tblGrid>{trs}</w:tbl><w:p/>')


def build_docx(doc: dict) -> bytes:
    """文書モデル → .docx。見出し／箇条書き／表／出典（ハイパーリンク）／ヘッダー・フッター（ページ番号）／目次フィールド。"""
    title = doc.get("title") or "レポート"
    body: list[str] = [_docx_p("Title", title)]
    if doc.get("subtitle"):
        body.append(_docx_p("Subtitle", doc["subtitle"]))
    for m in doc.get("meta") or []:
        body.append(_docx_p("Meta", m))
    blocks = doc.get("blocks") or []
    n_heads = sum(1 for b in blocks if b.get("t") == "h")
    if doc.get("toc") and n_heads >= 3:
        body.append(_docx_p("Heading2", "目次"))
        body.append('<w:p><w:fldSimple w:instr=" TOC \\o &quot;1-3&quot; \\h \\z \\u "><w:r><w:rPr><w:color w:val="888888"/></w:rPr>'
                    '<w:t>（Word で開いてフィールドを更新すると目次が入ります）</w:t></w:r></w:fldSimple></w:p>')
    for b in blocks:
        t = b.get("t")
        if t == "h":
            body.append(_docx_p(f"Heading{min(3, max(1, int(b.get('level') or 1)))}", b.get("text", "")))
        elif t == "p":
            body.append(_docx_p("Normal", b.get("text", "")))
        elif t == "ul":
            body.extend(_docx_p("Bullet", "• " + it) for it in b.get("items") or [])
        elif t == "ol":
            body.extend(_docx_p("Bullet", f"{i}. {it}") for i, it in enumerate(b.get("items") or [], 1))
        elif t == "table":
            body.append(_docx_table(b.get("header") or [], b.get("rows") or []))
        elif t == "hr":
            body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="BBBBBB"/></w:pBdr></w:pPr></w:p>')
    rel_items: list[tuple[str, str, str]] = [
        ("rId1", f"{_NS_OREL}/styles", "styles.xml"),
        ("rId2", f"{_NS_OREL}/settings", "settings.xml"),
        ("rId3", f"{_NS_OREL}/header", "header1.xml"),
        ("rId4", f"{_NS_OREL}/footer", "footer1.xml"),
    ]
    external: set[str] = set()
    sources = doc.get("sources") or []
    if sources:
        body.append(_docx_p("Heading2", "出典"))
        for k, s in enumerate(sources):
            head = f"[{s.get('n')}] {s.get('published') or ''} {s.get('source') or ''}｜"
            runs = _docx_runs(head, cite_style=False)
            link = str(s.get("link") or "")
            ttl = _clean_xml_text(s.get("title") or link or "")
            if link.startswith(("http://", "https://")):
                rid = f"rId{100 + k}"
                rel_items.append((rid, f"{_NS_OREL}/hyperlink", link)); external.add(rid)
                runs += (f'<w:hyperlink r:id="{rid}"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
                         f'<w:t xml:space="preserve">{xml(ttl)}</w:t></w:r></w:hyperlink>'
                         f'<w:r><w:rPr><w:color w:val="888888"/><w:sz w:val="15"/></w:rPr><w:t xml:space="preserve">  {xml(link)}</w:t></w:r>')
            else:
                runs += f'<w:r><w:t xml:space="preserve">{xml(ttl)}</w:t></w:r>'
            body.append(_docx_p("Source", "", runs))
    sect = ('<w:sectPr><w:headerReference w:type="default" r:id="rId3"/><w:footerReference w:type="default" r:id="rId4"/>'
            '<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1300" w:bottom="1300" w:left="1300" w:header="600" w:footer="600"/></w:sectPr>')
    document = f'{_XMLDECL}<w:document {_W}><w:body>{"".join(body)}{sect}</w:body></w:document>'

    def style(sid, name, size, bold=False, color=None, before=0, after=120, indent=0, italic=False):
        rpr = f'<w:sz w:val="{size}"/>' + ('<w:b/>' if bold else '') + ('<w:i/>' if italic else '') + (f'<w:color w:val="{color}"/>' if color else '')
        ppr = f'<w:spacing w:before="{before}" w:after="{after}"/>' + (f'<w:ind w:left="{indent}"/>' if indent else '')
        return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
                f'<w:pPr>{ppr}</w:pPr><w:rPr>{rpr}</w:rPr></w:style>')
    styles = (f'{_XMLDECL}<w:styles {_W}>'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Yu Gothic" w:hAnsi="Yu Gothic" w:eastAsia="Yu Gothic"/>'
              '<w:sz w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults>'
              + style("Normal", "Normal", 21)
              + style("Title", "Title", 36, True, before=0, after=120)
              + style("Subtitle", "Subtitle", 24, color="444444", after=120)
              + style("Meta", "Meta", 18, color="666666", after=60)
              + style("Heading1", "heading 1", 30, True, before=360, after=120)
              + style("Heading2", "heading 2", 26, True, color="1F3864", before=320, after=120)
              + style("Heading3", "heading 3", 23, True, before=240, after=80)
              + style("Bullet", "Bullet", 21, indent=360, after=60)
              + style("TableText", "Table Text", 18, after=0)
              + style("Source", "Source", 17, color="444444", after=40)
              + style("HeaderText", "Header Text", 16, color="888888", after=0)
              + '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/>'
                '<w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>'
              + '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr/></w:style>'
              + '</w:styles>')
    settings = f'{_XMLDECL}<w:settings {_W}><w:updateFields w:val="true"/></w:settings>'
    header = (f'{_XMLDECL}<w:hdr {_W}><w:p><w:pPr><w:pStyle w:val="HeaderText"/><w:jc w:val="right"/></w:pPr>'
              f'{_docx_runs(doc.get("header") or title, cite_style=False)}</w:p></w:hdr>')
    footer = (f'{_XMLDECL}<w:ftr {_W}><w:p><w:pPr><w:pStyle w:val="HeaderText"/><w:jc w:val="center"/></w:pPr>'
              + (f'<w:r><w:t xml:space="preserve">{xml(doc.get("footer"))}　</w:t></w:r>' if doc.get("footer") else "")
              + '<w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple><w:r><w:t xml:space="preserve"> / </w:t></w:r>'
                '<w:fldSimple w:instr=" NUMPAGES "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p></w:ftr>')
    ct = (f'{_XMLDECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
          '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
          '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
          '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
          '</Types>')
    return _zip({
        "[Content_Types].xml": ct,
        "_rels/.rels": _rels([("rId1", f"{_NS_OREL}/officeDocument", "word/document.xml")]),
        "word/document.xml": document,
        "word/styles.xml": styles,
        "word/settings.xml": settings,
        "word/header1.xml": header,
        "word/footer1.xml": footer,
        "word/_rels/document.xml.rels": _rels(rel_items, external),
    })


# ------------------------------------------------------------------ Excel (.xlsx)

def _col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_name(name: str, used: set[str]) -> str:
    s = re.sub(r"[\[\]:*?/\\]", " ", str(name or "Sheet")).strip()[:31] or "Sheet"
    base, k = s, 2
    while s.lower() in used:
        s = (base[:28] + f"({k})"); k += 1
    used.add(s.lower())
    return s


def build_xlsx(sheets: list[dict]) -> bytes:
    """[{name, columns:[{title,width}], rows:[[cell]], freeze, filter}] → .xlsx。
    cell は str / int / float / None、または {"v": 表示文字列, "link": URL}。先頭行は見出し（太字・網掛け・固定）。"""
    used: set[str] = set()
    parts: dict[str, bytes | str] = {}
    wb_sheets, wb_rels, ct_over = [], [], []
    for si, sh in enumerate(sheets or [{"name": "Sheet1", "columns": [], "rows": []}], 1):
        name = _sheet_name(sh.get("name"), used)
        cols = sh.get("columns") or []
        rows = sh.get("rows") or []
        ncol = max(1, len(cols), max((len(r) for r in rows), default=0))
        xcols = "".join(f'<col min="{i}" max="{i}" width="{float((cols[i - 1].get("width") if i - 1 < len(cols) and isinstance(cols[i - 1], dict) else None) or 14):.1f}" customWidth="1"/>'
                        for i in range(1, ncol + 1))
        links: list[tuple[str, str]] = []
        sd = []
        # 見出し行
        cells = "".join(f'<c r="{_col(i)}1" s="1" t="inlineStr"><is><t>{xml(_clean_xml_text((cols[i - 1].get("title") if i - 1 < len(cols) and isinstance(cols[i - 1], dict) else cols[i - 1]) if i - 1 < len(cols) else ""))}</t></is></c>'
                        for i in range(1, ncol + 1))
        sd.append(f'<row r="1">{cells}</row>')
        for ri, r in enumerate(rows, 2):
            cs = []
            for ci in range(1, ncol + 1):
                v = r[ci - 1] if ci - 1 < len(r) else None
                ref = f"{_col(ci)}{ri}"
                if v is None or v == "":
                    continue
                if isinstance(v, dict):
                    link = str(v.get("link") or "")
                    txt = _clean_xml_text(str(v.get("v") if v.get("v") is not None else link))
                    if link.startswith(("http://", "https://")):
                        links.append((ref, link))
                        cs.append(f'<c r="{ref}" s="4" t="inlineStr"><is><t>{xml(txt)}</t></is></c>')
                    else:
                        cs.append(f'<c r="{ref}" s="2" t="inlineStr"><is><t>{xml(txt)}</t></is></c>')
                elif isinstance(v, bool):
                    cs.append(f'<c r="{ref}" s="2" t="inlineStr"><is><t>{"TRUE" if v else "FALSE"}</t></is></c>')
                elif isinstance(v, (int, float)):
                    cs.append(f'<c r="{ref}" s="3"><v>{v}</v></c>')
                else:
                    cs.append(f'<c r="{ref}" s="2" t="inlineStr"><is><t xml:space="preserve">{xml(_clean_xml_text(str(v)))}</t></is></c>')
            sd.append(f'<row r="{ri}">{"".join(cs)}</row>')
        dim = f"A1:{_col(ncol)}{len(rows) + 1}"
        freeze = ('<sheetViews><sheetView workbookViewId="0"' + (' tabSelected="1"' if si == 1 else "") + '>'
                  '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                  '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>') if sh.get("freeze", True) else \
                 ('<sheetViews><sheetView workbookViewId="0"' + (' tabSelected="1"' if si == 1 else "") + '/></sheetViews>')
        autof = f'<autoFilter ref="{dim}"/>' if sh.get("filter", True) and rows else ""
        hl = ""
        if links:
            hl = "<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" r:id="rL{k}"/>' for k, (ref, _) in enumerate(links, 1)) + "</hyperlinks>"
            parts[f"xl/worksheets/_rels/sheet{si}.xml.rels"] = _rels(
                [(f"rL{k}", f"{_NS_OREL}/hyperlink", u) for k, (_, u) in enumerate(links, 1)],
                {f"rL{k}" for k in range(1, len(links) + 1)})
        parts[f"xl/worksheets/sheet{si}.xml"] = (
            f'{_XMLDECL}<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{_NS_OREL}">'
            f'<dimension ref="{dim}"/>{freeze}<sheetFormatPr defaultRowHeight="16"/><cols>{xcols}</cols>'
            f'<sheetData>{"".join(sd)}</sheetData>{autof}{hl}'
            '<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/></worksheet>')
        wb_sheets.append(f'<sheet name="{xml(name)}" sheetId="{si}" r:id="rId{si}"/>')
        wb_rels.append((f"rId{si}", f"{_NS_OREL}/worksheet", f"worksheets/sheet{si}.xml"))
        ct_over.append(f'<Override PartName="/xl/worksheets/sheet{si}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    n = len(wb_sheets)
    wb_rels.append((f"rId{n + 1}", f"{_NS_OREL}/styles", "styles.xml"))
    styles = (f'{_XMLDECL}<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.##"/></numFmts>'
              '<fonts count="3"><font><sz val="10.5"/><name val="Yu Gothic"/></font>'
              '<font><b/><sz val="10.5"/><name val="Yu Gothic"/></font>'
              '<font><u/><sz val="10.5"/><color rgb="FF0563C1"/><name val="Yu Gothic"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFDDE5F0"/><bgColor indexed="64"/></patternFill></fill></fills>'
              '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
              '<border><left style="thin"><color rgb="FFBBBBBB"/></left><right style="thin"><color rgb="FFBBBBBB"/></right>'
              '<top style="thin"><color rgb="FFBBBBBB"/></top><bottom style="thin"><color rgb="FFBBBBBB"/></bottom><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="5">'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
              '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment vertical="top"/></xf>'
              '<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="top"/></xf>'
              '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    parts.update({
        "[Content_Types].xml": (f'{_XMLDECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                                '<Default Extension="xml" ContentType="application/xml"/>'
                                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                                + "".join(ct_over) + '</Types>'),
        "_rels/.rels": _rels([("rId1", f"{_NS_OREL}/officeDocument", "xl/workbook.xml")]),
        "xl/workbook.xml": (f'{_XMLDECL}<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{_NS_OREL}">'
                            f'<bookViews><workbookView xWindow="0" yWindow="0" windowWidth="20000" windowHeight="12000"/></bookViews>'
                            f'<sheets>{"".join(wb_sheets)}</sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": _rels(wb_rels),
        "xl/styles.xml": styles,
    })
    return _zip(parts)


# ------------------------------------------------------------------ PowerPoint (.pptx)

_P = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      f'xmlns:r="{_NS_OREL}" '
      'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
SLIDE_W, SLIDE_H = 12192000, 6858000   # 16:9（EMU）
ACCENT, ACCENT2, INK, MUTED = "2B5C8F", "1F8A4C", "16202E", "6F7A8C"


def _theme_xml(name: str) -> str:
    def clr(k, v): return f'<a:{k}><a:srgbClr val="{v}"/></a:{k}>'
    fills = ('<a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
             '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>')
    lns = ('<a:lnStyleLst>' + ''.join(f'<a:ln w="{w}" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>'
                                   for w in (6350, 12700, 19050)) + '</a:lnStyleLst>')
    effs = '<a:effectStyleLst>' + '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3 + '</a:effectStyleLst>'
    return (f'{_XMLDECL}<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="{name}"><a:themeElements>'
            '<a:clrScheme name="Prism">' + clr("dk1", "16202E") + clr("lt1", "FFFFFF") + clr("dk2", "4A5669") + clr("lt2", "F3F5F8")
            + clr("accent1", ACCENT) + clr("accent2", ACCENT2) + clr("accent3", "6A5BB0") + clr("accent4", "A86A12")
            + clr("accent5", "2B6CB0") + clr("accent6", "9A5B00") + clr("hlink", "0563C1") + clr("folHlink", "954F72") + '</a:clrScheme>'
            '<a:fontScheme name="Prism"><a:majorFont><a:latin typeface="Yu Gothic"/><a:ea typeface="Yu Gothic"/><a:cs typeface=""/></a:majorFont>'
            '<a:minorFont><a:latin typeface="Yu Gothic"/><a:ea typeface="Yu Gothic"/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
            f'<a:fmtScheme name="Prism">{fills}{lns}{effs}{fills.replace("fillStyleLst", "bgFillStyleLst")}</a:fmtScheme>'
            '</a:themeElements></a:theme>')


def _sp_tree_head() -> str:
    return ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')


def _rpr(size: int, bold=False, color=None, base=False) -> str:
    return (f'<a:rPr lang="ja-JP" altLang="en-US" sz="{size * 100}"' + (' b="1"' if bold else "") + (' baseline="30000"' if base else "") + '>'
            + (f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>' if color else "")
            + '<a:latin typeface="Yu Gothic"/><a:ea typeface="Yu Gothic"/></a:rPr>')


def _runs(text: str, size: int, color: str | None = None, bold=False) -> str:
    out = []
    for kind, part in split_inline(_clean_xml_text(text)):
        if not part:
            continue
        if kind == "bold":
            out.append(f'<a:r>{_rpr(size, True, color)}<a:t>{xml(part)}</a:t></a:r>')
        elif kind == "cite":
            out.append(f'<a:r>{_rpr(max(8, size - 4), False, ACCENT, True)}<a:t>{xml(part)}</a:t></a:r>')
        else:
            out.append(f'<a:r>{_rpr(size, bold, color)}<a:t>{xml(part)}</a:t></a:r>')
    return "".join(out) or f'<a:r>{_rpr(size, bold, color)}<a:t></a:t></a:r>'


def _textbox(sid: int, name: str, x: int, y: int, cx: int, cy: int, paras: list[str], anchor: str = "t") -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{xml(name)}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
            f'<p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="{anchor}"><a:normAutofit/></a:bodyPr><a:lstStyle/>'
            + "".join(paras) + '</p:txBody></p:sp>')


def _rect(sid: int, x: int, y: int, cx: int, cy: int, color: str, name: str = "rect") -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{xml(name)}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="ja-JP"/></a:p></p:txBody></p:sp>')


def _para(runs: str, bullet: bool = False, align: str | None = None, space_after: int = 600) -> str:
    ppr = (f'<a:pPr{(" algn=" + chr(34) + align + chr(34)) if align else ""}'
           + (' marL="342900" indent="-342900"' if bullet else "") + f'><a:spcAft><a:spcPts val="{space_after}"/></a:spcAft>'
           + ('<a:buFont typeface="Arial"/><a:buChar char="•"/>' if bullet else '<a:buNone/>') + '</a:pPr>')
    return f'<a:p>{ppr}{runs}</a:p>'


def _table_frame(sid: int, x: int, y: int, cx: int, header: list[str], rows: list[list[str]], font: int = 11) -> str:
    n = max(1, len(header))
    colw = int(cx / n)
    row_h = 370000
    def tc(t, head=False):
        fill = f'<a:solidFill><a:srgbClr val="{"DDE5F0" if head else "FFFFFF"}"/></a:solidFill>'
        ln = "".join(f'<a:ln{side} w="6350"><a:solidFill><a:srgbClr val="BBBBBB"/></a:solidFill></a:ln{side}>' for side in ("L", "R", "T", "B"))
        return (f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>{_para(_runs(("**" + plain(t) + "**") if head and t else t, font, INK), space_after=0)}</a:txBody>'
                f'<a:tcPr marL="60000" marR="60000" marT="30000" marB="30000">{ln}{fill}</a:tcPr></a:tc>')
    trs = f'<a:tr h="{row_h}">' + "".join(tc(h, True) for h in header) + '</a:tr>'
    for r in rows:
        r = (list(r) + [""] * n)[:n]
        trs += f'<a:tr h="{row_h}">' + "".join(tc(c) for c in r) + '</a:tr>'
    cy = row_h * (len(rows) + 1)
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="table"/><p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>'
            f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></p:xfrm>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tblPr firstRow="1" bandRow="1"/>'
            f'<a:tblGrid>{"".join(f"<a:gridCol w={chr(34)}{colw}{chr(34)}/>" for _ in range(n))}</a:tblGrid>{trs}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')


def _bar_chart(sid_start: int, x: int, y: int, cx: int, cy: int, labels: list[str], values: list[float], color: str = ACCENT) -> str:
    """図形で描く簡易棒グラフ（ネイティブ chart ではなく rect + テキスト）。"""
    n = max(1, len(values))
    mx = max([v for v in values if isinstance(v, (int, float))] + [1])
    gap = int(cx / n)
    bw = int(gap * 0.6)
    label_h = 340000
    out = []
    sid = sid_start
    for i, (lab, v) in enumerate(zip(labels, values)):
        h = int((cy - label_h - 300000) * (float(v) / mx)) if mx else 0
        bx = x + i * gap + int((gap - bw) / 2)
        out.append(_rect(sid, bx, y + (cy - label_h - h), bw, max(h, 12700), color, f"bar{i}")); sid += 1
        out.append(_textbox(sid, f"val{i}", bx - int(gap * 0.2), y + (cy - label_h - h) - 300000, bw + int(gap * 0.4), 300000,
                            [_para(_runs(f"{v:g}" if isinstance(v, (int, float)) else str(v), 11, INK), align="ctr", space_after=0)], "b")); sid += 1
        out.append(_textbox(sid, f"lab{i}", bx - int(gap * 0.2), y + cy - label_h, bw + int(gap * 0.4), label_h,
                            [_para(_runs(str(lab), 10, MUTED), align="ctr", space_after=0)])); sid += 1
    return "".join(out)


def build_pptx(deck: dict) -> bytes:
    """スライド構成（{title, subtitle, footer, slides:[{title, bullets, table, chart, notes, foot}]}）→ .pptx。
    16:9。表紙＋各スライド（タイトル帯・箇条書き／表／簡易棒グラフ・フッター）、発表者ノートつき。"""
    slides_in = deck.get("slides") or []
    footer = _clean_xml_text(deck.get("footer") or "")
    slide_xmls: list[str] = []
    notes_in: list[str] = []
    # 表紙
    cover = (f'{_XMLDECL}<p:sld {_P}><p:cSld><p:spTree>{_sp_tree_head()}'
             + _rect(2, 0, 0, SLIDE_W, 457200, ACCENT, "band")
             + _textbox(3, "title", 685800, 2000000, SLIDE_W - 1371600, 1600000,
                        [_para(_runs(deck.get("title") or "レポート", 36, INK, True), space_after=600)], "b")
             + _textbox(4, "subtitle", 685800, 3700000, SLIDE_W - 1371600, 1200000,
                        [_para(_runs(deck.get("subtitle") or "", 18, MUTED), space_after=300)] +
                        ([_para(_runs(footer, 12, MUTED), space_after=0)] if footer else []))
             + '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
    slide_xmls.append(cover); notes_in.append(deck.get("cover_notes") or "")
    total = len(slides_in) + 1
    for k, s in enumerate(slides_in, 2):
        shapes = [_rect(2, 0, 0, 228600, SLIDE_H, ACCENT, "edge"),
                  _textbox(3, "title", 685800, 300000, SLIDE_W - 1371600, 900000,
                           [_para(_runs(s.get("title") or "", 26, INK, True), space_after=0)], "ctr"),
                  _rect(4, 685800, 1200000, SLIDE_W - 1371600, 19050, "DCE1E8", "rule")]
        sid = 5
        body_y, body_h = 1350000, 4700000
        if s.get("table") and isinstance(s["table"], dict):
            shapes.append(_table_frame(sid, 685800, body_y, SLIDE_W - 1371600, s["table"].get("header") or [], (s["table"].get("rows") or [])[:10],
                                       font=11 if len(s["table"].get("header") or []) <= 5 else 9)); sid += 1
        elif s.get("chart") and isinstance(s["chart"], dict):
            ch = s["chart"]
            labels = [str(x) for x in (ch.get("labels") or [])][:12]
            values = [float(v) if isinstance(v, (int, float)) else 0.0 for v in (ch.get("values") or [])][:12]
            if ch.get("title"):
                shapes.append(_textbox(sid, "ctitle", 685800, body_y, SLIDE_W - 1371600, 400000, [_para(_runs(ch["title"], 14, MUTED), space_after=0)])); sid += 1
            shapes.append(_bar_chart(sid, 685800, body_y + 450000, SLIDE_W - 1371600, body_h - 500000, labels, values)); sid += 3 * len(labels) + 1
            if s.get("bullets"):
                pass
        if s.get("bullets"):
            items = [str(b) for b in s["bullets"]][:8]
            size = 18 if len(items) <= 5 and all(len(plain(b)) <= 60 for b in items) else 15 if len(items) <= 7 else 13
            y0 = body_y if not (s.get("table") or s.get("chart")) else body_y + body_h - 1200000
            h0 = body_h if not (s.get("table") or s.get("chart")) else 1200000
            if s.get("table") or s.get("chart"):
                size = 12
            shapes.append(_textbox(sid, "body", 685800, y0, SLIDE_W - 1371600, h0, [_para(_runs(b, size, INK), bullet=True) for b in items])); sid += 1
        foot = _clean_xml_text(s.get("foot") or "")
        shapes.append(_textbox(sid, "footer", 685800, SLIDE_H - 520000, SLIDE_W - 2400000, 380000,
                               [_para(_runs(foot or footer, 10, MUTED), space_after=0)], "b")); sid += 1
        shapes.append(_textbox(sid, "pageno", SLIDE_W - 1500000, SLIDE_H - 520000, 900000, 380000,
                               [_para(_runs(f"{k} / {total}", 10, MUTED), align="r", space_after=0)], "b")); sid += 1
        slide_xmls.append(f'{_XMLDECL}<p:sld {_P}><p:cSld><p:spTree>{_sp_tree_head()}{"".join(shapes)}</p:spTree></p:cSld>'
                          '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
        notes_in.append(str(s.get("notes") or ""))
    parts: dict[str, bytes | str] = {}
    ct = [f'{_XMLDECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
          '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
          '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
          '<Override PartName="/ppt/notesMasters/notesMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml"/>'
          '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
          '<Override PartName="/ppt/theme/theme2.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>']
    pres_rels = [("rId1", f"{_NS_OREL}/slideMaster", "slideMasters/slideMaster1.xml"),
                 ("rId2", f"{_NS_OREL}/notesMaster", "notesMasters/notesMaster1.xml"),
                 ("rId3", f"{_NS_OREL}/theme", "theme/theme1.xml")]
    sld_ids = []
    for i, sx in enumerate(slide_xmls, 1):
        parts[f"ppt/slides/slide{i}.xml"] = sx
        parts[f"ppt/slides/_rels/slide{i}.xml.rels"] = _rels([("rId1", f"{_NS_OREL}/slideLayout", "../slideLayouts/slideLayout1.xml"),
                                                              ("rId2", f"{_NS_OREL}/notesSlide", f"../notesSlides/notesSlide{i}.xml")])
        notes_txt = _clean_xml_text(notes_in[i - 1])
        parts[f"ppt/notesSlides/notesSlide{i}.xml"] = (
            f'{_XMLDECL}<p:notes {_P}><p:cSld><p:spTree>{_sp_tree_head()}'
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image"/><p:cNvSpPr><a:spLocks noGrp="1" noRot="1" noChangeAspect="1"/></p:cNvSpPr><p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr><p:spPr/></p:sp>'
            '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/>{"".join(_para(_runs(ln, 12, INK), space_after=300) for ln in (notes_txt.split(chr(10)) if notes_txt else [""]))}</p:txBody></p:sp>'
            '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>')
        parts[f"ppt/notesSlides/_rels/notesSlide{i}.xml.rels"] = _rels([("rId1", f"{_NS_OREL}/notesMaster", "../notesMasters/notesMaster1.xml"),
                                                                        ("rId2", f"{_NS_OREL}/slide", f"../slides/slide{i}.xml")])
        ct.append(f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')
        ct.append(f'<Override PartName="/ppt/notesSlides/notesSlide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>')
        pres_rels.append((f"rId{10 + i}", f"{_NS_OREL}/slide", f"slides/slide{i}.xml"))
        sld_ids.append(f'<p:sldId id="{255 + i}" r:id="rId{10 + i}"/>')
    ct.append("</Types>")
    clrmap = '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
    parts.update({
        "[Content_Types].xml": "".join(ct),
        "_rels/.rels": _rels([("rId1", f"{_NS_OREL}/officeDocument", "ppt/presentation.xml")]),
        "ppt/presentation.xml": (f'{_XMLDECL}<p:presentation {_P} saveSubsetFonts="1">'
                                 '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                                 '<p:notesMasterIdLst><p:notesMasterId r:id="rId2"/></p:notesMasterIdLst>'
                                 f'<p:sldIdLst>{"".join(sld_ids)}</p:sldIdLst>'
                                 f'<p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/><p:notesSz cx="6858000" cy="9144000"/>'
                                 '<p:defaultTextStyle><a:defPPr><a:defRPr lang="ja-JP"/></a:defPPr></p:defaultTextStyle></p:presentation>'),
        "ppt/_rels/presentation.xml.rels": _rels(pres_rels),
        "ppt/slideMasters/slideMaster1.xml": (f'{_XMLDECL}<p:sldMaster {_P}><p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
                                             f'<p:spTree>{_sp_tree_head()}</p:spTree></p:cSld>{clrmap}'
                                             '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
                                             '<p:txStyles><p:titleStyle><a:lvl1pPr><a:defRPr sz="2800"/></a:lvl1pPr></p:titleStyle>'
                                             '<p:bodyStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:bodyStyle>'
                                             '<p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle></p:txStyles></p:sldMaster>'),
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": _rels([("rId1", f"{_NS_OREL}/slideLayout", "../slideLayouts/slideLayout1.xml"),
                                                               ("rId2", f"{_NS_OREL}/theme", "../theme/theme1.xml")]),
        "ppt/slideLayouts/slideLayout1.xml": (f'{_XMLDECL}<p:sldLayout {_P} type="blank" preserve="1"><p:cSld name="Blank"><p:spTree>{_sp_tree_head()}</p:spTree></p:cSld>'
                                             '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _rels([("rId1", f"{_NS_OREL}/slideMaster", "../slideMasters/slideMaster1.xml")]),
        "ppt/notesMasters/notesMaster1.xml": (f'{_XMLDECL}<p:notesMaster {_P}><p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
                                             f'<p:spTree>{_sp_tree_head()}</p:spTree></p:cSld>{clrmap}'
                                             '<p:notesStyle><a:lvl1pPr><a:defRPr sz="1200"/></a:lvl1pPr></p:notesStyle></p:notesMaster>'),
        "ppt/notesMasters/_rels/notesMaster1.xml.rels": _rels([("rId1", f"{_NS_OREL}/theme", "../theme/theme2.xml")]),
        "ppt/theme/theme1.xml": _theme_xml("Prism"),
        "ppt/theme/theme2.xml": _theme_xml("Prism Notes"),
    })
    return _zip(parts)


# ------------------------------------------------------------------ PDF（自前ライター・TrueType サブセット埋め込み）

FONT_CANDIDATES = [
    # Windows
    r"C:\Windows\Fonts\YuGothM.ttc", r"C:\Windows\Fonts\YuGothR.ttc", r"C:\Windows\Fonts\meiryo.ttc", r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\BIZ-UDGothicR.ttc", r"C:\Windows\Fonts\yugothic.ttf",
    # Linux
    "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf", "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/takao-gothic/TakaoPGothic.ttf", "/usr/share/fonts/truetype/vlgothic/VL-PGothic-Regular.ttf",
    # macOS（TrueType 形式のもの）
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


def find_jp_font(preferred: str | None = None) -> str | None:
    """日本語を含む TrueType（glyf）フォントを探す。環境変数 PRISM_PDF_FONT / 設定値を優先。"""
    cands = [p for p in (preferred, os.environ.get("PRISM_PDF_FONT")) if p] + FONT_CANDIDATES
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        cands += [os.path.join(windir, "Fonts", n) for n in ("YuGothM.ttc", "meiryo.ttc", "msgothic.ttc")]
    for p in cands:
        try:
            if os.path.isfile(p) and TTFont.is_truetype(p):
                return p
        except Exception:
            continue
    return None


class TTFont:
    """TrueType（.ttf / .ttc の先頭フォント）の必要最小限のパーサとサブセッタ。"""

    @staticmethod
    def is_truetype(path: str) -> bool:
        with open(path, "rb") as f:
            head = f.read(12)
        if head[:4] == b"ttcf":
            return True   # 中身は __init__ で glyf の有無を確認
        return head[:4] in (b"\x00\x01\x00\x00", b"true")

    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        off = 0
        if d[:4] == b"ttcf":
            off = struct.unpack(">I", d[12:16])[0]   # 先頭フォント
        ntab = struct.unpack(">H", d[off + 4:off + 6])[0]
        self.tables: dict[str, tuple[int, int]] = {}
        for i in range(ntab):
            rec = d[off + 12 + 16 * i: off + 28 + 16 * i]
            tag = rec[:4].decode("latin-1")
            toff, tlen = struct.unpack(">II", rec[8:16])
            self.tables[tag] = (toff, tlen)
        if "glyf" not in self.tables or "loca" not in self.tables:
            raise ValueError("glyf 形式の TrueType ではありません（CFF/OpenType は未対応）")
        head = self._t("head")
        self.units_per_em = struct.unpack(">H", head[18:20])[0] or 1000
        self.bbox = struct.unpack(">hhhh", head[36:44])
        self.index_to_loc = struct.unpack(">h", head[50:52])[0]
        hhea = self._t("hhea")
        self.ascender, self.descender = struct.unpack(">hh", hhea[4:8])
        self.num_hmetrics = struct.unpack(">H", hhea[34:36])[0]
        self.num_glyphs = struct.unpack(">H", self._t("maxp")[4:6])[0]
        hmtx = self._t("hmtx")
        self.advances = [struct.unpack(">H", hmtx[4 * i:4 * i + 2])[0] for i in range(self.num_hmetrics)]
        loca = self._t("loca")
        if self.index_to_loc == 0:
            self.loca = [2 * v for v in struct.unpack(">%dH" % (len(loca) // 2), loca[:len(loca) // 2 * 2])]
        else:
            self.loca = list(struct.unpack(">%dI" % (len(loca) // 4), loca[:len(loca) // 4 * 4]))
        self.glyf = self._t("glyf")
        self.cmap = self._parse_cmap()
        if "OS/2" in self.tables:
            os2 = self._t("OS/2")
            if len(os2) >= 90:
                self.cap_height = struct.unpack(">h", os2[88:90])[0]
            else:
                self.cap_height = int(self.ascender * 0.7)
        else:
            self.cap_height = int(self.ascender * 0.7)

    def _t(self, tag: str) -> bytes:
        o, n = self.tables[tag]
        return self.data[o:o + n]

    def _parse_cmap(self) -> dict[int, int]:
        cm = self._t("cmap")
        n = struct.unpack(">H", cm[2:4])[0]
        best = None
        for i in range(n):
            pid, eid, off = struct.unpack(">HHI", cm[4 + 8 * i: 12 + 8 * i])
            fmt = struct.unpack(">H", cm[off:off + 2])[0]
            score = {(3, 10): 5, (0, 4): 4, (3, 1): 3, (0, 3): 2, (0, 6): 1}.get((pid, eid), 0)
            if fmt in (4, 12) and (best is None or score > best[0]):
                best = (score, off, fmt)
        mp: dict[int, int] = {}
        if not best:
            return mp
        _, off, fmt = best
        if fmt == 4:
            segx2 = struct.unpack(">H", cm[off + 6:off + 8])[0]
            seg = segx2 // 2
            ends = struct.unpack(">%dH" % seg, cm[off + 14: off + 14 + segx2])
            starts = struct.unpack(">%dH" % seg, cm[off + 16 + segx2: off + 16 + 2 * segx2])
            deltas = struct.unpack(">%dh" % seg, cm[off + 16 + 2 * segx2: off + 16 + 3 * segx2])
            rng_off_pos = off + 16 + 3 * segx2
            rng = struct.unpack(">%dH" % seg, cm[rng_off_pos: rng_off_pos + segx2])
            for i in range(seg):
                s, e = starts[i], ends[i]
                if s == 0xFFFF:
                    continue
                for c in range(s, min(e, 0xFFFE) + 1):
                    if rng[i] == 0:
                        g = (c + deltas[i]) & 0xFFFF
                    else:
                        p = rng_off_pos + 2 * i + rng[i] + 2 * (c - s)
                        if p + 2 > len(cm):
                            continue
                        g = struct.unpack(">H", cm[p:p + 2])[0]
                        if g:
                            g = (g + deltas[i]) & 0xFFFF
                    if g:
                        mp[c] = g
        else:
            ngroups = struct.unpack(">I", cm[off + 12:off + 16])[0]
            for i in range(ngroups):
                s, e, g = struct.unpack(">III", cm[off + 16 + 12 * i: off + 28 + 12 * i])
                for c in range(s, min(e, s + 65535) + 1):
                    mp[c] = g + (c - s)
        return mp

    def gid(self, ch: str) -> int:
        return self.cmap.get(ord(ch), 0)

    def advance(self, g: int) -> int:
        if g < self.num_hmetrics:
            return self.advances[g]
        return self.advances[-1] if self.advances else self.units_per_em

    def _glyph_bytes(self, g: int) -> bytes:
        if g + 1 >= len(self.loca):
            return b""
        return self.glyf[self.loca[g]:self.loca[g + 1]]

    def _components(self, gb: bytes) -> list[int]:
        """複合グリフの構成要素 gid。"""
        if len(gb) < 10 or struct.unpack(">h", gb[:2])[0] >= 0:
            return []
        out, p = [], 10
        while True:
            flags, gi = struct.unpack(">HH", gb[p:p + 4]); p += 4
            out.append(gi)
            p += 4 if flags & 0x0001 else 2            # ARG_1_AND_2_ARE_WORDS
            if flags & 0x0008: p += 2                  # WE_HAVE_A_SCALE
            elif flags & 0x0040: p += 4                # WE_HAVE_AN_X_AND_Y_SCALE
            elif flags & 0x0080: p += 8                # WE_HAVE_A_TWO_BY_TWO
            if not flags & 0x0020:                     # MORE_COMPONENTS
                break
        return out

    def subset(self, chars: set[str]) -> tuple[bytes, dict[int, int]]:
        """使う文字だけを含む TrueType を作る。戻り値 (フォントバイト列, 元gid→新gid)。CIDToGIDMap は Identity 前提
        （PDF 側では新 gid をそのまま CID として使う）。"""
        gids: set[int] = {0}
        for ch in chars:
            gids.add(self.gid(ch))
        stack = list(gids)
        while stack:   # 複合グリフの依存を閉じる
            g = stack.pop()
            for c in self._components(self._glyph_bytes(g)):
                if c not in gids:
                    gids.add(c); stack.append(c)
        order = sorted(gids)
        new_of = {g: i for i, g in enumerate(order)}
        glyf_parts: list[bytes] = []
        loca: list[int] = [0]
        pos = 0
        for g in order:
            gb = bytearray(self._glyph_bytes(g))
            if len(gb) >= 10 and struct.unpack(">h", gb[:2])[0] < 0:   # 複合: 構成要素の gid を付け替え
                p = 10
                while True:
                    flags, gi = struct.unpack(">HH", gb[p:p + 4])
                    struct.pack_into(">H", gb, p + 2, new_of.get(gi, 0)); p += 4
                    p += 4 if flags & 0x0001 else 2
                    if flags & 0x0008: p += 2
                    elif flags & 0x0040: p += 4
                    elif flags & 0x0080: p += 8
                    if not flags & 0x0020:
                        break
            if len(gb) % 4:
                gb += b"\x00" * (4 - len(gb) % 4)
            glyf_parts.append(bytes(gb)); pos += len(gb); loca.append(pos)
        glyf = b"".join(glyf_parts)
        loca_b = struct.pack(">%dI" % len(loca), *loca)
        hmtx = b"".join(struct.pack(">Hh", self.advance(g), 0) for g in order)
        head = bytearray(self._t("head")); struct.pack_into(">h", head, 50, 1); struct.pack_into(">I", head, 8, 0)
        hhea = bytearray(self._t("hhea")); struct.pack_into(">H", hhea, 34, len(order))
        maxp = bytearray(self._t("maxp")); struct.pack_into(">H", maxp, 4, len(order))
        tables: list[tuple[str, bytes]] = [("head", bytes(head)), ("hhea", bytes(hhea)), ("maxp", bytes(maxp)),
                                           ("loca", loca_b), ("glyf", glyf), ("hmtx", hmtx)]
        for tag in ("cvt ", "fpgm", "prep"):
            if tag in self.tables:
                tables.append((tag, self._t(tag)))
        tables.sort(key=lambda t: t[0])
        n = len(tables)
        es = 0
        while (1 << (es + 1)) <= n:
            es += 1
        out = bytearray(struct.pack(">IHHHH", 0x00010000, n, (1 << es) * 16, es, n * 16 - (1 << es) * 16))
        off = 12 + 16 * n
        body = bytearray()
        for tag, tb in tables:
            pad = tb + b"\x00" * ((4 - len(tb) % 4) % 4)
            csum = sum(struct.unpack(">%dI" % (len(pad) // 4), pad)) & 0xFFFFFFFF
            out += tag.encode("latin-1") + struct.pack(">III", csum, off + len(body), len(tb))
            body += pad
        return bytes(out + body), new_of


class _PDF:
    """最小限の PDF オブジェクト書き出し。"""

    def __init__(self):
        self.objs: list[bytes | None] = []

    def reserve(self) -> int:
        self.objs.append(None)
        return len(self.objs)

    def set(self, num: int, body: bytes) -> None:
        self.objs[num - 1] = body

    def add(self, body: bytes) -> int:
        self.objs.append(body)
        return len(self.objs)

    @staticmethod
    def stream(dict_items: str, data: bytes, compress: bool = True) -> bytes:
        if compress:
            data = zlib.compress(data, 9)
            dict_items += " /Filter /FlateDecode"
        return f"<< {dict_items} /Length {len(data)} >>\nstream\n".encode("latin-1") + data + b"\nendstream"

    def build(self, root: int, info: int | None = None) -> bytes:
        out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        offs = []
        for i, body in enumerate(self.objs, 1):
            offs.append(len(out))
            out += f"{i} 0 obj\n".encode("latin-1") + (body or b"null") + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.objs) + 1}\n0000000000 65535 f \n".encode("latin-1")
        for o in offs:
            out += f"{o:010d} 00000 n \n".encode("latin-1")
        out += f"trailer\n<< /Size {len(self.objs) + 1} /Root {root} 0 R".encode("latin-1")
        if info:
            out += f" /Info {info} 0 R".encode("latin-1")
        out += f" >>\nstartxref\n{xref}\n%%EOF\n".encode("latin-1")
        return bytes(out)


def _pdf_str(s: str) -> str:
    """PDF の文字列リテラル（UTF-16BE BOM つき）。"""
    b = "\xfe\xff".encode("latin-1") + s.encode("utf-16-be")
    return "<" + b.hex() + ">"


def _uri_str(u: str) -> str:
    return "(" + u.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


class _Layout:
    """A4 縦・1段組みの簡易レイアウト。テキストはグリフ幅で折り返す。"""
    PW, PH = 595.28, 841.89
    ML, MR, MT, MB = 56.0, 56.0, 64.0, 60.0

    def __init__(self, font: TTFont):
        self.font = font
        self.pages: list[list[str]] = []
        self.links: list[list[tuple[float, float, float, float, str]]] = []
        self.chars: set[str] = set()
        self.y = 0.0
        self._new_page()

    # -- 低レベル
    def _new_page(self):
        self.pages.append([]); self.links.append([])
        self.y = self.PH - self.MT

    @property
    def ops(self) -> list[str]:
        return self.pages[-1]

    def width(self, text: str, size: float) -> float:
        f = self.font
        return sum(f.advance(f.gid(c)) for c in text) * size / f.units_per_em

    def _hex(self, text: str) -> str:
        self.chars.update(text)
        return "<" + "".join("%04X" % self.font.gid(c) for c in text) + ">"

    def _ensure(self, h: float):
        if self.y - h < self.MB:
            self._new_page()

    def text(self, x: float, y: float, text: str, size: float, color: str = "0 0 0", bold: bool = False):
        if not text:
            return
        ops = self.ops
        ops.append(f"BT /F1 {size:.1f} Tf {color} rg {x:.2f} {y:.2f} Td")
        if bold:
            ops.append(f"2 Tr {max(0.25, size * 0.028):.2f} w {color} RG")
        ops.append(f"{self._hex(text)} Tj")
        if bold:
            ops.append("0 Tr")
        ops.append("ET")

    def wrap(self, text: str, size: float, maxw: float) -> list[list[tuple[str, str]]]:
        """インライン記法つき文字列を行に分ける。各行は (種類, 文字列) の並び。"""
        lines: list[list[tuple[str, str]]] = [[]]
        cur = 0.0
        for kind, part in split_inline(text):
            sz = size * (0.7 if kind == "cite" else 1.0)
            # ASCII の単語は切らずに、それ以外は1文字ずつ
            tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-.,%]*\s*|\s+|.", part)
            buf = ""
            for tok in tokens:
                w = self.width(tok, sz)
                if cur + w > maxw and (buf or lines[-1]):
                    if buf:
                        lines[-1].append((kind, buf)); buf = ""
                    lines.append([]); cur = 0.0
                    if tok.isspace():
                        continue
                buf += tok; cur += w
            if buf:
                lines[-1].append((kind, buf))
        return lines

    def _draw_line(self, x: float, y: float, segs: list[tuple[str, str]], size: float, color: str):
        cx = x
        for kind, s in segs:
            if kind == "cite":
                self.text(cx, y + size * 0.35, s, size * 0.7, "0.17 0.36 0.56")
                cx += self.width(s, size * 0.7)
            else:
                self.text(cx, y, s, size, color, bold=(kind == "bold"))
                cx += self.width(s, size)

    # -- ブロック
    def paragraph(self, text: str, size: float = 10.5, color: str = "0.09 0.13 0.18", indent: float = 0.0,
                  bullet: str | None = None, after: float = 4.0, lead: float = 1.55):
        maxw = self.PW - self.ML - self.MR - indent - (12 if bullet else 0)
        lines = self.wrap(text, size, maxw)
        lh = size * lead
        for i, segs in enumerate(lines):
            self._ensure(lh)
            self.y -= lh
            x = self.ML + indent
            if bullet and i == 0:
                self.text(x, self.y, bullet, size, color)
            self._draw_line(x + (12 if bullet else 0), self.y, segs, size, color)
        self.y -= after

    def heading(self, text: str, level: int):
        size = {1: 15.5, 2: 13.5, 3: 11.5}.get(level, 11.5)
        color = {1: "0.09 0.13 0.18", 2: "0.17 0.36 0.56", 3: "0.09 0.13 0.18"}[min(3, max(1, level))]
        self._ensure(size * 3)
        self.y -= size * 0.9
        self.paragraph("**" + plain(text) + "**", size, color, after=3.0, lead=1.4)
        if level <= 2:
            self.ops.append(f"0.86 0.88 0.91 RG 0.6 w {self.ML:.2f} {self.y + 1:.2f} m {self.PW - self.MR:.2f} {self.y + 1:.2f} l S")
            self.y -= 3

    def rule(self):
        self._ensure(10); self.y -= 5
        self.ops.append(f"0.73 0.73 0.73 RG 0.5 w {self.ML:.2f} {self.y:.2f} m {self.PW - self.MR:.2f} {self.y:.2f} l S")
        self.y -= 5

    def table(self, header: list[str], rows: list[list[str]], size: float = 9.0):
        n = max(1, len(header))
        total_w = self.PW - self.ML - self.MR
        # 列幅: 見出しと内容の最大長（上限つき）に比例
        need = []
        for ci in range(n):
            cells = [plain(header[ci] if ci < len(header) else "")] + [plain(r[ci]) if ci < len(r) else "" for r in rows]
            need.append(max(self.width(c[:28], size) for c in cells) + 8)
        s = sum(need) or 1
        widths = [max(36.0, total_w * w / s) for w in need]
        k = total_w / sum(widths)
        widths = [w * k for w in widths]
        lh = size * 1.4
        pad = 3.0

        def row_lines(cells):
            return [self.wrap(c, size, widths[i] - 2 * pad) for i, c in enumerate(cells)]

        def draw_row(cells, head=False):
            ls = row_lines(cells)
            rh = max(len(x) for x in ls) * lh + 2 * pad
            self._ensure(rh)
            y_top = self.y
            x = self.ML
            if head:
                self.ops.append(f"0.87 0.90 0.94 rg {x:.2f} {y_top - rh:.2f} {total_w:.2f} {rh:.2f} re f")
            for i, lines in enumerate(ls):
                yy = y_top - pad
                for segs in lines:
                    yy -= lh
                    self._draw_line(x + pad, yy + lh * 0.28, [("bold" if head else kd, t) for kd, t in segs], size, "0.09 0.13 0.18")
                self.ops.append(f"0.73 0.73 0.73 RG 0.4 w {x:.2f} {y_top - rh:.2f} {widths[i]:.2f} {rh:.2f} re S")
                x += widths[i]
            self.y -= rh

        self.y -= 4
        draw_row([(h or "") for h in (list(header) + [""] * n)[:n]], True)
        for r in rows:
            draw_row((list(r) + [""] * n)[:n])
        self.y -= 6

    def source(self, s: dict, size: float = 8.5):
        head = f"[{s.get('n')}] {s.get('published') or ''} {s.get('source') or ''}｜"
        title = str(s.get("title") or s.get("link") or "")
        link = str(s.get("link") or "")
        lines = self.wrap(head + title, size, self.PW - self.ML - self.MR)
        lh = size * 1.45
        for i, segs in enumerate(lines):
            self._ensure(lh); self.y -= lh
            self._draw_line(self.ML, self.y, [("text", t) for _, t in segs], size, "0.27 0.27 0.27")
            if link.startswith(("http://", "https://")):
                w = sum(self.width(t, size) for _, t in segs)
                self.links[-1].append((self.ML, self.y - 2, self.ML + w, self.y + size, link))
        if link.startswith(("http://", "https://")):
            for segs in self.wrap(link, size * 0.9, self.PW - self.ML - self.MR):
                self._ensure(lh); self.y -= lh * 0.9
                self._draw_line(self.ML + 14, self.y, [("text", t) for _, t in segs], size * 0.9, "0.5 0.5 0.5")
        self.y -= 2


def build_pdf(doc: dict, font_path: str | None = None) -> bytes:
    """文書モデル → PDF。日本語 TrueType フォントをサブセット化して埋め込む（Identity-H / CIDFontType2）。
    フォントが見つからなければ ValueError。"""
    path = font_path or find_jp_font()
    if not path:
        raise ValueError("日本語の TrueType フォントが見つかりません（Windows なら Yu Gothic / Meiryo、Linux なら IPA ゴシック等。"
                         "設定 PRISM_PDF_FONT でパスを指定できます）")
    font = TTFont(path)
    L = _Layout(font)
    title = doc.get("title") or "レポート"
    L.paragraph("**" + plain(title) + "**", 17, "0.09 0.13 0.18", after=2, lead=1.3)
    if doc.get("subtitle"):
        L.paragraph(doc["subtitle"], 11, "0.3 0.3 0.3", after=2)
    for m in doc.get("meta") or []:
        L.paragraph(m, 8.5, "0.43 0.43 0.43", after=1, lead=1.4)
    L.rule()
    for b in doc.get("blocks") or []:
        t = b.get("t")
        if t == "h":
            L.heading(b.get("text", ""), int(b.get("level") or 1))
        elif t == "p":
            L.paragraph(b.get("text", ""))
        elif t == "ul":
            for it in b.get("items") or []:
                L.paragraph(it, indent=8, bullet="・", after=2)
            L.y -= 3
        elif t == "ol":
            for i, it in enumerate(b.get("items") or [], 1):
                L.paragraph(it, indent=8, bullet=f"{i}.", after=2)
            L.y -= 3
        elif t == "table":
            L.table(b.get("header") or [], b.get("rows") or [])
        elif t == "hr":
            L.rule()
    sources = doc.get("sources") or []
    if sources:
        L.heading("出典", 2)
        for s in sources:
            L.source(s)
    # ヘッダー・フッター（総ページ数が決まってから）
    header = plain(doc.get("header") or title)
    n_pages = len(L.pages)
    for i, ops in enumerate(L.pages, 1):
        hw = L.width(header[:60], 8)
        L.pages[i - 1] = [f"BT /F1 8 Tf 0.53 0.53 0.53 rg {L.PW - L.MR - hw:.2f} {L.PH - 36:.2f} Td {L._hex(header[:60])} Tj ET",
                          f"0.86 0.88 0.91 RG 0.5 w {L.ML:.2f} {L.PH - 42:.2f} m {L.PW - L.MR:.2f} {L.PH - 42:.2f} l S"] + ops
        foot = (plain(doc.get("footer") or "") + "　" if doc.get("footer") else "") + f"{i} / {n_pages}"
        fw = L.width(foot, 8)
        L.pages[i - 1].append(f"BT /F1 8 Tf 0.53 0.53 0.53 rg {(L.PW - fw) / 2:.2f} {34:.2f} Td {L._hex(foot)} Tj ET")
    # フォント（サブセット）
    sub, new_of = font.subset(L.chars)
    gid_map = {font.gid(c): new_of.get(font.gid(c), 0) for c in L.chars}
    scale = 1000.0 / font.units_per_em
    widths = sorted({new_of[g]: int(round(font.advance(g) * scale)) for g in new_of}.items())
    w_arr = " ".join(f"{g} [{w}]" for g, w in widths)
    # 内容ストリームは新しい gid で書き直す（レイアウト時は元 gid の hex を使ったので置換）
    def remap(op: str) -> str:
        return re.sub(r"<([0-9A-F]+)>", lambda m: "<" + "".join("%04X" % new_of.get(int(m.group(1)[i:i + 4], 16), 0)
                                                                for i in range(0, len(m.group(1)), 4)) + ">", op)
    pdf = _PDF()
    catalog = pdf.reserve(); pages_obj = pdf.reserve(); font_obj = pdf.reserve()
    ff = pdf.add(_PDF.stream(f"/Length1 {len(sub)}", sub))
    bbox = [int(round(v * scale)) for v in font.bbox]
    fd = pdf.add((f"<< /Type /FontDescriptor /FontName /PRISMJP+Subset /Flags 4 /FontBBox [{bbox[0]} {bbox[1]} {bbox[2]} {bbox[3]}] "
                  f"/ItalicAngle 0 /Ascent {int(round(font.ascender * scale))} /Descent {int(round(font.descender * scale))} "
                  f"/CapHeight {int(round(font.cap_height * scale))} /StemV 80 /FontFile2 {ff} 0 R >>").encode("latin-1"))
    cid = pdf.add((f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /PRISMJP+Subset /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                   f"/FontDescriptor {fd} 0 R /DW 1000 /W [{w_arr}] /CIDToGIDMap /Identity >>").encode("latin-1"))
    # ToUnicode（コピー＆検索用）
    pairs = sorted((new_of[font.gid(c)], c) for c in L.chars if font.gid(c) in new_of)
    bf = "".join(f"<{g:04X}> <{''.join('%04X' % u for u in struct.unpack('>%dH' % (len(c.encode('utf-16-be')) // 2), c.encode('utf-16-be')))}>\n" for g, c in pairs)
    tounicode = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def "
                 "/CMapName /Adobe-Identity-UCS def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange\n")
    chunks = [pairs[i:i + 100] for i in range(0, len(pairs), 100)]
    for ch in chunks:
        tounicode += f"{len(ch)} beginbfchar\n" + "".join(f"<{g:04X}> <{c.encode('utf-16-be').hex().upper()}>\n" for g, c in ch) + "endbfchar\n"
    tounicode += "endcmap CMapName currentdict /CMap defineresource pop end end"
    tu = pdf.add(_PDF.stream("", tounicode.encode("latin-1")))
    pdf.set(font_obj, (f"<< /Type /Font /Subtype /Type0 /BaseFont /PRISMJP+Subset /Encoding /Identity-H /DescendantFonts [{cid} 0 R] /ToUnicode {tu} 0 R >>").encode("latin-1"))
    page_ids = []
    for ops, links in zip(L.pages, L.links):
        content = pdf.add(_PDF.stream("", "\n".join(remap(o) for o in ops).encode("latin-1")))
        annots = ""
        if links:
            ids = []
            for (x0, y0, x1, y1, url) in links:
                ids.append(pdf.add((f"<< /Type /Annot /Subtype /Link /Rect [{x0:.2f} {y0:.2f} {x1:.2f} {y1:.2f}] /Border [0 0 0] "
                                    f"/A << /S /URI /URI {_uri_str(url)} >> >>").encode("latin-1")))
            annots = " /Annots [" + " ".join(f"{i} 0 R" for i in ids) + "]"
        page_ids.append(pdf.add((f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {L.PW:.2f} {L.PH:.2f}] "
                                 f"/Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content} 0 R{annots} >>").encode("latin-1")))
    pdf.set(pages_obj, (f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(page_ids)} >>").encode("latin-1"))
    pdf.set(catalog, f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode("latin-1"))
    info = pdf.add((f"<< /Title {_pdf_str(title)} /Producer (Prism docgen) /CreationDate (D:{datetime.now().strftime('%Y%m%d%H%M%S')}) >>").encode("latin-1"))
    return pdf.build(catalog, info)
