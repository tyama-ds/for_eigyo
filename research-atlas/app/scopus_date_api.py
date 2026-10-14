"""Bounded browser queue/import API. Browser credentials never reach this API."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from . import scopus_date_enrichment as dates

router = APIRouter()
MAX_BODY = 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class StartRequest(StrictModel):
    dataset_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    target: Literal["missing_month", "missing_day", "all"] = "missing_month"


class QueueRequest(StrictModel):
    batch_size: int = Field(default=100, strict=True, ge=1, le=500)
    retry_errors: bool = Field(default=False, strict=True)


class ApplyRequest(StrictModel):
    allow_year_change: bool = Field(default=False, strict=True)
    overwrite_existing: bool = Field(default=False, strict=True)
    snapshot_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class Observation(StrictModel):
    eid: str = Field(pattern=r"^2-s2\.0-[1-9]\d{0,19}$")
    url: str = Field(max_length=2048)
    status: Literal["found", "requires_access", "not_found", "selector_changed", "identity_mismatch", "timeout", "error"]
    date_text: str = Field(default="", max_length=512)
    matched_by: Literal["", "testid", "label"] = ""
    observed_at: str = Field(min_length=1, max_length=50)


class ImportRequest(StrictModel):
    schema_version: Literal["research-atlas.scopus-date-results.v1"] = Field(alias="schema")
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    batch_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    observations: list[Observation] = Field(min_length=1, max_length=500)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except dates.SnapshotConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except KeyError:
        raise HTTPException(404, "データが見つかりません。") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


async def _body(request, model):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "結果ファイルは1MB以下・500件以下に分割してください。")
        body.extend(chunk)
    try:
        return model.model_validate_json(body).model_dump(by_alias=True)
    except ValidationError:
        raise HTTPException(422, "送信形式を確認してください。取得キューまたは結果JSONの項目が一致しません。") from None


@router.get("/api/datasets/{dataset_id}/scopus-dates")
def summary(dataset_id: str):
    return _call(dates.dataset_summary, dataset_id)


@router.post("/api/scopus-date-jobs")
async def create(request: Request):
    body = await _body(request, StartRequest)
    return await run_in_threadpool(_call, dates.create_job, **body)


@router.get("/api/scopus-date-jobs/{job_id}")
def status(job_id: str):
    return _call(dates.get_job, job_id)


@router.post("/api/scopus-date-jobs/{job_id}/queue")
async def queue(job_id: str, request: Request):
    body = await _body(request, QueueRequest)
    return await run_in_threadpool(_call, dates.queue, job_id, **body)


@router.post("/api/scopus-date-jobs/{job_id}/import")
async def import_results(job_id: str, request: Request):
    body = await _body(request, ImportRequest)
    return await run_in_threadpool(_call, dates.import_results, job_id, body)


@router.get("/api/scopus-date-jobs/{job_id}/records")
def records(job_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
            allow_year_change: bool = False, overwrite_existing: bool = False,
            snapshot_id: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$")):
    return _call(dates.records, job_id, offset=offset, limit=limit, allow_year_change=allow_year_change,
                 overwrite_existing=overwrite_existing, snapshot_id=snapshot_id)


@router.post("/api/scopus-date-jobs/{job_id}/apply")
async def apply(job_id: str, request: Request):
    body = await _body(request, ApplyRequest)
    return await run_in_threadpool(_call, dates.apply_job, job_id, **body)


@router.get("/api/scopus-date-jobs/{job_id}/export")
def export(job_id: str, format: Literal["csv", "json"] = "csv", allow_year_change: bool = False,
           overwrite_existing: bool = False, snapshot_id: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$")):
    content = _call(dates.export_records, job_id, format=format, allow_year_change=allow_year_change,
                    overwrite_existing=overwrite_existing, snapshot_id=snapshot_id)
    return StreamingResponse(content, media_type="text/csv; charset=utf-8" if format == "csv" else "application/json",
        headers={"Content-Disposition": f'attachment; filename="research-atlas-scopus-dates-{job_id[:8]}.{format}"'})
