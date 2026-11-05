"""Score a search query against the labelled workspace rows.

The app cannot run queries on external databases. The only measurable proxy
is retrospective: how many human-labelled keep rows a query would match and how
many exclude rows it would let through. Scores here are proxies over the
imported CSV, never recall or precision on the real search population.
"""

from __future__ import annotations

import re
import unicodedata

from classification_symbols import parse_codes
from common import text_contains
from fi_parser import _raw, parse_fi
from query_formats import Node, build_tree, group, terms_of, tree_data, walk_tree

_CODE_FIELDS = (("IPC", "ipc"), ("CPC", "cpc"), ("F-term", "fterm"))


def row_codes(row):
    """Parsed classification codes per system from the row's own metadata.

    Malformed fields are skipped (never repaired). FI keeps the printed symbol
    with spaces removed so expansion suffixes stay comparable.
    """
    codes = {"IPC": set(), "CPC": set(), "F-term": set(), "FI": set()}
    for kind, field in _CODE_FIELDS:
        try:
            codes[kind].update(parse_codes(kind, _raw(row.get(field) or "")))
        except ValueError:
            continue
    try:
        for entry in parse_fi(_raw(row.get("fi") or ""), allow_compact_suffix=True):
            codes["FI"].add(re.sub(r"\s+", "", unicodedata.normalize("NFKC", entry["raw"])).upper())
    except ValueError:
        pass
    return codes


def _class_matches(node, codes):
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", node.value)).upper()
    own = codes.get(node.system, set())
    if node.system == "F-term":
        return value in own or any(code.startswith(value) for code in own)
    if node.system == "FI":
        base = value.split(",")[0].split("@")[0]
        return any(code == value or code.startswith(base) for code in own)
    # IPC/CPC: an upper symbol (B60W, B60W60/00) covers its subgroups.
    main = value.split("/")[0]
    return any(
        code == value or (value.endswith("/00") and code.startswith(main + "/")) or ("/" not in value and code.startswith(value))
        for code in own
    )


def evaluate(node, row, codes=None):
    """True when the row satisfies the Boolean tree on title/abstract and codes."""
    codes = row_codes(row) if codes is None else codes
    if node.op == "text":
        return text_contains(row, node.value)
    if node.op == "class":
        return _class_matches(node, codes)
    if node.op == "and":
        return all(evaluate(child, row, codes) for child in node.children)
    if node.op == "or":
        return any(evaluate(child, row, codes) for child in node.children)
    if node.op == "not":
        return evaluate(node.children[0], row, codes) and not evaluate(node.children[1], row, codes)
    raise ValueError("未対応の検索演算子です。")


def with_terms(tree, include=(), exclude=()):
    """Add include terms as one OR group ANDed, exclude terms as one NOT group."""
    include = [t for t in dict.fromkeys(include) if t]
    exclude = [t for t in dict.fromkeys(exclude) if t]
    if include:
        tree = group("and", [tree, group("or", [Node("text", t) for t in include])])
    if exclude:
        tree = Node("not", children=(tree, group("or", [Node("text", t) for t in exclude])))
    return tree


def score_tree(tree, rows, *, example_limit=20):
    """Retrospective counts over labelled rows. Predictions never count as labels."""
    counts = {"keep": [0, 0], "exclude": [0, 0], "unlabeled": [0, 0]}
    missed_keep, hit_exclude = [], []
    for row in rows:
        bucket = row.get("label") if row.get("label") in ("keep", "exclude") else "unlabeled"
        matched = evaluate(tree, row)
        counts[bucket][0] += 1
        if matched:
            counts[bucket][1] += 1
        if bucket == "keep" and not matched and len(missed_keep) < example_limit:
            missed_keep.append(row.get("id"))
        if bucket == "exclude" and matched and len(hit_exclude) < example_limit:
            hit_exclude.append(row.get("id"))
    keep_total, keep_hit = counts["keep"]
    exclude_total, exclude_hit = counts["exclude"]
    unlabeled_total, unlabeled_hit = counts["unlabeled"]
    return dict(
        keep_total=keep_total,
        keep_hit=keep_hit,
        exclude_total=exclude_total,
        exclude_hit=exclude_hit,
        unlabeled_total=unlabeled_total,
        unlabeled_hit=unlabeled_hit,
        keep_recall=(keep_hit / keep_total) if keep_total else None,
        exclude_leak=(exclude_hit / exclude_total) if exclude_total else None,
        missed_keep_ids=missed_keep,
        hit_exclude_ids=hit_exclude,
        expression_tree=tree_data(tree),
        note="取り込み済みの判定付き特許に対する再計算です。検索母集団全体の再現率・適合率ではありません。",
    )


def score_query(query, rows, *, include=(), exclude=()):
    """Score a saved query, optionally with extra include/exclude terms applied."""
    if not isinstance(query, dict):
        return None
    try:
        tree = build_tree(query)
    except ValueError as error:
        return dict(error=str(error), keep_total=0, keep_hit=0, exclude_total=0, exclude_hit=0)
    tree = with_terms(tree, include, exclude)
    result = score_tree(tree, rows)
    result["query_id"] = query.get("id")
    result["query_version"] = query.get("version")
    result["terms"] = terms_of(query)
    return result


def compare(base, candidate):
    """Delta between two score dicts (candidate minus base)."""
    if not base or not candidate or "error" in base or "error" in candidate:
        return None
    return dict(
        keep_hit=candidate["keep_hit"] - base["keep_hit"],
        exclude_hit=candidate["exclude_hit"] - base["exclude_hit"],
        unlabeled_hit=candidate["unlabeled_hit"] - base["unlabeled_hit"],
    )


def tree_terms(tree):
    return [node.value for node in walk_tree(tree) if node.op == "text"]
