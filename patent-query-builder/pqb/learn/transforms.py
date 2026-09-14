"""変換操作（Transform）の集合・適用・局所評価。企画書 §10.5 (b)。

| 操作 | 内容 | 方向 |
| ADD_TERM／DROP_TERM | 観点への語の追加・削除 | 追加=広げる（DB 実行要）、削除=狭める（局所評価可） |
| ADD_CODE／DROP_CODE | 分類コードの追加・削除 | 同上 |
| CODE_LEVEL_UP／DOWN | コード粒度の変更 | UP=広げる、DOWN=狭める |
| ADD_AXIS／DROP_AXIS | 補助観点を AND 軸に追加・削除 | 追加=狭める、削除=広げる |
| SPLIT_BLOCK／MERGE_BLOCK | 観点内の語群を 2 つの AND 軸に分割・統合 | 分割=狭める |
| TERM_CODE_JOIN | Block 内の語群とコード群の結合を OR ↔ AND | AND 化=狭める |
| FIELD_CHANGE | 対象フィールドの変更（TX → AB など） | 狭める方向のみ局所評価可 |
| PROXIMITY_ON／OFF | 近接演算子（方言が対応する場合。本実装では DSL に保持のみ） | 付与=狭める |
| ADD_EXCLUSION | NOT 条件の追加 | 既定で無効。有効時もプール文献を除外しないことを保証 |

狭める方向の操作は母集団 U の手元データで局所評価し、DB を叩かない。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..core.document import FIELDS
from ..core.dsl import Block, Code, Query, Term
from ..core.match import match_ids
from ..knowledge import codes as codelib
from ..util import new_id, norm_text

NARROW_OPS = {"DROP_TERM", "DROP_CODE", "CODE_LEVEL_DOWN", "ADD_AXIS", "SPLIT_BLOCK", "PROXIMITY_ON", "ADD_EXCLUSION"}
WIDEN_OPS = {"ADD_TERM", "ADD_CODE", "CODE_LEVEL_UP", "DROP_AXIS", "MERGE_BLOCK", "PROXIMITY_OFF"}
ALL_OPS = sorted(NARROW_OPS | WIDEN_OPS | {"TERM_CODE_JOIN", "FIELD_CHANGE"})


class TransformError(ValueError):
    pass


@dataclass
class Transform:
    op: str
    target: dict = field(default_factory=dict)
    source: str = "rsj"           # rsj | tree | llm | human
    reason: str = ""
    direction: str = ""           # narrow | widen
    transform_id: str = field(default_factory=lambda: new_id("tf-"))
    pred_hits: int | None = None
    pred_recall_pool: float | None = None
    pred_p_at_k: float | None = None
    pred_pool_hits: int | None = None
    local_eval: bool = False
    regression: bool = False
    status: str = "candidate"

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("pred_recall_pool", "pred_p_at_k"):
            if d[k] is not None:
                d[k] = round(d[k], 4)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Transform":
        return cls(op=d["op"], target=dict(d.get("target") or {}), source=d.get("source", "human"),
                   reason=d.get("reason", ""), direction=d.get("direction", ""),
                   transform_id=d.get("transform_id") or new_id("tf-"), pred_hits=d.get("pred_hits"),
                   pred_recall_pool=d.get("pred_recall_pool"), pred_p_at_k=d.get("pred_p_at_k"),
                   pred_pool_hits=d.get("pred_pool_hits"), local_eval=bool(d.get("local_eval")),
                   regression=bool(d.get("regression")), status=d.get("status", "candidate"))


def direction_of(op: str, target: dict, query: Query | None = None) -> str:
    if op == "COMPOSITE":
        return "narrow"
    if op in NARROW_OPS:
        return "narrow"
    if op in WIDEN_OPS:
        return "widen"
    if op == "TERM_CODE_JOIN":
        return "narrow" if (target.get("join") or "AND") == "AND" else "widen"
    if op == "FIELD_CHANGE":
        new = set(target.get("fields") or [])
        old: set[str] = set()
        if query is not None:
            b = query.block(target.get("axis_id", ""))
            if b:
                for t in b.adopted_terms():
                    old.update(t.fields)
        if "TX" in new and "TX" not in old:
            return "widen"
        if old and new <= old:
            return "narrow"
        return "narrow" if "TX" in old and "TX" not in new else "widen"
    return "unknown"


def summarize_query(query: Query) -> dict:
    return {"variant": query.variant, "blocks": [
        {"axis_id": b.axis_id, "axis_name": b.axis_name, "required": b.required, "join": b.effective_join(),
         "fields": sorted({f for t in b.adopted_terms() for f in t.fields}, key=FIELDS.index),
         "terms": [t.text for t in b.adopted_terms()],
         "codes": [{"scheme": c.scheme, "code": c.code, "level": c.level} for c in b.adopted_codes()]}
        for b in query.active_blocks()],
        "exclusions": [{"terms": [t.text for t in b.adopted_terms()],
                        "codes": [{"scheme": c.scheme, "code": c.code} for c in b.adopted_codes()]}
                       for b in query.active_exclusions()]}


# ------------------------------------------------------------------ 適用

def _need_block(query: Query, axis_id: str) -> Block:
    b = query.block(axis_id)
    if b is None:
        raise TransformError(f"観点 {axis_id} が検索式にありません")
    return b


def apply_transform(query: Query, t: Transform, iteration: int = 0, exclusions_enabled: bool = False) -> Query:
    q = query.copy()
    tg = t.target
    op = t.op
    origin = tg.get("origin") or f"{t.source}:iter{iteration}"
    if op == "COMPOSITE":
        steps = tg.get("steps") or []
        if not steps:
            raise TransformError("複合変換に手順がありません")
        for step in steps:
            q = apply_transform(q, Transform.from_dict(step), iteration, exclusions_enabled)
        t.direction = t.direction or "narrow"
        return q
    if op == "ADD_TERM":
        b = _need_block(q, tg["axis_id"])
        text = str(tg["text"]).strip()
        fields = list(tg.get("fields") or (b.adopted_terms()[0].fields if b.adopted_terms() else ["AB", "CL"]))
        for term in b.terms:
            if norm_text(term.text) == norm_text(text):
                term.status = "adopted"
                term.fields = fields
                return q
        b.terms.append(Term(text=text, fields=fields, origin=origin, status="adopted"))
    elif op == "DROP_TERM":
        b = _need_block(q, tg["axis_id"])
        hit = False
        for term in b.terms:
            if norm_text(term.text) == norm_text(str(tg["text"])) and term.status == "adopted":
                term.status = "rejected"
                hit = True
        if not hit:
            raise TransformError(f"語 {tg['text']} は観点 {tg['axis_id']} に採用されていません")
    elif op == "ADD_CODE":
        b = _need_block(q, tg["axis_id"])
        scheme, code = tg["scheme"], codelib.normalize(tg["scheme"], tg["code"])
        for c in b.codes:
            if c.scheme == scheme and c.code == code:
                c.status = "adopted"
                return q
        b.codes.append(Code(scheme=scheme, code=code, level=codelib.level_of(scheme, code), origin=origin,
                            status="adopted", title=tg.get("title", "")))
    elif op == "DROP_CODE":
        b = _need_block(q, tg["axis_id"])
        code = codelib.normalize(tg["scheme"], tg["code"])
        hit = False
        for c in b.codes:
            if c.scheme == tg["scheme"] and c.code == code and c.status == "adopted":
                c.status = "rejected"
                hit = True
        if not hit:
            raise TransformError(f"コード {tg['scheme']} {tg['code']} は採用されていません")
    elif op in ("CODE_LEVEL_UP", "CODE_LEVEL_DOWN"):
        b = _need_block(q, tg["axis_id"])
        code = codelib.normalize(tg["scheme"], tg["code"])
        new_code = codelib.normalize(tg["scheme"], tg.get("new_code") or "")
        if not new_code:
            new_code = codelib.coarser(tg["scheme"], code) if op == "CODE_LEVEL_UP" else ""
        if not new_code:
            raise TransformError("new_code が必要です（CODE_LEVEL_DOWN は細かいコードを指定）")
        hit = False
        for c in b.codes:
            if c.scheme == tg["scheme"] and c.code == code and c.status == "adopted":
                c.status = "rejected"
                hit = True
        if not hit:
            raise TransformError(f"コード {tg['scheme']} {tg['code']} は採用されていません")
        b.codes.append(Code(scheme=tg["scheme"], code=new_code, level=codelib.level_of(tg["scheme"], new_code),
                            origin=origin, status="adopted", title=tg.get("title", "")))
    elif op == "ADD_AXIS":
        b = q.block(tg["axis_id"])
        terms = [Term(text=x["text"], fields=list(x.get("fields") or ["AB", "CL"]), origin=x.get("origin", origin))
                 for x in tg.get("terms") or []]
        codes = [Code(scheme=x["scheme"], code=codelib.normalize(x["scheme"], x["code"]),
                      level=codelib.level_of(x["scheme"], x["code"]), origin=x.get("origin", origin),
                      title=x.get("title", "")) for x in tg.get("codes") or []]
        if b is None:
            b = Block(axis_id=tg["axis_id"], axis_name=tg.get("name", ""), required=False)
            q.blocks.append(b)
        if not b.adopted_terms() and not b.adopted_codes() and not terms and not codes:
            raise TransformError(f"観点 {tg['axis_id']} に追加する語・コードがありません")
        b.terms.extend(terms)
        b.codes.extend(codes)
    elif op == "DROP_AXIS":
        b = _need_block(q, tg["axis_id"])
        if len(q.active_blocks()) <= 1:
            raise TransformError("最後の観点は外せません")
        q.blocks = [x for x in q.blocks if x.axis_id != tg["axis_id"]]
    elif op == "SPLIT_BLOCK":
        b = _need_block(q, tg["axis_id"])
        move = {norm_text(x) for x in tg.get("terms") or []}
        moving = [term for term in b.adopted_terms() if norm_text(term.text) in move]
        if not moving or len(moving) == len(b.adopted_terms()):
            raise TransformError("分割する語の指定が不正です（一部の語を指定）")
        for term in moving:
            term.status = "rejected"
        new_id_ = tg.get("new_axis_id") or f"{b.axis_id}2"
        q.blocks.append(Block(axis_id=new_id_, axis_name=tg.get("name", f"{b.axis_name}（分割）"), required=b.required,
                              terms=[Term(text=term.text, fields=list(term.fields), origin=term.origin) for term in moving]))
    elif op == "MERGE_BLOCK":
        b = _need_block(q, tg["axis_id"])
        other = _need_block(q, tg["other_axis_id"])
        b.terms.extend(other.adopted_terms())
        b.codes.extend(other.adopted_codes())
        q.blocks = [x for x in q.blocks if x.axis_id != other.axis_id]
    elif op == "TERM_CODE_JOIN":
        b = _need_block(q, tg["axis_id"])
        join = (tg.get("join") or "AND").upper()
        if join not in ("AND", "OR"):
            raise TransformError("join は AND か OR")
        b.term_code_join = join
    elif op == "FIELD_CHANGE":
        b = _need_block(q, tg["axis_id"])
        fields = [f for f in FIELDS if f in set(tg.get("fields") or [])]
        if not fields:
            raise TransformError("fields が空です")
        for term in b.terms:
            term.fields = list(fields)
    elif op in ("PROXIMITY_ON", "PROXIMITY_OFF"):
        b = _need_block(q, tg["axis_id"])
        b.proximity = int(tg.get("distance") or 5) if op == "PROXIMITY_ON" else None
    elif op == "ADD_EXCLUSION":
        if not exclusions_enabled:
            raise TransformError("NOT（除外条件）は既定で無効です（config.exclusions_enabled）")
        terms = [Term(text=x["text"], fields=list(x.get("fields") or ["AB", "CL"]), origin=origin) for x in tg.get("terms") or []]
        codes = [Code(scheme=x["scheme"], code=codelib.normalize(x["scheme"], x["code"]), origin=origin) for x in tg.get("codes") or []]
        if not terms and not codes:
            raise TransformError("除外する語・コードがありません")
        q.exclusions.append(Block(axis_id=f"X{len(q.exclusions) + 1}", axis_name="除外", required=False, terms=terms, codes=codes))
    else:
        raise TransformError(f"未知の操作: {op}")
    if not t.direction:
        t.direction = direction_of(op, tg, query)
    return q


# ------------------------------------------------------------------ 局所評価

def local_evaluate(t: Transform, base_query: Query, population, pool_ids: set[str],
                   relevant: dict[str, bool] | None = None, k: int = 30, iteration: int = 0,
                   exclusions_enabled: bool = False) -> Transform:
    """母集団 U（population: rank 順の Document 列）の内側で変換後の式を評価する。"""
    relevant = relevant or {}
    if not t.direction:
        t.direction = direction_of(t.op, t.target, base_query)
    try:
        new_q = apply_transform(base_query, t, iteration, exclusions_enabled)
    except TransformError as e:
        t.status = "invalid"
        t.reason = (t.reason + " / " if t.reason else "") + str(e)
        return t
    base_hits = match_ids(base_query, population)
    new_hits = match_ids(new_q, population)
    t.pred_hits = len(new_hits)
    if pool_ids:
        t.pred_pool_hits = len(new_hits & pool_ids)
        t.pred_recall_pool = t.pred_pool_hits / len(pool_ids)
        t.regression = bool((base_hits & pool_ids) - new_hits)
    ranked = [d.doc_id for d in population if d.doc_id in new_hits][:k]
    judged = [relevant[d] for d in ranked if d in relevant]
    t.pred_p_at_k = (sum(judged) / len(judged)) if judged else None
    t.local_eval = t.direction == "narrow"
    if t.regression:
        t.status = "rejected"
        t.reason = (t.reason + " / " if t.reason else "") + "退行検知: プールの文献を落とすため棄却"
    return t


# ------------------------------------------------------------------ 統計からの候補生成

def _axis_for_text(query: Query, text: str, axis_terms: dict[str, list[str]] | None = None) -> str:
    """語を観点に割り当てる（既存の語との文字列重なり）。迷う場合は最初の Block（assigned=auto）。"""
    aid, _ = assign_axis(query, text, axis_terms)
    return aid


def assign_axis(query: Query, text: str, axis_terms: dict[str, list[str]] | None = None) -> tuple[str, bool]:
    """→ (axis_id, matched)。matched=False は自動割当（要確認）。"""
    best, best_len = None, 0
    table: dict[str, list[str]] = {b.axis_id: [t.text for t in b.adopted_terms()] for b in query.active_blocks()}
    for aid, terms in (axis_terms or {}).items():
        table.setdefault(aid, []).extend(terms)
    for aid, terms in table.items():
        for t in terms:
            ov = _overlap(norm_text(text), norm_text(t))
            if ov > best_len and ov >= 2:
                best, best_len = aid, ov
    if best is None:
        blocks = query.active_blocks()
        return (blocks[0].axis_id if blocks else "A"), False
    return best, True


def _overlap(a: str, b: str) -> int:
    best = 0
    for i in range(len(a)):
        for j in range(i + 2, len(a) + 1):
            if a[i:j] in b:
                best = max(best, j - i)
    return best


def propose_from_stats(query: Query, stats: dict, aux_axes: list[dict], iteration: int,
                       max_each: int = 5, code_ratio: float = 0.5,
                       axis_terms: dict[str, list[str]] | None = None) -> list[Transform]:
    """RSJ 統計（rank_candidates の出力）から変換候補を列挙する。"""
    out: list[Transform] = []
    in_terms = {norm_text(t.text): b.axis_id for b in query.active_blocks() for t in b.adopted_terms()}
    in_codes = {(c.scheme, c.code): b.axis_id for b in query.active_blocks() for c in b.adopted_codes()}
    in_blocks = {b.axis_id for b in query.active_blocks()}
    protected = {norm_text(t.text) for b in query.active_blocks() for t in b.adopted_terms() if t.origin == "input"}
    N = stats.get("N") or 0
    for s in stats.get("terms", [])[:max_each * 2]:
        if norm_text(s["feature"]) in in_terms:
            continue
        aid, matched = assign_axis(query, s["feature"], axis_terms)
        if aid not in in_blocks:
            continue     # 式に無い（補助）観点への語追加は候補表（G2）で扱う
        target = {"axis_id": aid, "text": s["feature"]}
        if not matched:
            target["assigned"] = "auto"
        out.append(Transform("ADD_TERM", target, source="rsj",
                             reason=f"RSJ w={s['w']} OW={s['ow']}（r={s['r']}/n={s['n']}）" + ("／観点は自動割当（要確認）" if not matched else ""),
                             direction="widen"))
        if sum(1 for t in out if t.op == "ADD_TERM") >= max_each:
            break
    for s in stats.get("negative_terms", []):
        aid = in_terms.get(norm_text(s["feature"]))
        if aid and norm_text(s["feature"]) not in protected:     # 入力由来の語は削除候補にしない
            out.append(Transform("DROP_TERM", {"axis_id": aid, "text": s["feature"]}, source="rsj",
                                 reason=f"非適合側に偏る語 w={s['w']}（n={s['n']}）", direction="narrow"))
    for s in stats.get("codes", [])[:max_each * 2]:
        key = (s["scheme"], s["feature"])
        if key in in_codes:
            continue
        # 既に採用中の祖先／子孫コードがあれば粒度変更として提案
        related = [(sc, c) for (sc, c) in in_codes if sc == s["scheme"] and
                   (codelib.matches(sc, c, s["feature"]) or codelib.matches(sc, s["feature"], c))]
        if related:
            sc, c = related[0]
            finer = len(codelib.levels(sc, s["feature"])) > len(codelib.levels(sc, c))
            out.append(Transform("CODE_LEVEL_DOWN" if finer else "CODE_LEVEL_UP",
                                 {"axis_id": in_codes[(sc, c)], "scheme": sc, "code": c, "new_code": s["feature"]},
                                 source="rsj", reason=f"粒度変更 OW={s['ow']}（n={s['n']}/N={N}）",
                                 direction="narrow" if finer else "widen"))
        else:
            out.append(Transform("ADD_CODE", {"axis_id": _axis_for_text(query, s.get("title") or ""),
                                              "scheme": s["scheme"], "code": s["feature"]},
                                 source="rsj", reason=f"RSJ w={s['w']} OW={s['ow']}（r={s['r']}/n={s['n']}）",
                                 direction="widen"))
    for s in stats.get("negative_codes", []):
        aid = in_codes.get((s["scheme"], s["feature"]))
        if aid:
            out.append(Transform("DROP_CODE", {"axis_id": aid, "scheme": s["scheme"], "code": s["feature"]},
                                 source="rsj", reason=f"非適合側に偏る分類 w={s['w']}（n={s['n']}）", direction="narrow"))
    present = {b.axis_id for b in query.active_blocks()}
    for a in aux_axes:
        if a["axis_id"] in present or not (a.get("terms") or a.get("codes")):
            continue
        out.append(Transform("ADD_AXIS", {"axis_id": a["axis_id"], "name": a.get("name", ""),
                                          "terms": a.get("terms") or [], "codes": a.get("codes") or []},
                             source="rsj", reason="補助観点を AND 軸に追加", direction="narrow"))
    for b in query.active_blocks():
        if b.adopted_terms() and b.adopted_codes() and b.effective_join() == "OR":
            out.append(Transform("TERM_CODE_JOIN", {"axis_id": b.axis_id, "join": "AND"}, source="rsj",
                                 reason="語群とコード群を AND にして狭める", direction="narrow"))
        fields = {f for t in b.adopted_terms() for f in t.fields}
        if "TX" in fields:
            out.append(Transform("FIELD_CHANGE", {"axis_id": b.axis_id, "fields": ["AB", "CL"]}, source="rsj",
                                 reason="全文 → 要約・請求の範囲に絞る", direction="narrow"))
    return dedupe_transforms(out)


def dedupe_transforms(ts: list[Transform]) -> list[Transform]:
    seen: set[str] = set()
    out = []
    for t in ts:
        key = t.op + ":" + str(sorted((k, str(v)) for k, v in t.target.items() if k in ("axis_id", "text", "scheme", "code", "new_code", "join", "fields", "other_axis_id")))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out
