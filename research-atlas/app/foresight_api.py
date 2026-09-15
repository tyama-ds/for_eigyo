"""Bounded background exploration with immutable assessment revisions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
import logging
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import storage, field_llm, foresight_llm
from .job_context import submit_with_context

router = APIRouter()
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlas-foresight")
log = logging.getLogger("atlas")


class AssessmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper_id: str = Field(min_length=1, max_length=500)
    relevance: Literal["relevant", "irrelevant", "unknown", "unjudged"]


class ExploreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=100)
    mode: Literal["local", "external"] = "local"
    provider: Literal["europepmc", "arxiv", "crossref", "scopus"] = "europepmc"
    max_rounds: int = Field(default=3, ge=1, le=3)
    limit: int = Field(default=40, ge=20, le=100)
    embedding: Literal["tfidf", "sbert"] = "tfidf"
    feedback: list[Feedback] = Field(default_factory=list, max_length=100)


class CommentaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=100)
    provider: Literal["none", "local", "openai"] = "none"
    model: str | None = Field(default=None, max_length=160)


def _progress(job_id, **update):
    from .main import JOBS, JOBS_LOCK
    with JOBS_LOCK:
        JOBS[job_id].update(**update)


def _candidate(assessment, identifier):
    candidate = next((c for c in assessment.get("candidates", []) if c["id"] == identifier), None)
    if candidate is None:
        raise ValueError("対象の推薦候補がありません。")
    return candidate


def _save(assessment, parent=None):
    value = deepcopy(assessment)
    value.update(id=storage.new_id(), created_at=storage.now(),
                 parent_id=parent.get("id") if parent else None,
                 revision=(parent.get("revision", 0) if parent else 0) + 1)
    value["numeric_hash"] = foresight_llm.digest([
        {"id": c["id"], **foresight_llm.numerical_payload(c)} for c in value["candidates"]])
    return storage.save("assessments", value)


def _fail(job_id, exc, persisted=None):
    message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "探索処理を完了できませんでした。データと実行環境を確認してください。"
    _progress(job_id, status="failed", stage="処理を完了できませんでした", error=message,
              **({"assessment_id": persisted["id"]} if persisted else {}))
    log.warning("Foresight operation failed (%s)", type(exc).__name__)


@router.get("/api/foresight/status")
def foresight_status():
    from . import foresight_sources
    return {"providers": foresight_sources.catalog(), "llm": field_llm.status(),
            "embeddings": [{"id": "tfidf", "available": True},
                           {"id": "sbert", "available": importlib.util.find_spec("sentence_transformers") is not None}],
            "max_rounds": 3, "pdf_available": importlib.util.find_spec("reportlab") is not None}


def run_create(job_id, result):
    try:
        from .foresight import build_assessment
        _progress(job_id, status="running", stage="候補・根拠・研究動向を集計")
        assessment = _save(build_assessment(result))
        _progress(job_id, status="completed", stage="推薦候補を作成しました", assessment_id=assessment["id"])
    except Exception as exc:
        _fail(job_id, exc)


@router.post("/api/assessments")
def create_assessment(body: AssessmentRequest):
    from .main import new_job
    from .foresight import PAPER_LIMIT
    summary = storage.read("results", body.result_id, include_papers=False)
    count = summary.get("meta", {}).get("paper_count", len(summary.get("papers", [])))
    if count > PAPER_LIMIT:
        raise ValueError("有望領域の反復探索は10,000件までです。大規模データは全件集計・技術マップの全件対応版で確認し、対象期間を絞った分析から反復探索してください。")
    result = storage.read("results", body.result_id)
    job = new_job("探索の準備中")
    submit_with_context(EXECUTOR, run_create, job, result)
    return {"job_id": job}


@router.get("/api/assessments")
def list_assessments(result_id: str):
    storage.read("results", result_id, include_papers=False)
    values = []
    folder = storage.data_root() / "assessments"
    if folder.exists():
        for path in folder.glob("*.json"):
            try:
                item = storage.read("assessments", path.stem)
            except KeyError:
                continue
            if item.get("result_id") == result_id:
                values.append({k: item.get(k) for k in ("id", "parent_id", "revision", "created_at", "result_id")})
    return {"assessments": sorted(values, key=lambda x: x.get("created_at") or "", reverse=True)[:100]}


@router.get("/api/assessments/{assessment_id}")
def get_assessment(assessment_id: str):
    return storage.read("assessments", assessment_id)


@router.get("/api/assessments/{assessment_id}/queries")
def assessment_queries(assessment_id: str, candidate_id: str):
    from . import foresight_sources
    value = storage.read("assessments", assessment_id)
    return foresight_sources.query_for_candidate(_candidate(value, candidate_id), value)


def run_explore(job_id, assessment, options):
    from .foresight import refine_assessment
    from . import foresight_sources
    persisted = assessment
    previous_top = None
    try:
        for index in range(options["max_rounds"]):
            _progress(job_id, status="running", stage=f"反復探索 {index + 1}/{options['max_rounds']}：検索方向を更新")
            before = len(persisted.get("rounds", []))
            revised = refine_assessment(persisted, options["candidate_id"],
                options["feedback"] if index == 0 else [], embedding=options["embedding"])
            report = None
            if options["mode"] == "external":
                candidate = _candidate(revised, options["candidate_id"])
                papers, report = foresight_sources.discover_for_candidate(candidate, revised, options["provider"], options["limit"],
                    progress=lambda stage: _progress(job_id, stage=f"反復 {index+1}：{stage}"))
                # One history row per round: recompute from the same parent with newly retrieved evidence.
                revised = refine_assessment(persisted, options["candidate_id"],
                    options["feedback"] if index == 0 else [], incoming_papers=papers, embedding=options["embedding"])
                if len(revised.get("papers", [])) > 10000:
                    raise ValueError("探索コーパスの上限10,000件を超えます。取得件数を減らしてください。")
                revised.setdefault("discovery_reports", []).append(report)
            history = revised.get("rounds", [])[before:]
            if history:
                history[-1]["mode"] = options["mode"]
                if report:
                    history[-1]["discovery_report"] = report
            current = _candidate(revised, options["candidate_id"])
            top = [r["paper_id"] for r in current.get("recommendations", [])[:20]]
            added = history[-1].get("added_papers", 0) if history else 0
            stop = top == previous_top and not added
            if stop:
                if history:
                    history[-1]["stop_reason"] = "候補順位と追加文献が変わらないため停止しました。予測の正しさを意味しません。"
                revised["stop_reason"] = history[-1]["stop_reason"] if history else "新しい文献がありません。"
            elif index + 1 == options["max_rounds"]:
                revised["stop_reason"] = "指定された反復回数に達しました。"
            persisted = _save(revised, persisted)
            _progress(job_id, assessment_id=persisted["id"], completed_rounds=index + 1)
            if stop:
                break
            previous_top = top
        _progress(job_id, status="completed", stage="反復探索を完了しました", assessment_id=persisted["id"])
    except Exception as exc:
        _fail(job_id, exc, persisted)


@router.post("/api/assessments/{assessment_id}/explore")
def explore_assessment(assessment_id: str, body: ExploreRequest):
    from .main import new_job
    from . import foresight_sources
    assessment = storage.read("assessments", assessment_id)
    _candidate(assessment, body.candidate_id)
    valid_ids = {p["id"] for p in assessment.get("papers", [])}
    if any(f.paper_id not in valid_ids for f in body.feedback):
        raise ValueError("この探索集合にない論文をフィードバックには使えません。")
    if body.mode == "external":
        if assessment.get("meta", {}).get("is_demo"):
            raise ValueError("合成デモには外部の実論文を混ぜられません。実論文のデータセットを選んでください。")
        item = next((p for p in foresight_sources.catalog() if p["id"] == body.provider), {})
        if not item.get("available", False):
            raise ValueError(item.get("reason") or "選択した論文APIを利用できません。")
    job = new_job("反復探索を準備中")
    submit_with_context(EXECUTOR, run_explore, job, assessment, body.model_dump())
    return {"job_id": job}


def run_commentary(job_id, assessment, options):
    persisted = assessment
    try:
        _progress(job_id, status="running", stage="根拠文を抽出し、支持・反証・将来像を評論")
        value = foresight_llm.generate(assessment, options["candidate_id"], options["provider"], options.get("model"),
                                      progress=lambda stage: _progress(job_id, stage=stage))
        persisted = _save(value, assessment)
        validation = (_candidate(persisted, options["candidate_id"]).get("narrative") or {}).get("validation", {})
        warned = validation.get("status") == "warning"
        _progress(job_id, status="completed", stage="⚠ 数値照合に失敗した箇所を含む評論を保存しました" if warned else "評論を保存しました",
                  assessment_id=persisted["id"], validation_status="warning" if warned else validation.get("status", "not_recorded"))
    except Exception as exc:
        try:
            value = deepcopy(assessment)
            _candidate(value, options["candidate_id"])["llm_error"] = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "評論の生成に失敗しました。"
            persisted = _save(value, assessment)
        except Exception:
            pass
        _fail(job_id, exc, persisted)


@router.post("/api/assessments/{assessment_id}/commentaries")
def create_commentary(assessment_id: str, body: CommentaryRequest):
    from .main import new_job
    value = storage.read("assessments", assessment_id)
    candidate = _candidate(value, body.candidate_id)
    if body.provider != "none":
        error = foresight_llm.commentary_input_error(value, candidate)
        if error:
            raise ValueError(error)
    job = new_job("評論を準備中")
    submit_with_context(EXECUTOR, run_commentary, job, value, body.model_dump())
    return {"job_id": job}


@router.get("/api/assessments/{assessment_id}/export")
def export_assessment(assessment_id: str, format: Literal["json", "csv", "pdf"] = "pdf", candidate_id: str | None = None):
    from . import foresight_exports
    value = storage.read("assessments", assessment_id)
    if candidate_id:
        _candidate(value, candidate_id)
    if format == "json":
        content, mime = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), "application/json"
    elif format == "csv":
        content, mime = foresight_exports.assessment_csv(value), "text/csv"
    else:
        content, mime = foresight_exports.assessment_pdf(value, candidate_id), "application/pdf"
    return Response(content, media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="research-atlas-foresight-{assessment_id[:8]}.{format}"'})
