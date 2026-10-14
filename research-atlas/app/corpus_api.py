"""Whole-corpus report API: bounded runs, checkpoints, summaries and exports."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import corpus_reporting as reports

router = APIRouter()


class RunOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    batch_size: int = Field(default=100, strict=True, ge=1, le=20000)
    run_all: bool = Field(default=False, strict=True)


class CreateRequest(RunOptions):
    result_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    provider: Literal["local", "openai"] = "local"
    model: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"^[\w][\w.:/\-]*$")


class ResumeRequest(RunOptions):
    retry_failed: bool = Field(default=False, strict=True)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except reports.ReportBusy as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/api/corpus-reports")
def create(body: CreateRequest):
    return _call(reports.create_report, **body.model_dump())


@router.get("/api/corpus-reports")
def list_all(result_id: str = Query(pattern=r"^[a-f0-9]{32}$")):
    return {"reports": reports.list_reports(result_id)}


@router.get("/api/corpus-reports/{report_id}")
def detail(report_id: str):
    return reports.get_report(report_id)


@router.post("/api/corpus-reports/{report_id}/resume")
def resume(report_id: str, body: ResumeRequest):
    return _call(reports.resume_report, report_id, **body.model_dump())


@router.post("/api/corpus-reports/{report_id}/pause")
def pause(report_id: str):
    return reports.pause_report(report_id)


@router.post("/api/corpus-reports/{report_id}/synthesize")
def synthesize(report_id: str):
    return _call(reports.synthesize_report, report_id)


@router.get("/api/corpus-reports/{report_id}/papers")
def papers(report_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
           status: Literal["pending", "completed", "missing", "failed"] | None = None):
    return reports.paper_page(report_id, offset=offset, limit=limit, status=status)


@router.get("/api/corpus-reports/{report_id}/export")
def export(report_id: str, format: Literal["pdf", "csv", "json"] = "pdf"):
    report = reports.get_report(report_id)
    headers = {"Content-Disposition": f'attachment; filename="research-atlas-corpus-{report_id[:8]}.{format}"'}
    if format == "pdf":
        from .corpus_exports import report_pdf
        return Response(report_pdf(report), media_type="application/pdf", headers=headers)
    return StreamingResponse(reports.iter_export(report_id, format), media_type="text/csv" if format == "csv" else "application/json", headers=headers)
