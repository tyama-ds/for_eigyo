"""Research Atlas: local-only bibliometric analysis service."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from app import connection_settings, insights, reports, sources, storage
from app.job_context import submit_with_context
from app.analytics import analyze
from app.ingest import attach_citation_history, demo_papers, parse_scopus_csv
from app.merge import merge_papers
from app.embedding_models import SBERT_MODELS
from app import field_llm, field_exports, author_exports
from app import large_storage
from app.limits import MAX_IMPORT_ROWS, MAX_DATASET_PAPERS, MAX_ANALYSIS_YEARS, PAPER_PAGE_LIMIT, public_limits
from app.cluster_models import cluster_model_catalog

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
app = FastAPI(title="Research Atlas", version="2.0.1", description="Multi-source bibliometrics & evidence-grounded technology foresight")
MAX_NON_UPLOAD_REQUEST_BYTES = 256 * 1024 * 1024
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlas-analysis")
SOURCE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlas-discovery")
REPORT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlas-report")
AUTHOR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlas-authors")
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.RLock()
logger = logging.getLogger("atlas")


@app.middleware("http")
async def local_security(request: Request, call_next):
    host = request.url.hostname
    if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
        return JSONResponse({"detail": "このツールはローカルホスト専用です。"}, status_code=400)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        if origin and origin != expected:
            return JSONResponse({"detail": "異なるサイトからのリクエストは受け付けません。"}, status_code=403)
        size = request.headers.get("content-length")
        if size:
            try:
                is_csv_upload = request.method == "POST" and (request.url.path == "/api/import"
                    or re.fullmatch(r"/api/datasets/[a-f0-9]{32}/citations", request.url.path))
                if not is_csv_upload and int(size) > MAX_NON_UPLOAD_REQUEST_BYTES:
                    return JSONResponse({"detail": "リクエストが大きすぎます。"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Content-Lengthが不正です。"}, status_code=400)
    try:
        settings = connection_settings.parse_header(request.headers.get("x-atlas-connection")
            if request.url.path.startswith("/api/") else None)
    except ValueError:
        return JSONResponse({"detail": "接続設定の形式が不正です。ブラウザの接続設定を確認してください。"}, status_code=422,
                            headers={"Cache-Control": "no-store"})
    with connection_settings.settings_context(settings):
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
    return response


@app.exception_handler(ValueError)
async def invalid_value(_request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request, exc):
    names = {"start_year": "開始年", "end_year": "終了年", "start_month": "開始月", "end_month": "終了月", "anchor_month": "月次の基準月", "window_months": "比較月数", "n_topics": "トピック数", "horizon": "予測期間", "embedding": "分析方式", "sbert_model": "SBERTモデル", "topic_model": "トピック分類法", "min_topic_size": "最小トピック件数", "dataset_id": "データセット", "result_id": "分析結果", "file": "CSVファイル"}
    messages = []
    for error in exc.errors():
        location = error.get("loc", ())
        field = str(location[-1]) if location else "入力"
        if error.get("type") == "value_error":
            messages.append(error.get("msg", "入力が不正です。").removeprefix("Value error, "))
        else:
            messages.append(f"{names.get(field, field)}の値または形式を確認してください。")
    return JSONResponse({"detail": " ".join(dict.fromkeys(messages))}, status_code=422)


@app.exception_handler(KeyError)
async def missing_value(_request, _exc):
    return JSONResponse({"detail": "データが見つかりません。画面を再読込してください。"}, status_code=404)


@app.get("/api/status")
def status():
    transformer = importlib.util.find_spec("sentence_transformers") is not None
    bertopic = transformer and all(importlib.util.find_spec(name) is not None for name in ("bertopic", "umap", "hdbscan"))
    return {"llm_configured": insights.configured(), "limits": public_limits(),
        "transformer_available": transformer,
        "sbert_models": [{"id": key, "model_id": identifier,
            "label": "多言語 MiniLM" if key == "multilingual_minilm" else "SBERT MPNet（英語）",
            "language": "multilingual" if key == "multilingual_minilm" else "en"}
            for key, identifier in SBERT_MODELS.items()],
        "topic_models": [dict(item, available=True) for item in cluster_model_catalog()] +
            [{"id": key, "label": label, "available": bertopic if key == "bertopic" else True}
             for key, label in (("nmf", "NMF"), ("lda", "LDA"), ("bertopic", "BERTopic"))],
        "datasets": storage.list_datasets(), "default_start_year": date.today().year - 5,
        "default_end_year": date.today().year - 1}


@app.post("/api/demo")
def create_demo():
    with storage.LOCK:
        for item in storage.list_datasets():
            if item["is_demo"] and storage.read("datasets", item["id"]).get("report", {}).get("built_in_demo"):
                return {"dataset": item}
        dataset = storage.create_dataset(demo_papers(), "Frontier research · 合成デモ", True,
            {"built_in_demo": True, "warnings": ["すべての論文・著者・引用回数は操作確認用の合成データです。"]})
    return {"dataset": storage.dataset_summary(dataset)}


@app.get("/api/connections/status")
def connections_status():
    return connection_settings.connection_status()


class ConnectionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["openai", "local", "proxy"]


@app.post("/api/connections/test")
def test_connection(body: ConnectionTestRequest):
    return connection_settings.test_connection(body.target)


def check_upload_filename(file: UploadFile):
    if not (file.filename or "").lower().endswith((".csv", ".tsv", ".txt")):
        raise HTTPException(422, "論文の書誌情報を含むCSVファイルを選択してください。")


@app.post("/api/import")
async def import_csv(file: UploadFile = File(...), provider: Literal["scopus_csv", "csv"] = Form("scopus_csv")):
    try:
        check_upload_filename(file)
        await file.seek(0)
        return await run_in_threadpool(_import_csv_dataset, file.file, provider, file.filename)
    finally:
        await file.close()


def _import_csv_dataset(content: bytes | BinaryIO, provider: str, filename: str | None):
    papers, report = parse_scopus_csv(content)
    retrieved_at = storage.now()
    for paper in papers:
        paper["providers"] = [provider]
        paper["citation_source"] = provider
        paper["retrieved_at"] = retrieved_at
        paper["citation_snapshots"] = [{"provider": provider, "count": paper["citations"], "retrieved_at": retrieved_at}] if paper.get("citations") is not None else []
        if paper.get("doi"):
            paper["external_url"] = "https://doi.org/" + paper["doi"]
    report["providers"] = [provider]
    if len(papers) > MAX_IMPORT_ROWS:
        raise HTTPException(422, f"1ファイルは{MAX_IMPORT_ROWS:,}論文以内で指定してください。")
    # Some clients supply the full Windows path as a filename.
    name = (filename or "Scopus CSV").replace("\\", "/").rsplit("/", 1)[-1]
    dataset = storage.create_dataset(papers, name, is_demo=bool(report.get("is_demo")), report=report)
    return {"dataset": storage.dataset_summary(dataset), "report": report}


@app.get("/api/sources")
def source_catalog():
    return {"providers": sources.catalog()}


class DiscoverRequest(BaseModel):
    provider: Literal["europepmc", "arxiv", "crossref"]
    query: str = Field(min_length=2, max_length=500)
    start_year: int = Field(default_factory=lambda: date.today().year - 5, ge=1900, le=2100)
    end_year: int = Field(default_factory=lambda: date.today().year - 1, ge=1900, le=2100)
    limit: int = Field(default=250, ge=20, le=1000)
    start_month: str | None = Field(default=None, pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$")
    end_month: str | None = Field(default=None, pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$")

    @model_validator(mode="after")
    def validate_search(self):
        self.query = self.query.strip()
        if len(self.query) < 2:
            raise ValueError("検索語を2文字以上で入力してください。")
        if self.start_year > self.end_year or self.end_year - self.start_year > 19:
            raise ValueError("検索期間は開始年から20年以内で指定してください。")
        if self.end_year > date.today().year:
            raise ValueError("未来の年を検索期間には指定できません。")
        if bool(self.start_month) != bool(self.end_month):
            raise ValueError("月単位の取得には開始月と終了月の両方を指定してください。")
        if self.start_month:
            start, end = date.fromisoformat(self.start_month + "-01"), date.fromisoformat(self.end_month + "-01")
            if start > end or (end.year - start.year) * 12 + end.month - start.month >= 24:
                raise ValueError("月単位の取得期間は、開始月から24か月以内で指定してください。")
            if start.year != self.start_year or end.year != self.end_year:
                raise ValueError("検索の開始年・終了年を、指定した月の年に合わせてください。")
            if self.end_month > date.today().strftime("%Y-%m"):
                raise ValueError("未来の月は検索できません。")
        return self


def new_job(stage: str) -> str:
    with JOBS_LOCK:
        if sum(j["status"] in {"queued", "running"} for j in JOBS.values()) >= 3:
            raise HTTPException(429, "処理が混み合っています。実行中の処理が完了してからお試しください。")
        if len(JOBS) > 100:
            for key in list(JOBS):
                if JOBS[key]["status"] in {"completed", "failed"}:
                    del JOBS[key]
                    if len(JOBS) <= 80:
                        break
        identifier = storage.new_id()
        JOBS[identifier] = {"status": "queued", "stage": stage}
        return identifier


def run_discovery(job_id: str, options: dict):
    def progress(stage):
        with JOBS_LOCK:
            JOBS[job_id].update(status="running", stage=str(stage))
    try:
        progress("公開論文サービスを検索しています")
        # Reuse an identical saved search for 24 hours, including after restart.
        for item in storage.list_datasets():
            saved = storage.read("datasets", item["id"], include_papers=False)
            report = saved.get("report", {})
            if report.get("discovery_request") != options or report.get("date_pipeline_version") != 2 or report.get("metadata_pipeline_version") != 3:
                continue
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(saved["created_at"])).total_seconds()
            if 0 <= age < 86400:
                with JOBS_LOCK:
                    JOBS[job_id].update(status="completed", stage="24時間以内の同じ検索結果を読み込みました", dataset=item, report=report)
                return
        papers, report = sources.discover(**options, progress=progress)
        if not papers:
            raise ValueError("該当する論文を取得できませんでした。検索語や期間を変更してください。")
        papers, merge_report = merge_papers([], papers)
        report["warnings"] = list(dict.fromkeys(report.get("warnings", []) + merge_report.get("warnings", [])))
        report["duplicates_removed"] = report.get("duplicates_removed", 0) + merge_report.get("duplicates_removed", 0)
        report["imported_count"] = len(papers)
        report["providers"] = [options["provider"]]
        report["discovery_request"] = options
        report["date_pipeline_version"] = 2
        report["metadata_pipeline_version"] = 3
        report["abstract_coverage"] = round(100 * sum(bool(p.get("abstract")) for p in papers) / len(papers), 1)
        period = f"{options['start_month']}–{options['end_month']}" if options.get("start_month") else f"{options['start_year']}–{options['end_year']}"
        name = f"{options['provider']} · {options['query']} · {period}"
        dataset = storage.create_dataset(papers, name, False, report)
        with JOBS_LOCK:
            JOBS[job_id].update(status="completed", stage="公開論文の取り込み完了", dataset=storage.dataset_summary(dataset), report=report)
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "公開論文の取得に失敗しました。時間をおいて再度お試しください。"
        logger.warning("Discovery failed (%s)", type(exc).__name__)
        with JOBS_LOCK:
            JOBS[job_id].update(status="failed", stage="公開論文を取得できませんでした", error=message)


@app.post("/api/discover")
def start_discovery(body: DiscoverRequest):
    identifier = new_job("公開論文検索の準備中")
    submit_with_context(SOURCE_EXECUTOR, run_discovery, identifier, body.model_dump())
    return {"job_id": identifier}


def source_reports(dataset: dict) -> list[dict]:
    report = dataset.get("report", {})
    return report.get("source_reports", []) + ([report] if report.get("discovery_request") else [])


def observed_months(dataset: dict) -> list[str] | None:
    """Collection windows, not publication presence, determine comparable periods.

    Repeated chunks of the same provider/query are united. Distinct search
    populations must each cover both comparison windows; use their intersection.
    A CSV has no declared collection window and is left explicitly unspecified.
    """
    groups: dict[tuple[str, str], set[str]] = {}
    for report in source_reports(dataset):
        key = (report.get("provider", ""), report.get("query", ""))
        months = groups.setdefault(key, set())
        if report.get("month_coverage") or report.get("start_month"):
            months.update(row["month"] for row in report.get("month_coverage", [])
                if row.get("allocated") != 0 or row.get("total") == 0)
        else:
            months.update(f"{int(row['year']):04d}-{month:02d}" for row in report.get("year_coverage", [])
                if row.get("allocated") != 0 or row.get("total") == 0 for month in range(1, 13))
    return sorted(set.intersection(*groups.values())) if groups else None


class MergeRequest(BaseModel):
    base_dataset_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    additional_dataset_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@app.post("/api/datasets/merge")
def merge_datasets(body: MergeRequest):
    if body.base_dataset_id == body.additional_dataset_id:
        raise ValueError("統合する異なるデータセットを2つ選択してください。")
    base = storage.read("datasets", body.base_dataset_id)
    additional = storage.read("datasets", body.additional_dataset_id)
    if base["is_demo"] != additional["is_demo"]:
        raise ValueError("合成デモと実際の論文は統合できません。実データ同士を選択してください。")
    papers, report = merge_papers(base["papers"], additional["papers"])
    if MAX_DATASET_PAPERS is not None and len(papers) > MAX_DATASET_PAPERS:
        raise ValueError(f"統合後の論文が{MAX_DATASET_PAPERS:,}件を超えます。検索範囲を絞ってください。")
    report["warnings"] = list(dict.fromkeys(base.get("report", {}).get("warnings", []) + additional.get("report", {}).get("warnings", []) + report.get("warnings", [])))
    report["source_reports"] = source_reports(base) + source_reports(additional)
    report["parent_dataset_ids"] = [base["id"], additional["id"]]
    report["providers"] = sorted({v for p in papers for v in p.get("providers", ["csv"])})
    report["truncated"] = any(r.get("truncated") for r in report["source_reports"])
    report["abstract_coverage"] = round(100 * sum(bool(p.get("abstract")) for p in papers) / len(papers), 1) if papers else 0
    merged = storage.create_dataset(papers, base["name"][:85] + " ＋ " + additional["name"][:85], base["is_demo"], report)
    return {"dataset": storage.dataset_summary(merged), "report": report}


@app.post("/api/datasets/{dataset_id}/citations")
async def import_citations(dataset_id: str, file: UploadFile = File(...)):
    try:
        check_upload_filename(file)
        await file.seek(0)
        return await run_in_threadpool(_import_citations_dataset, dataset_id, file.file)
    finally:
        await file.close()


def _import_citations_dataset(dataset_id: str, content: bytes | BinaryIO):
    dataset = storage.read("datasets", dataset_id)
    papers, report = attach_citation_history(dataset["papers"], content)
    # Create a new revision so existing analysis results remain reproducible.
    previous = dataset.get("report", {}).get("warnings", [])
    report["warnings"] = list(dict.fromkeys(previous + report.get("warnings", [])))
    report["source_reports"] = source_reports(dataset)
    report["providers"] = sorted({v for p in papers for v in p.get("providers", ["csv"])})
    updated = storage.create_dataset(papers, dataset["name"] + " · 引用履歴追加", dataset["is_demo"], report)
    return {"dataset": storage.dataset_summary(updated), "report": report}


class AnalyzeRequest(BaseModel):
    dataset_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    start_year: int = Field(default_factory=lambda: date.today().year - 5, ge=1900, le=2100)
    end_year: int = Field(default_factory=lambda: date.today().year - 1, ge=1900, le=2100)
    n_topics: int = Field(default=8, ge=2, le=20)
    embedding: Literal["tfidf", "transformer", "sbert"] = "tfidf"
    sbert_model: Literal["multilingual_minilm", "mpnet"] | None = None
    topic_model: Literal["kmeans", "kmeans_pp", "minibatch_kmeans", "xmeans", "knn_graph", "dbscan", "gmm", "birch", "agglomerative", "nmf", "lda", "bertopic"] = "kmeans"
    cluster_options: dict[str, float | int] = Field(default_factory=dict)
    map_projection: Literal["auto", "tsne", "pca", "umap"] = "auto"
    min_topic_size: int = Field(default=5, ge=2, le=100)
    horizon: int = Field(default=3, ge=1, le=3)
    window_months: Literal[1, 3, 6] = 3
    anchor_month: str | None = Field(default=None, pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$")

    @model_validator(mode="after")
    def years_valid(self):
        if self.topic_model in {"nmf", "lda"} and self.embedding != "tfidf":
            raise ValueError("NMF・LDAは語の出現情報を使います。文章表現をTF-IDFに設定してください（LDAには語のカウントを使用します）。")
        if self.topic_model == "bertopic" and self.embedding == "tfidf":
            raise ValueError("BERTopicにはSentence-BERTの意味ベクトルが必要です。文章表現をSBERTに設定してください。")
        if self.start_year > self.end_year:
            raise ValueError("開始年は終了年以下にしてください。")
        if self.end_year > date.today().year:
            raise ValueError("分析終了年に未来の年は指定できません。")
        if self.end_year - self.start_year >= MAX_ANALYSIS_YEARS:
            raise ValueError(f"分析期間は{MAX_ANALYSIS_YEARS}年以内で指定してください。")
        if self.anchor_month:
            anchor = date.fromisoformat(self.anchor_month + "-01")
            latest_complete = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            if not self.start_year <= anchor.year <= self.end_year:
                raise ValueError("月次比較の基準月は、分析対象年の範囲内で指定してください。")
            if self.anchor_month > latest_complete:
                raise ValueError("月次比較は完了した月までが対象です。当月・未来月は指定できません。")
        return self


def run_analysis(job_id: str, dataset: dict | str, options: dict):
    def progress(stage):
        with JOBS_LOCK:
            JOBS[job_id].update(status="running", stage=str(stage))
    try:
        progress("分析対象のデータを読み込み中")
        if isinstance(dataset, str):
            dataset = storage.read("datasets", dataset)
        collection_reports = source_reports(dataset)
        corpus_years = {int(year) for paper in dataset["papers"] if isinstance(paper, dict)
                        and re.fullmatch(r"\d{4}", year := str(paper.get("year", "")).strip())}
        single_year_corpus = not collection_reports and len(corpus_years) == 1
        analysis_options = {**options, "observed_months": observed_months(dataset),
                            "single_year_corpus": single_year_corpus}
        result = analyze(dataset["papers"], analysis_options, progress_callback=progress)
        result.update(id=storage.new_id(), dataset_id=dataset["id"], dataset_name=dataset["name"], created_at=storage.now())
        result["meta"]["is_demo"] = dataset["is_demo"]
        result["meta"]["source_reports"] = collection_reports
        result["meta"]["providers"] = sorted({v for p in result["papers"] for v in p.get("providers", ["csv"])})
        result["meta"]["sampled"] = any(r.get("truncated") for r in result["meta"]["source_reports"])
        result["meta"]["citation_sources"] = sorted({p.get("citation_source", "csv") for p in result["papers"] if p.get("citations") is not None or p.get("citation_history")})
        if dataset["is_demo"]:
            result["meta"]["providers"] = ["synthetic"]
        result["meta"]["warnings"] = list(dict.fromkeys(dataset.get("report", {}).get("warnings", []) + result["meta"].get("warnings", [])))
        if result["meta"]["sampled"]:
            result["meta"]["warnings"].insert(0, "取得標本内の分析です。各年・各月の関連度上位を抽出しているため、件数の増減・予測は検索全体の成長を示しません。")
        frontier = result.get("frontiers", {})
        frontier["scope"] = {"sampled": result["meta"]["sampled"], "providers": result["meta"]["providers"], "corpus_papers": result["meta"]["paper_count"]}
        if result["meta"]["sampled"]:
            warning = "年・月ごとの上限付き検索標本です。月次増加や少ない組み合わせは取得集合内の観測で、分野全体の増加・未研究の証拠にはなりません。"
            for key in ("monthly", "sparse"):
                if key in frontier:
                    frontier[key].setdefault("warnings", []).insert(0, warning)
        if len(result["meta"]["providers"]) > 1 and "monthly" in frontier:
            frontier["monthly"].setdefault("warnings", []).append("複数の取得元が混在します。初回投稿日・出版日など日付の定義や収録の遅れが異なります。")
        if "monthly" in frontier and result["meta"]["source_reports"] and set(result["meta"]["providers"]) & {"csv", "scopus_csv"}:
            frontier["monthly"]["has_unknown_collection_scope"] = True
            frontier["monthly"]["observation_scope_known"] = False
            frontier["monthly"].setdefault("warnings", []).append("CSV由来の論文も含まれています。公開APIの取得範囲は比較の制約として維持していますが、CSV部分の収集期間・網羅性は確認できません。")
        if len(result["meta"]["citation_sources"]) > 1:
            result["meta"]["warnings"].insert(0, "複数の引用計数元が混在しています。各論文で採用した1つの取得元の値を集計していますが、データベース間の網羅性が異なるため引用数・スコアの単純比較には向きません。")
        if dataset["is_demo"]:
            result["meta"]["warnings"].insert(0, "合成・テストデータを含む操作確認用の分析です。実際の研究動向の判断には使用できません。")
        result["options"] = {**options, "single_year_corpus": single_year_corpus}
        progress("全件の分析結果を保存しています")
        storage.save("results", result)
        with JOBS_LOCK:
            JOBS[job_id].update(status="completed", stage="分析完了", result_id=result["id"])
    except Exception as exc:
        # ValueErrors are deliberate analytical/input messages. Unexpected exception
        # details stay out of the UI to avoid leaking document text or credentials.
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "分析中にエラーが発生しました。データと依存ライブラリを確認してください。"
        logger.warning("Analysis failed (%s)", type(exc).__name__)
        with JOBS_LOCK:
            JOBS[job_id].update(status="failed", stage="分析を完了できませんでした", error=message)


@app.post("/api/analyze")
def start_analysis(body: AnalyzeRequest):
    storage.read("datasets", body.dataset_id, include_papers=False)
    with JOBS_LOCK:
        if any(j.get("kind") == "analysis" and j["status"] in {"queued", "running"} for j in JOBS.values()):
            raise HTTPException(409, "分析が進行中です。完了してから次の分析を実行してください。")
        identifier = new_job("分析の準備中")
        JOBS[identifier]["kind"] = "analysis"
    submit_with_context(EXECUTOR, run_analysis, identifier, body.dataset_id, body.model_dump(exclude={"dataset_id"}))
    return {"job_id": identifier}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, "分析ジョブが見つかりません。サーバー再起動後は分析を再実行してください。")
        return dict(JOBS[job_id])


@app.get("/api/results/{result_id}")
def get_result(result_id: str, view: Literal["full", "summary"] = "full"):
    result = storage.read("results", result_id, include_papers=view == "full")
    if view == "summary":
        descriptor = result.get("_large_store", {})
        if descriptor.get("count"):
            identifiers = [node["id"] for node in result.get("map", {}).get("nodes", [])]
            identifiers += [pid for topic in result.get("topics", []) for pid in topic.get("evidence_ids", [])]
            identifiers += [paper["id"] for paper in result.get("top_cited_papers", [])]
            result["papers"] = large_storage.papers_by_ids(result, storage.data_root(), identifiers[:600])
        count = descriptor.get("count", len(result.get("papers", [])))
        result.setdefault("meta", {}).update(papers_total=count, papers_loaded=len(result.get("papers", [])),
                                            papers_truncated=len(result.get("papers", [])) < count)
        result.pop("_large_store", None)
        result.pop("_dataset_summary", None)
    return result


@app.get("/api/results/{result_id}/papers")
def result_papers(result_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=PAPER_PAGE_LIMIT),
                  query: str = Query("", max_length=500), topic_id: str | None = Query(None, max_length=500),
                  author_id: str | None = Query(None, max_length=500), year: int | None = Query(None, ge=1900, le=2100),
                  sort: Literal["title", "year", "citations"] = "year"):
    result = storage.read("results", result_id, include_papers=False)
    return large_storage.paper_page(result, storage.data_root(), offset=offset, limit=limit, query=query,
                                    topic_id=topic_id, author_id=author_id, canonical_author_id=author_id, year=year, sort=sort)


@app.get("/api/results/{result_id}/papers/{paper_id:path}")
def result_paper(result_id: str, paper_id: str):
    result = storage.read("results", result_id, include_papers=False)
    return large_storage.paper_by_id(result, storage.data_root(), paper_id)


class FieldReportRequest(BaseModel):
    result_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    topic_id: str = Field(min_length=1, max_length=100)
    neighbor_id: str | None = Field(default=None, max_length=100)
    provider: Literal["none", "local", "openai"] = "none"
    model: str | None = Field(default=None, min_length=1, max_length=160)


@app.get("/api/llm/status")
def llm_status():
    return field_llm.status()


def run_field_report(job_id: str, result: dict | str, options: dict):
    from app.field_analysis import build_field_report
    report = None
    persisted_id = None
    try:
        if isinstance(result, str):
            result = storage.read("results", result)
        with JOBS_LOCK:
            JOBS[job_id].update(status="running", stage="分野と隣接領域の比較指標を集計")
        report = build_field_report(result, options["topic_id"], options.get("neighbor_id"))
        report.update(id=storage.new_id(), result_id=result["id"], created_at=storage.now(), options=options)
        report["narrative"] = field_llm.deterministic_narrative(report)
        storage.save("field_reports", report)
        persisted_id = report["id"]
        if options["provider"] != "none":
            with JOBS_LOCK:
                JOBS[job_id]["stage"] = "LLMで根拠付きの比較レポートを生成"
            def generation_progress(event):
                seconds = max(0, int(event["elapsed_seconds"]))
                count = max(0, int(event["received_chars"]))
                with JOBS_LOCK:
                    JOBS[job_id]["stage"] = f"比較レポートを生成：{seconds // 60}分{seconds % 60:02d}秒・{count:,}文字受信（検証前）"
            report["narrative"] = field_llm.generate(report, options["provider"], options.get("model"), progress=generation_progress)
            storage.save("field_reports", report)
        with JOBS_LOCK:
            JOBS[job_id].update(status="completed", stage="総合分析レポートを作成", field_report_id=report["id"])
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "総合分析を作成できませんでした。データと実行環境を確認してください。"
        extra = {"field_report_id": persisted_id} if persisted_id else {}
        if report is not None and report.get("id"):
            report["llm_error"] = message
            try:
                storage.save("field_reports", report)
                extra["field_report_id"] = report["id"]
            except Exception as save_error:
                logger.warning("Field report recovery save failed (%s)", type(save_error).__name__)
        with JOBS_LOCK:
            JOBS[job_id].update(status="failed", stage="レポート生成を完了できませんでした", error=message, **extra)
        logger.warning("Field report failed (%s)", type(exc).__name__)


@app.post("/api/field-reports")
def start_field_report(body: FieldReportRequest):
    result = storage.read("results", body.result_id, include_papers=False)
    ids = {t["id"] for t in result["topics"] if not t.get("is_outlier") and t.get("status") != "unclassified"}
    if body.topic_id not in ids or (body.neighbor_id and body.neighbor_id not in ids):
        raise ValueError("対象の分類済み分野が見つかりません。")
    if body.neighbor_id == body.topic_id:
        raise ValueError("比較先には別の分野を選んでください。")
    identifier = new_job("総合分析の準備中")
    submit_with_context(REPORT_EXECUTOR, run_field_report, identifier, body.result_id, body.model_dump(exclude={"result_id"}))
    return {"job_id": identifier}


@app.get("/api/field-reports/{report_id}")
def get_field_report(report_id: str):
    from app.limits import LARGE_CORPUS_THRESHOLD
    report = storage.read("field_reports", report_id)
    scope = report.get("scope") or {}
    counts = [scope.get("corpus_papers"), scope.get("focus_papers"), scope.get("neighbor_papers"),
              (report.get("focus") or {}).get("count"), (report.get("neighbor") or {}).get("count")]
    if max((value for value in counts if isinstance(value, (int, float))), default=0) > LARGE_CORPUS_THRESHOLD:
        # Keep the complete saved report and CSVs. Bound only the browser view;
        # this must not turn display sampling into a change of analytical scope.
        report = large_storage.display_network(report)
        report["display_limits"] = {"evidence_ids": 50, "export_data_included": False, "full_data_in_csv": True}
    return report


@app.get("/api/field-reports/{report_id}/export")
def export_field_report(report_id: str, kind: Literal["data", "report", "papers"] = "report"):
    report = storage.read("field_reports", report_id)
    if kind == "papers":
        content = field_exports.papers_csv(report, storage.read("results", report["result_id"]))
    else:
        content = field_exports.data_csv(report) if kind == "data" else field_exports.report_csv(report)
    return Response(content, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="research-atlas-field-{report_id[:8]}-{kind}.csv"'})


class AuthorNetworkRequest(BaseModel):
    result_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    group_by: Literal["id", "name", "institution", "community", "topic"] = "community"


def run_author_network(job_id: str, result: dict | str, group_by: str):
    try:
        if isinstance(result, str):
            result = storage.read("results", result)
        from app.author_network import build_author_network
        with JOBS_LOCK:
            JOBS[job_id].update(status="running", stage="著者の照合・所属の集計・共著クラスタを計算")
        network = build_author_network(result["papers"], group_by=group_by, topics=result.get("topics", []))
        network.update(id=storage.new_id(), result_id=result["id"], created_at=storage.now())
        network["scope"] = {"dataset_id": result.get("dataset_id"), "dataset_name": result.get("dataset_name"),
                            "start_year": result.get("meta", {}).get("start_year"),
                            "end_year": result.get("meta", {}).get("end_year"),
                            "sampled": bool(result.get("meta", {}).get("sampled")),
                            "is_demo": bool(result.get("meta", {}).get("is_demo")),
                            "providers": result.get("meta", {}).get("providers", [])}
        storage.save("author_networks", network)
        with JOBS_LOCK:
            JOBS[job_id].update(status="completed", stage="著者ネットワークの分類が完了", author_network_id=network["id"])
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else "著者ネットワークを作成できませんでした。データと実行環境を確認してください。"
        with JOBS_LOCK:
            JOBS[job_id].update(status="failed", stage="著者ネットワークの分類に失敗", error=message)
        logger.warning("Author network failed (%s)", type(exc).__name__)


@app.post("/api/author-networks")
def start_author_network(body: AuthorNetworkRequest):
    storage.read("results", body.result_id, include_papers=False)
    identifier = new_job("著者ネットワークを準備中")
    submit_with_context(AUTHOR_EXECUTOR, run_author_network, identifier, body.result_id, body.group_by)
    return {"job_id": identifier}


@app.get("/api/author-networks/{network_id}")
def get_author_network(network_id: str):
    network = storage.read("author_networks", network_id, include_papers=False)
    if network.pop("_large_store", None):
        network = large_storage.display_network(network)
    return network


@app.get("/api/author-networks/{network_id}/export")
def export_author_network(network_id: str, kind: Literal["authors", "edges", "clusters"] = "authors"):
    network = storage.read("author_networks", network_id)
    return Response(author_exports.network_csv(network, kind), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="research-atlas-authors-{network_id[:8]}-{kind}.csv"'})


class InsightRequest(BaseModel):
    result_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    topic_id: str | None = Field(default=None, max_length=100)
    question: str = Field(default="", max_length=2000)
    use_llm: bool = False


@app.post("/api/insights")
def get_insights(body: InsightRequest):
    result = get_result(body.result_id, view="summary")
    if body.use_llm:
        try:
            return insights.llm_insights(result, body.topic_id, body.question)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from None
    return insights.local_insights(result, body.topic_id)


@app.get("/api/results/{result_id}/export")
def export_result(result_id: str, format: Literal["json", "csv", "html"] = "json"):
    result = storage.read("results", result_id, include_papers=False)
    headers = {"Content-Disposition": f'attachment; filename="research-atlas-{result_id[:8]}.{format}"'}
    if format == "json":
        return StreamingResponse(large_storage.export_json(result, storage.data_root()), media_type="application/json", headers=headers)
    if any("citation_total" not in topic for topic in result.get("topics", [])):
        result = storage.read("results", result_id)
    if format == "csv":
        content, mime = reports.topic_csv(result), "text/csv"
    elif format == "html":
        if result.get("_large_store", {}).get("count"):
            from itertools import islice
            result["papers"] = list(islice(large_storage.iter_papers(result, storage.data_root()), 100))
        content, mime = reports.report_html(result), "text/html"
    return Response(content, media_type=mime, headers=headers)


@app.get("/api/demo.csv")
def download_demo():
    return Response(reports.papers_csv(demo_papers()), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="synthetic-scopus-demo.csv"'})


@app.get("/api/citation-template.csv")
def citation_template():
    return Response("\ufeffEID,Year,Citations\r\n2-s2.0-REPLACE_WITH_YOUR_EID,2024,5\r\n2-s2.0-REPLACE_WITH_YOUR_EID,2025,12\r\n", media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="citation-history-template.csv"'})


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


from app.foresight_api import router as foresight_router
app.include_router(foresight_router)
from app.map_terrain_api import router as terrain_router
app.include_router(terrain_router)
from app.landscape_api import router as landscape_router
app.include_router(landscape_router)
from app.landscape_reports_api import router as landscape_reports_router
app.include_router(landscape_reports_router)
app.mount("/static", StaticFiles(directory=str(ROOT / "static"), check_dir=False), name="static")
