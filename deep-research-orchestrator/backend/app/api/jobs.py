"""ジョブAPI — 作成、取得、キャンセル、結果、比較、統合、エクスポート。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import db_session, settings_dep
from app.api.schemas import (
    ClaimView,
    CreateJobRequest,
    EgressPreview,
    EvidenceView,
    JobView,
    NormalizedResultView,
    RunView,
    SourceView,
    SynthesisView,
)
from app.config import Settings
from app.db.models import (
    AuditLog,
    Claim,
    EngineConfig,
    EngineRun,
    Evidence,
    NormalizedResult,
    ResearchJob,
    RunStatus,
    Source,
    SynthesisResult,
)
from app.llm.profiles import ProfileNotConfiguredError, resolve_role_profile
from app.orchestrator.service import (
    IdempotencyConflictError,
    create_job,
    request_cancel_job,
    request_cancel_run,
)
from app.security.ssrf import SsrfBlockedError, validate_url
from app.synthesis.compare import compare_job

router = APIRouter(prefix="/api", tags=["jobs"])


def _run_view(run: EngineRun) -> RunView:
    elapsed = None
    if run.started_at is not None:
        end = run.finished_at
        if end is None:
            from datetime import UTC, datetime

            end = datetime.now(UTC)
        elapsed = (end - run.started_at).total_seconds()
    return RunView(
        id=run.id,
        engine_id=run.engine_id,
        status=run.status,
        stage=run.stage,
        attempt=run.attempt,
        max_attempts=run.max_attempts,
        error=run.error,
        warnings=run.warnings or [],
        metrics=run.metrics or {},
        cancel_requested=run.cancel_requested,
        created_at=run.created_at.isoformat(),
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        elapsed_seconds=elapsed,
    )


def _job_view(session: Session, job: ResearchJob) -> JobView:
    runs = list(
        session.scalars(
            select(EngineRun).where(EngineRun.job_id == job.id).order_by(EngineRun.engine_id)
        )
    )
    return JobView(
        id=job.id,
        status=job.status,
        topic=job.topic,
        objective=job.objective,
        instructions=job.instructions,
        language=job.language,
        options=job.options or {},
        warnings=job.warnings or [],
        error=job.error,
        cancel_requested=job.cancel_requested,
        created_at=job.created_at.isoformat(),
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        runs=[_run_view(r) for r in runs],
    )


def _get_job(session: Session, job_id: str) -> ResearchJob:
    job = session.get(ResearchJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/egress-preview", response_model=EgressPreview)
def egress_preview(
    engines: str = Query(default=""),
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> EgressPreview:
    """実行前の通信先一覧 (spec: 送信先を実行前にUIへ表示する)。"""
    destinations: list[dict[str, Any]] = []
    try:
        profile = resolve_role_profile(session, settings, "research")
        destinations.append(
            {
                "kind": "llm",
                "name": f"LLM ({profile.provider}: {profile.name})",
                "host": urlparse(profile.endpoint).hostname,
                "purpose": "research/summarization/synthesis等の生成AI処理",
            }
        )
    except ProfileNotConfiguredError:
        destinations.append(
            {"kind": "llm", "name": "LLM未設定", "host": None,
             "purpose": "LLM profileが未割り当てのため、LLMを必要とするエンジンは実行できません"}
        )
    if settings.search_provider == "searxng":
        destinations.append(
            {
                "kind": "search",
                "name": "SearXNG (self-hosted)",
                "host": urlparse(settings.searxng_endpoint).hostname,
                "purpose": "Web検索 (メタ検索エンジン経由で外部検索エンジンへ到達)",
            }
        )
    else:
        destinations.append(
            {"kind": "search", "name": "検索無効", "host": None,
             "purpose": "SEARCH_PROVIDER=disabled のため検索は行われません"}
        )
    destinations.append(
        {"kind": "web", "name": "一般Web取得", "host": "(検索結果のURL)",
         "purpose": "検索で見つかったページの本文取得 (SSRFガード適用)"}
    )
    for engine_id in [e for e in engines.split(",") if e]:
        cfg = session.get(EngineConfig, engine_id)
        if cfg is not None:
            destinations.append(
                {"kind": "runner", "name": f"Runner: {cfg.display_name}",
                 "host": urlparse(cfg.runner_url).hostname,
                 "purpose": "調査エンジンの実行 (内部サービス)"}
            )
    return EgressPreview(destinations=destinations)


@router.post("/jobs", response_model=JobView, status_code=201)
def post_job(
    body: CreateJobRequest,
    response: Response,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> JobView:
    # engine検証: 未知/無効エンジンは明示エラー (silent fallback禁止)
    for engine_id in body.engines:
        cfg = session.get(EngineConfig, engine_id)
        if cfg is None:
            raise HTTPException(status_code=400, detail=f"未知のエンジンです: {engine_id}")
        if not cfg.enabled or cfg.availability in ("unsupported", "disabled"):
            raise HTTPException(
                status_code=400,
                detail=f"エンジン {engine_id} は利用できません: "
                f"{cfg.unavailable_reason or cfg.availability}",
            )

    # ユーザー入力URLのSSRF検証 (登録時に拒否)
    for url in body.input_urls:
        try:
            validate_url(url, origin="untrusted")
        except SsrfBlockedError as e:
            raise HTTPException(status_code=400, detail=f"入力URLが拒否されました: {e}") from e

    options: dict[str, Any] = {
        "auto_synthesize": body.auto_synthesize,
        "input_urls": body.input_urls,
        "documents": body.documents,
    }
    if body.max_time_seconds is not None:
        options["max_time_seconds"] = body.max_time_seconds
    if body.max_searches is not None:
        options["max_searches"] = body.max_searches
    if body.max_cost_usd is not None:
        options["max_cost_usd"] = body.max_cost_usd

    idem = body.idempotency_key or idempotency_key_header
    try:
        job = create_job(
            session,
            topic=body.topic,
            engines=body.engines,
            objective=body.objective,
            instructions=body.instructions,
            language=body.language,
            options=options,
            idempotency_key=idem,
            engine_options=body.engine_options,
            default_timeout_seconds=settings.run_default_timeout_seconds,
            max_attempts=settings.run_max_attempts,
        )
    except IdempotencyConflictError as e:
        # 同じidempotency key → 既存jobを200で返す (重複実行しない)
        existing = _get_job(session, e.existing_job_id)
        response.status_code = 200
        return _job_view(session, existing)

    session.add(AuditLog(action="job.create", target=job.id,
                         detail={"topic": body.topic, "engines": body.engines}))
    session.commit()

    from app.orchestrator.tasks import dispatch_job

    dispatch_job.delay(job.id)
    return _job_view(session, job)


@router.get("/jobs", response_model=list[JobView])
def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(db_session),
) -> list[JobView]:
    jobs = list(
        session.scalars(
            select(ResearchJob).order_by(ResearchJob.created_at.desc()).limit(limit)
        )
    )
    return [_job_view(session, j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str, session: Session = Depends(db_session)) -> JobView:
    return _job_view(session, _get_job(session, job_id))


@router.post("/jobs/{job_id}/cancel", response_model=JobView)
def cancel_job(job_id: str, session: Session = Depends(db_session)) -> JobView:
    job = _get_job(session, job_id)
    request_cancel_job(session, job)
    session.add(AuditLog(action="job.cancel", target=job_id))
    return _job_view(session, job)


@router.post("/jobs/{job_id}/runs/{run_id}/cancel", response_model=JobView)
def cancel_run(
    job_id: str, run_id: str, session: Session = Depends(db_session)
) -> JobView:
    job = _get_job(session, job_id)
    run = session.get(EngineRun, run_id)
    if run is None or run.job_id != job_id:
        raise HTTPException(status_code=404, detail="run not found")
    request_cancel_run(session, run)
    session.add(AuditLog(action="run.cancel", target=run_id))
    return _job_view(session, job)


@router.get("/jobs/{job_id}/results", response_model=list[NormalizedResultView])
def get_results(job_id: str, session: Session = Depends(db_session)) -> list[NormalizedResultView]:
    _get_job(session, job_id)
    runs = {
        r.id: r for r in session.scalars(select(EngineRun).where(EngineRun.job_id == job_id))
    }
    results = session.scalars(
        select(NormalizedResult).where(NormalizedResult.run_id.in_(runs.keys()))
    )
    return [
        NormalizedResultView(
            run_id=nr.run_id,
            engine_id=runs[nr.run_id].engine_id,
            summary=nr.summary,
            report_markdown=nr.report_markdown,
            metrics=nr.metrics or {},
            warnings=nr.warnings or [],
            raw_artifact_id=nr.raw_artifact_id,
        )
        for nr in results
    ]


@router.get("/jobs/{job_id}/sources", response_model=list[SourceView])
def get_sources(job_id: str, session: Session = Depends(db_session)) -> list[SourceView]:
    _get_job(session, job_id)
    runs = {
        r.id: r.engine_id
        for r in session.scalars(select(EngineRun).where(EngineRun.job_id == job_id))
    }
    sources = session.scalars(select(Source).where(Source.job_id == job_id))
    return [
        SourceView(
            id=s.id,
            run_id=s.run_id,
            engine_id=runs.get(s.run_id),
            url=s.url,
            canonical_url=s.canonical_url,
            title=s.title,
            fetched_at=s.fetched_at.isoformat() if s.fetched_at else None,
            excerpt=s.excerpt,
        )
        for s in sources
    ]


@router.get("/jobs/{job_id}/claims", response_model=list[ClaimView])
def get_claims(job_id: str, session: Session = Depends(db_session)) -> list[ClaimView]:
    _get_job(session, job_id)
    runs = {
        r.id: r.engine_id
        for r in session.scalars(select(EngineRun).where(EngineRun.job_id == job_id))
    }
    claims = list(session.scalars(select(Claim).where(Claim.job_id == job_id)))
    views = []
    for c in claims:
        evidence_rows = list(session.scalars(select(Evidence).where(Evidence.claim_id == c.id)))
        ev_views = []
        for ev in evidence_rows:
            src = session.get(Source, ev.source_id)
            ev_views.append(
                EvidenceView(
                    id=ev.id,
                    source_id=ev.source_id,
                    url=src.url if src else None,
                    excerpt=ev.excerpt,
                    locator=ev.locator,
                    stance=ev.stance,
                    verification=ev.verification,
                )
            )
        views.append(
            ClaimView(
                id=c.id, run_id=c.run_id, engine_id=runs.get(c.run_id),
                text=c.text, meta=c.meta or {}, evidence=ev_views,
            )
        )
    return views


@router.get("/jobs/{job_id}/compare")
def get_compare(job_id: str, session: Session = Depends(db_session)) -> dict[str, Any]:
    _get_job(session, job_id)
    return compare_job(session, job_id)


@router.get("/jobs/{job_id}/synthesis", response_model=SynthesisView)
def get_synthesis(job_id: str, session: Session = Depends(db_session)) -> SynthesisView:
    _get_job(session, job_id)
    synthesis = session.scalar(select(SynthesisResult).where(SynthesisResult.job_id == job_id))
    if synthesis is None:
        raise HTTPException(status_code=404, detail="synthesisがまだ実行されていません")
    return SynthesisView(
        status=synthesis.status,
        attempt=synthesis.attempt,
        report_markdown=synthesis.report_markdown,
        sections=synthesis.sections or {},
        citations=synthesis.citations or [],
        llm_profile_id=synthesis.llm_profile_id,
        error=synthesis.error,
        warnings=synthesis.warnings or [],
    )


@router.post("/jobs/{job_id}/synthesis/retry", status_code=202)
def retry_synthesis_endpoint(
    job_id: str,
    profile_id: str | None = Query(default=None),
    session: Session = Depends(db_session),
) -> dict[str, str]:
    job = _get_job(session, job_id)
    succeeded = [
        r for r in session.scalars(select(EngineRun).where(EngineRun.job_id == job.id))
        if r.status == RunStatus.succeeded.value
    ]
    if not succeeded:
        raise HTTPException(status_code=409, detail="成功したrunがないため統合できません")
    session.add(AuditLog(action="synthesis.retry", target=job_id,
                         detail={"profile_id": profile_id}))
    session.commit()
    from app.orchestrator.tasks import retry_synthesis

    retry_synthesis.delay(job_id, profile_id)
    return {"status": "scheduled"}


@router.get("/jobs/{job_id}/export")
def export_job(
    job_id: str,
    format: str = Query(default="json", pattern="^(json|markdown)$"),
    session: Session = Depends(db_session),
) -> Response:
    """Markdown/JSONエクスポート。provenance (engine/URL/excerpt) を保持する。"""
    job = _get_job(session, job_id)
    job_view = _job_view(session, job).model_dump()
    sources = [s.model_dump() for s in get_sources(job_id, session)]
    claims = [c.model_dump() for c in get_claims(job_id, session)]
    results = [r.model_dump() for r in get_results(job_id, session)]
    synthesis_row = session.scalar(
        select(SynthesisResult).where(SynthesisResult.job_id == job_id)
    )
    synthesis = None
    if synthesis_row is not None:
        synthesis = {
            "status": synthesis_row.status,
            "report_markdown": synthesis_row.report_markdown,
            "sections": synthesis_row.sections,
            "citations": synthesis_row.citations,
            "warnings": synthesis_row.warnings,
        }
    payload = {
        "format_version": "1",
        "job": job_view,
        "results": results,
        "claims": claims,
        "sources": sources,
        "synthesis": synthesis,
    }
    if format == "json":
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=1),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="job-{job_id}.json"'},
        )

    lines = [f"# 調査ジョブ: {job.topic}", "",
             f"- ジョブID: {job.id}", f"- 状態: {job.status}",
             f"- 作成: {job.created_at.isoformat()}", ""]
    if synthesis and synthesis.get("report_markdown"):
        lines += ["## 統合レポート", "", synthesis["report_markdown"], ""]
        if synthesis.get("citations"):
            lines += ["### 統合レポートの引用", ""]
            for c in synthesis["citations"]:
                engines = ", ".join(c.get("engines", []))
                lines.append(f"- [{c['sid']}] {c.get('title') or ''} — {c['url']} (発見: {engines})")
            lines.append("")
    for r in results:
        lines += [f"## エンジン別レポート: {r['engine_id']}", ""]
        lines += [r["report_markdown"] or "(レポートなし)", ""]
    lines += ["## 全ソース (provenance付き)", ""]
    for s in sources:
        lines.append(f"- {s['title'] or '(無題)'} — {s['url']} (エンジン: {s['engine_id']})")
    return Response(
        content="\n".join(lines),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="job-{job_id}.md"'},
    )
