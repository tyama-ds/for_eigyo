"""model_generator — 問題類型テンプレートによる推定モデル候補の生成。

ルールベースで候補を出せる部分はルールで処理し、LLMは
テンプレートに適合しない曖昧な問いの補助に限定する(要件§5 Step 2-3)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fermiscope.domain.enums import StockOrFlow
from fermiscope.domain.models import ModelCandidate, ParameterEstimate, QuestionSpec
from fermiscope.formula.graph import build_graph
from fermiscope.formula.parser import FormulaParseError
from fermiscope.llm.base import LLMProvider

# 対象物 → エンティティ単位
_OBJECT_UNITS = {
    "ピアノ": "piano",
    "車": "vehicle",
    "自動車": "vehicle",
    "EV": "vehicle",
    "傘": "umbrella",
    "ペットボトル": "bottle",
    "充電器": "charger",
}

_OCCUPATION_PATTERN = re.compile(
    r"(?P<object>.+?)(?P<occupation>調律師|整備士|技術者|技師|保守要員|修理業者|点検員)"
)


def _object_unit(obj: str) -> str:
    for key, unit in _OBJECT_UNITS.items():
        if key in obj:
            return unit
    return "item"


@dataclass
class TemplateResult:
    key: str
    name: str
    approach: str
    description: str
    expression: str
    parameters: list[ParameterEstimate]
    base_scores: dict[str, float]  # 採点基準(0〜1)
    double_counting_risk: str = ""
    dependency_risk: str = ""
    correlated_parameter_ids: list[str] | None = None


def _tpl_maintenance_demand(spec: QuestionSpec) -> TemplateResult | None:
    """総需要 ÷ 供給者1人あたり能力(保守・調律等の職業人数)。"""
    m = _OCCUPATION_PATTERN.search(spec.subject)
    if not m or spec.target_unit != "person":
        return None
    obj = m.group("object").strip()
    occupation = m.group("occupation")
    obj_unit = _object_unit(obj)
    geo = spec.geography
    params = [
        ParameterEstimate(
            id="base_households",
            name=f"{geo}の世帯数",
            symbol="N_hh",
            definition=f"{geo}に居住する一般世帯の総数",
            unit="household",
            target_geography=geo,
            target_period=spec.reference_date,
            search_terms_ja=[f"{geo} 世帯数", "世帯数 統計", "国勢調査 世帯数"],
            search_terms_en=[f"{geo} number of households"],
        ),
        ParameterEstimate(
            id="ownership_rate",
            name=f"{obj}保有率",
            symbol="r_own",
            definition=f"世帯のうち{obj}を保有する割合(保有台数/世帯)",
            unit=f"{obj_unit}/household",
            target_geography=geo,
            target_period=spec.reference_date,
            search_terms_ja=[f"{obj} 保有率", f"{obj} 普及率", "消費動向調査 普及率"],
            search_terms_en=[f"{obj} ownership rate household"],
        ),
        ParameterEstimate(
            id="service_frequency",
            name=f"{obj}1台あたり年間{occupation.rstrip('師士者員')}回数",
            symbol="f_svc",
            definition=f"保有されている{obj}1台が1年間に{occupation}のサービスを受ける平均回数",
            unit=f"tuning/({obj_unit}*year)" if occupation == "調律師" else f"event/({obj_unit}*year)",
            target_geography=geo,
            target_period=spec.reference_date,
            search_terms_ja=[f"{obj} 調律 頻度", f"{obj} 調律 実施率", f"{obj} メンテナンス 頻度"],
            search_terms_en=[f"{obj} tuning frequency per year"],
        ),
        ParameterEstimate(
            id="provider_capacity",
            name=f"{occupation}1人あたり年間対応件数",
            symbol="c_prov",
            definition=f"{occupation}1人が1年間に対応する平均件数(専業・兼業込み)",
            unit="tuning/(person*year)" if occupation == "調律師" else "event/(person*year)",
            target_geography="日本",
            target_period=spec.reference_date,
            search_terms_ja=[f"{occupation} 年間 件数", f"{occupation} 1人あたり 件数"],
            search_terms_en=[f"{occupation} annual jobs per worker"],
        ),
    ]
    return TemplateResult(
        key="maintenance_demand",
        name="需要側モデル(保有ストック×サービス頻度÷1人あたり能力)",
        approach="demand_side",
        description=(
            f"{geo}の{obj}の保有台数から年間の{occupation}需要件数を推定し、"
            f"{occupation}1人あたりの年間対応能力で割って人数を求める。"
        ),
        expression="base_households * ownership_rate * service_frequency / provider_capacity",
        parameters=params,
        base_scores={
            "estimability": 0.85,
            "explainability": 0.9,
            "evidence_availability": 0.8,
            "double_counting_risk": 0.85,  # 高いほどリスク低
            "dependency_risk": 0.7,
            "independence": 0.8,
        },
        dependency_risk="保有率とサービス頻度は所得水準を通じて相関する可能性があります。",
        correlated_parameter_ids=["ownership_rate", "service_frequency"],
    )


def _tpl_association_supply(spec: QuestionSpec) -> TemplateResult | None:
    """供給側モデル: 団体会員数 ÷ 組織率。"""
    m = _OCCUPATION_PATTERN.search(spec.subject)
    if not m or spec.target_unit != "person":
        return None
    occupation = m.group("occupation")
    geo = spec.geography
    params = [
        ParameterEstimate(
            id="association_members",
            name=f"{occupation}団体の{geo}会員数",
            symbol="N_assoc",
            definition=f"{occupation}の主要業界団体に所属する{geo}の会員数",
            unit="person",
            target_geography=geo,
            target_period=spec.reference_date,
            search_terms_ja=[f"{occupation} 協会 会員数", f"{occupation} 団体 会員数 {geo}"],
            search_terms_en=[f"{occupation} association members {geo}"],
        ),
        ParameterEstimate(
            id="membership_rate",
            name="組織率(団体加入率)",
            symbol="r_member",
            definition=f"全{occupation}のうち業界団体に加入している割合",
            unit="dimensionless",
            target_geography="日本",
            target_period=spec.reference_date,
            search_terms_ja=[f"{occupation} 組織率", f"{occupation} 協会 加入率"],
            search_terms_en=[f"{occupation} association membership rate"],
        ),
    ]
    return TemplateResult(
        key="association_supply",
        name="供給側モデル(団体会員数÷組織率)",
        approach="supply_side",
        description=f"業界団体の会員数を組織率で割り戻して{occupation}の総数を推定する。",
        expression="association_members / membership_rate",
        parameters=params,
        base_scores={
            "estimability": 0.7,
            "explainability": 0.85,
            "evidence_availability": 0.6,
            "double_counting_risk": 0.9,
            "dependency_risk": 0.85,
            "independence": 0.9,  # 需要側モデルと独立性が高い
        },
    )


def _tpl_population_ratio(spec: QuestionSpec) -> TemplateResult | None:
    """人口 × 該当率 × 1人あたり数量(汎用)。"""
    if spec.target_unit == "person" and _OCCUPATION_PATTERN.search(spec.subject):
        return None  # 職業人数は専用テンプレートに委ねる
    geo = spec.geography
    per_unit = spec.target_unit if spec.target_unit != "person" else "event"
    is_flow = spec.stock_or_flow == StockOrFlow.FLOW
    qty_unit = f"{per_unit}/(person*year)" if is_flow else f"{per_unit}/person"
    params = [
        ParameterEstimate(
            id="population",
            name=f"{geo}の人口",
            symbol="N_pop",
            definition=f"{geo}の総人口",
            unit="person",
            target_geography=geo,
            target_period=spec.reference_date,
            search_terms_ja=[f"{geo} 人口", "人口推計"],
            search_terms_en=[f"{geo} population"],
        ),
        ParameterEstimate(
            id="applicable_rate",
            name=f"{spec.subject}の該当率",
            symbol="r_app",
            definition=f"人口のうち{spec.subject}に該当・関与する割合",
            unit="dimensionless",
            target_geography=geo,
            target_period=spec.reference_date,
            search_terms_ja=[f"{spec.subject} 割合", f"{spec.subject} 比率"],
            search_terms_en=[f"{spec.subject} rate"],
        ),
        ParameterEstimate(
            id="quantity_per_person",
            name="該当者1人あたり数量",
            symbol="q_pp",
            definition=f"該当者1人あたりの{spec.subject}の数量"
            + ("(年間)" if is_flow else ""),
            unit=qty_unit,
            target_geography=geo,
            target_period=spec.reference_date,
            search_terms_ja=[f"{spec.subject} 1人あたり"],
            search_terms_en=[f"{spec.subject} per capita"],
        ),
    ]
    return TemplateResult(
        key="population_ratio",
        name="人口比率モデル(人口×該当率×1人あたり数量)",
        approach="population_ratio",
        description=f"{geo}の人口に該当率と1人あたり数量を掛けて推定する。",
        expression="population * applicable_rate * quantity_per_person",
        parameters=params,
        base_scores={
            "estimability": 0.7,
            "explainability": 0.85,
            "evidence_availability": 0.6,
            "double_counting_risk": 0.8,
            "dependency_risk": 0.7,
            "independence": 0.6,
        },
    )


def _tpl_direct_lookup(spec: QuestionSpec) -> TemplateResult | None:
    """直接値の探索(統計・登録数がそのまま公表されている場合の検算用)。"""
    params = [
        ParameterEstimate(
            id="direct_value",
            name=f"{spec.subject}の公表値",
            symbol="V_direct",
            definition=f"{spec.geography}における{spec.subject}の数を直接示す統計・調査値",
            unit=spec.target_unit,
            target_geography=spec.geography,
            target_period=spec.reference_date,
            search_terms_ja=[f"{spec.geography} {spec.subject} 数", f"{spec.subject} 統計"],
            search_terms_en=[f"{spec.subject} count {spec.geography}"],
        )
    ]
    return TemplateResult(
        key="direct_lookup",
        name="直接調査モデル(公表値の探索)",
        approach="direct_lookup",
        description="公的統計や調査に直接の値が存在しないかを探索する。分解を伴わないため主モデルには不向き。",
        expression="direct_value",
        parameters=params,
        base_scores={
            "estimability": 0.4,
            "explainability": 0.6,
            "evidence_availability": 0.4,
            "double_counting_risk": 0.95,
            "dependency_risk": 0.95,
            "independence": 0.7,
        },
    )


_TEMPLATES = [
    _tpl_maintenance_demand,
    _tpl_association_supply,
    _tpl_population_ratio,
    _tpl_direct_lookup,
]

_SCORE_WEIGHTS = {
    "estimability": 0.20,
    "unit_consistency": 0.20,
    "explainability": 0.15,
    "evidence_availability": 0.20,
    "double_counting_risk": 0.10,
    "dependency_risk": 0.05,
    "independence": 0.10,
}


def _build_candidate(
    result: TemplateResult, spec: QuestionSpec, proposed_by: str = "rule"
) -> tuple[ModelCandidate, dict[str, ParameterEstimate]] | None:
    params = {p.id: p for p in result.parameters}
    units = {pid: p.unit for pid, p in params.items()}
    try:
        graph = build_graph(result.expression, spec.target_unit, units)
    except FormulaParseError:
        return None
    scores = dict(result.base_scores)
    scores["unit_consistency"] = 1.0 if graph.unit_check_passed else 0.0
    total = sum(_SCORE_WEIGHTS.get(k, 0.0) * v for k, v in scores.items())
    candidate = ModelCandidate(
        name=result.name,
        approach=result.approach,
        template_key=result.key,
        description=result.description,
        formula=graph,
        parameter_ids=list(params.keys()),
        scores=scores,
        total_score=round(total, 3),
        proposed_by=proposed_by,  # type: ignore[arg-type]
        double_counting_risk=result.double_counting_risk,
        dependency_risk=result.dependency_risk,
        correlated_parameter_ids=result.correlated_parameter_ids or [],
    )
    return candidate, params


async def generate_model_candidates(
    spec: QuestionSpec,
    llm: LLMProvider | None = None,
) -> tuple[list[ModelCandidate], dict[str, ParameterEstimate], bool]:
    """モデル候補を生成し採点する。

    Returns:
        (候補リスト(採点降順・roleつき), パラメータ辞書, ai_assisted)
    """
    candidates: list[ModelCandidate] = []
    all_params: dict[str, ParameterEstimate] = {}
    ai_assisted = False

    for template in _TEMPLATES:
        result = template(spec)
        if result is None:
            continue
        built = _build_candidate(result, spec)
        if built is None:
            continue
        candidate, params = built
        candidates.append(candidate)
        all_params.update(params)

    # ルールテンプレートが2件未満の場合のみLLM補助(AIフォールバック条件)
    if len(candidates) < 2 and llm is not None and llm.available:
        proposals = await llm.propose_models(
            f"{spec.original_question}(対象: {spec.subject}、地域: {spec.geography})",
            spec.target_unit,
        )
        for proposal in proposals or []:
            params_list = [
                ParameterEstimate(
                    id=p.id,
                    name=p.name,
                    unit=p.unit,
                    description=p.description,
                    target_geography=spec.geography,
                    target_period=spec.reference_date,
                    search_terms_ja=p.search_terms_ja,
                    search_terms_en=p.search_terms_en,
                    ai_assisted=True,
                )
                for p in proposal.parameters
            ]
            result = TemplateResult(
                key="llm_proposal",
                name=proposal.name,
                approach=proposal.approach or "llm_proposal",
                description=proposal.description,
                expression=proposal.expression,
                parameters=params_list,
                base_scores={
                    "estimability": 0.5,
                    "explainability": 0.5,
                    "evidence_availability": 0.5,
                    "double_counting_risk": 0.5,
                    "dependency_risk": 0.5,
                    "independence": 0.5,
                },
            )
            built = _build_candidate(result, spec, proposed_by="llm")
            if built is None:
                continue  # 式が不正なLLM提案は破棄(検証済みのみ採用)
            candidate, params = built
            if not candidate.formula.unit_check_passed:
                candidate.selection_reason = "LLM提案だが単位検査に不合格のため不採用。"
                candidate.role = "rejected"
            ai_assisted = True
            candidates.append(candidate)
            all_params.update(params)

    candidates.sort(key=lambda c: -c.total_score)

    # 役割の付与: 最上位=主モデル、次点(単位検査合格)=検算モデル
    primary_set = False
    check_set = False
    for c in candidates:
        if c.role == "rejected":
            continue
        if not primary_set and c.formula.unit_check_passed:
            c.role = "primary"
            c.selection_reason = f"総合スコア最上位({c.total_score:.2f})のため主モデルに採用。"
            primary_set = True
        elif not check_set and c.formula.unit_check_passed:
            c.role = "check"
            c.selection_reason = (
                f"次点(スコア {c.total_score:.2f})かつ主モデルとの独立性があるため検算モデルに採用。"
            )
            check_set = True
        else:
            c.role = "rejected"
            c.selection_reason = c.selection_reason or "スコアが採用候補に及ばないため不採用。"

    # 採用されなかったパラメータは削除しない(監査のため保持)
    return candidates, all_params, ai_assisted
