"""Celeryタスク — durable orchestration。

設計原則:
- PostgreSQLが正本。Redis/Celeryはディスパッチにのみ使い、状態を持たない
- at-least-once配信を前提に全タスク冪等。runごとのadvisory lockで同時実行を防止
- Runner APIのclient_run_idは "{run_id}:a{attempt}" — 再試行は新しいrunner run、
  同一attemptの再送は同じrunner runへ冪等に合流する
- 1Runnerの失敗は他へ波及しない。maybe_finalize_jobが部分成功を集約する
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, text

from app.artifacts.store import ArtifactStore
from app.config import get_settings
from app.db.events import append_event
from app.db.models import (
    RUN_TERMINAL,
    EngineConfig,
    EngineRun,
    JobStatus,
    ResearchJob,
    RunStatus,
)
from app.db.session import session_scope
from app.llm.profiles import (
    ProfileNotConfiguredError,
    effective_proxy_policy,
    load_allowlist,
    resolve_role_profile,
)
from app.normalizer.normalize import normalize_run_result
from app.orchestrator.celery_app import celery_app
from app.orchestrator.service import (
    active_run_count,
    check_circuit,
    record_engine_result,
    transition_job,
    transition_run,
)
from app.runners.client import RunnerClient, RunnerError, RunnerUnavailableError
from app.security.redaction import redact
from app.synthesis.compare import compare_job
from app.synthesis.synthesize import run_synthesis

logger = structlog.get_logger(__name__)

# runイベントのうちrun.metricsへ集約するもの
_METRIC_EVENT_FIELDS = {
    "token_usage": ("prompt_tokens", "completion_tokens", "total_tokens"),
    "cost": ("llm_cost_usd", "search_api_cost_usd"),
}


def _try_advisory_lock(session, key: str) -> bool:
    """transaction-scoped advisory lockの取得を試みる (non-blocking)。"""
    got = session.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext(key))))
    return bool(got)


def _runner_url_for(session, engine_id: str) -> str | None:
    cfg = session.get(EngineConfig, engine_id)
    if cfg is None or not cfg.enabled:
        return None
    return cfg.runner_url


def _build_run_request(session, settings, run: EngineRun, job: ResearchJob) -> dict[str, Any]:
    """Runner APIへのRunRequestを構築する。平文keyはrun単位でのみ渡す。"""
    body: dict[str, Any] = {
        "client_run_id": f"{run.id}:a{run.attempt}",
        "engine_id": run.engine_id,
        "input": {
            "topic": job.topic,
            "objective": job.objective,
            "instructions": job.instructions,
            "language": job.language,
            "input_urls": job.options.get("input_urls", []),
            "documents": job.options.get("documents", []),
        },
        "options": run.options or {},
        "max_time_seconds": run.timeout_seconds,
        "max_searches": job.options.get("max_searches"),
    }
    # LLM設定: research roleのprofileを解決。mockエンジンはLLM不要のため
    # 未設定でも起動可能 (llm=None)。実エンジンはRunner側capabilitiesの
    # required_configで事前に弾かれる。
    try:
        profile = resolve_role_profile(session, settings, "research")
        body["llm"] = {
            "profile_id": profile.profile_id,
            "api": profile.api,
            "endpoint": profile.endpoint,
            "model": profile.model,
            "api_key": profile.api_key,
            "timeout_seconds": profile.timeout_seconds,
            "embedding_model": job.options.get("embedding_model"),
        }
    except ProfileNotConfiguredError:
        body["llm"] = None

    if settings.search_provider == "searxng":
        body["search"] = {
            "provider": "searxng",
            "endpoint": settings.searxng_endpoint,
            "timeout_seconds": settings.search_timeout_seconds,
            "max_results": min(
                int(job.options.get("max_search_results", settings.search_max_results)),
                settings.search_max_results,
            ),
        }
    else:
        body["search"] = {"provider": "disabled"}

    policy = effective_proxy_policy(session, settings, engine_id=run.engine_id)
    body["proxy_env"] = policy.to_env()
    return body


@celery_app.task(name="app.orchestrator.tasks.dispatch_job", bind=True, max_retries=3)
def dispatch_job(self, job_id: str) -> None:
    """job作成後の起動タスク。冪等 (既にdispatch済みなら何もしない)。"""
    settings = get_settings()
    with session_scope() as session:
        job = session.get(ResearchJob, job_id)
        if job is None:
            return
        if job.status != JobStatus.created.value:
            return  # 冪等: 二重配信
        transition_job(session, job, JobStatus.dispatching)
        run_ids = [
            r.id
            for r in session.scalars(select(EngineRun).where(EngineRun.job_id == job_id))
        ]
        transition_job(session, job, JobStatus.running)
    for run_id in run_ids:
        execute_run.delay(run_id)


@celery_app.task(
    name="app.orchestrator.tasks.execute_run",
    bind=True,
    max_retries=None,  # 再試行はrun.attempt/max_attemptsで自前管理
)
def execute_run(self, run_id: str) -> None:
    settings = get_settings()
    import uuid as _uuid

    lease_owner = str(_uuid.uuid4())

    # --- フェーズ1: 前提チェックと開始 (短いtransaction) ---
    with session_scope() as session:
        if not _try_advisory_lock(session, f"run:{run_id}"):
            # 他のworkerが処理中 — 二重実行防止
            logger.info("run_locked_elsewhere", run_id=run_id)
            return
        run = session.get(EngineRun, run_id)
        if run is None or RunStatus(run.status) in RUN_TERMINAL:
            return
        # lease: heartbeatが新しい他ownerのleaseがあれば譲る (二重実行防止)
        lease_fresh = (
            run.heartbeat_at is not None
            and datetime.now(UTC) - run.heartbeat_at
            < timedelta(seconds=settings.stuck_run_heartbeat_seconds)
        )
        if run.lease_owner and lease_fresh:
            logger.info("run_leased_elsewhere", run_id=run_id)
            return
        run.lease_owner = lease_owner
        job = session.get(ResearchJob, run.job_id)
        if job is None:
            return

        if run.cancel_requested or job.cancel_requested:
            transition_run(session, run, RunStatus.cancelled)
            _finalize_soon(run.job_id)
            return

        # 同時実行制限 (PGカウントベース、durable)
        engine_cfg = session.get(EngineConfig, run.engine_id)
        engine_limit = engine_cfg.max_concurrency if engine_cfg else settings.default_engine_max_concurrency
        if (
            active_run_count(session) >= settings.global_max_concurrent_runs
            or active_run_count(session, run.engine_id) >= engine_limit
        ):
            raise self.retry(countdown=2.0)

        # circuit breaker: 開いていれば待機、上限到達で失敗 (silent fallbackしない)
        breaker_reason = check_circuit(session, run.engine_id)
        if breaker_reason:
            run.attempt += 1
            if run.attempt >= run.max_attempts:
                transition_run(session, run, RunStatus.failed, error=breaker_reason)
                _finalize_soon(run.job_id)
                return
            backoff = min(
                settings.retry_backoff_base_seconds * (2 ** run.attempt),
                settings.retry_backoff_max_seconds,
            )
            append_event(
                session, job_id=run.job_id, run_id=run.id, engine_id=run.engine_id,
                type="retry_scheduled",
                payload={"attempt": run.attempt, "backoff_seconds": backoff,
                         "error": breaker_reason},
            )
            session.commit()
            execute_run.apply_async(args=[run_id], countdown=backoff)
            return

        runner_url = _runner_url_for(session, run.engine_id)
        if runner_url is None:
            transition_run(
                session, run, RunStatus.failed,
                error=f"engine {run.engine_id} は無効化されているかRunner URL未設定です",
            )
            _finalize_soon(run.job_id)
            return

        # runner_run_idが残っていれば必ず既存runner runへ合流する (重複起動防止)
        resuming = run.runner_run_id is not None
        if not resuming:
            run.attempt += 1
            transition_run(session, run, RunStatus.starting)
        request_body = None if resuming else _build_run_request(session, settings, run, job)
        client_run_id = run.runner_run_id or f"{run.id}:a{run.attempt}"
        run.heartbeat_at = datetime.now(UTC)
        job_id = run.job_id
        engine_id = run.engine_id
        timeout_seconds = run.timeout_seconds or settings.run_default_timeout_seconds
        last_seq = run.last_runner_seq

    # --- フェーズ2: Runner起動 (DB transaction外) ---
    client = RunnerClient(runner_url, settings)
    try:
        if request_body is not None:
            try:
                created = client.create_run(request_body)
                client_run_id = created["run_id"]
            except (RunnerError, RunnerUnavailableError) as e:
                _handle_run_failure(run_id, redact(str(e)), retryable=True)
                return
            with session_scope() as session:
                run = session.get(EngineRun, run_id)
                if run is None:
                    return
                run.runner_run_id = client_run_id
                transition_run(session, run, RunStatus.researching)
        else:
            # 再開時: starting止まりならresearchingへ進める
            with session_scope() as session:
                run = session.get(EngineRun, run_id)
                if run is not None and run.status == RunStatus.starting.value:
                    transition_run(session, run, RunStatus.researching)

        # --- フェーズ3: ポーリングループ ---
        _poll_until_terminal(
            client, run_id, client_run_id, job_id, engine_id,
            timeout_seconds=timeout_seconds, last_seq=last_seq, settings=settings,
            lease_owner=lease_owner,
        )
    finally:
        client.close()

    _finalize_soon(job_id)


def _poll_until_terminal(
    client: RunnerClient,
    run_id: str,
    runner_run_id: str,
    job_id: str,
    engine_id: str,
    *,
    timeout_seconds: int,
    last_seq: int,
    settings,
    lease_owner: str,
) -> None:
    poll_interval = settings.run_poll_interval_seconds
    deadline_check_started: datetime | None = None

    while True:
        time.sleep(poll_interval)
        try:
            events_resp = client.get_events(runner_run_id, after=last_seq)
            status = client.get_run(runner_run_id)
        except RunnerUnavailableError as e:
            _handle_run_failure(run_id, redact(str(e)), retryable=True)
            return
        except RunnerError as e:
            # Runner再起動でrunが消えた場合等 — 再試行 (新しいrunner run)
            _handle_run_failure(run_id, redact(str(e)), retryable=True)
            return

        with session_scope() as session:
            run = session.get(EngineRun, run_id)
            if run is None:
                return
            if run.lease_owner and run.lease_owner != lease_owner:
                # 別workerがleaseを奪取済み (自分のheartbeatが途絶していた) — 譲る
                logger.info("lease_lost", run_id=run_id)
                return
            job = session.get(ResearchJob, job_id)
            run.heartbeat_at = datetime.now(UTC)

            for ev in events_resp.get("events", []):
                last_seq = max(last_seq, int(ev.get("seq", 0)))
                _ingest_runner_event(session, run, ev)
            run.last_runner_seq = last_seq

            if run.started_at is None:
                run.started_at = datetime.now(UTC)
            deadline_check_started = run.started_at

            cancel = run.cancel_requested or (job is not None and job.cancel_requested)
            state = status.get("state")
            stage = status.get("stage")
            if stage and stage != run.stage:
                run.stage = stage

        # タイムアウト判定 (orchestrator側の最終防衛。Runner側にも渡している)
        timed_out = (
            deadline_check_started is not None
            and datetime.now(UTC) - deadline_check_started > timedelta(seconds=timeout_seconds)
        )

        if cancel and state not in ("succeeded", "failed", "timed_out", "cancelled"):
            try:
                client.cancel_run(runner_run_id)
            except RunnerError:
                pass
            continue  # runner側のcancelled遷移を待つ

        if timed_out and state not in ("succeeded", "failed", "timed_out", "cancelled"):
            try:
                client.cancel_run(runner_run_id)
            except RunnerError:
                pass
            _mark_terminal(run_id, RunStatus.timed_out,
                           error=f"タイムアウト ({timeout_seconds}秒) に達しました")
            return

        if state == "succeeded":
            _handle_success(client, run_id, runner_run_id, job_id, settings)
            return
        if state == "failed":
            _handle_run_failure(
                run_id, status.get("error") or "Runnerがエラーを返しました", retryable=True
            )
            return
        if state == "timed_out":
            _mark_terminal(run_id, RunStatus.timed_out, error=status.get("error") or "Runner内タイムアウト")
            return
        if state == "cancelled":
            _mark_terminal(run_id, RunStatus.cancelled)
            return


def _ingest_runner_event(session, run: EngineRun, ev: dict[str, Any]) -> None:
    ev_type = ev.get("type", "log")
    payload = ev.get("payload") or {}
    append_event(
        session,
        job_id=run.job_id,
        run_id=run.id,
        engine_id=run.engine_id,
        type=f"engine_{ev_type}",
        payload=payload,
    )
    if ev_type == "stage" and payload.get("stage"):
        run.stage = str(payload["stage"])
    fields = _METRIC_EVENT_FIELDS.get(ev_type)
    if fields:
        metrics = dict(run.metrics or {})
        for f in fields:
            if payload.get(f) is not None:
                metrics[f] = payload[f]
        run.metrics = metrics
    if ev_type == "search":
        metrics = dict(run.metrics or {})
        metrics["searches"] = int(metrics.get("searches") or 0) + 1
        run.metrics = metrics
    if ev_type == "source_found":
        metrics = dict(run.metrics or {})
        metrics["sources"] = int(metrics.get("sources") or 0) + 1
        run.metrics = metrics


def _handle_success(
    client: RunnerClient, run_id: str, runner_run_id: str, job_id: str, settings
) -> None:
    try:
        result = client.get_result(runner_run_id)
    except RunnerError as e:
        _handle_run_failure(run_id, redact(str(e)), retryable=True)
        return

    with session_scope() as session:
        run = session.get(EngineRun, run_id)
        if run is None or RunStatus(run.status) in RUN_TERMINAL:
            return
        try:
            transition_run(session, run, RunStatus.normalizing)
        except Exception:
            return

        # 生出力をartifactへ保存 (再正規化可能にする)
        store = ArtifactStore(session, settings)
        raw_bytes = json.dumps(result, ensure_ascii=False, default=str).encode()
        artifact = store.save(
            content=raw_bytes,
            kind="raw_result",
            mime="application/json",
            job_id=run.job_id,
            run_id=run.id,
        )
        append_event(
            session, job_id=run.job_id, run_id=run.id, engine_id=run.engine_id,
            type="raw_artifact_saved", payload={"artifact_id": artifact.id, "size": artifact.size},
        )

        try:
            normalized = normalize_run_result(session, run, result, raw_artifact_id=artifact.id)
        except Exception as e:  # 正規化失敗はrun失敗 (生出力は保存済み)
            transition_run(session, run, RunStatus.failed, error=redact(f"正規化に失敗: {e}"))
            record_engine_result(
                session, run.engine_id, success=False,
                failure_threshold=settings.circuit_breaker_failure_threshold,
                reset_seconds=settings.circuit_breaker_reset_seconds,
            )
            return

        # 結果metricsをrunへ反映 (取得できた値のみ。nullは保持)
        metrics = dict(run.metrics or {})
        for k, v in (result.get("metrics") or {}).items():
            if v is not None:
                metrics[k] = v
        run.metrics = metrics
        run.warnings = list(run.warnings or []) + list(result.get("warnings") or [])

        transition_run(session, run, RunStatus.succeeded)
        record_engine_result(
            session, run.engine_id, success=True,
            failure_threshold=settings.circuit_breaker_failure_threshold,
            reset_seconds=settings.circuit_breaker_reset_seconds,
        )
        append_event(
            session, job_id=run.job_id, run_id=run.id, engine_id=run.engine_id,
            type="normalized", payload={"normalized_result_id": normalized.id},
        )


def _handle_run_failure(run_id: str, error: str, *, retryable: bool) -> None:
    settings = get_settings()
    with session_scope() as session:
        run = session.get(EngineRun, run_id)
        if run is None or RunStatus(run.status) in RUN_TERMINAL:
            return
        job = session.get(ResearchJob, run.job_id)
        cancelled = run.cancel_requested or (job is not None and job.cancel_requested)
        if cancelled:
            transition_run(session, run, RunStatus.cancelled)
            _finalize_soon(run.job_id)
            return

        record_engine_result(
            session, run.engine_id, success=False,
            failure_threshold=settings.circuit_breaker_failure_threshold,
            reset_seconds=settings.circuit_breaker_reset_seconds,
        )
        if retryable and run.attempt < run.max_attempts:
            # exponential backoffで再試行。runner_run_idを破棄し新しいrunner runを作る
            run.runner_run_id = None
            run.last_runner_seq = 0
            run.lease_owner = None
            backoff = min(
                settings.retry_backoff_base_seconds * (2 ** run.attempt),
                settings.retry_backoff_max_seconds,
            )
            transition_run(session, run, RunStatus.queued)
            append_event(
                session, job_id=run.job_id, run_id=run.id, engine_id=run.engine_id,
                type="retry_scheduled",
                payload={"attempt": run.attempt, "max_attempts": run.max_attempts,
                         "backoff_seconds": backoff, "error": error},
            )
            job_id = run.job_id
            execute_run.apply_async(args=[run_id], countdown=backoff)
            return
        transition_run(session, run, RunStatus.failed, error=error)
        job_id = run.job_id
    _finalize_soon(job_id)


def _mark_terminal(run_id: str, status: RunStatus, *, error: str | None = None) -> None:
    with session_scope() as session:
        run = session.get(EngineRun, run_id)
        if run is None or RunStatus(run.status) in RUN_TERMINAL:
            return
        transition_run(session, run, status, error=error)
        job_id = run.job_id
    _finalize_soon(job_id)


def _finalize_soon(job_id: str) -> None:
    maybe_finalize_job.delay(job_id)


@celery_app.task(name="app.orchestrator.tasks.maybe_finalize_job", bind=True, max_retries=3)
def maybe_finalize_job(self, job_id: str) -> None:
    """全run終了後のjob集約。advisory lockで一度だけ実行される。冪等。"""
    settings = get_settings()
    with session_scope() as session:
        if not _try_advisory_lock(session, f"finalize:{job_id}"):
            return
        job = session.get(ResearchJob, job_id)
        if job is None or JobStatus(job.status) in (
            JobStatus.completed, JobStatus.partial, JobStatus.failed, JobStatus.cancelled
        ):
            return
        runs = list(session.scalars(select(EngineRun).where(EngineRun.job_id == job_id)))
        if not runs or any(RunStatus(r.status) not in RUN_TERMINAL for r in runs):
            return  # まだ実行中のrunがある

        succeeded = [r for r in runs if r.status == RunStatus.succeeded.value]

        # 比較は決定論的に常に実行 (成功runが1件以上ある場合)
        compare_result: dict[str, Any] | None = None
        if succeeded:
            compare_result = compare_job(session, job_id)
            append_event(
                session, job_id=job_id, type="compare_ready",
                payload={
                    "agreements": len(compare_result["agreements"]),
                    "conflicts": len(compare_result["conflicts"]),
                    "unsupported_claims": len(compare_result["unsupported_claims"]),
                },
            )

        auto_synthesize = bool(job.options.get("auto_synthesize", True))
        if succeeded and auto_synthesize:
            transition_job(session, job, JobStatus.synthesizing)
            synthesis = run_synthesis(
                session, settings, job_id, compare_result or {}, language=job.language
            )
            append_event(
                session, job_id=job_id, type="synthesis_status",
                payload={"status": synthesis.status, "error": synthesis.error,
                         "warnings": synthesis.warnings},
            )
        elif succeeded and compare_result is not None:
            # 統合なしでも比較結果は保存する
            from app.db.models import SynthesisResult

            synthesis = session.scalar(
                select(SynthesisResult).where(SynthesisResult.job_id == job_id)
            )
            if synthesis is None:
                synthesis = SynthesisResult(job_id=job_id, status="unavailable")
                session.add(synthesis)
            synthesis.sections = compare_result
            synthesis.error = "自動統合が無効化されています (auto_synthesize=false)"

        # 最終状態の決定
        if job.cancel_requested and not succeeded:
            transition_job(session, job, JobStatus.cancelled)
        elif not succeeded:
            transition_job(session, job, JobStatus.failed)
            job.error = "全エンジンが失敗しました"
        elif len(succeeded) == len(runs):
            transition_job(session, job, JobStatus.completed)
        else:
            transition_job(session, job, JobStatus.partial)
            failed_engines = [r.engine_id for r in runs if r.status != RunStatus.succeeded.value]
            job.warnings = list(job.warnings or []) + [
                f"一部のエンジンが完了しませんでした: {', '.join(failed_engines)}"
            ]


@celery_app.task(name="app.orchestrator.tasks.retry_synthesis")
def retry_synthesis(job_id: str, profile_id: str | None = None) -> None:
    """synthesis only retry — 調査をやり直さず統合だけ再実行する。"""
    settings = get_settings()
    with session_scope() as session:
        job = session.get(ResearchJob, job_id)
        if job is None:
            return
        compare_result = compare_job(session, job_id)
        synthesis = run_synthesis(
            session, settings, job_id, compare_result,
            language=job.language, profile_id=profile_id,
        )
        append_event(
            session, job_id=job_id, type="synthesis_status",
            payload={"status": synthesis.status, "error": synthesis.error,
                     "attempt": synthesis.attempt, "warnings": synthesis.warnings},
        )


@celery_app.task(name="app.orchestrator.tasks.reconcile_stuck_runs")
def reconcile_stuck_runs() -> None:
    """worker再起動回復: heartbeatが途絶えたactive runを再enqueueする。"""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.stuck_run_heartbeat_seconds)
    with session_scope() as session:
        stuck = list(
            session.scalars(
                select(EngineRun).where(
                    EngineRun.status.in_(
                        [RunStatus.starting.value, RunStatus.researching.value,
                         RunStatus.normalizing.value]
                    ),
                    (EngineRun.heartbeat_at.is_(None)) | (EngineRun.heartbeat_at < cutoff),
                )
            )
        )
        # queuedのまま放置されたrun (dispatch喪失) も回復
        stale_queued = list(
            session.scalars(
                select(EngineRun).where(
                    EngineRun.status == RunStatus.queued.value,
                    EngineRun.created_at < cutoff,
                )
            )
        )
        ids = [r.id for r in stuck + stale_queued]
        for r in stuck:
            r.lease_owner = None  # 死んだworkerのleaseを解放
            append_event(
                session, job_id=r.job_id, run_id=r.id, engine_id=r.engine_id,
                type="reconcile", payload={"reason": "heartbeat途絶のため再開します"},
            )
    for run_id in ids:
        execute_run.delay(run_id)


@celery_app.task(name="app.orchestrator.tasks.retention_cleanup")
def retention_cleanup() -> None:
    settings = get_settings()
    with session_scope() as session:
        store = ArtifactStore(session, settings)
        removed = store.cleanup_expired()
        cutoff = datetime.now(UTC) - timedelta(days=settings.event_retention_days)
        session.execute(
            text(
                "DELETE FROM job_events WHERE created_at < :cutoff AND job_id IN "
                "(SELECT id FROM research_jobs WHERE finished_at IS NOT NULL "
                " AND finished_at < :cutoff)"
            ),
            {"cutoff": cutoff},
        )
        audit_cutoff = datetime.now(UTC) - timedelta(days=settings.audit_retention_days)
        session.execute(
            text("DELETE FROM audit_log WHERE created_at < :cutoff"), {"cutoff": audit_cutoff}
        )
        logger.info("retention_cleanup_done", artifacts_removed=removed)
