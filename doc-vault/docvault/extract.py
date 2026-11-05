"""文書からのテキスト抽出（構造と位置情報つき）。

- PDF : PyMuPDF（あれば。最も高精度）→ pypdf（純 Python）の順に使う。ページ単位
- DOCX: 標準ライブラリ（zip + XML）。見出しスタイルで節に分け、表は「 | 」区切りの行に
- PPTX: 標準ライブラリ。スライド単位（タイトル・本文・表・ノート）
- XLSX: 標準ライブラリ。シートごとに見出し行を検出し「列名: 値」の行に変換
- TXT / MD / CSV / HTML: 文字コード自動判定（UTF-8 / CP932 / EUC-JP）

戻り値は {"title", "kind", "pages", "sections": [{locator, heading, page, text}], "warnings"}。
locator は回答の出典表示（p.3 / スライド2 / Sheet1!行2-31 / 見出し名）に使う。
"""
from __future__ import annotations

import csv
import html
import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from .textutil import normalize_text

SUPPORTED = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".txt", ".md", ".markdown",
             ".csv", ".tsv", ".html", ".htm", ".json", ".log"}
MAX_SHEET_ROWS = 5000
ROWS_PER_SECTION = 30

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "dc": "http://purl.org/dc/elements/1.1/",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
}


class ExtractError(ValueError):
    """抽出できない（未対応形式・破損・依存ライブラリ不足）。UI に表示できる日本語。"""


def _q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def extract(data: bytes, filename: str) -> dict:
    ext = PurePosixPath(filename.lower()).suffix
    if ext not in SUPPORTED:
        raise ExtractError(f"未対応のファイル形式です: {ext or '(拡張子なし)'}"
                           "（PDF / Word / PowerPoint / Excel / テキスト / Markdown / CSV / HTML）")
    stem = PurePosixPath(filename).stem
    try:
        if ext == ".pdf":
            out = _pdf(data)
        elif ext == ".docx":
            out = _docx(data)
        elif ext == ".pptx":
            out = _pptx(data)
        elif ext in (".xlsx", ".xlsm"):
            out = _xlsx(data)
        elif ext in (".csv", ".tsv"):
            out = _csv(data, "\t" if ext == ".tsv" else ",")
        elif ext in (".html", ".htm"):
            out = _html(data)
        elif ext in (".md", ".markdown"):
            out = _markdown(decode_text(data))
        else:
            out = _plain(decode_text(data))
    except ExtractError:
        raise
    except zipfile.BadZipFile as e:
        raise ExtractError(f"ファイルが壊れているか、暗号化（パスワード保護）されています: {e}") from e
    except ET.ParseError as e:
        raise ExtractError(f"文書の XML を解析できませんでした: {e}") from e
    out["kind"] = ext.lstrip(".")
    out["title"] = (out.get("title") or "").strip()[:200] or stem
    for sec in out["sections"]:
        sec["text"] = normalize_text(sec.get("text", ""))
        sec["heading"] = normalize_text(sec.get("heading") or "")[:120]
    out["sections"] = [s for s in out["sections"] if s["text"]]
    out.setdefault("warnings", [])
    if not out["sections"]:
        hint = "（スキャン画像の PDF は OCR が必要です）" if ext == ".pdf" else ""
        raise ExtractError(f"テキストを抽出できませんでした{hint}")
    return out


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp932", "euc-jp"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- PDF

