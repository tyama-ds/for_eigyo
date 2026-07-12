"""ドメイン列挙型。"""

from __future__ import annotations

from enum import StrEnum


class StockOrFlow(StrEnum):
    STOCK = "stock"
    FLOW = "flow"
    UNKNOWN = "unknown"


class ResearchMode(StrEnum):
    FAST = "fast"  # 高速: 検索を絞り敵対的検証を軽量化
    STANDARD = "standard"  # 標準
    CAREFUL = "careful"  # 慎重: 検索・検証を最大化


class SourceClass(StrEnum):
    S = "S"
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    UNKNOWN = "unknown"


class DocumentType(StrEnum):
    HTML = "html"
    PDF = "pdf"
    CSV = "csv"
    JSON = "json"
    XLSX = "xlsx"
    DOCX = "docx"
    PPTX = "pptx"
    TEXT = "text"
    UNKNOWN = "unknown"


class ValueBasis(StrEnum):
    """パラメータ値の由来。全パラメータで必須(絶対条件3)。"""

    EVIDENCE = "evidence"  # 出典付きの証拠
    USER_INPUT = "user_input"  # ユーザー入力
    ASSUMPTION = "assumption"  # 明示された仮定
    DERIVED = "derived"  # 他パラメータからの導出
    UNRESOLVED = "unresolved"  # 未解決(値の捏造をしない)


class ParameterStatus(StrEnum):
    PENDING = "pending"
    RESEARCHING = "researching"
    ESTIMATED = "estimated"
    UNRESOLVED = "unresolved"
    USER_OVERRIDDEN = "user_overridden"


class DistributionKind(StrEnum):
    FIXED = "fixed"
    TRIANGULAR = "triangular"
    LOGNORMAL = "lognormal"
    UNIFORM = "uniform"
    LOGUNIFORM = "loguniform"
    EMPIRICAL = "empirical"


class SearchPurpose(StrEnum):
    DEFINITION = "definition"  # 定義確認
    DIRECT_VALUE = "direct_value"  # 直接値の探索
    PRIMARY_SOURCE = "primary_source"  # 公的・一次資料の探索
    METHODOLOGY = "methodology"  # 調査方法の確認
    LATEST_VALUE = "latest_value"  # 最新値・基準時点に近い値
    ALTERNATIVE_VALUE = "alternative_value"  # 代替値の探索
    COUNTER_EVIDENCE = "counter_evidence"  # 反証・批判・限界
    CORRECTION = "correction"  # 訂正・改訂情報


class IssueType(StrEnum):
    DEFINITION_MISMATCH = "definition_mismatch"
    STOCK_FLOW_CONFUSION = "stock_flow_confusion"
    POPULATION_MISMATCH = "population_mismatch"
    GEOGRAPHY_MISMATCH = "geography_mismatch"
    TIME_MISMATCH = "time_mismatch"
    UNIT_MISMATCH = "unit_mismatch"
    RATE_PERIOD_CONFUSION = "rate_period_confusion"
    NOMINAL_REAL_CONFUSION = "nominal_real_confusion"
    ENTITY_CONFUSION = "entity_confusion"  # 個人/世帯/事業所の混同
    DOUBLE_COUNTING = "double_counting"
    INACTIVE_INCLUDED = "inactive_included"
    MEAN_MEDIAN_CONFUSION = "mean_median_confusion"
    SAMPLE_BIAS = "sample_bias"
    SURVIVORSHIP_BIAS = "survivorship_bias"
    CONFLICT_OF_INTEREST = "conflict_of_interest"
    OPAQUE_METHODOLOGY = "opaque_methodology"
    STALE_EXTRAPOLATION = "stale_extrapolation"
    RATIO_TRANSFER = "ratio_transfer"  # 比率を異なる母集団へ適用
    CORRELATED_PARAMETERS = "correlated_parameters"
    DUPLICATE_PRIMARY_SOURCE = "duplicate_primary_source"
    COUNTER_EVIDENCE_EXISTS = "counter_evidence_exists"
    RETRACTION_OR_REVISION = "retraction_or_revision"
    SINGLE_SOURCE = "single_source"
    NO_EVIDENCE = "no_evidence"
    WIDE_UNCERTAINTY = "wide_uncertainty"
    AI_HYPOTHESIS = "ai_hypothesis"  # 根拠のないLLM批判は仮説として区別


class ResolutionStatus(StrEnum):
    OPEN = "open"
    RESOLVED_BY_DECOMPOSITION = "resolved_by_decomposition"
    RESOLVED_BY_EVIDENCE = "resolved_by_evidence"
    RESOLVED_BY_USER = "resolved_by_user"
    ACKNOWLEDGED = "acknowledged"  # 対応不能だが注記済み
    DISMISSED = "dismissed"
    HYPOTHESIS = "hypothesis"  # 根拠のないAI批判


class DecompositionStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    DECOMPOSED = "decomposed"
    IRREDUCIBLE = "irreducible"
    NOT_NEEDED = "not_needed"


class RunStage(StrEnum):
    PARSING = "parsing"
    MODEL_GENERATION = "model_generation"
    PLANNING = "planning"
    SEARCHING = "searching"
    EXTRACTING = "extracting"
    RANKING = "ranking"
    FUSING = "fusing"
    VERIFYING = "verifying"
    DECOMPOSING = "decomposing"
    SIMULATING = "simulating"
    SENSITIVITY = "sensitivity"
    VALIDATING = "validating"
    REPORTING = "reporting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
