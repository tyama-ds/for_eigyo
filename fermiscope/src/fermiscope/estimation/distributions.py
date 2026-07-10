"""確率分布の構築とサンプリング。

各分布は逆累積分布関数(PPF)として実装する。
一様乱数 u ∈ (0,1) を PPF に通す方式なので、ガウスコピュラによる
相関サンプリングと自然に組み合わせられる。すべて決定論的な
NumPy/SciPy 計算であり、生成AIは関与しない。
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from scipy import stats

from fermiscope.domain.enums import DistributionKind
from fermiscope.domain.models import ParameterEstimate

_Z90 = 1.2815515655446004  # 標準正規分布の90%点(P10/P90⇔μ±zσ)


class DistributionError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DistributionError(message)


def build_ppf(param: ParameterEstimate) -> Callable[[np.ndarray], np.ndarray]:
    """ParameterEstimate から PPF(逆CDF)を構築する。

    low/high は原則 P10/P90 と解釈する(fusion と整合)。
    """
    kind = param.distribution
    central = param.central
    low = param.low if param.low is not None else central
    high = param.high if param.high is not None else central

    if kind == DistributionKind.FIXED:
        _require(central is not None, f"{param.id}: fixed分布には central が必要です")
        value = float(central)  # type: ignore[arg-type]
        return lambda u: np.full_like(np.asarray(u, dtype=float), value)

    _require(low is not None and high is not None, f"{param.id}: low/high が必要です")
    lo = float(low)  # type: ignore[arg-type]
    hi = float(high)  # type: ignore[arg-type]
    _require(hi >= lo, f"{param.id}: high >= low が必要です")

    if kind == DistributionKind.TRIANGULAR:
        _require(central is not None, f"{param.id}: triangular には central が必要です")
        mode = float(central)  # type: ignore[arg-type]
        # low/high を P10/P90 と解釈し、裾を外側へ拡張して三角分布の端点を求める。
        # 拡張が不安定な場合は low/high をそのまま端点として使う(注記される)。
        a, b = _triangular_bounds_from_p10_p90(lo, mode, hi)
        c = (mode - a) / (b - a) if b > a else 0.5
        dist = stats.triang(c=min(max(c, 0.0), 1.0), loc=a, scale=max(b - a, 1e-12))
        return lambda u: np.asarray(dist.ppf(u), dtype=float)

    if kind == DistributionKind.LOGNORMAL:
        _require(lo > 0 and hi > 0, f"{param.id}: lognormal には正の low/high が必要です")
        if central is not None and central > 0:
            mu = np.log(float(central))
        else:
            mu = (np.log(lo) + np.log(hi)) / 2.0
        sigma = max((np.log(hi) - np.log(lo)) / (2.0 * _Z90), 1e-9)
        return lambda u: np.exp(mu + sigma * stats.norm.ppf(u))

    if kind == DistributionKind.UNIFORM:
        return lambda u: lo + (hi - lo) * np.asarray(u, dtype=float)

    if kind == DistributionKind.LOGUNIFORM:
        _require(lo > 0 and hi > 0, f"{param.id}: loguniform には正の low/high が必要です")
        ln_lo, ln_hi = np.log(lo), np.log(hi)
        return lambda u: np.exp(ln_lo + (ln_hi - ln_lo) * np.asarray(u, dtype=float))

    if kind == DistributionKind.EMPIRICAL:
        # distribution_parameters の分位点(例 p10, p25, p50, p75, p90)を
        # 区分線形の逆CDFとして使う。
        pts = sorted(
            (float(k[1:]) / 100.0, v)
            for k, v in param.distribution_parameters.items()
            if k.startswith("p") and k[1:].replace(".", "").isdigit()
        )
        _require(len(pts) >= 2, f"{param.id}: empirical には p10 等の分位点が2点以上必要です")
        qs = np.array([p for p, _ in pts])
        vs = np.array([v for _, v in pts])
        return lambda u: np.interp(np.asarray(u, dtype=float), qs, vs)

    raise DistributionError(f"{param.id}: 未対応の分布 {kind}")


def _triangular_bounds_from_p10_p90(p10: float, mode: float, p90: float) -> tuple[float, float]:
    """P10/P90 と最頻値から三角分布の端点 (a, b) を近似的に求める。"""
    if p90 <= p10:
        return p10, max(p90, p10 + abs(p10) * 1e-9 + 1e-12)
    # 単純な外挿: P10/P90 の外側に幅の25%ずつ余裕を持たせ、モードを含むよう調整
    span = p90 - p10
    a = min(p10 - 0.25 * span, mode)
    b = max(p90 + 0.25 * span, mode)
    return a, b


def sample_parameters(
    params: dict[str, ParameterEstimate],
    n: int,
    seed: int,
    correlations: list[tuple[str, str, float]] | None = None,
) -> dict[str, np.ndarray]:
    """全パラメータのサンプル行列を生成する。

    相関指定がある場合はガウスコピュラ(正規スコアの相関)を使う。
    指定がなければ独立にサンプリングする。
    """
    ids = list(params.keys())
    k = len(ids)
    rng = np.random.default_rng(seed)

    corr = np.eye(k)
    if correlations:
        index = {pid: i for i, pid in enumerate(ids)}
        for a, b, rho in correlations:
            if a in index and b in index:
                _require(-1.0 < rho < 1.0, f"相関係数は(-1,1)で指定してください: {a},{b}")
                corr[index[a], index[b]] = rho
                corr[index[b], index[a]] = rho
        # 正定値性の検査(不成立は明示的に失敗させる)
        eigvals = np.linalg.eigvalsh(corr)
        _require(bool(eigvals.min() > -1e-10), "相関行列が正半定値ではありません")
        corr = corr + np.eye(k) * 1e-10

    chol = np.linalg.cholesky(corr)
    z = rng.standard_normal((n, k)) @ chol.T
    u = stats.norm.cdf(z)
    # 数値安定化(0や1ちょうどを避ける)
    u = np.clip(u, 1e-12, 1 - 1e-12)

    out: dict[str, np.ndarray] = {}
    for i, pid in enumerate(ids):
        ppf = build_ppf(params[pid])
        out[pid] = np.asarray(ppf(u[:, i]), dtype=float)
    return out