def _pdf(data: bytes) -> dict:
    pages: list[str] = []
    title = ""
    warnings: list[str] = []
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass and not doc.authenticate(""):
            raise ExtractError("パスワード保護された PDF は取り込めません")
        title = (doc.metadata or {}).get("title") or ""
        for page in doc:
            pages.append(page.get_text("text", sort=True) or "")
    except ImportError:
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise ExtractError("PDF の取り込みには pypdf が必要です: pip install pypdf"
                               "（より高精度にするなら pip install pymupdf）") from e
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ExtractError("パスワード保護された PDF は取り込めません")
            meta = reader.metadata
            title = (getattr(meta, "title", None) or "") if meta else ""
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception as e:  # noqa: BLE001 - 1ページの失敗で全体を止めない
                    warnings.append(f"p.{len(pages) + 1} の抽出に失敗: {e}")
                    pages.append("")
        except ExtractError:
            raise
        except Exception as e:  # noqa: BLE001 - pypdf は多様な例外を投げる
            raise ExtractError(f"PDF を読み込めませんでした: {e}") from e
    sections = []
    empty = []
    for i, text in enumerate(pages, 1):
        text = _fix_pdf_text(text)
        if len(text.strip()) < 15:
            empty.append(i)
            continue
        sections.append({"locator": f"p.{i}", "page": i, "heading": "", "text": text})
    if empty and sections:
        shown = ", ".join(str(p) for p in empty[:10]) + ("…" if len(empty) > 10 else "")
        warnings.append(f"{len(empty)} ページで文字が取れませんでした（p.{shown}。図・スキャン画像の可能性）")
    if not title and sections:
        first = next((ln.strip() for ln in sections[0]["text"].splitlines() if ln.strip()), "")
        title = first[:80]
    return {"title": title, "pages": len(pages), "sections": sections, "warnings": warnings}


def _fix_pdf_text(text: str) -> str:
    """PDF 抽出特有の崩れを軽く補正（行末ハイフン連結・日本語の不要な改行）。"""
    text = re.sub(r"-\n(?=[a-z])", "", text)
    # 日本語の行の途中改行（句点で終わらない行）を連結。見出しや箇条書きっぽい行は残す
    lines = text.split("\n")
    out: list[str] = []
    for ln in lines:
        s = ln.rstrip()
        if out and out[-1] and s and re.search(r"[぀-ヿ㐀-鿿、]$", out[-1]) \
                and not re.match(r"^\s*([・●○■□◆◇\-\*]|\d+[\.\)．）]|第\d)", s) \
                and len(out[-1]) > 20:
            out[-1] += s.strip()
        else:
            out.append(s)
    return "\n".join(out)


# ---------------------------------------------------------------- DOCX

def _docx(data: bytes) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(data))
    try:
        root = ET.fromstring(zf.read("word/document.xml"))
    except KeyError as e:
        raise ExtractError("Word 文書の本文（word/document.xml）が見つかりません") from e
    heading_styles = _docx_heading_styles(zf)
    title = _core_title(zf)
    body = root.find(_q("w", "body"))
    blocks: list[tuple[int, str]] = []          # (見出しレベル 0=本文, テキスト)
    for el in (body if body is not None else []):
        tag = el.tag
        if tag == _q("w", "p"):
            text, level = _docx_para(el, heading_styles)
            if text.strip():
                blocks.append((level, text))
        elif tag == _q("w", "tbl"):
            blocks.append((0, _docx_table(el)))
        elif tag == _q("w", "sdt"):
            for p in el.iter(_q("w", "p")):
                text, level = _docx_para(p, heading_styles)
                if text.strip():
                    blocks.append((level, text))
    sections: list[dict] = []
    path: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            heading = " > ".join(path[-2:])
            sections.append({"locator": path[-1][:40] if path else "本文", "page": None,
                             "heading": heading, "text": "\n".join(buf)})
            buf.clear()

    for level, text in blocks:
        if level:
            flush()
            path[:] = path[:level - 1] + [text.strip()]
            buf.append(text.strip())
        else:
            buf.append(text)
    flush()
    if not title and blocks:
        title = next((t for lv, t in blocks if lv), blocks[0][1])[:80]
    return {"title": title, "pages": None, "sections": sections, "warnings": []}


