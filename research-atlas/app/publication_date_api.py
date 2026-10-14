"""Public endpoints for separately stored publication-date observations."""
from typing import Literal
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import publication_date_enrichment as dates

router = APIRouter()
Policy = Literal["same_year", "online_first", "print_first"]


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    batch_size: int = Field(default=500, strict=True, ge=1, le=20000)
    contact_email: str = Field(default="", max_length=254)

    @field_validator("contact_email")
    @classmethod
    def valid_contact(cls, value):
        if value and (not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value)
                      or ".." in value or any(ord(char) < 33 for char in value)):
            raise ValueError("連絡先メールアドレスの形式を確認してください。")
        return value


class StartRequest(BatchRequest):
    dataset_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    target: Literal["missing_month", "missing_day", "all"] = "missing_month"


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Policy = "same_year"
    overwrite_existing: bool = Field(default=False, strict=True)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except dates.JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/api/datasets/{dataset_id}/publication-dates")
def summary(dataset_id: str):
    return dates.dataset_summary(dataset_id)


@router.post("/api/publication-date-jobs")
def start(body: StartRequest):
    return _call(dates.create_job, **body.model_dump())


@router.get("/api/publication-date-jobs/{job_id}")
def status(job_id: str):
    return dates.get_job(job_id)


@router.post("/api/publication-date-jobs/{job_id}/resume")
def resume(job_id: str, body: BatchRequest):
    return _call(dates.resume_job, job_id, **body.model_dump())


@router.post("/api/publication-date-jobs/{job_id}/pause")
def pause(job_id: str):
    return dates.pause_job(job_id)


@router.get("/api/publication-date-jobs/{job_id}/records")
def records(job_id: str, limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0),
            policy: Policy = "same_year", overwrite_existing: bool = False):
    return dates.records(job_id, limit=limit, offset=offset, policy=policy, overwrite_existing=overwrite_existing)


@router.get("/api/publication-date-jobs/{job_id}/export")
def export(job_id: str, policy: Policy = "same_year", overwrite_existing: bool = False):
    return StreamingResponse(dates.export_csv(job_id, policy=policy, overwrite_existing=overwrite_existing), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="research-atlas-publication-dates-{job_id[:8]}.csv"'})


@router.post("/api/publication-date-jobs/{job_id}/apply")
def apply(job_id: str, body: ApplyRequest):
    return _call(dates.apply_job, job_id, **body.model_dump())
