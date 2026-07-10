"""decomposition_engine — 批判が重大なパラメータの再分解。

再分解条件(設定変更可):
- critique_severity >= threshold
- importance >= threshold(importance = 正規化感度 × 正規化不確実性 × 正規化批判重大度)
- 分解深度・末端数・再検討回数が上限未満

ルールテンプレートを優先し、必要な場合のみLLM候補を使う。
LLM候補はPython側で次元一致・循環参照・上限を検査する。
分解不能な場合は IrreducibleAssumption を明示する。
"""

from __future__ import annotations

import re

from fermiscope.config import Settings
from fermiscope.domain.enums import DecompositionStatus, ValueBasis
from fermiscope.domain.models import (
    Critique,
    DecompositionAttempt,
    IrreducibleAssumption,
    ModelCandidate,
    ParameterEstimate,
    SensitivityResult,
)
from fermiscope.formula.graph import replace_parameter
from fermiscope.formula.parser import FormulaParseError, parse_expression
from fermiscope.formula.units import check_graph_units
from fermiscope.llm.base import LLMProvider
from fermiscope.llm.schemas import DecompositionProposal, ParameterProposal

# ルールテンプレート1: 年間処理能力 → 1日あたり件数 × 年間稼働日数
_ANNUAL_CAPACITY_UNIT = re.compile(r"^(?P<num>\w+)/\((?P<den>\w+)\*year\)$")


def _rule_decompose(param: ParameterEstimate) -> DecompositionProposal | None:
    """ルールベースの分解候補。"""
    m = _ANNUAL_CAPACITY_UNIT.match(param.unit.replace(" ", ""))
    if m:
        num, den = m.group("num"), m.group("den")
        daily_id = f"{param.id}_daily"
        days_id = f"{param.id}_working_days"
        return DecompositionProposal(
            expression=f"{daily_id} * {days_id}",
            rationale=(
                "年間処理能力は『1日あたり件数 × 年間稼働日数』に分解でき、"
                "それぞれ業務実態調査から観測しやすい。"
            ),
            parameters=[
                ParameterProposal(
                    id=daily_id,
                    name=f"{param.name}(1日あたり)",
                    unit=f"{num}/({den}*day)",
                    description=f"{param.name} の1日あたり件数",
                    search_terms_ja=["1日 件数", "1日あたり 件数"],
                    search_terms_en=["tunings per day", "jobs per day"],
                ),
                ParameterProposal(
                    id=days_id,
                    name="年間稼働日数",
                    unit="day/year",
                    description="1年間の平均稼働日数",
                    search_terms_ja=["稼働日数", "年間 稼働日数"],
                    search_terms_en=["working days per year"],
                ),
            ],
        )
    return None


def validate_decomposition(
    param: ParameterEstimate,
    proposal: DecompositionProposal,
    existing_parameters: dict[str, ParameterEstimate],
    settings: Settings,
    current_leaf_count: int,
) -> tuple[dict[str, bool], list[str]]:
    """分解候補のPython側検査(次元一致・循環・上限・観測可能性)。"""
    checks: dict[str, bool] = {}
    details: list[str] = []

    if not proposal.expression.strip():
        return {"has_expression": False}, ["分解式が空です"]
    checks["has_expression"] = True

    child_ids = {p.id for p in proposal.parameters}
    # 循環参照・自己参照・既存IDとの衝突
    no_cycle = param.id not in child_ids and all(
        cid not in existing_parameters or cid.startswith(param.id) for cid in child_ids
    )
    checks["no_cycle"] = no_cycle
    if not no_cycle:
        details.append("子パラメータが親または既存パラメータと循環・衝突しています")

    # 式が子パラメータのみで構成されているか
    try:
        tree = parse_expression(proposal.expression, child_ids)
        checks["expression_valid"] = True
    except FormulaParseError as exc:
        checks["expression_valid"] = False
        details.append(f"分解式が不正です: {exc}")
        return checks, details

    # 次元一致: 分解式の単位 = 親パラメータの単位
    child_units = {p.id: p.unit for p in proposal.parameters}
    unit_check = check_graph_units(tree, child_units, param.unit)
    checks["dimension_match"] = unit_check.passed
    if not unit_check.passed:
        details.append(f"次元不一致: {unit_check.detail}")

    # 末端数の上限
    new_leaf_count = current_leaf_count - 1 + len(proposal.parameters)
    checks["within_leaf_limit"] = new_leaf_count <= settings.decomposition.max_leaves_after_expansion
    if not checks["within_leaf_limit"]:
        details.append(
            f"分解後の末端数 {new_leaf_count} が上限 "
            f"{settings.decomposition.max_leaves_after_expansion} を超えます"
        )

    # 観測可能性(検索語があるか)
    observable = all(p.search_terms_ja or p.search_terms_en for p in proposal.parameters)
    checks["more_observable"] = observable
    if not observable:
        details.append("子パラメータに検索手がかり(検索語)がなく、証拠取得が容易になりません")

    # 不必要な相関変数の増加(同名の語幹を持つ子同士は相関しやすい)
    checks["not_overly_correlated"] = len(child_ids) <= 4
    if not checks["not_overly_correlated"]:
        details.append("子パラメータが多すぎ、相関した変数を増やすリスクがあります")

    return checks, details