def _docx_heading_styles(zf: zipfile.ZipFile) -> dict[str, int]:
    """styles.xml から 見出しスタイルID → レベル の対応表を作る（日本語版 Word の ID '1' 等に対応）。"""
    out: dict[str, int] = {}
    try:
        root = ET.fromstring(zf.read("word/styles.xml"))
    except (KeyError, ET.ParseError):
        return out
    for st in root.iter(_q("w", "style")):
        sid = st.get(_q("w", "styleId")) or ""
        name_el = st.find(_q("w", "name"))
        name = (name_el.get(_q("w", "val")) if name_el is not None else "") or ""
        m = re.match(r"^(?:heading|見出し)\s*(\d)$", name.strip(), re.IGNORECASE)
        if m:
            out[sid] = int(m.group(1))
            continue
        if name.strip().lower() == "title":
            out[sid] = 1
            continue
        lvl = st.find(f"{_q('w', 'pPr')}/{_q('w', 'outlineLvl')}")
        if lvl is not None and (lvl.get(_q("w", "val")) or "").isdigit():
            level = int(lvl.get(_q("w", "val"))) + 1
            if level <= 6:
                out[sid] = level
    return out


def _docx_para(p, heading_styles: dict[str, int]) -> tuple[str, int]:
    parts = []
    for node in p.iter():
        if node.tag == _q("w", "t") and node.text:
            parts.append(node.text)
        elif node.tag == _q("w", "tab"):
            parts.append("\t")
        elif node.tag in (_q("w", "br"), _q("w", "cr")):
            parts.append("\n")
    text = "".join(parts)
    level = 0
    ppr = p.find(_q("w", "pPr"))
    if ppr is not None:
        style = ppr.find(_q("w", "pStyle"))
        if style is not None:
            sid = style.get(_q("w", "val")) or ""
            level = heading_styles.get(sid, 0)
            if not level:
                m = re.match(r"^(?:heading|見出し)\s*(\d)$", sid, re.IGNORECASE)
                level = int(m.group(1)) if m else 0
        outline = ppr.find(_q("w", "outlineLvl"))
        if not level and outline is not None and (outline.get(_q("w", "val")) or "").isdigit():
            level = int(outline.get(_q("w", "val"))) + 1
        if ppr.find(_q("w", "numPr")) is not None and not level and text.strip():
            text = "・" + text
    if level and len(text) > 120:            # 長すぎる「見出し」は本文扱い
        level = 0
    return text, level


def _docx_table(tbl) -> str:
    rows = []
    for tr in tbl.iter(_q("w", "tr")):
        cells = []
        for tc in tr.findall(_q("w", "tc")):
            texts = ["".join(t.text or "" for t in p.iter(_q("w", "t")))
                     for p in tc.iter(_q("w", "p"))]
            cells.append(" ".join(x for x in texts if x).strip())
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _core_title(zf: zipfile.ZipFile) -> str:
    try:
        root = ET.fromstring(zf.read("docProps/core.xml"))
    except (KeyError, ET.ParseError):
        return ""
    el = root.find(_q("dc", "title"))
    return (el.text or "").strip() if el is not None else ""


# ---------------------------------------------------------------- PPTX

