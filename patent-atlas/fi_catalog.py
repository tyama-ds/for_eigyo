"""Full JPO FI scheme, with independently sourced Japanese captions/concordance."""

from functools import lru_cache
import copy
import gzip
import json
from pathlib import Path
import re
import unicodedata
from collections import defaultdict

PATTERN = r"[A-H]\d{2}[A-Z]\d{1,4}/\d{2,6}(?:,\d{3})?(?:@[A-Z])?"
DATA = Path(__file__).resolve().parent / "resources" / "fi" / "fi_202604.json.gz"


def normalize(code):
    code = unicodedata.normalize("NFKC", str(code)).strip().upper()
    code = re.sub(r"(/\d+)\s+(\d{3})(?=\s|[A-Z]$|$)", r"\1,\2", code)
    code = re.sub(r"\s+", "", code)
    # Conventional display notation -> the documented J-PlatPat FI notation.
    if "/" in code:
        code = re.sub(r"(?<=[0-9])([A-Z])$", r"@\1", code)
    if not re.fullmatch(PATTERN + r"|[A-H](?:\d{2}(?:[A-Z])?)?", code):
        raise ValueError("FIは完全なコードで入力してください（例 B60L15/20@A）。")
    return code


@lru_cache(maxsize=1)
def _data():
    with gzip.open(DATA, "rt", encoding="utf-8") as f:
        data = json.load(f)
    data["records"] = {r[0]: r for r in data["records"]}
    children = defaultdict(list)
    for row in data["records"].values():
        children[row[2]].append(row[0])
    data["children"] = dict(children)
    return data


def _public(row):
    data = _data()
    records = data["records"]
    ancestors = []
    code = row[2]
    while code:
        ancestors.append(code)
        code = records[code][2]
    ancestors.reverse()
    source = data["metadata"]["sources"][0]["url"]
    ja_source = data["metadata"]["sources"][1]["url"]
    parent = records.get(row[2])
    return dict(
        code=row[0],
        kind="FI",
        title=row[1],
        title_en=row[1],
        title_ja=row[5],
        title_en_status="official",
        title_ja_status="official" if row[5] else "unavailable",
        title_en_source=source,
        title_ja_source=ja_source if row[5] else "",
        title_en_version="2026.04",
        title_ja_version="2026-08-18 snapshot",
        title_en_parent=parent[1] if parent else "",
        title_ja_parent=parent[5] if parent else "",
        title_en_parent_code=row[2] or "",
        title_ja_parent_code=row[2] or "",
        parent=row[2],
        level=row[3],
        dots=row[4],
        ancestors=ancestors,
        path_title=" > ".join(records[c][1] for c in ancestors + [row[0]]),
        verified=True,
        selectable="/" in row[0],
        version="2026.04",
        source=source,
        language="en",
        children_count=len(data["children"].get(row[0], [])),
        concordance_ipc=list(row[6]),
    )


def lookup(code):
    row = _data()["records"].get(normalize(code))
    return _public(row) if row else None


def parent(code):
    row = lookup(code)
    return lookup(row["parent"]) if row and row.get("parent") else None


def children(code=None):
    data = _data()
    return [_public(data["records"][key]) for key in data["children"].get(normalize(code) if code else None, [])]


def search(text, limit=30):
    text = unicodedata.normalize("NFKC", str(text)).casefold().strip()
    if not text:
        return []
    if re.fullmatch(r"[a-h][0-9a-z/,@]*", text.replace(" ", "")):
        return [
            _public(row) for row in _data()["records"].values() if row[0].casefold().startswith(text.replace(" ", ""))
        ][:limit]
    from classification_retrieval import search_definitions

    return search_definitions("FI", text, limit=limit)


def metadata():
    return copy.deepcopy(_data()["metadata"])
