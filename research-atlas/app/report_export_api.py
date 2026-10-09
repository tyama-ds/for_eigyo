"""Portable, graph-inclusive exports of saved reports; no new LLM generation."""
from __future__ import annotations

import base64
import binascii
from io import BytesIO
import threading
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from . import corpus_reporting, large_storage, storage
from .report_bundle import build_bundle

router = APIRouter()
Format = Literal["pdf", "docx", "xlsx", "pptx"]
Kind = Literal["corpus", "landscape", "annual", "field", "foresight", "result"]
MIME = {"pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
STORES = {"landscape": "landscape_reports", "annual": "annual_landscape_reports",
          "field": "field_reports", "foresight": "assessments", "result": "results"}
_RENDERS = threading.BoundedSemaphore(2)
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_EXPORT_REQUEST = 12 * 1024 * 1024


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    result_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(min_length=1, max_length=200)
    caption: str = Field(default="", max_length=2000)
    data_url: str = Field(max_length=2_796_230)


class ExportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    format: Format = "pdf"
    include_figures: bool = Field(default=True, strict=True)
    snapshots: list[Snapshot] = Field(default_factory=list, max_length=4)
    candidate_id: str | None = Field(default=None, max_length=200)


def _snapshots(rows, result_id):
    """Only bounded raster bytes, never SVG, filesystem paths or remote URLs."""
    from PIL import Image, UnidentifiedImageError
    output = []
    for row in rows:
        if row.result_id != result_id:
            raise ValueError("追加する図とレポートの分析結果が一致しません。対象の図を追加し直してください。")
        if not row.data_url.startswith("data:image/png;base64,"):
            raise ValueError("追加する図はPNG画像で指定してください。")
        try:
            data = base64.b64decode(row.data_url.split(",", 1)[1], validate=True)
            if not data or len(data) > MAX_SNAPSHOT_BYTES:
                raise ValueError()
            with Image.open(BytesIO(data)) as picture:
                if picture.format != "PNG" or min(picture.size) < 1 or max(picture.size) > 4096 or picture.width * picture.height > 12_000_000:
                    raise ValueError()
                picture.verify()
            with Image.open(BytesIO(data)) as picture:
                clean = BytesIO()
                rgba = picture.convert("RGBA")
                background = Image.new("RGBA", picture.size, "white")
                Image.alpha_composite(background, rgba).convert("RGB").save(clean, format="PNG")
                width, height = picture.size
        except (ValueError, binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ValueError("追加画像を読み取れません。1枚2MB以下・最大4096pxのPNG図を追加し直してください。") from None
        output.append({"title": row.title, "caption": "画面から追加した静止図。" + row.caption,
                       "png": clean.getvalue(), "width": width, "height": height})
    return output


def _paper_records(kind, report, result, candidate_id=None):
    if kind == "result":
        yield from large_storage.iter_papers(result, storage.data_root())
    elif kind == "field":
        ids = {report.get("focus", {}).get("id"), (report.get("neighbor") or {}).get("id")} - {None}
        yield from (paper for paper in large_storage.iter_papers(result, storage.data_root()) if paper.get("topic_id") in ids)
    elif kind == "foresight":
        ids = None
        if candidate_id:
            candidate = next(row for row in report.get("candidates", []) if row["id"] == candidate_id)
            ids = set(candidate.get("paper_ids", []))
            ids.update(row.get("paper_id") for row in candidate.get("recommendations", []))
            ids.update(row.get("paper_id") for row in candidate.get("content_facts", candidate.get("evidence", [])))
        yield from (paper for paper in large_storage.iter_papers(report, storage.data_root()) if ids is None or paper.get("id") in ids)
    else:
        seen = set()
        children = [report] if kind == "landscape" else [row["report"] for row in [*report.get("years", []), *report.get("transitions", [])] if row.get("report")]
        for child in children:
            for paper in child.get("evidence_papers", []):
                if paper.get("id") not in seen:
                    seen.add(paper.get("id"))
                    yield paper


def _render(kind, report, result, options, records=None):
    from .report_documents import render_report
    from .report_figures import build_report_figures
    bundle = build_bundle(kind, report, result, candidate_id=options.candidate_id)
    # Validate ownership even if a client disabled figures after selecting them.
    snapshots = _snapshots(options.snapshots, result["id"])
    if options.include_figures:
        figure_report = report
        if options.candidate_id and kind == "foresight":
            figure_report = {**report, "candidates": [row for row in report.get("candidates", []) if row["id"] == options.candidate_id]}
        notes = []
        bundle["figures"] = [*build_report_figures(result, figure_report, kind=kind, options={"notes": notes}), *snapshots]
        bundle["warnings"].extend(notes)
        if not bundle["figures"]:
            bundle["warnings"].append("この保存結果には描画できる座標・集計がありません。画面で作成した図を追加して出力できます。")
    else:
        bundle["scope_note"] += "この出力では図の収録をオフにしています。"
    if options.format == "xlsx" and records is None:
        records = _paper_records(kind, report, result, options.candidate_id)
    return render_report(bundle, options.format, records=records if options.format == "xlsx" else None)


def document_response(kind, report_id, options):
    if not _RENDERS.acquire(blocking=False):
        raise HTTPException(409, "レポートを出力中です。現在の出力が終了してからもう一度実行してください。")
    try:
        if options.candidate_id and kind != "foresight":
            raise ValueError("候補の選択は有望領域レポートで指定してください。")
        if kind == "corpus":
            def render(report, records):
                result = storage.read("results", report["result_id"], include_papers=False)
                return _render(kind, report, result, options, records)
            content = corpus_reporting.render_export(report_id, render)
        else:
            # Field reports keep their complete comparison rows in an auxiliary
            # file (no corpus paper list). Load it for the XLSX audit as well.
            report = storage.read(STORES[kind], report_id, include_papers=kind == "field")
            result = report if kind == "result" else storage.read("results", report["result_id"], include_papers=False)
            content = _render(kind, report, result, options)
    finally:
        _RENDERS.release()
    return Response(content, media_type=MIME[options.format], headers={
        "Content-Disposition": f'attachment; filename="research-atlas-{kind}-{report_id[:8]}.{options.format}"'})


@router.post("/api/report-exports/{kind}/{report_id}")
async def export_document(request: Request, kind: Kind, report_id: str = Path(pattern=r"^[a-f0-9]{32}$")):
    # Bound this raster request independently of unlimited bibliographic uploads.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_EXPORT_REQUEST:
            raise HTTPException(413, "追加画像の合計サイズが大きすぎます。図の枚数を減らしてください。")
        body.extend(chunk)
    try:
        options = ExportOptions.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "出力形式・図の設定を確認してください。PNGは最大4枚です。") from None
    return await run_in_threadpool(document_response, kind, report_id, options)


@router.get("/api/report-exports/{kind}/{report_id}")
def download_document(kind: Kind, report_id: str = Path(pattern=r"^[a-f0-9]{32}$"),
                      format: Format = "pdf", include_figures: bool = True, candidate_id: str | None = None):
    return document_response(kind, report_id, ExportOptions(format=format, include_figures=include_figures, candidate_id=candidate_id))
