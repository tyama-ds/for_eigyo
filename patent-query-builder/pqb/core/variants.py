"""観点×語×分類の表から 3 案（広め／標準／狭め）の DSL を組む。企画書 §9.2 の variant 規則。

| variant  | Block の選択                                   | 語群とコード群 |
| broad    | 必須観点から AND 軸を 1 つ減らす（分類コードを持つ観点を残す）。
|          | フィールドは広い側（TX）、コードは一段粗い粒度       | OR |
| standard | 必須観点すべて。フィールドは AB・CL                  | OR |
| narrow   | 必須観点＋補助観点                                | 分類がある Block では AND |
"""
from __future__ import annotations

from ..knowledge import codes as codelib
from .dsl import Block, Code, DateRange, Query, Term

DEFAULT_FIELDS = {"broad": ["TX"], "standard": ["AB", "CL"], "narrow": ["AB", "CL"]}


def _terms_for(axis_id: str, candidates: list[dict], fields: list[str]) -> list[Term]:
    out = []
    for c in candidates:
        if c.get("kind") != "term" or c.get("axis_id") != axis_id:
            continue
        out.append(Term(text=c["value"], fields=list(c.get("fields") or fields),
                        origin=c.get("origin", "input"), status=c.get("status", "candidate"),
                        note=c.get("note", "") or ""))
    return out


def _codes_for(axis_id: str, candidates: list[dict], level_up: bool = False) -> list[Code]:
    out = []
    seen = set()
    for c in candidates:
        if c.get("kind") != "code" or c.get("axis_id") != axis_id:
            continue
        scheme, code = c.get("scheme", "FI"), c["value"]
        status = c.get("status", "candidate")
        if level_up and status == "adopted":
            up = codelib.coarser(scheme, code)
            if up:
                code = up
        key = (scheme, code, status)
        if key in seen:
            continue
        seen.add(key)
        out.append(Code(scheme=scheme, code=code, level=codelib.level_of(scheme, code),
                        origin=c.get("origin", "input"), status=status,
                        title=c.get("title", "") or ""))
    return out


def choose_broad_drop(axes: list[dict], candidates: list[dict]) -> str | None:
    """広め案で外す必須観点。分類コードを持つ観点を優先して残す。必須が 1 つなら None。"""
    required = [a for a in axes if a.get("kind", "required") == "required"]
    if len(required) < 2:
        return None

    def has_codes(a: dict) -> bool:
        return any(c.get("kind") == "code" and c.get("status") == "adopted"
                   and c.get("axis_id") == a["axis_id"] for c in candidates)

    def n_terms(a: dict) -> int:
        return sum(1 for c in candidates if c.get("kind") == "term" and c.get("status") == "adopted"
                   and c.get("axis_id") == a["axis_id"])

    without = [a for a in required if not has_codes(a)]
    pool = without or required
    # 語が少ない（= 弱い）観点から外す。同点なら後ろの観点
    pool = sorted(pool, key=lambda a: (n_terms(a), -required.index(a)))
    return pool[0]["axis_id"]


def build_variants(case: dict, axes: list[dict], candidates: list[dict], iteration: int,
                   fields_cfg: dict | None = None, broad_drop_axis: str | None = None,
                   parent_ids: dict | None = None) -> tuple[dict, dict]:
    """→ ({variant: Query}, notes)"""
    fields_cfg = fields_cfg or DEFAULT_FIELDS
    parent_ids = parent_ids or {}
    case_id = case["case_id"]
    required = [a for a in axes if a.get("kind", "required") == "required"]
    auxiliary = [a for a in axes if a.get("kind") == "auxiliary"]
    drop = broad_drop_axis if broad_drop_axis is not None else choose_broad_drop(axes, candidates)
    notes = {"broad_drop_axis": drop, "iteration": iteration}

    def make(variant: str, use_axes: list[dict], fields: list[str], level_up: bool, and_join: bool) -> Query:
        blocks = []
        for a in use_axes:
            b = Block(axis_id=a["axis_id"], axis_name=a.get("name", ""),
                      required=a.get("kind", "required") == "required",
                      terms=_terms_for(a["axis_id"], candidates, fields),
                      codes=_codes_for(a["axis_id"], candidates, level_up=level_up))
            if and_join and b.adopted_codes() and b.adopted_terms():
                b.term_code_join = "AND"
            blocks.append(b)
        q = Query(query_id=f"{case_id}/v{iteration}/{variant}", case_id=case_id,
                  purpose=case.get("purpose", "prior_art"), variant=variant, blocks=blocks,
                  date_range=DateRange(from_=case.get("date_from") or None, to=case.get("date_to") or None),
                  countries=list(case.get("countries") or ["JP"]),
                  provenance={"iteration": iteration, "parent_query_id": parent_ids.get(variant)})
        return q

    broad_axes = [a for a in required if a["axis_id"] != drop] if drop else list(required)
    queries = {
        "broad": make("broad", broad_axes, fields_cfg.get("broad", ["TX"]), True, False),
        "standard": make("standard", required, fields_cfg.get("standard", ["AB", "CL"]), False, False),
        "narrow": make("narrow", required + auxiliary, fields_cfg.get("narrow", ["AB", "CL"]), False, True),
    }
    return queries, notes
