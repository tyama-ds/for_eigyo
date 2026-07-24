"""Runner API v1 の共通データモデル。

Control Plane と全Runner (mock / gpt-researcher / open_deep_research) が共有する契約。
共通化できないengine固有オプションは options (自由形式) に保持し、無理に共通抽象化しない。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RunState = Literal[
    "queued", "starting", "researching", "finalizing", "succeeded", "failed",
    "timed_out", "cancelled",
]

TERMINAL_STATES: set[str] = {"succeeded", "failed", "timed_out", "cancelled"}


class LlmRunConfig(BaseModel):
    """run単位でRunnerへ渡すLLM設定。profile IDとrun-scoped secretのみを渡し、
    Runner側はこれをjob/eventへ永続化しない。"""

    profile_id: str
    api: Literal["openai-compatible", "anthropic"] = "openai-compatible"
    endpoint: str
    model: str
    api_key: str | None = None
    embedding_model: str | None = None
    embedding_endpoint: str | None = None
    timeout_seconds: int = 120


class SearchRunConfig(BaseModel):
    provider: Literal["searxng", "disabled"] = "searxng"
    endpoint: str | None = None  # SearXNG base URL
    timeout_seconds: int = 20
    max_results: int = 10


class RunInput(BaseModel):
    topic: str
    objective: str | None = None
    instructions: str | None = None
    language: str = "ja"
    input_urls: list[str] = Field(default_factory=list)
    documents: list[dict[str, Any]] = Field(default_factory=list)  # {name, text}


class RunRequest(BaseModel):
    client_run_id: str  # Control Plane採番のrun ID (冪等性キー)
    engine_id: str
    input: RunInput
    # engine固有オプション (breadth/depth/seed/speed_factor等)
    options: dict[str, Any] = Field(default_factory=dict)
    llm: LlmRunConfig | None = None
    search: SearchRunConfig | None = None
    proxy_env: dict[str, str] = Field(default_factory=dict)
    max_time_seconds: int | None = None
    max_searches: int | None = None


class RunEvent(BaseModel):
    seq: int
    type: str
    # type例: status, stage, log, search, source_found, token_usage, cost,
    #        warning, error, result_ready
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str


class SourceRecord(BaseModel):
    url: str
    title: str | None = None
    fetched_at: str | None = None
    excerpt: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class EvidenceRecord(BaseModel):
    source_url: str
    excerpt: str | None = None
    locator: str | None = None
    stance: Literal["supports", "contradicts", "context"] = "supports"
    verification: Literal["verified", "unverified", "failed"] = "unverified"


class ClaimRecord(BaseModel):
    text: str
    # key/value はengineが構造化された主張を返せる場合のみ (mock等)。
    # 実エンジンは通常textのみで、比較は正規化側のクラスタリングに委ねる。
    key: str | None = None
    value: str | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class RunMetrics(BaseModel):
    """取得できた値のみ。不明値はnullのまま返し、推測しない。"""

    searches: int | None = None
    sources: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    llm_cost_usd: float | None = None
    llm_cost_is_estimate: bool | None = None
    search_api_cost_usd: float | None = None  # self-hosted searchでは 0
    infra_cost: str | None = None  # self-hostedでは "not_measured"
    duration_seconds: float | None = None


class RunResult(BaseModel):
    output_kind: Literal["report", "answer", "evidence"] = "report"
    summary: str | None = None
    report_markdown: str | None = None
    claims: list[ClaimRecord] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    warnings: list[str] = Field(default_factory=list)
    # engine生出力 (正規化前) — Control Planeがraw artifactとして保存
    raw: dict[str, Any] = Field(default_factory=dict)


class RunStatusResponse(BaseModel):
    run_id: str
    engine_id: str
    state: RunState
    stage: str | None = None
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    last_seq: int = 0


class EngineCapabilities(BaseModel):
    engine_id: str
    name: str
    version: str | None = None
    output_kind: Literal["report", "answer", "evidence"] = "report"
    streaming: bool = True
    cancel: bool = True
    citations: bool = False
    token_usage: bool = False
    cost: bool = False
    local_files: bool = False
    # engine固有オプションのJSON Schema (UIが動的フォームに使う)
    options_schema: dict[str, Any] = Field(default_factory=dict)
    # available | unhealthy | disabled | unsupported
    health: str = "available"
    health_reason: str | None = None
    required_config: list[str] = Field(default_factory=list)
