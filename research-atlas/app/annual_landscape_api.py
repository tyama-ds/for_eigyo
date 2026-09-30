"""Annual-only report jobs, partial snapshots, exports and cooperative stops."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import threading
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import annual_landscape_reports as reports, storage
from .job_context import submit_with_context
from .landscape_reports_api import _generation_failure
from .limits import MAX_ANALYSIS_YEARS

router = APIRouter()
_CONTROL_LOCK = threading.RLock()
_CANCELLATIONS: dict[str, dict] = {}


class AnnualLandscapeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    projection: Literal["auto", "pca", "umap", "tsne"] = "auto"
    projection_id: str | None = Field(default=None, max_length=100)
    interval: Literal["year"] = "year"
    scope: Literal["sample", "full"] = "sample"
    topic_id: str = Field(min_length=1, max_length=100)
    start_year: int = Field(strict=True, ge=1500)
    end_year: int = Field(strict=True, ge=1500)
    provider: Literal["none", "local", "openai"] = "none"
    model: str | None = Field(default=None, min_length=1, max_length=160)
    include_transitions: bool = False

    @model_validator(mode="after")
    def valid_range(self):
        if self.start_year > self.end_year or self.end_year > date.today().year:
            raise ValueError("年次レポートの開始年・終了年を確認してください。")
        if self.end_year - self.start_year >= MAX_ANALYSIS_YEARS:
            raise ValueError(f"年次レポートは最大{MAX_ANALYSIS_YEARS}年の範囲を指定してください。")
        return self


def _progress(job_id, **values):
    from .main import JOBS, JOBS_LOCK
    with JOBS_LOCK:
        JOBS[job_id].update(**values)


def _read(report_id):
    report = storage.read(reports.STORE_KIND, report_id)
    if report.get("kind") != "annual":
        raise KeyError(report_id)
    return report


def run_annual_report(job_id, report_id, options, cancel_event):
    report = _read(report_id)
    created_at = report["created_at"]
    def persist(value):
        value["created_at"] = created_at
        with _CONTROL_LOCK:
            value["cancel_requested"] = cancel_event.is_set()
            storage.save(reports.STORE_KIND, value)
        _progress(job_id, annual_report_id=report_id, projection_id=value.get("projection_id"),
                  generation_status=value["generation_status"], progress=deepcopy(value["progress"]))
    def on_generation(label, event):
        seconds = max(0, int(event.get("elapsed_seconds", 0)))
        count = max(0, int(event.get("received_chars", 0)))
        suffix = " · 中止要求済み（現在の生成完了後に停止）" if cancel_event.is_set() else ""
        _progress(job_id, stage=f"{label}の評論：{seconds // 60}分{seconds % 60:02d}秒・{count:,}文字受信（検証前）{suffix}")
    try:
        if cancel_event.is_set():
            raise reports.AnnualCancelled()
        _progress(job_id, status="running", stage="年単位の共通座標・根拠論文を準備", annual_report_id=report_id)
        report = reports.prepare_report(options["result_id"], options["projection"], options["scope"],
            options["topic_id"], options["start_year"], options["end_year"], projection_id=options.get("projection_id"),
            provider=options["provider"], include_transitions=options["include_transitions"],
            report_id=report_id, on_prepare=persist, cancelled=cancel_event.is_set)
        persist(report)
        report = reports.generate_children(report, model=options.get("model"), cancelled=cancel_event.is_set,
                                           on_save=persist, on_progress=on_generation)
        persist(report)
        stage = {"partial": "一部の評論を生成できませんでした。年別の計測値と保存済み評論を表示します。",
                 "failed": "評論の生成に失敗しました。年別の計測値と原文抜粋は保存されています。",
                 "not_generated": "評論に使える根拠が不足しているため生成していません。年別の計測値は保存されています。",
                 "not_requested": "LLMを使わず年別の計測値・読解ガイドを保存しました。"}.get(
                     report["generation_status"], "年次レポートを保存しました")
        _progress(job_id, status="completed", stage=stage, generation_status=report["generation_status"],
                  annual_report_id=report_id, progress=deepcopy(report["progress"]))
    except reports.AnnualCancelled:
        # Preparation can publish chapters before returning its full object.
        report = _read(report_id)
        reports.cancel_remaining(report)
        persist(report)
        _progress(job_id, status="completed", stage="年次レポートを中止しました。保存済みの結果は残っています。",
                  generation_status="cancelled", cancelled=True, annual_report_id=report_id,
                  progress=deepcopy(report["progress"]))
    except Exception as exc:
        report = _read(report_id)
        kind, message = _generation_failure(exc)
        # Preparation ValueErrors come from local selection/ownership checks,
        # but never persist arbitrary external-client exception text.
        message = "年次レポートの対象期間・トピック・座標の版を確認してください。" if isinstance(exc, ValueError) else message
        report.update(generation_status="failed", generation_error_kind=kind, llm_error=message)
        persist(report)
        _progress(job_id, status="failed", stage="年次レポートの準備を完了できませんでした", error=message,
                  generation_status="failed", annual_report_id=report_id)
    finally:
        with _CONTROL_LOCK:
            _CANCELLATIONS.pop(report_id, None)


@router.post("/api/landscape-annual-reports")
def start_annual_report(body: AnnualLandscapeRequest):
    from .main import new_job, REPORT_EXECUTOR
    options = body.model_dump()
    result = storage.read("results", body.result_id, include_papers=False)
    report = reports.initial_report(options, result)
    identifier = new_job("年次レポートを準備中")
    event = threading.Event()
    with _CONTROL_LOCK:
        storage.save(reports.STORE_KIND, report)
        _CANCELLATIONS[report["id"]] = {"event": event, "job_id": identifier, "result_id": report["result_id"]}
    _progress(identifier, kind="annual_landscape_report", annual_report_id=report["id"], generation_status="preparing",
              progress=deepcopy(report["progress"]))
    submit_with_context(REPORT_EXECUTOR, run_annual_report, identifier, report["id"], options, event)
    return {"job_id": identifier, "annual_report_id": report["id"]}


@router.get("/api/landscape-annual-reports/{report_id}")
def get_annual_report(report_id: str):
    return _read(report_id)


@router.post("/api/landscape-annual-reports/{report_id}/cancel")
def cancel_annual_report(report_id: str):
    with _CONTROL_LOCK:
        report = _read(report_id)
        control = _CANCELLATIONS.get(report_id)
        requested = report["generation_status"] not in reports.TERMINAL_STATUSES
        if requested:
            if control:
                if control["result_id"] != report["result_id"]:
                    raise KeyError(report_id)
                control["event"].set()
                report["cancel_requested"] = True
                _progress(control["job_id"], stage="中止要求を受け付けました。現在の生成完了後に停止します。", cancel_requested=True)
            else:
                # A saved interrupted job has no running model after restart.
                reports.cancel_remaining(report)
            storage.save(reports.STORE_KIND, report)
    return {"annual_report_id": report_id, "cancel_requested": requested,
            "generation_status": report["generation_status"], "report": report}


@router.get("/api/landscape-annual-reports/{report_id}/export")
def export_annual_report(report_id: str, format: Literal["csv", "json"] = "csv"):
    report = _read(report_id)
    text = reports.export_csv(report) if format == "csv" else json.dumps(report, ensure_ascii=False, allow_nan=False)
    return Response(text, media_type="text/csv" if format == "csv" else "application/json", headers={
        "Content-Disposition": f'attachment; filename="research-atlas-annual-{report_id[:8]}.{format}"'})
