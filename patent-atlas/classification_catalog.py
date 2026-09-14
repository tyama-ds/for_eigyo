"""Offline, source-backed IPC 2026.01 hierarchy and English title search.

The bundled catalogue contains every symbol from WIPO's valid-symbol list.
Relationships are imported from the nested scheme XML, never guessed from digits.
The caller must keep CPC, FI and F-term separate; unsupported kinds return no hits.
"""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import gzip
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlencode

from classification_translations import label_fields

_DATA = Path(__file__).resolve().parent / "resources" / "ipc" / "ipc_2026_01_en.json.gz"


def _kind(kind: str) -> bool:
    return str(kind).strip().upper() == "IPC"


def normalize_code(code: str) -> str:
    """Normalize formatting only; neither infer hierarchy nor remap classification."""
    code = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(code))).upper()
    if re.fullmatch(r"[A-H][0-9]{2}[A-Z][0-9]{10}", code):
        return f"{code[:4]}{int(code[4:8])}/{code[8:].rstrip('0').ljust(2, '0')}"
    match = re.fullmatch(r"([A-H][0-9]{2}[A-Z])([0-9]{1,4})/([0-9]{2,6})", code)
    if match:
        return f"{match[1]}{int(match[2])}/{match[3]}"
    return code


@lru_cache(maxsize=1)
def _catalog() -> tuple[dict, dict, dict]:
    with gzip.open(_DATA, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    rows = {row[0]: row for row in data["records"]}
    child_map: dict[str | None, list[str]] = defaultdict(list)
    for row in rows.values():
        child_map[row[2]].append(row[0])
    return data["metadata"], rows, dict(child_map)


def _public(row: list) -> dict:
    meta, rows, child_map = _catalog()
    ancestor_codes: list[str] = []
    code = row[2]
    while code is not None:
        ancestor_codes.append(code)
        code = rows[code][2]
    ancestor_codes.reverse()
    result = {
        "code": row[0], "kind": "IPC", "title": row[1], "parent": row[2],
        "level": row[3], "dots": row[4], "entry_type": row[5],
        "source": "https://ipcpub.wipo.int/?" + urlencode({"notion": "scheme", "version": meta["edition"], "symbol": row[6], "lang": "en"}),
        "verified": True, "version": meta["version"], "language": "en",
        "children_count": len(child_map.get(row[0], [])),
        "ancestors": ancestor_codes,
        "path_title": " > ".join([rows[c][1] for c in ancestor_codes] + [row[1]]),
    }
    result.update(title_en=row[1], title_en_status='official', title_en_source=result['source'],
                  title_en_version=meta['version'], **label_fields('IPC', row[0]))
    parent_row = rows.get(row[2])
    result.update(title_en_parent=parent_row[1] if parent_row else '',
                  title_ja_parent=label_fields('IPC', row[2]).get('title_ja', '') if parent_row else '',
                  title_en_parent_code=row[2] or '', title_ja_parent_code=row[2] or '')
    return result


def lookup(kind: str, code: str) -> dict | None:
    """Return an exact valid symbol, or None. Deleted/unknown codes are not guessed."""
    if not _kind(kind) or not code:
        return None
    row = _catalog()[1].get(normalize_code(code))
    return _public(row) if row is not None else None


def parent(kind: str, code: str) -> dict | None:
    """Return the direct official parent (None for a section or an unknown code)."""
    node = lookup(kind, code)
    return lookup(kind, node["parent"]) if node and node["parent"] else None


def children(kind: str, code: str | None = None) -> list[dict]:
    """Direct children in WIPO scheme order; None/empty code returns 8 sections."""
    if not _kind(kind):
        return []
    _, rows, child_map = _catalog()
    key = normalize_code(code) if code else None
    return [_public(rows[c]) for c in child_map.get(key, [])]


def ancestors(kind: str, code: str) -> list[dict]:
    """The official path from section to direct parent; excludes the supplied node."""
    node = lookup(kind, code)
    if not node:
        return []
    rows = _catalog()[1]
    return [_public(rows[c]) for c in node["ancestors"]]


def metadata() -> dict:
    """Return a detached, JSON-serializable source/version/coverage description."""
    meta = _catalog()[0]
    return json.loads(json.dumps(meta))


@lru_cache(maxsize=1)
def _titles() -> list[tuple[str, str]]:
    return [(row[0], unicodedata.normalize("NFKC", row[1]).casefold()) for row in _catalog()[1].values()]


def search(kind: str, text: str, limit: int = 30) -> list[dict]:
    """Search exact/code prefixes and English titles, using official parent context.

    Text words are ANDed (case-insensitive substring matching). At least one word
    must occur in the node's own title; remaining words may occur in ancestors.
    Exact codes rank first, then code prefixes, direct titles, and contextual hits.
    This is literal catalogue search, not an AI semantic classification claim.
    """
    if not _kind(kind) or not str(text).strip():
        return []
    try:
        limit = min(200, max(0, int(limit)))
    except (ValueError, TypeError):
        limit = 30
    if not limit:
        return []
    _, rows, _ = _catalog()
    code_query = normalize_code(text)
    if re.fullmatch(r"[A-H](?:[0-9]{0,2}(?:[A-Z](?:[0-9]{0,4}(?:/[0-9]{0,6})?)?)?)?", code_query):
        codes = [c for c in rows if c.startswith(code_query)]
        if code_query in rows:
            codes = [code_query] + [c for c in codes if c != code_query]
        return [_public(rows[c]) for c in codes[:limit]]
    query = unicodedata.normalize("NFKC", str(text)).casefold().strip()
    words = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    if not words:
        return []
    ranked: list[tuple[int, int, str]] = []
    for position, (code, title) in enumerate(_titles()):
        present = [word in title for word in words]
        if not any(present):
            continue
        if all(present):
            rank = 0 if title == query else 1 if query in title else 2
        else:
            missing = [word for word, found in zip(words, present) if not found]
            parent_code = rows[code][2]
            while parent_code is not None and missing:
                title_parent = unicodedata.normalize("NFKC", rows[parent_code][1]).casefold()
                missing = [word for word in missing if word not in title_parent]
                parent_code = rows[parent_code][2]
            if missing:
                continue
            rank = 3
        ranked.append((rank, position, code))
    ranked.sort()
    return [_public(rows[code]) for _, _, code in ranked[:limit]]
