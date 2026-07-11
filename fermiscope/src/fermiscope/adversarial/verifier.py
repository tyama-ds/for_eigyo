"""adversarial_verifier — 各パラメータの敵対的検証。

決定論的チェック(単位・地域・時点・母集団・利益相反・転載等)と、
反証検索で得られた証拠の解析を組み合わせる。
LLMによる根拠のない批判は「仮説」(HYPOTHESIS)として区別する。
"""

from __future__ import annotations

import re
from typing import Literal, cast

from fermiscope.config import Settings
from fermiscope.domain.enums import IssueType, ResolutionStatus, SearchPurpose
from fermiscope.domain.models import Critique, EvidenceItem, ModelCandidate, ParameterEstimate
from fermiscope.evidence.dates import parse_year
from fermiscope.evidence.normalize import normalize_value
from fermiscope.llm.base import LLMProvider
from fermiscope.security.boundary import wrap_untrusted

_REVISION_PATTERN = re.compile(r"訂正|撤回|改訂|修正版|revised|correction|retraction")


def _used_evidence(param: ParameterEstimate, evidence: dict[str, EvidenceItem]) -> list[EvidenceItem]:
    return [evidence[eid] for eid in param.evidence_ids if eid in evidence]


def deterministic_checks(
    param: ParameterEstimate,
    evidence: dict[str, EvidenceItem],
    model: ModelCandidate | None,
    settings: Settings,
    reference_year: int | None,
) -> list[Critique]:
    """決定論的な検査。各Critiqueには検査内容(check_detail)を必ず記録する。"""
    critiques: list[Critique] = []
    used = _used_evidence(param, evidence)
    accepted = [e for e in used if e.accepted and not e.incompatible_reason]

    def add(
        issue: IssueType,
        claim: str,
        severity: float,
        detail: str,
        direction: str = "unknown",
        action: str = "",
        supporting: list[str] | None = None,
    ) -> None:
        critiques.append(
            Critique(
                parameter_id=param.id,
                issue_type=issue,
                claim=claim,
                severity=severity,
                probability=0.7,
                likely_direction_of_bias=direction,  # type: ignore[arg-type]
                recommended_action=action,
                detected_by="deterministic_check",
                check_detail=detail,
                supporting_evidence_ids=supporting or [],
            )
        )

    # 証拠の有無
    if param.central is None and not accepted:
        add(
            IssueType.NO_EVIDENCE,
            f"{param.name} には利用可能な証拠がなく、値が未解決です。",
            0.9,
            "検査: 採用済み証拠件数 = 0。",
            action="値のユーザー入力、または追加検索が必要です。",
        )
        return critiques

    # 独立情報源の数(転載クラスタ考慮)
    clusters = {e.cluster_id or e.id for e in accepted}
    if len(clusters) == 1 and accepted:
        top_score = max(e.evidence_score or 0 for e in accepted)
        severity = 0.6 if top_score < 75 else 0.45
        add(
            IssueType.SINGLE_SOURCE,
            f"{param.name} は独立した情報源が1系統しかありません。",
            severity,
            f"検査: 独立情報源クラスタ数 = 1(証拠 {len(accepted)} 件、最高スコア {top_score:.0f})。",
            action="代替の一次資料の探索、またはパラメータの分解を検討してください。",
        )
    if len(accepted) > len(clusters):
        add(
            IssueType.DUPLICATE_PRIMARY_SOURCE,
            "同じ一次資料を転載した複数記事が含まれています(独立証拠として数えていません)。",
            0.35,
            f"検査: 証拠 {len(accepted)} 件に対し独立クラスタ {len(clusters)} 系統。",
        )

    # 地域の一致
    geo_mismatch = [
        e
        for e in accepted
        if e.geography
        and param.target_geography
        and e.geography != param.target_geography
        and param.target_geography not in e.geography
    ]
    if geo_mismatch:
        nationwide = [e for e in geo_mismatch if e.geography in ("日本", "全国", "国内")]
        if nationwide:
            add(
                IssueType.GEOGRAPHY_MISMATCH,
                f"{param.name} に全国値を {param.target_geography} へ適用しています。"
                "地域特性(都市部の偏り等)により実態と乖離する可能性があります。",
                0.5,
                f"検査: 証拠の対象地域 {[e.geography for e in geo_mismatch]} ≠ 目標 {param.target_geography}。",
                action=f"{param.target_geography} 固有の統計を探すか、地域補正係数の導入を検討。",
                supporting=[e.id for e in geo_mismatch],
            )
        else:
            add(
                IssueType.GEOGRAPHY_MISMATCH,
                f"{param.name} の証拠の対象地域が目標地域と一致しません。",
                0.65,
                f"検査: 証拠地域 {[e.geography for e in geo_mismatch]} ≠ 目標 {param.target_geography}。",
                supporting=[e.id for e in geo_mismatch],
            )

    # 時点の一致・古いデータの外挿
    if reference_year:
        stale = []
        for e in accepted:
            y = parse_year(e.time_period) or parse_year(e.publication_date)
            if y and reference_year - y > settings.scoring.time.stale_threshold_years:
                stale.append((e, y))
        if stale:
            add(
                IssueType.STALE_EXTRAPOLATION,
                f"{param.name} は {min(y for _, y in stale)} 年の古いデータに依存しており、"
                f"基準時点({reference_year}年)への外挿リスクがあります。",
                0.55,
                f"検査: 証拠時点 {[y for _, y in stale]} と基準年の乖離 > "
                f"{settings.scoring.time.stale_threshold_years:g} 年。",
                action="より新しい統計の探索、または明示的な時点補正の追加。",
                supporting=[e.id for e, _ in stale],
            )

    # 利益相反
    coi = [e for e in accepted if "conflict_of_interest_penalty" in e.penalties_applied]
    if coi:
        add(
            IssueType.CONFLICT_OF_INTEREST,
            f"{param.name} の証拠に利益相反の疑いがある資料が含まれます(自社に有利な主張)。",
            0.5,
            f"検査: 利益相反ペナルティ適用済み証拠 {len(coi)} 件。",
            direction="up",
            supporting=[e.id for e in coi],
        )

    # 方法の透明性
    if accepted and all(not e.methodology_summary for e in accepted):
        add(
            IssueType.OPAQUE_METHODOLOGY,
            f"{param.name} のすべての証拠で調査方法が非公開です。",
            0.5,
            "検査: methodology_summary を持つ証拠 = 0 件。",
            action="調査方法が明示された一次資料の探索。",
        )

    # 標本バイアス
    biased = [e for e in accepted if "sample_bias_penalty" in e.penalties_applied]
    if biased:
        add(
            IssueType.SAMPLE_BIAS,
            f"{param.name} の証拠に標本偏りの疑いがあります(自社ユーザー調査等)。",
            0.45,
            f"検査: 標本バイアスペナルティ適用済み証拠 {len(biased)} 件。",
            supporting=[e.id for e in biased],
        )

    # 不確実性幅
    if (
        param.low is not None
        and param.high is not None
        and param.low > 0
        and param.high / param.low > 5
    ):
        add(
            IssueType.WIDE_UNCERTAINTY,
            f"{param.name} の不確実性幅が大きい(high/low = {param.high / param.low:.1f}倍)。",
            0.5,
            f"検査: high({param.high:g}) / low({param.low:g}) > 5。",
            action="パラメータの分解、または追加証拠による幅の縮小。",
        )

    # 平均・中央値の混同の可能性
    mean_sources = [e for e in accepted if "平均" in e.short_supporting_excerpt]
    if mean_sources and re.search(r"中央値|median", param.definition):
        add(
            IssueType.MEAN_MEDIAN_CONFUSION,
            f"{param.name} の定義は中央値だが、証拠は平均値を報告している可能性があります。",
            0.4,
            "検査: 定義に「中央値」、証拠抜粋に「平均」を検出。",
            supporting=[e.id for e in mean_sources],
        )

    # モデル由来の相関リスク(該当パラメータのみ)
    if model is not None and model.dependency_risk and param.id in model.correlated_parameter_ids:
        add(
            IssueType.CORRELATED_PARAMETERS,
            model.dependency_risk,
            0.3,
            f"検査: モデルテンプレートが相関リスクを申告(対象: {', '.join(model.correlated_parameter_ids)})。",
            action="必要に応じてシミュレーション設定で相関係数を指定してください。",
        )

    return critiques


