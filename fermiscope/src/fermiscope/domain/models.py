"""ドメインモデル定義(Pydantic)。

すべての数値パラメータは value_basis(由来)を必須とし、
出典なしの値が事実として表示されないことを型レベルで支える。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from fermiscope.domain.enums import (
    DecompositionStatus,
    DistributionKind,
    DocumentType,
    IssueType,
    ParameterStatus,
    ResearchMode,
    ResolutionStatus,
    RunStage,
    RunStatus,
    SearchPurpose,
    SourceClass,
    StockOrFlow,
    ValueBasis,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class ProvisionalField(BaseModel):
    """暫定値として置いたフィールドの記録(勝手に確定しない)。"""

    field: str
    assumed_value: str
    reason: str


class QuestionSpec(BaseModel):
    """正規化された問い。"""

    original_question: str
    subject: str = ""
    geography: str = ""
    reference_date: str = ""  # 例 "2026" / "2026-07"
    time_period: str = ""  # フローの場合の対象期間 例 "1年間"
    stock_or_flow: StockOrFlow = StockOrFlow.UNKNOWN
    target_metric: str = ""
    target_unit: str = ""
    inclusions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    known_facts: list[str] = Field(default_factory=list)
    intended_use: str = ""
    requested_precision: str = "order_of_magnitude"
    language: str = "ja"
    provisional: list[ProvisionalField] = Field(default_factory=list)
    parsed_by: Literal["rule", "llm", "user"] = "rule"


class FormulaNode(BaseModel):
    """式ツリーのノード。parameter(末端)/ op(演算)/ constant。"""

    id: str = Field(default_factory=lambda: new_id("node"))
    kind: Literal["parameter", "op", "constant"] = "parameter"
    parameter_id: str = ""  # kind == parameter のとき
    op: Literal["+", "-", "*", "/", "**", ""] = ""  # kind == op のとき
    value: float | None = None  # kind == constant のとき
    children: list[FormulaNode] = Field(default_factory=list)

    def leaf_parameter_ids(self) -> list[str]:
        if self.kind == "parameter":
            return [self.parameter_id]
        out: list[str] = []
        for c in self.children:
            for pid in c.leaf_parameter_ids():
                if pid not in out:
                    out.append(pid)
        return out


class FormulaGraph(BaseModel):
    """推定式(ツリー+表示用の式文字列+目標単位)。"""

    root: FormulaNode
    expression: str = ""
    target_unit: str = ""
    unit_check_passed: bool | None = None
    unit_check_detail: str = ""

    def leaf_parameter_ids(self) -> list[str]:
        return self.root.leaf_parameter_ids()


class ValueChange(BaseModel):
    """数値・設定の変更履歴。"""

    timestamp: datetime = Field(default_factory=utcnow)
    field: str
    old_value: Any = None
    new_value: Any = None
    actor: Literal["system", "user", "ai"] = "system"
    note: str = ""


class TimeAdjustment(BaseModel):
    """基準時点への明示的な補正記録(無言の補正はしない)。"""

    id: str = Field(default_factory=lambda: new_id("adj"))
    parameter_id: str
    original_value: float
    adjusted_value: float
    original_period: str = ""
    target_period: str = ""
    factor: float = 1.0
    formula: str = ""
    rationale: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class ParameterEstimate(BaseModel):
    """末端または中間パラメータの推定値。"""

    id: str
    name: str
    symbol: str = ""
    description: str = ""
    definition: str = ""  # このパラメータの厳密な定義
    unit: str = "dimensionless"
    central: float | None = None
    low: float | None = None
    high: float | None = None
    distribution: DistributionKind = DistributionKind.TRIANGULAR
    distribution_parameters: dict[str, float] = Field(default_factory=dict)
    distribution_rationale: str = ""  # 分布選択理由(保存必須)
    target_geography: str = ""
    target_period: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    confidence: float | None = None  # 0〜1
    sensitivity: float | None = None
    critique_ids: list[str] = Field(default_factory=list)
    decomposition_status: DecompositionStatus = DecompositionStatus.NOT_ATTEMPTED
    user_overridden: bool = False

    value_basis: ValueBasis = ValueBasis.UNRESOLVED
    status: ParameterStatus = ParameterStatus.PENDING
    unresolved_reason: str = ""
    search_terms_ja: list[str] = Field(default_factory=list)
    search_terms_en: list[str] = Field(default_factory=list)
    depth: int = 0
    parent_parameter_id: str = ""
    revisit_count: int = 0
    ai_assisted: bool = False
    verification_note: str = ""  # 敵対的検証の実施記録(指摘ゼロでも残す)
    adjustments: list[TimeAdjustment] = Field(default_factory=list)
    history: list[ValueChange] = Field(default_factory=list)
    fusion_note: str = ""  # 統合方法の説明(重み付き中央値等)

    def record_change(
        self,
        field: str,
        old_value: Any,
        new_value: Any,
        actor: Literal["system", "user", "ai"] = "system",
        note: str = "",
    ) -> None:
        self.history.append(
            ValueChange(field=field, old_value=old_value, new_value=new_value, actor=actor, note=note)
        )


class ModelCandidate(BaseModel):
    """推定モデル候補。"""

    id: str = Field(default_factory=lambda: new_id("model"))
    name: str
    approach: str = ""  # demand_side / supply_side / population_ratio / ...
    template_key: str = ""
    description: str = ""
    formula: FormulaGraph
    parameter_ids: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)  # 採点基準ごとの0〜1
    total_score: float = 0.0
    selection_reason: str = ""
    role: Literal["primary", "check", "rejected", "candidate"] = "candidate"
    proposed_by: Literal["rule", "llm", "user"] = "rule"
    double_counting_risk: str = ""
    dependency_risk: str = ""
    correlated_parameter_ids: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    """1件の証拠。必ず原典URL・取得日・根拠箇所を持つ。"""

    id: str = Field(default_factory=lambda: new_id("ev"))
    url: str = Field(min_length=1)  # 出典なしの証拠は作らない(絶対条件2・4)
    canonical_url: str = ""
    title: str = ""
    publisher: str = ""
    source_class: SourceClass = SourceClass.UNKNOWN
    publication_date: str = ""
    revision_date: str = ""
    retrieval_date: datetime = Field(default_factory=utcnow)
    author: str = ""
    document_type: DocumentType = DocumentType.UNKNOWN
    search_query: str = ""
    search_purpose: SearchPurpose | None = None
    parameter_id: str = ""
    extracted_value: float | None = None
    extracted_low: float | None = None
    extracted_high: float | None = None
    unit: str = ""
    normalized_value: float | None = None  # パラメータ単位へ正規化後
    normalized_unit: str = ""
    geography: str = ""
    population_definition: str = ""
    time_period: str = ""
    exact_definition: str = ""
    methodology_summary: str = ""
    short_supporting_excerpt: str = ""
    locator: str = ""  # 文書内位置(表番号・ページ・セクション等)
    content_hash: str = ""
    parent_source_id: str = ""  # 転載元(一次資料)の証拠ID or URL
    cluster_id: str = ""  # 転載クラスタID
    evidence_score: float | None = None
    subscores: dict[str, float] = Field(default_factory=dict)
    penalties_applied: dict[str, float] = Field(default_factory=dict)
    scoring_reasons: list[str] = Field(default_factory=list)
    extraction_method: Literal["structured", "pattern", "pdf", "rule_text", "llm", "fixture", ""] = ""
    ai_assisted: bool = False
    verified: bool = False
    accepted: bool = True
    rejection_reason: str = ""
    incompatible_reason: str = ""  # 統合から除外した場合の理由(定義差等)


class SearchQuery(BaseModel):
    id: str = Field(default_factory=lambda: new_id("q"))
    parameter_id: str = ""
    purpose: SearchPurpose = SearchPurpose.DIRECT_VALUE
    query: str
    language: str = "ja"
    provider: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    executed_at: datetime | None = None
    cache_hit: bool = False
    deduplicated: bool = False
    results_count: int = 0
    estimated_cost_usd: float = 0.0
    error: str = ""


class SearchHit(BaseModel):
    id: str = Field(default_factory=lambda: new_id("hit"))
    query_id: str = ""
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0
    published_hint: str = ""


class Critique(BaseModel):
    """パラメータ・証拠への批判。根拠URLまたは決定論検査結果を紐づける。"""

    id: str = Field(default_factory=lambda: new_id("cr"))
    parameter_id: str
    issue_type: IssueType
    claim: str
    severity: float = 0.0  # 0〜1
    probability: float = 0.5  # 0〜1
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    opposing_evidence_ids: list[str] = Field(default_factory=list)
    likely_direction_of_bias: Literal["up", "down", "unknown"] = "unknown"
    estimated_impact: str = ""
    recommended_action: str = ""
    resolution_status: ResolutionStatus = ResolutionStatus.OPEN
    resolution_note: str = ""
    detected_by: Literal["deterministic_check", "critique_search", "llm", "user"] = (
        "deterministic_check"
    )
    check_detail: str = ""  # 決定論検査の内容
    ai_assisted: bool = False


class DecompositionAttempt(BaseModel):
    id: str = Field(default_factory=lambda: new_id("dec"))
    parameter_id: str
    proposed_by: Literal["rule", "llm", "user"] = "rule"
    expression: str = ""
    child_parameters: list[ParameterEstimate] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)  # 次元一致・循環なし等
    check_details: list[str] = Field(default_factory=list)
    accepted: bool = False
    rejection_reason: str = ""
    trigger_critique_ids: list[str] = Field(default_factory=list)
    importance_at_decision: float | None = None
    created_at: datetime = Field(default_factory=utcnow)


class IrreducibleAssumption(BaseModel):
    """これ以上信頼できる下位データへ分解できなかった仮定。"""

    parameter_id: str
    reason: str
    attempted_decompositions: list[str] = Field(default_factory=list)  # DecompositionAttempt IDs
    why_rejected: list[str] = Field(default_factory=list)
    remaining_uncertainty: str = ""
    result_impact: str = ""
    what_new_evidence_would_resolve_it: str = ""
    user_editable_value: bool = True


class Scenario(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sc"))
    name: str
    kind: Literal["bear", "base", "bull", "custom", "extreme_low", "extreme_high"] = "custom"
    value: float | None = None
    quantile: float | None = None  # MC分位点由来の場合
    parameter_overrides: dict[str, float] = Field(default_factory=dict)
    description: str = ""
    model_id: str = ""


class SimulationConfig(BaseModel):
    iterations: int = 20000
    seed: int = 20260710
    correlations: list[tuple[str, str, float]] = Field(default_factory=list)
    independence_note: str = "相関未指定のパラメータは独立と仮定しています。"


class SimulationResult(BaseModel):
    model_id: str = ""
    iterations: int = 0
    seed: int = 0
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    quantiles: dict[str, float] = Field(default_factory=dict)  # "0.1" -> value
    histogram_bin_edges: list[float] = Field(default_factory=list)
    histogram_counts: list[int] = Field(default_factory=list)
    parameter_spearman: dict[str, float] = Field(default_factory=dict)
    failed_iterations: int = 0
    note: str = ""


class SensitivityResult(BaseModel):
    model_id: str = ""
    parameter_id: str
    parameter_name: str = ""
    oat_low_output: float | None = None  # そのパラメータのみlowにした出力
    oat_high_output: float | None = None
    oat_span: float | None = None
    elasticity: float | None = None  # 局所弾力性 (dY/Y)/(dX/X)
    spearman: float | None = None  # MCサンプルとの順位相関
    uncertainty_span: float | None = None  # (high-low)/central
    direction: Literal["increase", "decrease", "nonmonotonic", "unknown"] = "unknown"
    critique_severity: float = 0.0
    importance: float = 0.0
    contribution_rank: int = 0
    expected_improvement: str = ""  # 値を精密化した場合の改善見込み


class Contradiction(BaseModel):
    """証拠間の矛盾。平均で隠さず表示する。"""

    id: str = Field(default_factory=lambda: new_id("con"))
    parameter_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    ratio: float | None = None
    analysis: dict[str, str] = Field(default_factory=dict)  # definition/time/geography/method
    note: str = ""


class ValidationResult(BaseModel):
    """主モデルと検算モデルの比較。"""

    primary_model_id: str = ""
    check_model_id: str = ""
    primary_central: float | None = None
    check_central: float | None = None
    central_ratio: float | None = None
    primary_interval: tuple[float, float] | None = None
    check_interval: tuple[float, float] | None = None
    interval_overlap: float | None = None  # 0〜1
    warnings: list[str] = Field(default_factory=list)
    shared_evidence_ids: list[str] = Field(default_factory=list)
    shared_weak_primary_source: bool = False
    difference_analysis: dict[str, str] = Field(default_factory=dict)
    agreement: Literal["consistent", "moderate", "discrepant", "unknown"] = "unknown"
    note: str = ""


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("audit"))
    timestamp: datetime = Field(default_factory=utcnow)
    category: str  # search / fetch / value_change / decomposition / ai_fallback / seed / version / ...
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class ResearchRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    project_id: str = ""
    status: RunStatus = RunStatus.IDLE
    stage: RunStage = RunStage.PARSING
    mode: ResearchMode = ResearchMode.STANDARD
    started_at: datetime | None = None
    finished_at: datetime | None = None
    searches_executed: int = 0
    search_cache_hits: int = 0
    documents_fetched: int = 0
    evidence_found: int = 0
    parameters_verified: int = 0
    warnings_count: int = 0
    ai_fallback_uses: int = 0
    seed: int = 0
    app_version: str = ""
    config_hash: str = ""
    error: str = ""
    cancel_requested: bool = False


class EstimateProject(BaseModel):
    """1つの推定プロジェクトの全状態(単一の真実源)。"""

    id: str = Field(default_factory=lambda: new_id("prj"))
    name: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    question: QuestionSpec
    research_mode: ResearchMode = ResearchMode.STANDARD
    max_searches: int | None = None
    max_cost_usd: float | None = None

    models: list[ModelCandidate] = Field(default_factory=list)
    parameters: dict[str, ParameterEstimate] = Field(default_factory=dict)
    evidence: dict[str, EvidenceItem] = Field(default_factory=dict)
    searches: list[SearchQuery] = Field(default_factory=list)
    search_hits: list[SearchHit] = Field(default_factory=list)
    critiques: dict[str, Critique] = Field(default_factory=dict)
    contradictions: list[Contradiction] = Field(default_factory=list)
    decomposition_attempts: list[DecompositionAttempt] = Field(default_factory=list)
    irreducible_assumptions: list[IrreducibleAssumption] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    simulation_config: SimulationConfig = Field(default_factory=SimulationConfig)
    simulation_results: list[SimulationResult] = Field(default_factory=list)
    sensitivity_results: list[SensitivityResult] = Field(default_factory=list)
    validation: ValidationResult | None = None
    audit_events: list[AuditEvent] = Field(default_factory=list)
    runs: list[ResearchRun] = Field(default_factory=list)

    overall_confidence: float | None = None  # 0〜1
    confidence_reasons: list[str] = Field(default_factory=list)
    key_caveats: list[str] = Field(default_factory=list)
    app_version: str = ""
    config_hash: str = ""

    def primary_model(self) -> ModelCandidate | None:
        return next((m for m in self.models if m.role == "primary"), None)

    def check_model(self) -> ModelCandidate | None:
        return next((m for m in self.models if m.role == "check"), None)

    def audit(self, category: str, message: str, **data: Any) -> AuditEvent:
        ev = AuditEvent(category=category, message=message, data=data)
        self.audit_events.append(ev)
        self.updated_at = utcnow()
        return ev

    def current_run(self) -> ResearchRun | None:
        return self.runs[-1] if self.runs else None
