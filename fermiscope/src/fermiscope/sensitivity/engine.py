"""感度分析エンジン。

- One-at-a-time(OAT): 1パラメータのみ low/high にした出力(トルネードチャート用)
- 局所弾力性: 有限差分による (dY/Y)/(dX/X)
- Spearman順位相関: モンテカルロサンプルとの順位相関
- 総合重要度: importance = 正規化感度 × 正規化不確実性 × 正規化批判重大度
"""

from __future__ import annotations

import numpy as np

from fermiscope.domain.models import (
    Critique,
    ModelCandidate,
    ParameterEstimate,
    SensitivityResult,
    SimulationResult,
)
from fermiscope.estimation.engine import deterministic_evaluate


def _elasticity(
    model: ModelCandidate,
    parameters: dict[str, ParameterEstimate],
    pid: str,
    central_output: float,
    rel_step: float = 0.01,
) -> float | None:
    p = parameters[pid]
    if p.central is None or p.central == 0 or central_output == 0:
        return None
    dx = p.central * rel_step
    try:
        up = deterministic_evaluate(model, parameters, {pid: p.central + dx})
        down = deterministic_evaluate(model, parameters, {pid: p.central - dx})
    except Exception:
        return None
    dy = (up - down) / 2.0
    return float((dy / central_output) / (dx / p.central))


def analyze_sensitivity(
    model: ModelCandidate,
    parameters: dict[str, ParameterEstimate],
    sim_result: SimulationResult,
    critiques: dict[str, Critique],
) -> list[SensitivityResult]:
    """モデルの全末端パラメータについて感度指標を計算する。"""
    leaf_ids = model.formula.leaf_parameter_ids()
    try:
        central_output = deterministic_evaluate(model, parameters)
    except Exception:
        return []

    results: list[SensitivityResult] = []
    raw_sens: dict[str, float] = {}
    raw_unc: dict[str, float] = {}
    raw_sev: dict[str, float] = {}

    for pid in leaf_ids:
        p = parameters[pid]
        low = p.low if p.low is not None else p.central
        high = p.high if p.high is not None else p.central
        oat_low = oat_high = None
        try:
            if low is not None:
                oat_low = deterministic_evaluate(model, parameters, {pid: float(low)})
            if high is not None:
                oat_high = deterministic_evaluate(model, parameters, {pid: float(high)})
        except Exception:  # noqa: S110 — 評価不能なパラメータはOAT対象外として続行
            pass

        oat_span = None
        direction: str = "unknown"
        if oat_low is not None and oat_high is not None:
            oat_span = abs(oat_high - oat_low)
            if oat_high > oat_low:
                direction = "increase"
            elif oat_high < oat_low:
                direction = "decrease"
            else:
                direction = "nonmonotonic"

        elasticity = _elasticity(model, parameters, pid, central_output)
        spearman = sim_result.parameter_spearman.get(pid)

        uncertainty_span = None
        if (
            p.central is not None
            and p.central != 0
            and p.low is not None
            and p.high is not None
        ):
            uncertainty_span = float(abs(p.high - p.low) / abs(p.central))

        severity = max(
            (c.severity for c in critiques.values() if c.parameter_id == pid),
            default=0.0,
        )

        raw_sens[pid] = abs(spearman) if spearman is not None else (abs(elasticity) if elasticity else 0.0)
        raw_unc[pid] = uncertainty_span or 0.0
        raw_sev[pid] = severity

        results.append(
            SensitivityResult(
                model_id=model.id,
                parameter_id=pid,
                parameter_name=p.name,
                oat_low_output=oat_low,
                oat_high_output=oat_high,
                oat_span=oat_span,
                elasticity=elasticity,
                spearman=spearman,
                uncertainty_span=uncertainty_span,
                direction=direction,  # type: ignore[arg-type]
                critique_severity=severity,
            )
        )

    # 正規化(最大値=1)。批判重大度は0でも重要度を潰さないよう下駄(0.2)を履かせる。
    def normalize(d: dict[str, float], floor: float = 0.0) -> dict[str, float]:
        mx = max(d.values(), default=0.0)
        if mx <= 0:
            return dict.fromkeys(d, floor)
        return {k: max(v / mx, floor) for k, v in d.items()}

    n_sens = normalize(raw_sens)
    n_unc = normalize(raw_unc)
    n_sev = normalize(raw_sev, floor=0.2)

    for r in results:
        r.importance = round(
            n_sens.get(r.parameter_id, 0.0)
            * n_unc.get(r.parameter_id, 0.0)
            * n_sev.get(r.parameter_id, 0.2),
            4,
        )
        parameters[r.parameter_id].sensitivity = r.spearman if r.spearman is not None else r.elasticity

    # 寄与順位(OATスパン降順)と精密化の期待効果
    results.sort(key=lambda r: -(r.oat_span or 0.0))
    spans = [r.oat_span or 0.0 for r in results]
    total_span = sum(spans) or 1.0
    for rank, r in enumerate(results, start=1):
        r.contribution_rank = rank
        share = (r.oat_span or 0.0) / total_span
        if share > 0.4:
            r.expected_improvement = (
                f"結果幅への寄与が最大級({share:.0%})。このパラメータの精密化が最も効果的です。"
            )
        elif share > 0.15:
            r.expected_improvement = f"結果幅への寄与は中程度({share:.0%})。精密化の効果があります。"
        else:
            r.expected_improvement = f"結果幅への寄与は小({share:.0%})。精密化の優先度は低めです。"
    return results


def spearman_check(x: np.ndarray, y: np.ndarray) -> float:
    """テスト用の素朴なSpearman実装(scipyとの照合に使用)。"""
    rx = np.argsort(np.argsort(x))
    ry = np.argsort(np.argsort(y))
    rx_c = rx - rx.mean()
    ry_c = ry - ry.mean()
    denom = float(np.sqrt((rx_c**2).sum() * (ry_c**2).sum()))
    if denom == 0:
        return 0.0
    return float((rx_c * ry_c).sum() / denom)
