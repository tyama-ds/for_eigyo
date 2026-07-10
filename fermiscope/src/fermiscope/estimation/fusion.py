"""複数証拠からのパラメータ値統合。

- 単位正規化(Pint)
- 互換性判定(地域・時点・定義)— 非互換は平均せず矛盾として残す
- 転載クラスタは1証拠として扱う(重み = クラスタ内最大スコア)
- 対数空間IQRによる外れ値検出
- 証拠スコア重み付き中央値・分位点(正の乗法的パラメータは対数空間補間)
"""

from __future__ import annotations

import math

import numpy as np

from fermiscope.config import Settings
from fermiscope.domain.enums import DistributionKind, ParameterStatus, ValueBasis
from fermiscope.domain.models import EvidenceItem, ParameterEstimate
from fermiscope.evidence.dates import parse_year
from fermiscope.evidence.normalize import normalize_value


def weighted_quantile(
    values: list[float],
    weights: list[float],
    q: float,
    log_space: bool = False,
) -> float:
    """重み付き分位点。log_space=True なら対数空間で補間する(正値のみ)。"""
    if not values:
        raise ValueError("値が空です")
    if len(values) != len(weights):
        raise ValueError("値と重みの長さが一致しません")
    if not 0.0 <= q <= 1.0:
        raise ValueError("分位点は0〜1で指定してください")
    arr = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    if np.any(w < 0):
        raise ValueError("重みは非負である必要があります")
    if w.sum() <= 0:
        raise ValueError("重みの合計が0です")
    if log_space:
        if np.any(arr <= 0):
            raise ValueError("対数空間補間には正の値が必要です")
        arr = np.log(arr)

    order = np.argsort(arr)
    arr, w = arr[order], w[order]
    cum = np.cumsum(w)
    # Hazen型の重み付き分位点(各点の中心累積重み)
    centers = (cum - 0.5 * w) / w.sum()
    if q <= centers[0]:
        result = arr[0]
    elif q >= centers[-1]:
        result = arr[-1]
    else:
        result = float(np.interp(q, centers, arr))
    return float(math.exp(result)) if log_space else float(result)


def weighted_median(values: list[float], weights: list[float], log_space: bool = False) -> float:
    return weighted_quantile(values, weights, 0.5, log_space=log_space)


def _normalize_evidence_value(ev: EvidenceItem, target_unit: str) -> tuple[float | None, str]:
    """証拠値をパラメータ単位へ正規化する(不能なら None)。注記を返す。"""
    if ev.extracted_value is None:
        return None, ""
    if not target_unit:
        return ev.extracted_value, ""
    return normalize_value(ev.extracted_value, ev.unit, target_unit)


def _detect_log_outliers(values: list[float], multiplier: float) -> list[bool]:
    """対数空間IQRで外れ値を検出(True=外れ値)。正値のみ対象。"""
    if len(values) < 4 or any(v <= 0 for v in values):
        return [False] * len(values)
    logs = np.log(np.asarray(values))
    q1, q3 = np.percentile(logs, [25, 75])
    iqr = q3 - q1
    if iqr <= 0:
        return [False] * len(values)
    lo, hi = q1 - multiplier * iqr, q3 + multiplier * iqr
    return [bool(x < lo or x > hi) for x in logs]


