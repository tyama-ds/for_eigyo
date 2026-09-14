"""CSV 取り込み（人が DB で実行して出力した CSV）。企画書 §9.5 csv モード。

- エンコーディング自動判定（UTF-8／BOM 付き UTF-8／cp932）
- 区切り文字の判定（カンマ／タブ）
- 列名マッピングは config/csv_dialects/*.json（候補列名を左から探す）
- 分類コードの多値は区切り文字（; , | 改行）で分割し正規化する
"""
from __future__ import annotations

import csv
import io
import re

from ..core.document import SCHEMES, Document
from ..knowledge import codes as codelib
from ..util import decode_auto, nfkc

ABSTRACT_FIELDS = ("doc_id", "title", "abstract", "claims", "FI", "FT", "IPC", "pub_date", "applicant", "citations")


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", "", nfkc(h or "")).lower()


def detect_columns(header: list[str], csv_dialect: dict) -> dict[str, str]:
    """抽象フィールド → 実際の列名。"""
    normalized = {_norm_header(h): h for h in header}
    mapping: dict[str, str] = {}
    for field_name, options in (csv_dialect.get("columns") or {}).items():
        for opt in options:
            key = _norm_header(opt)
            if key in normalized:
                mapping[field_name] = normalized[key]
                break
    return mapping


def split_multi(value: str, separators: list[str]) -> list[str]:
    if not value:
        return []
    pattern = "|".join(re.escape(s) for s in separators) or ";"
    return [v.strip() for v in re.split(pattern, value) if v and v.strip()]


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
    except csv.Error:
        return "\t" if sample.count("\t") > sample.count(",") else ","


def parse_csv_text(text: str, csv_dialect: dict) -> tuple[list[Document], dict]:
    text = text.lstrip("﻿")
    delimiter = _sniff_delimiter(text[:4000])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return [], {"columns": {}, "missing": list(ABSTRACT_FIELDS), "n_rows": 0, "delimiter": delimiter}
    header = [h.strip() for h in rows[0]]
    mapping = detect_columns(header, csv_dialect)
    seps = csv_dialect.get("multi_value_separators") or [";", ","]
    rank_col = csv_dialect.get("rank_column")
    idx = {h: i for i, h in enumerate(header)}

    def get(row: list[str], field_name: str) -> str:
        col = mapping.get(field_name)
        if col is None or idx[col] >= len(row):
            return ""
        return (row[idx[col]] or "").strip()

    docs: list[Document] = []
    seen: set[str] = set()
    for i, row in enumerate(rows[1:], start=1):
        if not any(c.strip() for c in row):
            continue
        doc_id = codelib.normalize("", get(row, "doc_id")) or f"ROW{i:05d}"
        if doc_id in seen:
            continue
        seen.add(doc_id)
        codes = {}
        for scheme in SCHEMES:
            codes[scheme] = [codelib.normalize(scheme, c) for c in split_multi(get(row, scheme), seps)]
        rank = None
        if rank_col and rank_col in idx and idx[rank_col] < len(row):
            try:
                rank = int(row[idx[rank_col]])
            except ValueError:
                rank = None
        docs.append(Document(doc_id=doc_id, title=get(row, "title"), abstract=get(row, "abstract"),
                             claims=get(row, "claims"), codes=codes, pub_date=get(row, "pub_date"),
                             applicant=get(row, "applicant"),
                             citations=split_multi(get(row, "citations"), seps),
                             rank=rank if rank is not None else len(docs) + 1))
    missing = [f for f in ABSTRACT_FIELDS if f not in mapping]
    info = {"columns": mapping, "missing": missing, "n_rows": len(docs), "delimiter": delimiter,
            "has_abstract": "abstract" in mapping, "has_codes": any(s in mapping for s in SCHEMES)}
    return docs, info


def import_csv_bytes(data: bytes, csv_dialect: dict) -> tuple[list[Document], dict]:
    text, encoding = decode_auto(data)
    docs, info = parse_csv_text(text, csv_dialect)
    info["encoding"] = encoding
    return docs, info


def export_csv_text(docs: list[Document], bom: bool = True) -> str:
    """文献リストを汎用列名で CSV に書き出す（Excel 互換のため BOM 付き UTF-8 を選択可）。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["doc_id", "title", "abstract", "claims", "FI", "FT", "IPC", "pub_date", "applicant", "citations"])
    for d in docs:
        w.writerow([d.doc_id, d.title, d.abstract, d.claims, ";".join(d.codes["FI"]), ";".join(d.codes["FT"]),
                    ";".join(d.codes["IPC"]), d.pub_date, d.applicant, ";".join(d.citations)])
    return ("﻿" if bom else "") + buf.getvalue()
