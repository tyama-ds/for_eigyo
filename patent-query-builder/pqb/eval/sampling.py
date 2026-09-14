"""無作為標本による再現率推定。企画書 §10.8。

1. 母集団 U から無作為に m 件抽出（乱数シードを記録）し適合度を判定する
2. 標本中の適合文献数 s、そのうち狭い式 Q に含まれるもの t
3. recall_hat(Q | U) = t / s。信頼区間は Wilson 区間（試行数 s、成功数 t）
4. p_hat = s / m から U 内の適合文献総数の推定 p_hat × |U| を併記する

限界: 推定は U の内側に対するもの。広め案が取り逃した文献は測れない。
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass


def draw_sample(ids, m: int, seed: int, exclude=()) -> list[str]:
    pool = [i for i in ids if i not in set(exclude)]
    rng = random.Random(seed)
    if m >= len(pool):
        rng.shuffle(pool)
        return pool
    return rng.sample(pool, m)


def stratified_sample(strata: dict[str, list[str]], m: int, seed: int, exclude=()) -> list[str]:
    """層（例: 分類コード粒度）ごとに比例配分して抽出する（分散低減オプション）。"""
    ex = set(exclude)
    strata = {k: [i for i in v if i not in ex] for k, v in strata.items()}
    total = sum(len(v) for v in strata.values())
    if total == 0:
        return []
    rng = random.Random(seed)
    out: list[str] = []
    for key in sorted(strata):
        ids = strata[key]
        take = round(m * len(ids) / total)
        take = min(len(ids), take)
        out.extend(rng.sample(ids, take) if take < len(ids) else ids)
    return out


def wilson_interval(t: int, s: int, z: float = 1.96) -> tuple[float, float]:
    if s <= 0:
        return (0.0, 1.0)
    p = t / s
    denom = 1 + z * z / s
    centre = (p + z * z / (2 * s)) / denom
    half = z * math.sqrt(p * (1 - p) / s + z * z / (4 * s * s)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class Estimate:
    m: int
    s: int
    t: int
    recall_hat: float | None
    ci_low: float
    ci_high: float
    low_confidence: bool
    p_hat: float | None
    est_relevant_total: float | None
    population_size: int
    seed: int | None = None
    basis: str = "U"

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("recall_hat", "ci_low", "ci_high", "p_hat"):
            if d[k] is not None:
                d[k] = round(d[k], 4)
        if d["est_relevant_total"] is not None:
            d["est_relevant_total"] = round(d["est_relevant_total"], 1)
        return d


def estimate_recall(sample_judgments: dict[str, bool], q_hits: set[str], population_size: int,
                    s_min: int = 30, z: float = 1.96, seed: int | None = None) -> Estimate:
    m = len(sample_judgments)
    relevant = [d for d, rel in sample_judgments.items() if rel]
    s = len(relevant)
    t = sum(1 for d in relevant if d in q_hits)
    lo, hi = wilson_interval(t, s, z)
    return Estimate(
        m=m, s=s, t=t, recall_hat=(t / s) if s else None, ci_low=lo, ci_high=hi,
        low_confidence=s < s_min, p_hat=(s / m) if m else None,
        est_relevant_total=(s / m * population_size) if m else None,
        population_size=population_size, seed=seed,
    )