async def decide_decompositions(
    model: ModelCandidate,
    parameters: dict[str, ParameterEstimate],
    critiques: dict[str, Critique],
    sensitivity: list[SensitivityResult],
    settings: Settings,
    llm: LLMProvider | None = None,
) -> tuple[list[DecompositionAttempt], list[IrreducibleAssumption], bool]:
    """再分解の判断と実行。

    Returns:
        (試行リスト, 分解不能仮定リスト, ai_assisted)
    """
    dc = settings.decomposition
    attempts: list[DecompositionAttempt] = []
    irreducibles: list[IrreducibleAssumption] = []
    ai_assisted = False

    importance_by_param = {s.parameter_id: s.importance for s in sensitivity}
    leaf_ids = model.formula.leaf_parameter_ids()

    for pid in list(leaf_ids):
        param = parameters[pid]
        param_critiques = [c for c in critiques.values() if c.parameter_id == pid]
        max_severity = max((c.severity for c in param_critiques), default=0.0)
        importance = importance_by_param.get(pid, 0.0)

        if max_severity < dc.critique_severity_threshold:
            continue
        if importance < dc.importance_threshold:
            continue
        if param.depth >= dc.max_depth:
            irreducibles.append(
                IrreducibleAssumption(
                    parameter_id=pid,
                    reason=f"分解深度が上限({dc.max_depth})に達しています。",
                    remaining_uncertainty=_uncertainty_text(param),
                    result_impact=f"重要度 {importance:.2f}(感度×不確実性×批判重大度)。",
                    what_new_evidence_would_resolve_it="このパラメータを直接測定した一次統計。",
                )
            )
            continue
        if param.revisit_count >= dc.max_revisits_per_parameter:
            continue
        param.revisit_count += 1

        trigger_ids = [c.id for c in param_critiques if c.severity >= dc.critique_severity_threshold]

        # 1) ルールテンプレート優先
        proposal = _rule_decompose(param)
        proposed_by = "rule"
        # 2) 必要な場合のみLLM(ルール候補なしのとき)
        if proposal is None and llm is not None and llm.available:
            proposal = await llm.propose_decomposition(param.name, param.unit, param.description)
            proposed_by = "llm"
            if proposal is not None:
                ai_assisted = True

        if proposal is None or not proposal.expression.strip():
            attempt = DecompositionAttempt(
                parameter_id=pid,
                proposed_by=proposed_by,  # type: ignore[arg-type]
                expression="",
                accepted=False,
                rejection_reason="適用可能な分解テンプレートがなく、LLM候補も得られませんでした。",
                trigger_critique_ids=trigger_ids,
                importance_at_decision=importance,
            )
            attempts.append(attempt)
            param.decomposition_status = DecompositionStatus.IRREDUCIBLE
            irreducibles.append(
                IrreducibleAssumption(
                    parameter_id=pid,
                    reason="これ以上、信頼できる下位データへ分解できませんでした。",
                    attempted_decompositions=[attempt.id],
                    why_rejected=[attempt.rejection_reason],
                    remaining_uncertainty=_uncertainty_text(param),
                    result_impact=f"重要度 {importance:.2f}。",
                    what_new_evidence_would_resolve_it=(
                        f"「{param.name}」を直接測定した調査・統計(方法明示)が見つかれば解決します。"
                    ),
                )
            )
            continue

        current_leaves = len(model.formula.leaf_parameter_ids())
        checks, details = validate_decomposition(
            param, proposal, parameters, settings, current_leaves
        )
        attempt = DecompositionAttempt(
            parameter_id=pid,
            proposed_by=proposed_by,  # type: ignore[arg-type]
            expression=proposal.expression,
            checks=checks,
            check_details=details,
            trigger_critique_ids=trigger_ids,
            importance_at_decision=importance,
        )

        if not all(checks.values()):
            attempt.accepted = False
            attempt.rejection_reason = "検査不合格: " + "; ".join(details)
            attempts.append(attempt)
            param.decomposition_status = DecompositionStatus.IRREDUCIBLE
            irreducibles.append(
                IrreducibleAssumption(
                    parameter_id=pid,
                    reason="分解候補はありましたが、検査(次元一致・循環・上限)に不合格でした。",
                    attempted_decompositions=[attempt.id],
                    why_rejected=details,
                    remaining_uncertainty=_uncertainty_text(param),
                    result_impact=f"重要度 {importance:.2f}。",
                    what_new_evidence_would_resolve_it=(
                        f"「{param.name}」の直接測定値、または検証可能な分解構造。"
                    ),
                )
            )
            continue

        # 採用: 子パラメータを登録し、式を置換
        child_params: list[ParameterEstimate] = []
        for p in proposal.parameters:
            child = ParameterEstimate(
                id=p.id,
                name=p.name,
                unit=p.unit,
                description=p.description,
                definition=p.description,
                target_geography=param.target_geography,
                target_period=param.target_period,
                search_terms_ja=p.search_terms_ja,
                search_terms_en=p.search_terms_en,
                depth=param.depth + 1,
                parent_parameter_id=param.id,
                ai_assisted=(proposed_by == "llm"),
            )
            parameters[child.id] = child
            child_params.append(child)
        attempt.child_parameters = child_params
        attempt.accepted = True
        attempts.append(attempt)

        all_units = {pid2: p2.unit for pid2, p2 in parameters.items()}
        model.formula = replace_parameter(model.formula, pid, proposal.expression, all_units)
        model.parameter_ids = model.formula.leaf_parameter_ids()
        param.decomposition_status = DecompositionStatus.DECOMPOSED
        param.value_basis = ValueBasis.DERIVED
        param.record_change(
            "decomposition",
            None,
            proposal.expression,
            actor="ai" if proposed_by == "llm" else "system",
            note=f"批判(重大度 {max_severity:.2f})を受けて分解。{proposal.rationale}",
        )

    return attempts, irreducibles, ai_assisted


def _uncertainty_text(param: ParameterEstimate) -> str:
    if param.low is not None and param.high is not None and param.low > 0:
        return f"現在の不確実性幅は {param.high / param.low:.1f} 倍(low {param.low:g}〜high {param.high:g})。"
    return "値が未解決、または幅が算出できていません。"
