"""Offline F-term navigation from the official PMGS 5H029 table.

Only the bundled theme is supported. Parent links were extracted from the
official table's order and dot indentation, never from numerical prefixes.
Theme/aspect nodes are navigation containers, not selectable search terms.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata


_RESOURCE = Path(__file__).resolve().parent / "resources" / "fterm_5h029.json"
_THEMES_RESOURCE = Path(__file__).resolve().parent / 'resources' / 'fterm' / 'themes_2026_06.json'


def _normalize(code):
    if not isinstance(code, str):
        return None
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", code)).upper()
    # Do not strip unknown suffixes or collapse an unverified additional code.
    if not re.fullmatch(r"[0-9][A-Z][0-9]{3}(?:[A-Z]{2}[0-9]{2})?", value):
        return None
    return value


@lru_cache(maxsize=1)
def _catalog():
    data = json.loads(_RESOURCE.read_text(encoding="utf-8"))
    index = {}
    by_parent = {}
    for node in data["nodes"]:
        code = node["code"]
        if code in index:
            raise ValueError(f"Duplicate F-term node: {code}")
        index[code] = node
        by_parent.setdefault(node["parent"], []).append(code)
    for node in index.values():
        parent_code = node["parent"]
        if parent_code is not None:
            ancestor = index.get(parent_code)
            if ancestor is None or ancestor["level"] != node["level"] - 1:
                raise ValueError(f"Invalid F-term hierarchy: {node['code']}")
    return data["metadata"], index, by_parent


def lookup(code):
    """Return a copy of the exact supported node, or None if unknown."""
    normalized = _normalize(code)
    if normalized is None:
        return None
    node = _catalog()[1].get(normalized)
    return deepcopy(node) if node is not None else None


def parent(code):
    """Return the official immediate parent, including navigation containers."""
    node = lookup(code)
    return lookup(node["parent"]) if node is not None else None


def children(code=None):
    """Return immediate children in official order; None lists supported themes."""
    _, index, by_parent = _catalog()
    if code is None:
        normalized = None
    else:
        normalized = _normalize(code)
        if normalized is None or normalized not in index:
            return []
    return [deepcopy(index[child]) for child in by_parent.get(normalized, [])]


def metadata():
    """Describe snapshot provenance, exact coverage, and verification limits."""
    return deepcopy(_catalog()[0])


@lru_cache(maxsize=1)
def _themes():
    data = json.loads(_THEMES_RESOURCE.read_text(encoding='utf-8'))
    return data['metadata'], {row['code']: row for row in data['themes']}


def theme_mapping(code):
    """Official theme coverage, separate from individual term validation."""
    normalized = _normalize(code)
    if normalized is None:
        return None
    meta, themes = _themes()
    row = themes.get(normalized[:5])
    if row is None:
        return None
    return dict(deepcopy(row), source=meta['source'], source_version=meta['version'], source_sha256=meta['source_sha256'])


def theme_metadata():
    return deepcopy(_themes()[0])
