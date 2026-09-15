"""Queued, request-context-owned narrative endpoints for the landscape."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import landscape_reports, storage
from .job_context import submit_with_context
from .local_llm_stream import LocalStreamError

router = APIRouter()


_GENERATION_ERRORS = {
    "malformed_json": "LLMの最終回答をJSONとして読み取れませんでした。構造化出力の設定・対応モデルを確認してください。",
    "incomplete": "LLMの最終回答が完了前に途切れました。サーバーのログと接続状態を確認して再試行してください。",
    "token_limit": "LLMの出力がトークン上限に達し、最終回答が完成しませんでした。出力上限や思考モードの設定を確認してください。",
    "reasoning_incomplete": "LLMの思考部分（<think>）が閉じられず、完成した最終回答を確認できませんでした。出力上限や思考モードの設定を確認してください。",
    "missing_final_answer": "LLMから評論に使える最終回答が返りませんでした。思考部分だけで終了していないか、出力上限や思考モードの設定を確認してください。",
    "read_timeout": "LLMからの受信が長時間停止しました。サーバーの生成状況・処理速度・接続状態を確認してください。",
    "invalid_schema": "LLMの最終回答が評論に必要な形式を満たしていませんでした。構造化出力に対応するモデル・設定を確認してください。",
    "invalid_evidence_ids": "LLMの回答に、提供した根拠資料にない論文IDが含まれていました。この回答は評論として採用していません。",
    "missing_abstracts": "対象の代表論文に抄録がなく、内容に基づくLLM評論を生成できませんでした。抄録を含む論文データを追加してください。",
}
_GENERATION_ERROR_DEFAULT = "LLMへの接続、回答形式、または根拠論文の照合を完了できませんでした。接続設定とモデルのログを確認してください。"


def _generation_failure(exc: Exception) -> tuple[str, str]:
    # Never expose exception text: HTTP/client errors may contain connection secrets.
    kind = exc.kind if isinstance(exc, (LocalStreamError, landscape_reports.NarrativeValidationError)) else None
    if kind in _GENERATION_ERRORS:
        return kind, _GENERATION_ERRORS[kind]
    return "generation_failed", _GENERATION_ERROR_DEFAULT


class LandscapeReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    projection: Literal["auto", "pca", "umap", "tsne"] = "auto"
    projection_id: str | None = Field(default=None, max_length=100)
    interval: Literal["year", "quarter", "month"] = "year"
    scope: Literal["sample", "full"] = "sample"
    kind: Literal["movement", "centroid"] = "movement"
    movement_id: str | None = Field(default=None, min_length=1, max_length=200)
    topic_id: str | None = Field(default=None, min_length=1, max_length=100)
    period_id: str | None = Field(default=None, min_length=1, max_length=30)
    provider: Literal["none", "local", "openai"] = "none"
    model: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def target_valid(self):
        if self.kind == "movement" and not self.movement_id:
            raise ValueError("比較する重心移動を指定してください。")
        if self.kind == "centroid" and (not self.topic_id or not self.period_id):
            raise ValueError("代表論文を読むクラスターと期間を指定してください。")
        return self


def _progress(job_id, **values):
    from .main import JOBS, JOBS_LOCK
    with JOBS_LOCK:
        JOBS[job_id].update(**values)


def run_landscape_report(job_id: str, options: dict):
    report = None
    try:
        _progress(job_id, status="running", stage="共有座標と前後の根拠論文を照合")
        if options.get("kind") == "centroid":
            from .centroid_reports import prepare_report
            report = prepare_report(options["result_id"], options["projection"], options["interval"],
                                    options["topic_id"], options["period_id"], options.get("projection_id"), options.get("scope", "sample"))
        else:
            report = landscape_reports.prepare_report(options["result_id"], options["projection"], options["interval"],
                                                      options["movement_id"], options.get("projection_id"), options.get("scope", "sample"))
        report["requested_provider"] = options["provider"]
        report["generation_status"] = "not_requested" if options["provider"] == "none" else "generating"
        storage.save("landscape_reports", report)
        _progress(job_id, landscape_report_id=report["id"], projection_id=report["projection_id"])
        if options["provider"] != "none":
            _progress(job_id, stage="LLMで話題の変化を解釈")
            def on_generation(event):
                seconds, count = max(0, int(event["elapsed_seconds"])), max(0, int(event["received_chars"]))
                _progress(job_id, stage=f"話題の変化を解釈：{seconds // 60}分{seconds % 60:02d}秒・{count:,}文字受信（検証前）")
            try:
                report["narrative"] = landscape_reports.generate(report, options["provider"], options.get("model"), progress=on_generation)
                report["generation_status"] = "generated"
            except Exception as exc:
                report["generation_status"] = "failed"
                report["generation_error_kind"], report["llm_error"] = _generation_failure(exc)
                report["narrative"]["validation"] = {"status": "warning", "warnings": [{
                    "code": "llm_failed", "message": report["llm_error"]}]}
            storage.save("landscape_reports", report)
        warned = report["narrative"].get("validation", {}).get("status") == "warning"
        stage = "⚠ LLM評論の生成に失敗しました。計算結果は保存されています。" if report["generation_status"] == "failed" else "⚠ 照合の確認事項付きで評論を保存" if warned else "計算結果の説明を保存" if report["generation_status"] == "not_requested" else "評論を保存"
        _progress(job_id, status="completed", stage=stage,
                  landscape_report_id=report["id"], projection_id=report["projection_id"],
                  generation_status=report["generation_status"], validation_status="warning" if warned else "passed")
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
