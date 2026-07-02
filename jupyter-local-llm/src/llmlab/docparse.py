"""版面解析つき PDF パース（OCR・フォントサイズ見出し検出）。

BookRAG の既定 PDF パースは pypdf のテキスト抽出＋素朴なヒューリスティックだが、
本モジュールはより高精度な解析を提供する（任意依存, 未導入時は None を返し呼び出し側が
pypdf にフォールバック）。

- **版面解析（layout）**: pymupdf(fitz) の span フォントサイズで見出し階層を判定
  （pypdf はフォント情報を持たないため、これが実質の“レイアウト認識”強化）。
- **OCR**: テキスト層の無い/薄いページを pymupdf で画像化し pytesseract で文字起こし。
- **MinerU**: layout="mineru" 指定かつ magic_pdf 導入時はそれを試す（best-effort）。

戻り値は bookindex のブロック形式: {"content","type","page","font","level"}
"""

from __future__ import annotations

import re
from pathlib import Path


def available() -> bool:
    try:
        import fitz  # noqa: F401  PyMuPDF

        return True
    except Exception:  # noqa: BLE001
        return False


def parse_pdf(path: Path, *, ocr="auto", layout="auto") -> list[dict] | None:
    """PDF を版面解析つきで解析。pymupdf が無ければ None（呼び出し側が pypdf へ）。

    ocr:    "auto"(テキストが薄いページのみOCR) / True(全ページOCR) / False(OCRしない)
    layout: "auto"(fitz のフォントサイズで見出し判定) / "mineru"(MinerU試行) / False(行=Text)
    """
    if layout == "mineru":
        blocks = _try_mineru(path)
        if blocks is not None:
            return blocks
        # 失敗時は下の pymupdf 経路へ

    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001
        return None

    doc = fitz.open(str(path))
    # 1) 各ページの行(テキスト+最大フォントサイズ)を集める
    page_lines: dict[int, list[tuple[str, float]]] = {}
    sizes: list[float] = []
    try:
        for pno in range(len(doc)):
            page = doc.load_page(pno)
            lines: list[tuple[str, float]] = []
            try:
                data = page.get_text("dict")
            except Exception:  # noqa: BLE001
                data = {"blocks": []}
            for block in data.get("blocks", []):
                if block.get("type") != 0:  # 0=text
                    continue
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    txt = "".join(sp.get("text", "") for sp in spans).strip()
                    if not txt:
                        continue
                    size = max((sp.get("size", 0.0) for sp in spans), default=0.0)
                    lines.append((txt, size))
                    sizes.append(size)
            # OCR 判定: テキストが薄いページ
            need_ocr = ocr is True or (ocr == "auto" and _text_len(lines) < 40)
            if need_ocr:
                ocr_text = _ocr_page(page)
                if ocr_text:
                    for ln in ocr_text.splitlines():
                        ln = ln.strip()
                        if ln:
                            lines.append((ln, 0.0))  # OCR 行はサイズ不明→本文扱い
            page_lines[pno + 1] = lines
    finally:
        try:
            doc.close()  # fitz Document のリソース解放
        except Exception:  # noqa: BLE001
            pass

    if not sizes and not any(page_lines.values()):
        return []  # 完全に空（OCR も失敗）

    # 2) 本文フォントサイズを基準に見出し閾値を決める。
    #    行数の最頻値ではなく「文字数で重み付け」する: 本文は圧倒的に文字数が多いため、
    #    見出しが多い/行数が拮抗する文書でも本文サイズを取り違えない。
    body = _body_size(page_lines) if sizes else 0.0
    # サイズ→見出しレベルのマップ（本文より大きいサイズを大きい順に level 1,2,3...）
    big = sorted({round(s, 1) for s in sizes if s > body + 0.5}, reverse=True)
    size_to_level = {s: i + 1 for i, s in enumerate(big[:4])}

    blocks: list[dict] = []
    if layout is False:
        for pno, lines in page_lines.items():
            for txt, _ in lines:
                blocks.append(_blk(txt, "Text", pno))
        return blocks

    for pno, lines in page_lines.items():
        for txt, size in lines:
            level = size_to_level.get(round(size, 1))
            if level is None and size == 0.0:
                # OCR/サイズ不明行: 番号見出しヒューリスティックのみ
                if len(txt) < 80 and (_NUM.match(txt) or txt.isupper()):
                    blocks.append(_blk(txt, "Title", pno, level=None))
                else:
                    blocks.append(_blk(txt, "Text", pno))
            elif level is not None:
                blocks.append(_blk(txt, "Title", pno, level=level))
            else:
                blocks.append(_blk(txt, "Text", pno))
    return blocks


_NUM = re.compile(r"^(\d+(\.\d+){0,3})\s+\S|^第[0-9一二三四五六七八九十百]+[章節編]")


def _blk(content: str, typ: str, page: int, *, level=None) -> dict:
    return {"content": content, "type": typ, "page": page, "font": None, "level": level}


def _text_len(lines: list[tuple[str, float]]) -> int:
    return sum(len(t) for t, _ in lines)


def _body_size(page_lines: dict[int, list[tuple[str, float]]]) -> float:
    """本文フォントサイズ = 文字数が最も多いサイズ（OCR 行 size=0.0 は除外）。"""
    from collections import Counter

    weight: Counter = Counter()
    for lines in page_lines.values():
        for txt, size in lines:
            if size > 0:
                weight[round(size, 1)] += len(txt)
    return weight.most_common(1)[0][0] if weight else 0.0


def _ocr_page(page) -> str:
    """1ページを画像化して OCR。pytesseract/Tesseract 未導入なら空文字。"""
    try:
        import io

        import pytesseract
        from PIL import Image
    except Exception:  # noqa: BLE001
        return ""
    try:
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        # 日本語+英語（jpn は Tesseract の言語データが必要。無ければ eng で再試行）
        try:
            return pytesseract.image_to_string(img, lang="jpn+eng")
        except Exception:  # noqa: BLE001
            return pytesseract.image_to_string(img)
    except Exception as e:  # noqa: BLE001
        print(f"[docparse] OCR に失敗（スキップ）: {e}")
        return ""


def _try_mineru(path: Path) -> list[dict] | None:
    """MinerU(magic_pdf) が導入済みなら試す。失敗時は None を返し pymupdf 経路へ。"""
    try:
        from magic_pdf.data.dataset import PymuDocDataset  # type: ignore
        from magic_pdf.model.doc_analyze_by_custom_model import doc_analyze  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        raw = path.read_bytes()
        ds = PymuDocDataset(raw)
        infer = ds.apply(doc_analyze, ocr=True)
        md = infer.pipe_ocr_mode(None).get_markdown("")  # Markdown 化
        from .bookindex import _parse_markdown  # 見出し階層をそのまま活用

        return _parse_markdown(md)
    except Exception as e:  # noqa: BLE001
        print(f"[docparse] MinerU の解析に失敗（pymupdf 経路へフォールバック）: {e}")
        return None
