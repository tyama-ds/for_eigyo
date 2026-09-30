"""資料ファイルから本文を取り出して Markdown にする。

画像以外の主な業務ファイルに対応する。PDF 以外は標準ライブラリだけで読む。

| 種類 | 拡張子 | 方式 |
|------|--------|------|
| テキスト | .txt .log .md .markdown | 文字コード自動判定（UTF-8 / UTF-8 BOM / Shift_JIS / EUC-JP） |
| データ | .csv .tsv | 表（Markdown テーブル） |
| 構造化テキスト | .json .xml .yaml .yml | コードブロック |
| Web | .html .htm | 見出し・段落・箇条書きを保った本文 |
| Word | .docx | 見出し・箇条書き・表 |
| Excel | .xlsx .xlsm | シートごとの表 |
| PowerPoint | .pptx | スライドごとのタイトル・本文・ノート |
| メール | .eml | 件名・差出人・日時・本文・添付ファイル名 |
| PDF | .pdf | ページごとの本文（``pip install pypdf`` が必要） |

旧形式（.doc .xls .ppt）とスキャン画像だけの PDF は読めない。
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_CHARS = 3_000_000          # 取り出す本文の上限（これを超えたら切り詰める）
MAX_TABLE_ROWS = 3000
MAX_TABLE_COLS = 60

KINDS: dict[str, str] = {
    ".md": "markdown", ".markdown": "markdown",
    ".txt": "text", ".log": "text",
    ".csv": "csv", ".tsv": "csv",
    ".json": "code", ".xml": "code", ".yaml": "code", ".yml": "code",
    ".html": "html", ".htm": "html",
    ".docx": "word", ".xlsx": "excel", ".xlsm": "excel", ".pptx": "powerpoint",
    ".eml": "email", ".pdf": "pdf",
}

# 画面で形式ごとにオン・オフするためのまとまり
TYPE_GROUPS: dict[str, dict] = {
    "note": {"label": "ノート (.md)", "exts": [".md", ".markdown"]},
    "text": {"label": "テキスト (.txt .log)", "exts": [".txt", ".log"]},
    "csv": {"label": "CSV / TSV", "exts": [".csv", ".tsv"]},
    "code": {"label": "JSON / XML / YAML", "exts": [".json", ".xml", ".yaml", ".yml"]},
    "html": {"label": "HTML", "exts": [".html", ".htm"]},
    "word": {"label": "Word (.docx)", "exts": [".docx"]},
    "excel": {"label": "Excel (.xlsx)", "exts": [".xlsx", ".xlsm"]},
    "powerpoint": {"label": "PowerPoint (.pptx)", "exts": [".pptx"]},
    "email": {"label": "メール (.eml)", "exts": [".eml"]},
    "pdf": {"label": "PDF", "exts": [".pdf"]},
}
EXT_GROUP = {ext: g for g, info in TYPE_GROUPS.items() for ext in info["exts"]}


class ExtractError(Exception):
    """読めなかった理由（画面にそのまま出す）。"""


def is_supported(name: str) -> bool:
    return Path(name).suffix.lower() in KINDS


def group_of(name: str) -> str:
    return EXT_GROUP.get(Path(name).suffix.lower(), "")


def pdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except BaseException:  # noqa: BLE001 - 壊れた依存（cryptography 等）で panic することがある
        return False


# ---------------------------------------------------------------- 共通

def decode_text(data: bytes) -> str:
    """文字コードを推定してデコードする（日本語の業務ファイルを想定）。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8", "cp932", "euc_jp"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).replace("|", "\\|").strip()


def md_table(rows: list[list[str]]) -> str:
    rows = [r[:MAX_TABLE_COLS] for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(_cell(c) or " " for c in rows[0]) + " |",
           "|" + "---|" * width]
    for r in rows[1:MAX_TABLE_ROWS]:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    if len(rows) > MAX_TABLE_ROWS:
        out.append(f"\n（{len(rows) - MAX_TABLE_ROWS} 行を省略）")
    return "\n".join(out)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------- 各形式

def _text(path: Path) -> str:
    return decode_text(path.read_bytes())


def _csv(path: Path) -> str:
    text = decode_text(path.read_bytes())
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    if delim == ",":
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
        except csv.Error:
            pass
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    return md_table(rows)


