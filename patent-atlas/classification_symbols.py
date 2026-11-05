"""Classification symbol validation and display enrichment without extraction dependencies."""

import copy
import re
import unicodedata
import classification_catalog as ipc_catalog
import fterm_catalog
import fi_catalog
from classification_translations import label_fields
from catalog import CATALOG
from fi_parser import parse_fi


def key(item):
    return item["kind"] + ":" + item["code"]


def normalize(kind, code):
    if kind not in ("IPC", "CPC", "F-term", "FI") or not isinstance(code, str):
        raise ValueError("分類体系とコードを確認してください。")
    if kind == "FI":
        return fi_catalog.normalize(code)
    code = re.sub(r"\s+", "", unicodedata.normalize("NFKC", code)).upper()
    # IPCPUB's documented 14-character symbol form is already understood by
    # the official catalogue. Apply that formatting conversion consistently at
    # the input boundary too; never apply IPC padding rules to CPC/F-term.
    if kind == "IPC":
        code = ipc_catalog.normalize_code(code)
    section = "[A-HY]" if kind == "CPC" else "[A-H]"
    pattern = (
        section + r"(?:\d{2}(?:[A-Z](?:\d{1,4}/\d{1,6})?)?)?" if kind != "F-term" else r"\d[A-Z]\d{3}(?:[A-Z]{2}\d{2})?"
    )
    if not re.fullmatch(pattern, code):
        raise ValueError(f"{kind} のコード形式を確認してください: {code}")
    if kind in ("IPC", "CPC"):
        match = re.fullmatch(r"([A-HY]\d{2}[A-Z])(\d+)/(\d+)", code)
        if match:
            code = match[1] + str(int(match[2])) + "/" + match[3]
    return code


def parse_codes(kind, text):
    """Consume every character; unknown suffixes must never be silently discarded."""
    if not isinstance(text, str) or len(text) > 12000:
        raise ValueError("分類は12,000文字以内で入力してください。")
    if kind == "FI":
        # CSV/target extraction and manual seeds must consume printed FI
        # expansion symbols in the same way, preserving the complete suffix.
        entries = parse_fi(text, allow_compact_suffix=True)
        codes = list(dict.fromkeys(normalize("FI", entry["raw"]) for entry in entries))
        if len(codes) > 100:
            raise ValueError("一度に追加できる分類は体系ごとに100件までです。")
        return codes
    text = unicodedata.normalize("NFKC", text).strip().upper()
    section = "[A-HY]" if kind == "CPC" else "[A-H]"
    pattern = (
        section + r"(?:\s*\d{2}(?:\s*[A-Z](?:\s*\d{1,4}\s*/\s*\d{1,6})?)?)?"
        if kind != "F-term"
        else r"\d[A-Z]\d{3}(?:\s*[A-Z]{2}\d{2})?"
    )
    if kind == "IPC":
        pattern = r"(?:[A-H]\s*\d{2}\s*[A-Z]\s*\d{10}|" + pattern + ")"
    out, pos = [], 0
    while pos < len(text):
        match = re.match(pattern, text[pos:])
        if not match:
            raise ValueError(
                f"{kind} の未解析部分があります: {text[pos : pos + 50]}。体系ごとの欄に完全なコードを入力してください。"
            )
        code = normalize(kind, match[0])
        pos += match.end()
        # WIPO also displays edition indicators in square brackets. They are
        # formatting metadata, not a licence to infer a missing symbol prefix.
        version_pattern = r"\s*(?:\(\d{4}\.\d{2}\)|\[\d{4}\.\d{2}\])" if kind == "IPC" else r"\s*\(\d{4}\.\d{2}\)"
        version = re.match(version_pattern, text[pos:])
        if version:
            pos += version.end()
        if code not in out:
            out.append(code)
        if pos < len(text):
            delimiter = re.match(r"[\s,;、；|]+", text[pos:])
            if not delimiter:
                raise ValueError(f"{kind} のコードの後に未解析部分があります: {text[pos : pos + 50]}")
            pos += delimiter.end()
    if len(out) > 100:
        raise ValueError("一度に追加できる分類は体系ごとに100件までです。")
    return out


def lookup(kind, code):
    code = normalize(kind, code)
    if kind == "FI":
        return fi_catalog.lookup(code)
    return fterm_catalog.lookup(code) if kind == "F-term" else ipc_catalog.lookup(kind, code)


def parent(kind, code):
    if kind == "FI":
        return fi_catalog.parent(code)
    return fterm_catalog.parent(code) if kind == "F-term" else ipc_catalog.parent(kind, code)


def children(kind, code=None):
    if kind == "FI":
        return fi_catalog.children(code)
    return fterm_catalog.children(code) if kind == "F-term" else ipc_catalog.children(kind, code)


def enrich(item):
    kind, code = item["kind"], normalize(item["kind"], item["code"])
    known = lookup(kind, code)
    local = next((c for c in CATALOG if c["kind"] == kind and c["code"] == code), None)
    result = {**copy.deepcopy(item), **(known or {}), "kind": kind, "code": code}
    if known:
        result["title_official"] = known["title"]
    if kind == "F-term":
        result.update(
            title_ja=(known or local or {}).get("title", ""),
            title_ja_status="official" if known else "app_translation" if local else "unavailable",
            title_ja_source=(known or local or {}).get("source", ""),
            **label_fields(kind, code),
        )
    if local:
        result.update(title=local["title"], source=local["source"])
        result["title_short_ja"] = local["title"]
    result["terms"] = list(
        dict.fromkeys(
            [term for term in item.get("terms", []) if isinstance(term, str)]
            + (local.get("terms", []) if local else [])
        )
    )[:48]
    result.setdefault("title", code)
    result.setdefault("source", "https://www.j-platpat.inpit.go.jp/p1101")
    result["verified"] = bool(known or local)
    result["key"] = key(result)
    result["selectable"] = (
        bool(re.fullmatch(r"[A-HY]\d{2}[A-Z]\d{1,4}/\d{1,6}", code))
        if kind != "F-term"
        else bool(re.fullmatch(r"\d[A-Z]\d{3}[A-Z]{2}\d{2}", code))
    )
    if kind == "FI":
        result["selectable"] = True
    upper_ipc = kind == "IPC" and bool(known) and "/" not in code
    if upper_ipc:
        result["selectable"] = True
    if known and known.get("selectable") is False:
        result["selectable"] = False
    result["selection_scope"] = "subtree" if upper_ipc else "code"
    result.setdefault(
        "reason",
        "公式分類の階層から表示。技術への適合性は定義を確認してください。"
        if known
        else "利用者の指定。存在・定義は未確認です。",
    )
    result.setdefault("group", "分類探索")
    result.setdefault("score", 0)
    result.setdefault("origins", [])
    return result
