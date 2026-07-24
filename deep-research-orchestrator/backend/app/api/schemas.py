"""Control API のリクエスト/レスポンスschema。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateJobRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=4000)
    objective: str | None = Field(default=None, max_length=4000)
    instructions: str | None = Field(default=None, max_length=8000)
    language: str = "ja"
    engines: list[str] = Field(min_length=1)
    max_time_seconds: int | None = Field(default=None, ge=10, le=6 * 3600)
    max_searches: int | None = Field(default=None, ge=1, le=200)
    max_cost_usd: float | None = Field(default=None, ge=0)
    auto_synthesize: bool = True
    input_urls: list[str] = Field(default_factory=list, max_length=20)
    documents: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    engine_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)


class RunView(BaseModel):
    id: str
    engine_id: str
    status: str
    stage: str | None
    attempt: int
    max_attempts: int
    error: str | None
    warnings: list[Any]
    metrics: dict[str, Any]
    cancel_requested: bool
    created_at: str
    started_at: str | None
    finished_at: str | None
    elapsed_seconds: float | None


class JobView(BaseModel):
    id: str
    status: str
    topic: str
    objective: str | None
    instructions: str | None
    language: str
    options: dict[str, Any]
    warnings: list[Any]
    error: str | None
    cancel_requested: bool
    created_at: str
    finished_at: str | None
    runs: list[RunView]


class SourceView(BaseModel):
    id: str
    run_id: str
    engine_id: str | None = None
    url: str
    canonical_url: str
    title: str | None
    fetched_at: str | None
    excerpt: str | None


class EvidenceView(BaseModel):
    id: str
    source_id: str
    url: str | None
    excerpt: str | None
    locator: str | None
    stance: str
    verification: str


class ClaimView(BaseModel):
    id: str
    run_id: str
    engine_id: str | None
    text: str
    meta: dict[str, Any]
    evidence: list[EvidenceView]


class NormalizedResultView(BaseModel):
    run_id: str
    engine_id: str
    summary: str | None
    report_markdown: str | None
    metrics: dict[str, Any]
    warnings: list[Any]
    raw_artifact_id: str | None


class SynthesisView(BaseModel):
    status: str
    attempt: int
    report_markdown: str | None
    sections: dict[str, Any]
    citations: list[Any]
    llm_profile_id: str | None
    error: str | None
    warnings: list[Any]


class LlmProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["local", "openai", "anthropic"]
    api: Literal["openai-compatible", "anthropic"] = "openai-compatible"
    endpoint: str | None = None
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = None  # 書き込み専用。応答へは返さない
    timeout_seconds: int = Field(default=120, ge=5, le=3600)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    enabled: bool = True


class LlmProfileView(BaseModel):
    id: str
    name: str
    provider: str
    api: str
    endpoint: str | None
    model: str
    has_api_key: bool
    api_key_masked: str | None  # "sk-....****" 形式のplaceholderのみ
    timeout_seconds: int
    max_concurrency: int
    enabled: bool


class ProxyConfigIn(BaseModel):
    scope: str = "global"
    mode: Literal["off", "inherit", "explicit"]
    http_proxy: str | None = None  # 書き込み専用
    https_proxy: str | None = None
    all_proxy: str | None = None
    no_proxy: list[str] = Field(default_factory=list)
    ca_bundle_path: str | None = None


class ProxyConfigView(BaseModel):
    scope: str
    mode: str
    has_http_proxy: bool
    has_https_proxy: bool
    has_all_proxy: bool
    no_proxy: list[str]
    ca_bundle_path: str | None


class EngineView(BaseModel):
    engine_id: str
    display_name: str
    enabled: bool
    availability: str
    unavailable_reason: str | None
    max_concurrency: int
    capabilities: dict[str, Any] | None
    healthy: bool | None


class EgressPreview(BaseModel):
    """実行前にUIへ表示する通信先一覧。"""

    destinations: list[dict[str, Any]]