def fuse_evidence(
    param: ParameterEstimate,
    evidence_items: list[EvidenceItem],
    settings: Settings,
    reference_year: int | None = None,
) -> ParameterEstimate:
    """証拠リストからパラメータの low/central/high・分布を決定する。

    証拠が無い/使えない場合は値を捏造せず UNRESOLVED にする(絶対条件8)。
    """
    fusion = settings.fusion
    notes: list[str] = []

    candidates: list[tuple[EvidenceItem, float, float]] = []  # (証拠, 正規化値, 重み)
    for ev in evidence_items:
        if not ev.accepted:
            continue
        if ev.incompatible_reason:
            continue  # 非互換な定義は無理に平均しない
        if ev.evidence_score is None or ev.evidence_score < fusion.min_evidence_score:
            continue
        value, bridge_note = _normalize_evidence_value(ev, param.unit)
        if value is None:
            continue
        if bridge_note:
            notes.append(f"{ev.id}: {bridge_note}")
        ev.normalized_value = value
        ev.normalized_unit = param.unit
        candidates.append((ev, value, float(ev.evidence_score)))

    # 古いデータの排除: 基準時点に近い高スコア証拠が存在する場合、
    # stale閾値を超えて古い証拠は統合から除外する(過度な外挿の防止)。
    if reference_year and len(candidates) >= 2:
        stale_years = settings.scoring.time.stale_threshold_years

        def ev_year(e: EvidenceItem) -> int | None:
            return parse_year(e.time_period) or parse_year(e.publication_date)

        has_recent = any(
            (y := ev_year(e)) is not None
            and reference_year - y <= stale_years
            and w >= 50
            for e, _, w in candidates
        )
        if has_recent:
            kept: list[tuple[EvidenceItem, float, float]] = []
            for e, v, w in candidates:
                y = ev_year(e)
                if y is not None and reference_year - y > stale_years:
                    notes.append(
                        f"{e.id}: {y}年の古いデータのため統合から除外(新しい証拠が存在)。表示には残します。"
                    )
                else:
                    kept.append((e, v, w))
            candidates = kept

    # 転載クラスタは代表1件(最高スコア)のみ採用 — 証拠の水増しを防ぐ
    seen_clusters: dict[str, tuple[EvidenceItem, float, float]] = {}
    for ev, value, weight in candidates:
        cluster = ev.cluster_id or ev.id
        if cluster in seen_clusters:
            if weight > seen_clusters[cluster][2]:
                seen_clusters[cluster] = (ev, value, weight)
            notes.append(f"転載クラスタ({cluster})の複数記事は1証拠として扱いました。")
            continue
        seen_clusters[cluster] = (ev, value, weight)
    usable = list(seen_clusters.values())

    if not usable:
        param.status = ParameterStatus.UNRESOLVED
        param.value_basis = ValueBasis.UNRESOLVED
        param.unresolved_reason = (
            "利用可能な証拠がありません(検索不能・証拠不足・単位変換不能のいずれか)。"
            "値は捏造していません。画面から値を入力できます。"
        )
        return param

    values = [v for _, v, _ in usable]
    weights = [w for _, _, w in usable]

    # 外れ値検出(除外するが証拠自体は表示に残す)
    outliers = _detect_log_outliers(values, fusion.outlier_iqr_multiplier)
    if any(outliers):
        for (ev, v, _), is_out in zip(usable, outliers, strict=True):
            if is_out:
                notes.append(
                    f"外れ値として統合から除外: {ev.id}(値 {v:g}、対数空間IQR基準)。表示には残します。"
                )
        usable = [t for t, o in zip(usable, outliers, strict=True) if not o]
        values = [v for _, v, _ in usable]
        weights = [w for _, _, w in usable]

    positive = all(v > 0 for v in values)
    log_space = fusion.log_space_for_positive and positive and len(values) >= 2

    old_central = param.central
    if len(values) == 1:
        ev = usable[0][0]
        central = values[0]
        if ev.extracted_low is not None and ev.extracted_high is not None:
            low, _ = _normalize_evidence_value(
                ev.model_copy(update={"extracted_value": ev.extracted_low}), param.unit
            )
            high, _ = _normalize_evidence_value(
                ev.model_copy(update={"extracted_value": ev.extracted_high}), param.unit
            )
            notes.append("単一証拠のため、証拠自身の範囲を low/high として採用しました。")
        else:
            factor = 1.5
            low = central / factor if positive else central * 0.5
            high = central * factor if positive else central * 1.5
            param.assumptions.append(
                "単一証拠のため不確実性幅(×/÷1.5)を仮定として設定しました。"
            )
            notes.append("単一証拠: 幅は仮定(×/÷1.5)です。")
    else:
        central = weighted_median(values, weights, log_space=log_space)
        low = weighted_quantile(values, weights, fusion.low_quantile, log_space=log_space)
        high = weighted_quantile(values, weights, fusion.high_quantile, log_space=log_space)
        notes.append(
            f"{len(values)}件の独立証拠から証拠スコア重み付き中央値"
            f"{'(対数空間補間)' if log_space else ''}で統合しました。"
        )
        # 証拠が少ない場合は分位点が潰れるため最低限の幅を確保
        if positive and high > 0 and low > 0 and high / low < 1.1:
            low, high = central / 1.15, central * 1.15
            notes.append("証拠間のばらつきが小さいため、最小幅(×/÷1.15)を適用しました。")

    param.central = float(central)
    param.low = float(low) if low is not None else None
    param.high = float(high) if high is not None else None

    # 分布選択と理由の保存
    if not positive:
        param.distribution = DistributionKind.TRIANGULAR
        param.distribution_rationale = "値が正に限られないため三角分布を選択。"
    elif param.high is not None and param.low is not None and param.low > 0 and param.high / param.low > 10:
        param.distribution = DistributionKind.LOGUNIFORM
        param.distribution_rationale = (
            "桁レベルの不確実性(high/low > 10)のため対数一様分布を選択。"
        )
    else:
        param.distribution = DistributionKind.LOGNORMAL
        param.distribution_rationale = (
            "正の乗法的パラメータのため対数正規分布を選択(low/highをP10/P90と解釈)。"
        )

    param.evidence_ids = list({*param.evidence_ids, *[ev.id for ev, _, _ in usable]})
    param.value_basis = ValueBasis.EVIDENCE
    param.status = ParameterStatus.ESTIMATED
    param.fusion_note = " ".join(notes)
    # 信頼度: 証拠スコアの重み付き平均を0〜1へ、件数ボーナス
    mean_score = float(np.average([w for _, _, w in usable]))
    param.confidence = round(min(1.0, (mean_score / 100.0) * (1.0 + 0.05 * (len(usable) - 1))), 3)
    param.record_change(
        "central", old_central, param.central, actor="system", note="証拠統合により更新"
    )
    return param
