"""SQLAlchemy ORM models — PostgreSQLを長時間ジョブ状態の正本とする。"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class JobStatus(str, enum.Enum):
    created = "created"
    dispatching = "dispatching"
    running = "running"
    normalizing = "normalizing"
    synthesizing = "synthesizing"
    completed = "completed"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"


JOB_TERMINAL = {JobStatus.completed, JobStatus.partial, JobStatus.failed, JobStatus.cancelled}

# 状態遷移表 — これ以外の遷移は StateTransitionError
JOB_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.created: {JobStatus.dispatching, JobStatus.cancelled, JobStatus.failed},
    JobStatus.dispatching: {JobStatus.running, JobStatus.cancelled, JobStatus.failed},
    JobStatus.running: {
        JobStatus.normalizing,
        JobStatus.synthesizing,
        JobStatus.completed,
        JobStatus.partial,
        JobStatus.failed,
        JobStatus.cancelled,
    },
    JobStatus.normalizing: {
        JobStatus.synthesizing,
        JobStatus.completed,
        JobStatus.partial,
        JobStatus.failed,
        JobStatus.cancelled,
    },
    JobStatus.synthesizing: {
        JobStatus.completed,
        JobStatus.partial,
        JobStatus.failed,
        JobStatus.cancelled,
    },
}


class RunStatus(str, enum.Enum):
    queued = "queued"
    starting = "starting"
    researching = "researching"
    normalizing = "normalizing"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"


RUN_TERMINAL = {RunStatus.succeeded, RunStatus.failed, RunStatus.timed_out, RunStatus.cancelled}

RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.queued: {RunStatus.starting, RunStatus.cancelled, RunStatus.failed},
    RunStatus.starting: {
        RunStatus.researching,
        RunStatus.failed,
        RunStatus.timed_out,
        RunStatus.cancelled,
        # retry時にqueuedへ戻す
        RunStatus.queued,
    },
    RunStatus.researching: {
        RunStatus.normalizing,
        RunStatus.failed,
        RunStatus.timed_out,
        RunStatus.cancelled,
        RunStatus.queued,
    },
    RunStatus.normalizing: {
        RunStatus.succeeded,
        RunStatus.failed,
        RunStatus.timed_out,
        RunStatus.cancelled,
    },
}


class ResearchJob(Base):
    __tablename__ = "research_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.created.value, index=True)
    topic: Mapped[str] = mapped_column(Text)
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="ja")
    # max_time_seconds / max_searches / max_cost_usd / auto_synthesize / seed / input_urls...
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    runs: Mapped[list[EngineRun]] = relationship(back_populates="job")


class EngineRun(Base):
    __tablename__ = "engine_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("research_jobs.id"), index=True)
    engine_id: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.queued.value, index=True)
    stage: Mapped[str | None] = mapped_column(String(100), nullable=True)
    runner_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # tokens/cost(llm_cost_usd, search_api_cost_usd, infra_cost)/searches/sources は取得できた
    # 値のみ。不明値は null のまま保持し捏造しない。
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_runner_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # worker lease — 二重実行防止。heartbeatが新しい他ownerのleaseがある間は他workerが触らない
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[ResearchJob] = relationship(back_populates="runs")

    __table_args__ = (UniqueConstraint("job_id", "engine_id", name="uq_engine_runs_job_engine"),)


class JobEvent(Base):
    """SSEで配信する全イベントの正本。seqはjob内で単調増加。"""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("research_jobs.id"), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    engine_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("job_id", "seq", name="uq_job_events_job_seq"),
        Index("ix_job_events_job_seq", "job_id", "seq"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(50))  # raw_result | report_md | snapshot | log | export
    # relative_path が null の場合は content_inline (PostgreSQL) に保存されている
    relative_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content_inline: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mime: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NormalizedResult(Base):
    __tablename__ = "normalized_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("engine_runs.id"), unique=True, index=True)
    normalizer_version: Mapped[str] = mapped_column(String(20), default="1")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    raw_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (Index("ix_sources_job_canonical", "job_id", "canonical_url"),)


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    locator: Mapped[str | None] = mapped_column(String(500), nullable=True)
    stance: Mapped[str] = mapped_column(String(20), default="supports")
    # verified: excerptが取得snapshotと照合済み / unverified: 照合未実施 / failed: 照合失敗
    verification: Mapped[str] = mapped_column(String(20), default="unverified")


class SynthesisResult(Base):
    __tablename__ = "synthesis_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | running | succeeded | failed | unavailable
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    report_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # agreements / partial_findings / conflicts / unsupported_claims / coverage_gaps /
    # open_questions — comparison engineの決定論的出力とLLM統合の両方を保持
    sections: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    citations: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    llm_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LlmProfile(Base):
    __tablename__ = "llm_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    provider: Mapped[str] = mapped_column(String(20))  # local | openai | anthropic
    api: Mapped[str] = mapped_column(String(30), default="openai-compatible")
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str] = mapped_column(String(200))
    api_key_secret_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RoleAssignment(Base):
    """生成AI role (research/summarization/normalization/synthesis) → LLM profile。"""

    __tablename__ = "role_assignments"

    role: Mapped[str] = mapped_column(String(30), primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("llm_profiles.id"))


class SecretItem(Base):
    """暗号化保存されたsecret。平文はAPI/log/SSEへ出さない。"""

    __tablename__ = "secrets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProxyConfig(Base):
    """proxy設定 (scope='global' がグローバル、scope=engine_id がengine別override)。"""

    __tablename__ = "proxy_configs"

    scope: Mapped[str] = mapped_column(String(100), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10), default="off")  # off | inherit | explicit
    http_proxy_secret_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    https_proxy_secret_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    all_proxy_secret_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    no_proxy: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    ca_bundle_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EngineConfig(Base):
    __tablename__ = "engine_configs"

    engine_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    runner_url: Mapped[str] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # available | experimental | unsupported | disabled — provider matrixの分類
    availability: Mapped[str] = mapped_column(String(20), default="available")
    unavailable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # circuit breaker状態
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    circuit_open_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LlmEndpointAllowlist(Base):
    """管理者が登録したLocal LLM endpointのallowlist。

    SSRF policy: private network宛の通信はこの表に載っているendpointだけ許可する。
    調査入力からは変更不可 (settings APIのみが書き込む)。
    """

    __tablename__ = "llm_endpoint_allowlist"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    host: Mapped[str] = mapped_column(String(300))
    port: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("host", "port", name="uq_llm_allowlist_host_port"),)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(200), default="anonymous")
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str | None] = mapped_column(String(300), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