def _code(path: Path) -> str:
    lang = path.suffix.lower().lstrip(".")
    body = decode_text(path.read_bytes()).replace("```", "``​`")
    return f"```{lang}\n{body}\n```"


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "section", "article", "tr", "table", "ul", "ol", "br", "hr", "header", "footer"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self._in_title = False
        elif re.fullmatch(r"h[1-6]", tag) or tag in ("p", "div", "table"):
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(re.sub(r"[ \t\r\n]+", " ", data))


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    body = "".join(p.out)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if p.title.strip() and not body.startswith("# "):
        body = f"# {p.title.strip()}\n\n{body}"
    return body


def _html(path: Path) -> str:
    return html_to_text(decode_text(path.read_bytes()))


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ExtractError("ファイルが壊れているか、パスワード付きです（Office の新形式として開けません）") from e


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx(path: Path) -> str:
    with _open_zip(path) as z:
        try:
            root = ET.fromstring(z.read("word/document.xml"))
        except KeyError as e:
            raise ExtractError("Word 文書の本文が見つかりません") from e
        heading_styles: dict[str, int] = {}
        if "word/styles.xml" in z.namelist():
            for st in ET.fromstring(z.read("word/styles.xml")).iter(W + "style"):
                sid = st.get(W + "styleId", "")
                name_el = st.find(W + "name")
                name = (name_el.get(W + "val", "") if name_el is not None else "").lower()
                m = re.match(r"(?:heading|見出し)\s*(\d)", name)
                if m:
                    heading_styles[sid] = int(m.group(1))
                elif name == "title":
                    heading_styles[sid] = 1

    def para_text(p) -> str:
        parts = []
        for el in p.iter():
            tag = _local(el.tag)
            if tag == "t" and el.text:
                parts.append(el.text)
            elif tag == "tab":
                parts.append("\t")
            elif tag in ("br", "cr"):
                parts.append("\n")
        return "".join(parts)

    out: list[str] = []
    body = root.find(W + "body")
    for el in (body if body is not None else []):
        tag = _local(el.tag)
        if tag == "p":
            text = para_text(el).strip()
            if not text:
                continue
            ppr = el.find(W + "pPr")
            level = 0
            if ppr is not None:
                st = ppr.find(W + "pStyle")
                if st is not None:
                    level = heading_styles.get(st.get(W + "val", ""), 0)
                    if not level:
                        m = re.match(r"heading(\d)", st.get(W + "val", "").lower())
                        level = int(m.group(1)) if m else 0
                ol = ppr.find(W + "outlineLvl")
                if not level and ol is not None:
                    level = int(ol.get(W + "val", "0")) + 1
                if not level and ppr.find(W + "numPr") is not None:
                    out.append("- " + text.replace("\n", " "))
                    continue
            out.append(("#" * min(level, 6) + " " + text) if level else text)
        elif tag == "tbl":
            rows = []
            for tr in el.iter(W + "tr"):
                rows.append([" ".join(para_text(p).strip() for p in tc.iter(W + "p")).strip()
                             for tc in tr.findall(W + "tc")])
            table = md_table(rows)
            if table:
                out.append(table)
    return "\n\n".join(out)


S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx(path: Path) -> str:
    with _open_zip(path) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(S + "t")))
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
        except KeyError as e:
            raise ExtractError("Excel ブックの構成が見つかりません") from e
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(PKG_REL + "Relationship"):
                target = r.get("Target", "")
                target = target.lstrip("/")
                rels[r.get("Id")] = target if target.startswith("xl/") else "xl/" + target
        out: list[str] = []
        for sh in wb.iter(S + "sheet"):
            name = sh.get("name", "Sheet")
            target = rels.get(sh.get(R_NS + "id"), "")
            if target not in names:
                continue
            grid: dict[int, dict[int, str]] = {}
            for row in ET.fromstring(z.read(target)).iter(S + "row"):
                for i, c in enumerate(row.findall(S + "c")):
                    ref = c.get("r")
                    col = _col_index(ref) if ref else i
                    if col >= MAX_TABLE_COLS:
                        continue
                    t = c.get("t", "")
                    v = c.find(S + "v")
                    if t == "s" and v is not None and v.text and v.text.isdigit():
                        idx = int(v.text)
                        val = shared[idx] if idx < len(shared) else ""
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(S + "t"))
                    elif t == "b" and v is not None:
                        val = "TRUE" if v.text == "1" else "FALSE"
                    else:
                        val = v.text if v is not None and v.text is not None else ""
                    if val != "":
                        rnum = int(re.sub(r"[A-Z]", "", ref)) - 1 if ref else len(grid)
                        grid.setdefault(rnum, {})[col] = val
                if len(grid) > MAX_TABLE_ROWS:
                    break
            if not grid:
                continue
            width = max(max(r) for r in grid.values()) + 1
            rows = [[grid[r].get(ci, "") for ci in range(width)] for r in sorted(grid)]
            out.append(f"## {name}\n\n{md_table(rows)}")
        return "\n\n".join(out)


