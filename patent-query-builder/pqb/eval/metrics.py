"""評価指標とパレート判定。企画書 §5.4・§10.6。

- 既知文献再現率 recall_seed（必須条件: 1.0）
- 累積プール再現率 recall_pool = |Q ∩ Pool| / |Pool|
- 母集団サイズ |Q|、上位 K 件適合率 P@K、複雑さ complexity
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Metrics:
    hit_count: int = 0
    recall_seed: float | None = None
    recall_pool: float | None = None
    p_at_k: float | None = None
    k_judged: int = 0
    k: int = 0
    complexity: int = 0
    seeds_missing: list = field(default_factory=list)
    pool_missing: list = field(default_factory=list)
    seeds_total: int = 0
    pool_total: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("recall_seed", "recall_pool", "p_at_k"):
            if d[key] is not None:
                d[key] = round(d[key], 4)
        return d


def evaluate(hit_ids: set[str], hit_count: int | None, ranked_ids: list[str],
             seeds: set[str], pool: set[str], relevant: dict[str, bool] | None = None,
             k: int = 30, complexity: int = 0) -> Metrics:
    relevant = relevant or {}
    m = Metrics(hit_count=hit_count if hit_count is not None else len(hit_ids), k=k,
                complexity=complexity, seeds_total=len(seeds), pool_total=len(pool))
    if seeds:
        missing = sorted(s for s in seeds if s not in hit_ids)
        m.seeds_missing = missing
        m.recall_seed = 1.0 - len(missing) / len(seeds)
    if pool:
        missing = sorted(p for p in pool if p not in hit_ids)
        m.pool_missing = missing
        m.recall_pool = 1.0 - len(missing) / len(pool)
    top = [d for d in ranked_ids[:k] if d in relevant]
    m.k_judged = len(top)
    if top:
        m.p_at_k = sum(1 for d in top if relevant[d]) / len(top)
    return m


def satisfies_constraints(m: Metrics, tau_pool: float, require_seed: bool = True) -> bool:
    if require_seed and m.seeds_total and (m.recall_seed or 0.0) < 1.0:
        return False
    if m.pool_total and (m.recall_pool or 0.0) < tau_pool:
        return False
    return True


def _key(item: dict) -> tuple[float, float, float]:
    """(母集団サイズ↓, P@K↑, 複雑さ↓) を「小さいほど良い」に揃える。"""
    return (float(item.get("hit_count") or 0), -float(item.get("p_at_k") or 0.0),
            float(item.get("complexity") or 0))


def dominates(a: dict, b: dict) -> bool:
    ka, kb = _key(a), _key(b)
    return all(x <= y for x, y in zip(ka, kb)) and any(x < y for x, y in zip(ka, kb))


def pareto_front(items: list[dict]) -> list[dict]:
    """items は hit_count / p_at_k / complexity を持つ dict。制約判定は呼び出し側。"""
    front = []
    for it in items:
        if not any(dominates(other, it) for other in items if other is not it):
            front.append(it)
    return front


def is_pareto_improvement(new: dict, old: dict) -> bool:
    return dominates(new, old)


def in_range(hit_count: int | None, population_range) -> bool | None:
    if hit_count is None or not population_range:
        return None
    lo, hi = population_range
    return lo <= hit_count <= hi


def select_by_policy(front: list[dict], selection: str, population_range) -> dict | None:
    """調査種別ごとの選択方針（§10.6）で 1 案を選ぶ。"""
    if not front:
        return None
    if selection == "recall_first":
        return sorted(front, key=lambda x: (-(x.get("recall_pool") or 0.0), x.get("hit_count") or 0))[0]
    if selection == "range_then_p_at_k":
        ranged = [x for x in front if in_range(x.get("hit_count"), population_range)] or front
        return sorted(ranged, key=lambda x: (-(x.get("p_at_k") or 0.0), x.get("hit_count") or 0))[0]
    ranged = [x for x in front if in_range(x.get("hit_count"), population_range)] or front
    return sorted(ranged, key=lambda x: (x.get("complexity") or 0, x.get("hit_count") or 0))[0]