def _rels(zf: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """part（例 ppt/slides/slide1.xml）の rels を {rId: (type, 絶対パス)} で返す。"""
    pp = PurePosixPath(part)
    rels_path = str(pp.parent / "_rels" / (pp.name + ".rels"))
    try:
        root = ET.fromstring(zf.read(rels_path))
    except (KeyError, ET.ParseError):
        return {}
    out = {}
    for rel in root.iter(_q("rel", "Relationship")):
        target = rel.get("Target") or ""
        if rel.get("TargetMode") == "External":
            continue
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            parts: list[str] = []
            for seg in (str(pp.parent) + "/" + target).split("/"):
                if seg == "..":
                    if parts:
                        parts.pop()
                elif seg and seg != ".":
                    parts.append(seg)
            path = "/".join(parts)
        out[rel.get("Id") or ""] = (rel.get("Type") or "", path)
    return out


def _pptx(data: bytes) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(data))
    try:
        pres = ET.fromstring(zf.read("ppt/presentation.xml"))
    except KeyError as e:
        raise ExtractError("PowerPoint の構造（ppt/presentation.xml）が見つかりません") from e
    rels = _rels(zf, "ppt/presentation.xml")
    slide_paths = []
    lst = pres.find(_q("p", "sldIdLst"))
    for sld in (lst if lst is not None else []):
        rid = sld.get(_q("r", "id")) or ""
        if rid in rels:
            slide_paths.append(rels[rid][1])
    if not slide_paths:      # 念のため: ファイル名の番号順
        slide_paths = sorted((n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                             key=lambda n: int(re.findall(r"\d+", n)[-1]))
    sections = []
    title = _core_title(zf)
    for no, path in enumerate(slide_paths, 1):
        try:
            root = ET.fromstring(zf.read(path))
        except KeyError:
            continue
        slide_title, body = _pptx_shapes(root)
        notes = ""
        for typ, target in _rels(zf, path).values():
            if typ.endswith("/notesSlide"):
                try:
                    _, note_lines = _pptx_shapes(ET.fromstring(zf.read(target)), notes=True)
                    notes = "\n".join(note_lines)
                except (KeyError, ET.ParseError):
                    pass
        text_parts = ([slide_title] if slide_title else []) + body
        if notes.strip():
            text_parts.append("（ノート）" + notes)
        text = "\n".join(text_parts)
        if text.strip():
            sections.append({"locator": f"スライド{no}", "page": no,
                             "heading": slide_title[:80], "text": text})
        if not title and slide_title:
            title = slide_title
    return {"title": title, "pages": len(slide_paths), "sections": sections, "warnings": []}


def _pptx_shapes(root, *, notes: bool = False) -> tuple[str, list[str]]:
    title = ""
    lines: list[str] = []
    for sp in root.iter():
        if sp.tag == _q("p", "sp"):
            ph = sp.find(f".//{_q('p', 'nvPr')}/{_q('p', 'ph')}")
            ph_type = ph.get("type") if ph is not None else ""
            if ph_type in ("sldNum", "sldImg", "dt", "ftr", "hdr"):
                continue
            paras = []
            for para in sp.iter(_q("a", "p")):
                t = "".join(x.text or "" for x in para.iter(_q("a", "t")))
                if t.strip():
                    paras.append(t)
            if not paras:
                continue
            if not notes and ph_type in ("title", "ctrTitle") and not title:
                title = " ".join(paras)
            else:
                lines.extend(paras)
        elif sp.tag == _q("a", "tbl"):
            for tr in sp.iter(_q("a", "tr")):
                cells = [" ".join("".join(x.text or "" for x in p.iter(_q("a", "t")))
                                  for p in tc.iter(_q("a", "p"))).strip()
                         for tc in tr.findall(_q("a", "tc"))]
                if any(cells):
                    lines.append(" | ".join(cells))
    return title, lines


# ---------------------------------------------------------------- XLSX

def _col_index(ref: str) -> int:
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _col_name(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _xlsx(data: bytes) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(data))
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
    except KeyError as e:
        raise ExtractError("Excel の構造（xl/workbook.xml）が見つかりません") from e
    shared: list[str] = []
    try:
        sst = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in sst.findall(_q("s", "si")):
            shared.append("".join(t.text or "" for t in si.iter(_q("s", "t"))))
    except KeyError:
        pass
    rels = _rels(zf, "xl/workbook.xml")
    sections: list[dict] = []
    warnings: list[str] = []
    sheets = wb.find(_q("s", "sheets"))
    for sheet in (sheets if sheets is not None else []):
        name = sheet.get("name") or "Sheet"
        if sheet.get("state") in ("hidden", "veryHidden"):
            continue
        rid = sheet.get(_q("r", "id")) or ""
        if rid not in rels:
            continue
        try:
            root = ET.fromstring(zf.read(rels[rid][1]))
        except KeyError:
            continue
        rows: list[tuple[int, dict[int, str]]] = []
        truncated = False
        for row in root.iter(_q("s", "row")):
            cells: dict[int, str] = {}
            for c in row.findall(_q("s", "c")):
                val = _xlsx_cell(c, shared)
                if val != "":
                    cells[_col_index(c.get("r") or "")] = val
            if cells:
                rows.append((int(row.get("r") or len(rows) + 1), cells))
            if len(rows) >= MAX_SHEET_ROWS:
                truncated = True
                break
        if truncated:
            warnings.append(f"シート「{name}」は先頭 {MAX_SHEET_ROWS} 行のみ取り込みました")
        sections.extend(_table_sections(name, rows))
    return {"title": _core_title(zf), "pages": None, "sections": sections, "warnings": warnings}


def _xlsx_cell(c, shared: list[str]) -> str:
    t = c.get("t") or "n"
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter(_q("s", "t"))).strip()
    v = c.find(_q("s", "v"))
    raw = (v.text or "") if v is not None else ""
    if t == "s":
        try:
            return shared[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    if t == "b":
        return "TRUE" if raw == "1" else "FALSE"
    if t == "n" and raw:
        try:
            num = float(raw)
            return str(int(num)) if num.is_integer() and abs(num) < 1e15 else f"{num:.6g}"
        except ValueError:
            return raw
    return raw.strip()


def _table_sections(name: str, rows: list[tuple[int, dict[int, str]]]) -> list[dict]:
    """表 → 「列名: 値」形式の行を ROWS_PER_SECTION 行ずつのセクションに。"""
    if not rows:
        return []
    header: dict[int, str] = {}
    first_no, first = rows[0]
    if len(first) >= 2 and all(not re.fullmatch(r"[-\d\.,%]+", v) for v in first.values()):
        header = first
        body = rows[1:]
    else:
        body = rows
    sections = []
    for i in range(0, max(1, len(body)), ROWS_PER_SECTION):
        part = body[i:i + ROWS_PER_SECTION]
        if not part and header:
            part = []
        lines = []
        if header:
            lines.append("列: " + " | ".join(header[k] for k in sorted(header)))
        for _, cells in part:
            if header:
                lines.append(" / ".join(f"{header.get(k, _col_name(k))}: {v}"
                                        for k, v in sorted(cells.items())))
            else:
                lines.append(" | ".join(v for _, v in sorted(cells.items())))
        if not part and not header:
            continue
        lo = part[0][0] if part else first_no
        hi = part[-1][0] if part else first_no
        sections.append({"locator": f"{name}!行{lo}-{hi}", "page": None,
                         "heading": f"シート {name}", "text": "\n".join(lines)})
    return sections


# ---------------------------------------------------------------- テキスト系

def _csv(data: bytes, delim: str) -> dict:
    text = decode_text(data)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = []
    for i, row in enumerate(reader, 1):
        cells = {j: v.strip() for j, v in enumerate(row) if v.strip()}
        if cells:
            rows.append((i, cells))
        if len(rows) >= MAX_SHEET_ROWS:
            break
    return {"title": "", "pages": None, "sections": _table_sections("CSV", rows), "warnings": []}


def _markdown(text: str) -> dict:
    sections: list[dict] = []
    path: list[str] = []
    buf: list[str] = []
    title = ""

    def flush():
        if "".join(buf).strip():
            sections.append({"locator": path[-1][:40] if path else "本文", "page": None,
                             "heading": " > ".join(path[-2:]), "text": "\n".join(buf)})
        buf.clear()

    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            level = len(m.group(1))
            path[:] = path[:level - 1] + [m.group(2).strip()]
            title = title or m.group(2).strip()
            buf.append(m.group(2).strip())
        else:
            buf.append(line)
    flush()
    return {"title": title, "pages": None, "sections": sections, "warnings": []}


def _plain(text: str) -> dict:
    title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:80]
    return {"title": title, "pages": None,
            "sections": [{"locator": "本文", "page": None, "heading": "", "text": text}],
            "warnings": []}


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
             "article", "table", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("\n# ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_data(self, d):
        if self._skip:
            return
        if self._in_title:
            self.title += d
            return
        self.parts.append(d)


def _html(data: bytes) -> dict:
    parser = _HTMLText()
    parser.feed(decode_text(data))
    text = html.unescape("".join(parser.parts))
    out = _markdown(re.sub(r"\n\s*\n+", "\n\n", text))
    out["title"] = parser.title.strip() or out["title"]
    return out
