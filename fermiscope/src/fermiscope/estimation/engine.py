"""シナリオ計算とモンテカルロシミュレーション(すべて決定論的Python計算)。"""

from __future__ import annotations

import numpy as np
from scipy import stats

from fermiscope.config import Settings
from fermiscope.domain.models import (
    ModelCandidate,
    ParameterEstimate,
    Scenario,
    SimulationConfig,
    SimulationResult,
)
from fermiscope.estimation.distributions import sample_parameters
from fermiscope.formula.graph import FormulaEvalError, evaluate_graph


class EstimationError(ValueError):
    pass


def _model_params(
    model: ModelCandidate, parameters: dict[str, ParameterEstimate]
) -> dict[str, ParameterEstimate]:
    leaf_ids = model.formula.leaf_parameter_ids()
    missing = [pid for pid in leaf_ids if pid not in parameters]
    if missing:
        raise EstimationError(f"モデル {model.name} のパラメータが未定義です: {missing}")
    unresolved = [
        pid for pid in leaf_ids if parameters[pid].central is None
    ]
    if unresolved:
        raise EstimationError(
            f"未解決のパラメータがあるため計算できません: {unresolved}。"
            "値を入力するか証拠を追加してください。"
        )
    return {pid: parameters[pid] for pid in leaf_ids}


def deterministic_evaluate(
    model: ModelCandidate,
    parameters: dict[str, ParameterEstimate],
    overrides: dict[str, float] | None = None,
) -> float:
    """中心値(+上書き)での決定論的な点推定。"""
    params = _model_params(model, parameters)
    values: dict[str, float] = {}
    for pid, p in params.items():
        v = (overrides or {}).get(pid, p.central)
        if v is None:
            raise EstimationError(f"パラメータ {pid} の値がありません")
        values[pid] = float(v)
    result = evaluate_graph(model.formula, values)
    return float(result)


def run_monte_carlo(
    model: ModelCandidate,
    parameters: dict[str, ParameterEstimate],
    config: SimulationConfig,
    settings: Settings,
) -> tuple[SimulationResult, dict[str, np.ndarray], np.ndarray]:
    """モンテカルロ計算。

    Returns:
        (結果サマリ, パラメータ別サンプル, 出力サンプル)
        サンプルは感度分析用で、永続化はサマリのみ。
    """
    params = _model_params(model, parameters)
    n = config.iterations
    samples = sample_parameters(params, n, config.seed, config.correlations)
    outputs = np.asarray(evaluate_graph(model.formula, samples), dtype=float)

    valid = np.isfinite(outputs)
    failed = int(n - valid.sum())
    clean = outputs[valid]
    if clean.size == 0:
        raise EstimationError("モンテカルロの全反復が失敗しました(ゼロ除算等)。")

    sq = settings.simulation.scenario_quantiles
    q_list = sorted({sq.bear, sq.base, sq.bull, *settings.simulation.extra_quantiles})
    quantiles = {f"{q:g}": float(np.quantile(clean, q)) for q in q_list}

    counts, edges = np.histogram(clean, bins=settings.simulation.histogram_bins)

    spearman: dict[str, float] = {}
    for pid, arr in samples.items():
        if np.std(arr[valid]) <= 0 or np.std(clean) <= 0:
            spearman[pid] = 0.0
            continue
        rho = stats.spearmanr(arr[valid], clean).statistic
        spearman[pid] = float(rho) if np.isfinite(rho) else 0.0

    result = SimulationResult(
        model_id=model.id,
        iterations=n,
        seed=config.seed,
        mean=float(np.mean(clean)),
        median=float(np.median(clean)),
        std=float(np.std(clean)),
        quantiles=quantiles,
        histogram_bin_edges=[float(e) for e in edges],
        histogram_counts=[int(c) for c in counts],
        parameter_spearman=spearman,
        failed_iterations=failed,
        note=(
            config.independence_note
            if not config.correlations
            else "指定された相関行列(ガウスコピュラ)を適用しました。"
        ),
    )
    return result, samples, outputs


def compute_scenarios(
    model: ModelCandidate,
    parameters: dict[str, ParameterEstimate],
    sim_result: SimulationResult,
    settings: Settings,
    custom_overrides: dict[str, float] | None = None,
) -> list[Scenario]:
    """弱気/基準/強気(MC分位点)+極端範囲+カスタムシナリオ。

    全変数同時min/maxのみに依存しない(要件§10)。弱気・強気は出力分布の
    分位点で定義し、全low/全highは参考の極端範囲として別掲する。
    """
    sq = settings.simulation.scenario_quantiles
    scenarios: list[Scenario] = []
    for kind, name, q in (
        ("bear", "弱気シナリオ", sq.bear),
        ("base", "基準シナリオ", sq.base),
        ("bull", "強気シナリオ", sq.bull),
    ):
        key = f"{q:g}"
        value = sim_result.quantiles.get(key)
        scenarios.append(
            Scenario(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                value=value,
                quantile=q,
                description=(
                    f"モンテカルロ出力分布の P{int(q * 100)}"
                    f"(反復 {sim_result.iterations:,}、シード {sim_result.seed})"
                ),
                model_id=model.id,
            )
        )

    # 参考: 方向を考慮して全パラメータを同時に不利側/有利側へ置いた極端範囲。
    # 分母側パラメータは low が結果を増やすため、OAT方向で各パラメータの割当を決める。
    params = _model_params(model, parameters)
    try:
        minimizing: dict[str, float] = {}
        maximizing: dict[str, float] = {}
        for pid, p in params.items():
            low = float(p.low) if p.low is not None else float(p.central)  # type: ignore[arg-type]
            high = float(p.high) if p.high is not None else float(p.central)  # type: ignore[arg-type]
            out_low = deterministic_evaluate(model, parameters, {pid: low})
            out_high = deterministic_evaluate(model, parameters, {pid: high})
            if out_low <= out_high:
                minimizing[pid], maximizing[pid] = low, high
            else:
                minimizing[pid], maximizing[pid] = high, low
        scenarios.append(
            Scenario(
                name="極端下限(参考)",
                kind="extreme_low",
                value=deterministic_evaluate(model, parameters, minimizing),
                parameter_overrides=minimizing,
                description="全パラメータを同時に不利側へ置いた参考値。同時に起こる可能性は低く、非現実的に広い範囲です。",
                model_id=model.id,
            )
        )
        scenarios.append(
            Scenario(
                name="極端上限(参考)",
                kind="extreme_high",
                value=deterministic_evaluate(model, parameters, maximizing),
                parameter_overrides=maximizing,
                description="全パラメータを同時に有利側へ置いた参考値。同時に起こる可能性は低く、非現実的に広い範囲です。",
                model_id=model.id,
            )
        )
    except (EstimationError, FormulaEvalError, ArithmeticError):
        # 参考の極端範囲はベストエフォート。境界値でゼロ除算等が起きても
        # 本体のシナリオ生成は続行する(ここで全体を落とさない)。
        pass

    if custom_overrides:
        scenarios.append(
            Scenario(
                name="カスタムシナリオ",
                kind="custom",
                value=deterministic_evaluate(model, parameters, custom_overrides),
                parameter_overrides=dict(custom_overrides),
                description="ユーザー指定値による決定論的再計算。",
                model_id=model.id,
            )
        )
    return scenarios
