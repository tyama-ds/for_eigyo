"""Queued, request-context-owned narrative endpoints for the landscape."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import landscape_reports, storage
from .job_context import submit_with_context

router = APIRouter()


class LandscapeReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    projection: Literal["auto", "pca", "umap", "tsne"] = "auto"
    projection_id: str | None = Field(default=None, max_length=100)
    interval: Literal["year", "quarter", "month"] = "year"
    movement_id: str = Field(min_length=1, max_length=200)
    provider: Literal["none", "local", "openai"] = "none"
    model: str | None = Field(default=None, min_length=1, max_length=160)


def _progress(job_id, **values):
    from .main import JOBS, JOBS_LOCK
    with JOBS_LOCK:
        JOBS[job_id].update(**values)


def run_landscape_report(job_id: str, options: dict):
    report = None
    try:
        _progress(job_id, status="running", stage="共有座標と前後の根拠論文を照合")
        report = landscape_reports.prepare_report(options["result_id"], options["projection"], options["interval"],
                                                  options["movement_id"], options.get("projection_id"))
        storage.save("landscape_reports", report)
        _progress(job_id, landscape_report_id=report["id"], projection_id=report["projection_id"])
        if options["provider"] != "none":
            _progress(job_id, stage="LLMで話題の変化を解釈")
            def on_generation(event):
                seconds, count = max(0, int(event["elapsed_seconds"])), max(0, int(event["received_chars"]))
                _progress(job_id, stage=f"話題の変化を解釈：{seconds // 60}分{seconds % 60:02d}秒・{count:,}文字受信（検証前）")
            try:
                report["narrative"] = landscape_reports.generate(report, options["provider"], options.get("model"), progress=on_generation)
            except Exception:
                # Connection exceptions can embed a URL/key. Keep only a fixed public message.
                report["llm_error"] = "LLMの接続・回答形式または根拠照合を確認できませんでした。接続設定と抄録の有無を確認してください。計測値と定型解釈は保持しています。"
                report["narrative"]["validation"] = {"status": "warning", "warnings": [{
                    "code": "llm_failed", "message": report["llm_error"]}]}
            storage.save("landscape_reports", report)
        warned = report["narrative"].get("validation", {}).get("status") == "warning"
        _progress(job_id, status="completed", stage="⚠ 確認事項付きで重心変化の解釈を保存" if warned else "重心変化の解釈を保存",
                  landscape_report_id=report["id"], projection_id=report["projection_id"], validation_status="warning" if warned else "passed")
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else "重心変化の解釈を作成できませんでした。分析結果と実行環境を確認してください。"
        _progress(job_id, status="failed", stage="解釈の作成を完了できませんでした", error=message)


@router.post("/api/landscape-reports")
def start_landscape_report(body: LandscapeReportRequest):
    from .main import new_job, REPORT_EXECUTOR
    storage.read("results", body.result_id, include_papers=False)
    identifier = new_job("重心変化の解釈を準備中")
    submit_with_context(REPORT_EXECUTOR, run_landscape_report, identifier, body.model_dump())
    return {"job_id": identifier}


@router.get("/api/landscape-reports/{report_id}")
def get_landscape_report(report_id: str):
    return storage.read("landscape_reports", report_id)


@router.get("/api/landscape-reports/{report_id}/export")
def export_landscape_report(report_id: str):
    report = storage.read("landscape_reports", report_id)
    return Response(landscape_reports.export_csv(report), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="research-atlas-movement-{report_id[:8]}.csv"'})