A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _slide_texts(root) -> tuple[str, list[str]]:
    title, body = "", []
    for sp in root.iter(P + "sp"):
        ph = sp.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
        paras = []
        for p in sp.iter(A + "p"):
            t = "".join(x.text or "" for x in p.iter(A + "t")).strip()
            if t:
                paras.append(t)
        if not paras:
            continue
        if ph is not None and ph.get("type") in ("title", "ctrTitle") and not title:
            title = " ".join(paras)
        else:
            body.extend(paras)
    for tbl in root.iter(A + "tbl"):
        rows = [["".join(x.text or "" for x in tc.iter(A + "t")) for tc in tr.findall(A + "tc")]
                for tr in tbl.findall(A + "tr")]
        t = md_table(rows)
        if t:
            body.append(t)
    return title, body


def _pptx(path: Path) -> str:
    with _open_zip(path) as z:
        names = z.namelist()
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        if not slides:
            raise ExtractError("スライドが見つかりません")
        out = []
        for i, name in enumerate(slides, 1):
            title, body = _slide_texts(ET.fromstring(z.read(name)))
            part = [f"## スライド {i}" + (f": {title}" if title else "")]
            part += [b if b.startswith("|") else f"- {b}" for b in body]
            note = f"ppt/notesSlides/notesSlide{i}.xml"
            if note in names:
                _, nb = _slide_texts(ET.fromstring(z.read(note)))
                nb = [t for t in nb if not t.isdigit()]
                if nb:
                    part.append("> ノート: " + " / ".join(nb))
            out.append("\n".join(part))
        return "\n\n".join(out)


def _eml(path: Path) -> str:
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    subject = str(msg.get("subject", "") or "(件名なし)")
    head = [f"# {subject}", ""]
    for key, label in (("from", "差出人"), ("to", "宛先"), ("cc", "CC"), ("date", "日時")):
        if msg.get(key):
            head.append(f"- {label}: {msg.get(key)}")
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        try:
            content = body_part.get_content()
        except (LookupError, UnicodeDecodeError):
            content = body_part.get_payload(decode=True) or b""
            content = decode_text(content) if isinstance(content, bytes) else str(content)
        body = html_to_text(content) if body_part.get_content_type() == "text/html" else content
    attachments = [a.get_filename() for a in msg.iter_attachments() if a.get_filename()]
    out = "\n".join(head) + "\n\n" + body.strip()
    if attachments:
        out += "\n\n## 添付ファイル\n" + "\n".join(f"- {a}" for a in attachments)
    return out


def _pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except BaseException as e:  # noqa: BLE001 - ImportError 以外の panic も拾う
        raise ExtractError("PDF を読むには pypdf が必要です（pip install pypdf）") from e
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:  # noqa: BLE001
                raise ExtractError("パスワード付きの PDF は読めません") from e
        pages = []
        for i, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"## p.{i}\n\n{text}")
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 - 壊れた PDF など
        raise ExtractError(f"PDF を読めませんでした: {e}") from e
    if not pages:
        raise ExtractError("PDF に文字が含まれていません（スキャン画像の PDF は読めません）")
    return "\n\n".join(pages)


_READERS = {
    "markdown": _text, "text": _text, "csv": _csv, "code": _code, "html": _html,
    "word": _docx, "excel": _xlsx, "powerpoint": _pptx, "email": _eml, "pdf": _pdf,
}


def extract(path: Path | str) -> str:
    """資料を Markdown に変換して返す。読めない場合は ExtractError。"""
    path = Path(path)
    kind = KINDS.get(path.suffix.lower())
    if not kind:
        raise ExtractError(f"対応していない形式です: {path.suffix or '(拡張子なし)'}")
    try:
        text = _READERS[kind](path)
    except ExtractError:
        raise
    except (OSError, ET.ParseError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise ExtractError(f"読み込みに失敗しました: {e}") from e
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n（長すぎるため以降を省略）"
    return text
