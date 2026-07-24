"""Orchestratorドメインロジック — API層とCeleryタスクの両方から呼ばれる。

状態遷移はJOB_TRANSITIONS/RUN_TRANSITIONSで検証し、不正遷移は例外にする。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.events import append_event
from app.db.models import (
    JOB_TRANSITIONS,
    RUN_TERMINAL,
    RUN_TRANSITIONS,
    EngineConfig,
    EngineRun,
    JobStatus,
    ResearchJob,
    RunStatus,
)


class StateTransitionError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    def __init__(self, existing_job_id: str):
        super().__init__(f"同じidempotency keyのジョブが既に存在します: {existing_job_id}")
        self.existing_job_id = existing_job_id


def transition_job(session: Session, job: ResearchJob, to: JobStatus) -> None:
    current = JobStatus(job.status)
    if current == to:
        return
    allowed = JOB_TRANSITIONS.get(current, set())
    if to not in allowed:
        raise StateTransitionError(f"job遷移が不正です: {current.value} -> {to.value}")
    job.status = to.value
    if to in (JobStatus.completed, JobStatus.partial, JobStatus.failed, JobStatus.cancelled):
        job.finished_at = datetime.now(UTC)
    append_event(session, job_id=job.id, type="job_status", payload={"status": to.value})


def transition_run(session: Session, run: EngineRun, to: RunStatus, *, error: str | None = None) -> None:
    current = RunStatus(run.status)
    if current == to:
        return
    if current in RUN_TERMINAL:
        raise StateTransitionError(f"run {run.id} は終了済みです ({current.value})")
    allowed = RUN_TRANSITIONS.get(current, set())
    if to not in allowed:
        raise StateTransitionError(f"run遷移が不正です: {current.value} -> {to.value}")
    run.status = to.value
    if error is not None:
        run.error = error
    if to == RunStatus.researching and run.started_at is None:
        run.started_at = datetime.now(UTC)
    if to in RUN_TERMINAL:
        run.finished_at = datetime.now(UTC)
    append_event(
        session,
        job_id=run.job_id,
        run_id=run.id,
        engine_id=run.engine_id,
        type="run_status",
        payload={"status": to.value, "error": error, "attempt": run.attempt},
    )


def create_job(
    session: Session,
    *,
    topic: str,
    engines: list[str],
    objective: str | None = None,
    instructions: str | None = None,
    language: str = "ja",
    options: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    engine_options: dict[str, dict[str, Any]] | None = None,
    default_timeout_seconds: int = 1800,
    max_attempts: int = 3,
) -> ResearchJob:
    """ジョブとengine run群を作成する。idempotency keyで重複実行を防止。"""
    if idempotency_key:
        existing = session.scalar(
            select(ResearchJob).where(ResearchJob.idempotency_key == idempotency_key)
        )
        if existing is not None:
            raise IdempotencyConflictError(existing.id)

    options = dict(options or {})
    job = ResearchJob(
        topic=topic,
        objective=objective,
        instructions=instructions,
        language=language,
        options=options,
        idempotency_key=idempotency_key,
    )
    session.add(job)
    session.flush()

    timeout = int(options.get("max_time_seconds") or default_timeout_seconds)
    for engine_id in engines:
        run = EngineRun(
            job_id=job.id,
            engine_id=engine_id,
            options=(engine_options or {}).get(engine_id, {}),
            timeout_seconds=timeout,
            max_attempts=max_attempts,
        )
        session.add(run)
    session.flush()
    append_event(
        session,
        job_id=job.id,
        type="job_created",
        payload={"topic": topic, "engines": engines, "options": options},
    )
    return job


def request_cancel_job(session: Session, job: ResearchJob) -> None:
    job.cancel_requested = True
    for run in session.scalars(select(EngineRun).where(EngineRun.job_id == job.id)):
        if RunStatus(run.status) not in RUN_TERMINAL:
            run.cancel_requested = True
    append_event(session, job_id=job.id, type="cancel_requested", payload={"scope": "job"})


def request_cancel_run(session: Session, run: EngineRun) -> None:
    if RunStatus(run.status) in RUN_TERMINAL:
        return
    run.cancel_requested = True
    append_event(
        session,
        job_id=run.job_id,
        run_id=run.id,
        engine_id=run.engine_id,
        type="cancel_requested",
        payload={"scope": "run"},
    )


def active_run_count(session: Session, engine_id: str | None = None) -> int:
    q = select(func.count(EngineRun.id)).where(
        EngineRun.status.in_([RunStatus.starting.value, RunStatus.researching.value,
                              RunStatus.normalizing.value])
    )
    if engine_id:
        q = q.where(EngineRun.engine_id == engine_id)
    return session.scalar(q) or 0


def check_circuit(session: Session, engine_id: str) -> str | None:
    """circuit breakerが開いていれば理由文字列を返す。"""
    cfg = session.get(EngineConfig, engine_id)
    if cfg is None:
        return None
    if cfg.circuit_open_until and cfg.circuit_open_until > datetime.now(UTC):
        return (
            f"engine {engine_id} のcircuit breakerが開いています "
            f"(連続失敗{cfg.consecutive_failures}回、{cfg.circuit_open_until.isoformat()}まで)"
        )
    return None


def record_engine_result(
    session: Session,
    engine_id: str,
    *,
    success: bool,
    failure_threshold: int,
    reset_seconds: int,
) -> None:
    cfg = session.get(EngineConfig, engine_id)
    if cfg is None:
        return
    if success:
        cfg.consecutive_failures = 0
        cfg.circuit_open_until = None
    else:
        cfg.consecutive_failures += 1
        if cfg.consecutive_failures >= failure_threshold:
            from datetime import timedelta

            cfg.circuit_open_until = datetime.now(UTC) + timedelta(seconds=reset_seconds)
    session.flush()