def analyze_counter_evidence(
    param: ParameterEstimate,
    evidence: dict[str, EvidenceItem],
    settings: Settings,
) -> list[Critique]:
    """反証・訂正検索で得られた証拠の解析(検索結果に基づく批判)。"""
    critiques: list[Critique] = []
    counter_items = [
        e
        for e in evidence.values()
        if e.parameter_id == param.id
        and e.search_purpose in (SearchPurpose.COUNTER_EVIDENCE, SearchPurpose.CORRECTION)
    ]
    for ce in counter_items:
        text = ce.short_supporting_excerpt + ce.title + ce.exact_definition
        if _REVISION_PATTERN.search(text):
            critiques.append(
                Critique(
                    parameter_id=param.id,
                    issue_type=IssueType.RETRACTION_OR_REVISION,
                    claim=f"{param.name} の資料に訂正・改訂情報が存在する可能性があります: {ce.title}",
                    severity=0.6,
                    probability=0.6,
                    supporting_evidence_ids=[ce.id],
                    detected_by="critique_search",
                    check_detail=f"反証検索ヒット: {ce.url}",
                    recommended_action="訂正後の値を確認し、証拠を更新してください。",
                )
            )
        if ce.extracted_value is not None and param.central is not None:
            counter_value, _note = normalize_value(ce.extracted_value, ce.unit, param.unit)
            if counter_value is not None and counter_value > 0 and param.central > 0:
                ratio = max(counter_value, param.central) / min(counter_value, param.central)
                if ratio > settings.scoring.contradiction.ratio_threshold:
                    # 反証自体の証拠力でスケール(弱い反証で重大度を上げすぎない)
                    quality_scale = min(1.0, (ce.evidence_score or 30.0) / 60.0)
                    critiques.append(
                        Critique(
                            parameter_id=param.id,
                            issue_type=IssueType.COUNTER_EVIDENCE_EXISTS,
                            claim=(
                                f"{param.name} に対して反対方向の証拠が存在します"
                                f"(採用値 {param.central:g} に対し {counter_value:g})。"
                            ),
                            severity=round(min(0.3 + 0.1 * ratio, 0.8) * quality_scale, 2),
                            probability=0.5,
                            opposing_evidence_ids=[ce.id],
                            detected_by="critique_search",
                            check_detail=(
                                f"反証検索ヒットの値と {ratio:.1f} 倍の乖離"
                                f"(反証の証拠スコア {ce.evidence_score or 0:.0f} で重み付け)。"
                            ),
                            recommended_action="定義・時点・地域の差を確認し、矛盾分析を参照してください。",
                        )
                    )
    return critiques


