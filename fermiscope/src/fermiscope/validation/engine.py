"""validation_engine — 主モデルと検算モデルの比較。

- 中心値の比(3倍超で警告)
- P10-P90区間の重なり
- 同じ弱い一次資料への依存
- 定義不一致・二重計上リスクの検出
差が大きい場合は「モデル間不一致」として結果の一部にする(統合しない)。
"""

from __future__ import annotations

from fermiscope.config import Settings
from fermiscope.domain.enums import IssueType, SourceClass
from fermiscope.domain.models import (
    Critique,
    EvidenceItem,
    ModelCandidate,
    ParameterEstimate,
    SimulationResult,
    ValidationResult,
)
from fermiscope.formula.units import units_directly_comparable


def _interval_overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """区間の重なり率(重なり長 ÷ 小さい方の区間長)。0〜1。"""
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    if hi <= lo:
        return 0.0
    shorter = min(a[1] - a[0], b[1] - b[0])
    if shorter <= 0:
        return 0.0
    return (hi - lo) / shorter


def validate_models(
    primary: ModelCandidate,
    check: ModelCandidate,
    primary_sim: SimulationResult,
    check_sim: SimulationResult,
    parameters: dict[str, ParameterEstimate],
    evidence: dict[str, EvidenceItem],
    critiques: dict[str, Critique],
    settings: Settings,
) -> ValidationResult:
    result = ValidationResult(
        primary_model_id=primary.id,
        check_model_id=check.id,
        primary_central=primary_sim.median,
        check_central=check_sim.median,
    )
    warnings: list[str] = []
    analysis: dict[str, str] = {}

    # 目標単位・期間の互換性を先に判定する。非互換(例: 日次 vs 年次)なら
    # 換算根拠なしに数値比較してはならない → 検算不成立として返す。
    primary_unit = primary.formula.target_unit
    check_unit = check.formula.target_unit
    if not units_directly_comparable(primary_unit, check_unit):
        result.comparable = False
        result.agreement = "incompatible"
        result.warnings = [
            f"主モデルの目標単位 [{primary_unit or '(無次元)'}] と検算モデルの目標単位 "
            f"[{check_unit or '(無次元)'}] は次元・期間が非互換です。換算根拠がないため"
            f"数値比較(中心値の比・区間の重なり)は行いません(検算不成立)。"
        ]
        result.note = (
            "両モデルの目標単位・期間が非互換のため検算できませんでした。"
            "同じ期間・単位に揃えるか、明示的な換算根拠を与えてください。"
        )
        return result

    # 中心値の比
    if primary_sim.median and check_sim.median and primary_sim.median > 0 and check_sim.median > 0:
        ratio = max(primary_sim.median, check_sim.median) / min(primary_sim.median, check_sim.median)
        result.central_ratio = round(ratio, 2)
        if ratio > settings.validation.central_ratio_warning:
            warnings.append(
                f"主モデルと検算モデルの中心値が {ratio:.1f} 倍乖離しています"
                f"(警告閾値 {settings.validation.central_ratio_warning:g} 倍)。"
            )

    # 区間の重なり
    p_lo, p_hi = primary_sim.quantiles.get("0.1"), primary_sim.quantiles.get("0.9")
    c_lo, c_hi = check_sim.quantiles.get("0.1"), check_sim.quantiles.get("0.9")
    if None not in (p_lo, p_hi, c_lo, c_hi):
        result.primary_interval = (p_lo, p_hi)  # type: ignore[assignment]
        result.check_interval = (c_lo, c_hi)  # type: ignore[assignment]
        overlap = _interval_overlap((p_lo, p_hi), (c_lo, c_hi))  # type: ignore[arg-type]
        result.interval_overlap = round(overlap, 3)
        if overlap < settings.validation.interval_overlap_warning:
            warnings.append(
                f"両モデルの妥当区間(P10-P90)がほとんど重なりません(重なり率 {overlap:.0%})。"
            )

    # 証拠の共有(同じ一次資料への依存)
    primary_ev = {
        eid for pid in primary.formula.leaf_parameter_ids() for eid in parameters[pid].evidence_ids
    }
    check_ev = {
        eid for pid in check.formula.leaf_parameter_ids() for eid in parameters[pid].evidence_ids
    }
    shared = primary_ev & check_ev
    # クラスタ単位でも比較(転載を通じた間接共有)
    primary_clusters = {evidence[e].cluster_id for e in primary_ev if e in evidence}
    check_clusters = {evidence[e].cluster_id for e in check_ev if e in evidence}
    shared_clusters = {c for c in (primary_clusters & check_clusters) if c}
    result.shared_evidence_ids = sorted(shared)
    if shared or shared_clusters:
        weak_shared = [
            e
            for e in evidence.values()
            if (e.id in shared or e.cluster_id in shared_clusters)
            and e.source_class in (SourceClass.C, SourceClass.D, SourceClass.E)
        ]
        if weak_shared:
            result.shared_weak_primary_source = True
            warnings.append(
                "両モデルが同じ弱い情報源(Cクラス以下)に依存しており、独立した検算になっていません。"
            )
        else:
            warnings.append("両モデルが一部同じ証拠を共有しています(独立性は部分的)。")

    # 片方のモデルのみの定義不一致・二重計上
    for m, label in ((primary, "主モデル"), (check, "検算モデル")):
        defs = [
            c
            for c in critiques.values()
            if c.parameter_id in m.formula.leaf_parameter_ids()
            and c.issue_type in (IssueType.DEFINITION_MISMATCH, IssueType.POPULATION_MISMATCH)
        ]
        if defs:
            warnings.append(f"{label}のパラメータに定義不一致の批判があります({len(defs)}件)。")
        if m.double_counting_risk:
            warnings.append(f"{label}に二重計上リスクの注記: {m.double_counting_risk}")

    # 差の分析
    if result.central_ratio and result.central_ratio > settings.validation.central_ratio_warning:
        analysis["scope"] = "対象範囲: 両モデルの対象範囲(専業/兼業、稼働/非稼働の扱い)を確認してください。"
        analysis["definition"] = "定義: 各パラメータの定義差(会員/非会員、世帯/事業所)が乖離要因になり得ます。"
        analysis["coverage"] = "カバレッジ: 検算モデルの母集団(団体会員等)が全体を捕捉していない可能性があります。"
        analysis["informal"] = "非公式部門: 未登録・非組織の対象が一方のモデルにだけ含まれる可能性があります。"
        analysis["utilization"] = "稼働率: 供給能力ベースのモデルは実稼働率の仮定に敏感です。"
        result.note = (
            "両モデルの差は統合せず「モデル間不一致」として表示しています。"
            "上記の分析観点を確認してください。"
        )

    # 総合判定
    if result.central_ratio is None:
        result.agreement = "unknown"
    elif result.central_ratio <= 1.8 and (result.interval_overlap or 0) >= 0.3:
        result.agreement = "consistent"
        result.note = result.note or (
            f"検算モデルとの中心値の比は {result.central_ratio:.1f} 倍で、桁は整合しています。"
        )
    elif result.central_ratio <= settings.validation.central_ratio_warning:
        result.agreement = "moderate"
        result.note = result.note or (
            f"検算モデルとの中心値の比は {result.central_ratio:.1f} 倍です。桁は同一ですが差の要因確認を推奨します。"
        )
    else:
        result.agreement = "discrepant"

    result.warnings = warnings
    result.difference_analysis = analysis
    return result
