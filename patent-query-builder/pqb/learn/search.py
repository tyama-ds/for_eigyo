"""目的関数付きの変換探索（GEPA 接続）。企画書 §10.6。

- 変異 = 変換操作（狭める方向。母集団 U の手元データで局所評価できるもの）
- 目的 = パレート軸（母集団サイズ↓、上位K適合率↑、複雑さ↓）
- 制約 = 既知文献再現率 1.0、累積プール再現率 ≥ τ（いずれも U の内側で評価）
- 反省的変異 = P5（前世代の結果と失敗理由をフィードバックとして渡し、意味的な変換を提案させる）

評価が検索の機械指標なので LLM-as-judge より安定する。広げる方向の変換は DB 実行が要るため
ここでは扱わず、単独の候補として G4 に残す。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.document import Document
from ..core.dsl import Query
from ..core.match import match_ids
from ..eval.metrics import pareto_front
from .transforms import Transform, TransformError, apply_transform


@dataclass
class Candidate:
    steps: list = field(default_factory=list)     # list[Transform]
    query: Query | None = None
    hit_count: int = 0
    recall_pool: float | None = None
    recall_seed: float | None = None
    p_at_k: float | None = None
    complexity: int = 0
    feasible: bool = False
    lost_pool: list = field(default_factory=list)

    def key(self) -> str:
        return "|".join(sorted(_step_key(t) for t in self.steps))

    def metrics(self) -> dict:
        return {"hit_count": self.hit_count, "recall_pool": self.recall_pool, "recall_seed": self.recall_seed,
                "p_at_k": self.p_at_k, "complexity": self.complexity, "feasible": self.feasible}

    def to_dict(self) -> dict:
        d = self.metrics()
        d["steps"] = [t.to_dict() for t in self.steps]
        d["lost_pool"] = self.lost_pool[:10]
        return d


def _step_key(t: Transform) -> str:
    return t.op + ":" + str(sorted((k, str(v)) for k, v in t.target.items()
                                   if k in ("axis_id", "text", "scheme", "code", "new_code", "join", "fields", "other_axis_id")))


def evaluate_query(q: Query, population: list[Document], pool_ids: set[str], seed_ids: set[str],
                   relevant: dict[str, bool], k: int, tau: float) -> dict:
    u_ids = [d.doc_id for d in population]
    hits = match_ids(q, population)
    pool_in = pool_ids & set(u_ids)
    seeds_in = seed_ids & set(u_ids)
    recall_pool = (len(hits & pool_in) / len(pool_in)) if pool_in else None
    recall_seed = (len(hits & seeds_in) / len(seeds_in)) if seeds_in else None
    ranked = [d for d in u_ids if d in hits][:k]
    judged = [relevant[d] for d in ranked if d in relevant]
    p_at_k = (sum(judged) / len(judged)) if judged else None
    feasible = (recall_seed in (None, 1.0)) and (recall_pool is None or recall_pool >= tau)
    return {"hits": hits, "hit_count": len(hits), "recall_pool": recall_pool, "recall_seed": recall_seed,
            "p_at_k": p_at_k, "complexity": q.complexity(), "feasible": feasible,
            "lost_pool": sorted(pool_in - hits)}


def pareto_search(base: Query, population: list[Document], *, pool_ids: set[str], seed_ids: set[str],
                  relevant: dict[str, bool], mutations: list[Transform], tau: float, k: int = 30,
                  generations: int = 3, beam: int = 6, max_evals: int = 120, min_steps: int = 2,
                  exclusions_enabled: bool = False, reflect=None, iteration: int = 0) -> dict:
    """狭める変換の合成を探索し、制約を満たしパレート改善する複合変換のフロントを返す。

    reflect(context) -> list[Transform]: 反省的変異（P5）。context には前世代のフロントと失敗理由が入る。
    """
    base_m = evaluate_query(base, population, pool_ids, seed_ids, relevant, k, tau)
    root = Candidate(steps=[], query=base, **{kk: base_m[kk] for kk in ("hit_count", "recall_pool", "recall_seed", "p_at_k", "complexity", "feasible")},
                     lost_pool=base_m["lost_pool"])
    pool: list[Transform] = [t for t in mutations if t.direction == "narrow" and t.op != "ADD_EXCLUSION"]
    seen: set[str] = {root.key()}
    evaluated = 0
    frontier: list[Candidate] = [root]
    all_feasible: list[Candidate] = []
    failures: list[dict] = []
    gen_done = 0
    for gen in range(1, generations + 1):
        children: list[Candidate] = []
        for parent in frontier:
            used = {_step_key(t) for t in parent.steps}
            for mut in pool:
                if evaluated >= max_evals:
                    break
                if _step_key(mut) in used:
                    continue
                try:
                    q = apply_transform(parent.query, mut, iteration, exclusions_enabled)
                except TransformError:
                    continue
                cand = Candidate(steps=parent.steps + [mut], query=q)
                if cand.key() in seen:
                    continue
                seen.add(cand.key())
                m = evaluate_query(q, population, pool_ids, seed_ids, relevant, k, tau)
                evaluated += 1
                for kk in ("hit_count", "recall_pool", "recall_seed", "p_at_k", "complexity", "feasible"):
                    setattr(cand, kk, m[kk])
                cand.lost_pool = m["lost_pool"]
                if cand.feasible:
                    children.append(cand)
                    all_feasible.append(cand)
                elif len(failures) < 20:
                    failures.append({"steps": [_step_key(t) for t in cand.steps], "lost_pool": cand.lost_pool[:5],
                                     "recall_pool": cand.recall_pool, "recall_seed": cand.recall_seed})
            if evaluated >= max_evals:
                break
        gen_done = gen
        if not children:
            break
        # パレートフロント（母集団↓ P@K↑ 複雑さ↓）から次世代のビームを選ぶ
        front = pareto_front([dict(c.metrics(), _obj=c) for c in children])
        ranked = sorted((f["_obj"] for f in front), key=lambda c: (c.hit_count, -(c.p_at_k or 0), c.complexity))
        frontier = ranked[:beam]
        if reflect is not None and gen < generations and evaluated < max_evals:
            try:
                extra = reflect({"generation": gen, "front": [c.to_dict() for c in frontier[:3]], "failures": failures[:5],
                                 "base": root.metrics()})
            except Exception:      # noqa: BLE001 — 反省的変異の失敗は探索を止めない
                extra = []
            known = {_step_key(t) for t in pool}
            for t in extra or []:
                if t.direction == "narrow" and _step_key(t) not in known:
                    pool.append(t)
                    known.add(_step_key(t))
        if evaluated >= max_evals:
            break
    # 最終フロント: 実行可能で base をパレート改善する複合変換（min_steps 以上）
    improving = [c for c in all_feasible if len(c.steps) >= min_steps and _dominates(c, root)]
    front = pareto_front([dict(c.metrics(), _obj=c) for c in improving]) if improving else []
    result = sorted((f["_obj"] for f in front), key=lambda c: (c.hit_count, -(c.p_at_k or 0), c.complexity))
    return {"base": root.metrics(), "front": [c.to_dict() for c in result[:beam]], "evaluated": evaluated,
            "generations": gen_done, "n_feasible": len(all_feasible), "n_mutations": len(pool),
            "candidates": result[:beam]}


def _dominates(c: Candidate, base: Candidate) -> bool:
    better_or_equal = (c.hit_count <= base.hit_count and (c.p_at_k or 0) >= (base.p_at_k or 0))
    strictly = c.hit_count < base.hit_count or (c.p_at_k or 0) > (base.p_at_k or 0)
    keeps_pool = (c.recall_pool is None) or (base.recall_pool is None) or c.recall_pool >= base.recall_pool
    return better_or_equal and strictly and keeps_pool


def composite_transform(cand: Candidate, base_metrics: dict) -> Transform:
    steps = [t.to_dict() for t in cand.steps]
    label = " → ".join(f"{t.op}({_short_target(t.target)})" for t in cand.steps)
    t = Transform("COMPOSITE", {"steps": steps, "label": label}, source="search",
                  reason=f"複合変換（探索）: 母集団 {base_metrics.get('hit_count')} → {cand.hit_count}、P@K {_f(base_metrics.get('p_at_k'))} → {_f(cand.p_at_k)}",
                  direction="narrow")
    t.pred_hits = cand.hit_count
    t.pred_recall_pool = cand.recall_pool
    t.pred_p_at_k = cand.p_at_k
    t.local_eval = True
    t.regression = False
    return t


def _short_target(tg: dict) -> str:
    for k in ("text", "code", "axis_id", "join", "fields"):
        if tg.get(k) not in (None, "", []):
            return f"{tg[k]}" if k != "code" else f"{tg.get('scheme', '')} {tg[k]}".strip()
    return ""


def _f(v) -> str:
    return "—" if v is None else f"{v:.2f}"