async def verify_parameter(
    param: ParameterEstimate,
    evidence: dict[str, EvidenceItem],
    model: ModelCandidate | None,
    settings: Settings,
    reference_year: int | None,
    llm: LLMProvider | None = None,
) -> tuple[list[Critique], bool]:
    """敵対的検証の統合入口。

    Returns:
        (Critiqueリスト, ai_assisted)
    """
    critiques = deterministic_checks(param, evidence, model, settings, reference_year)
    critiques.extend(analyze_counter_evidence(param, evidence, settings))

    ai_assisted = False
    # 決定論チェックで批判が出なかった場合のみ、LLMに仮説を出させる(補助)
    if not critiques and llm is not None and llm.available:
        used = _used_evidence(param, evidence)
        # 証拠タイトルは取得した外部Webページ由来の不信データ。プロンプト
        # インジェクションを防ぐため、LLM へ渡す前にデータ境界で包む。
        summary = wrap_untrusted(
            "; ".join(
                f"{e.title}({e.source_class.value}, score={e.evidence_score})" for e in used[:5]
            )
        )
        proposals = await llm.propose_critique(param.name, param.definition, summary)
        for p in proposals or []:
            ai_assisted = True
            direction: Literal["up", "down", "unknown"] = "unknown"
            if p.likely_direction_of_bias in ("up", "down"):
                direction = cast(Literal["up", "down"], p.likely_direction_of_bias)
            critiques.append(
                Critique(
                    parameter_id=param.id,
                    issue_type=IssueType.AI_HYPOTHESIS,
                    claim=p.claim,
                    severity=min(p.severity, 0.4),  # 根拠のないAI批判は重大度を制限
                    probability=0.3,
                    likely_direction_of_bias=direction,
                    recommended_action=p.recommended_action,
                    resolution_status=ResolutionStatus.HYPOTHESIS,
                    detected_by="llm",
                    ai_assisted=True,
                    check_detail="根拠となるURL・検査結果なし(AI仮説として区別)。",
                )
            )

    # 影響の推定(方向つき)
    for c in critiques:
        if not c.estimated_impact:
            if c.severity >= 0.6:
                c.estimated_impact = "結果の桁に影響し得る重大な問題です。"
            elif c.severity >= 0.4:
                c.estimated_impact = "結果に中程度(数十%)の影響があり得ます。"
            else:
                c.estimated_impact = "結果への影響は限定的とみられます。"
    return critiques, ai_assisted


def evidence_class_summary(items: list[EvidenceItem]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in items:
        out[e.source_class.value] = out.get(e.source_class.value, 0) + 1
    return out


__all__ = [
    "analyze_counter_evidence",
    "deterministic_checks",
    "evidence_class_summary",
    "verify_parameter",
]
