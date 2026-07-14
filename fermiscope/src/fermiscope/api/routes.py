"""APIエンドポイント。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from fermiscope import __version__
from fermiscope.api.app_state import current_llm
from fermiscope.api.runs import sse_format
from fermiscope.domain.enums import DistributionKind, ParameterStatus, ResearchMode, ValueBasis
from fermiscope.domain.models import EstimateProject, ParameterEstimate, SimulationConfig
from fermiscope.estimation.distributions import DistributionError
from fermiscope.estimation.fusion import fuse_evidence
from fermiscope.evidence.dates import parse_year
from fermiscope.models.generator import generate_model_candidates
from fermiscope.question.parser import parse_question
from fermiscope.reporting.builder import build_report
from fermiscope.reporting.export import export_csv, export_html, export_markdown
from fermiscope.research.orchestrator import ResearchOrchestrator, recalculate_project
from fermiscope.research.search.service import SearchService

router = APIRouter()


# ---------- リクエストモデル ----------


class CreateProjectRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    name: str = ""
    geography: str = ""
    reference_date: str = ""
    target_unit: str = ""
    known_facts: list[str] = Field(default_factory=list)
    research_mode: ResearchMode = ResearchMode.STANDARD
    max_searches: int | None = Field(default=None, ge=1, le=500)
    max_cost_usd: float | None = Field(default=None, ge=0, le=100)
    iterations: int | None = Field(default=None, ge=1000, le=100000)
    seed: int | None = None


class UpdateQuestionRequest(BaseModel):
    subject: str | None = None
    geography: str | None = None
    reference_date: str | None = None
    target_unit: str | None = None
    time_period: str | None = None
    stock_or_flow: str | None = None
    inclusions: list[str] | None = None
    exclusions: list[str] | None = None
    known_facts: list[str] | None = None
    regenerate_models: bool = True


def _validate_correlations(
    v: list[tuple[str, str, float]] | None,
) -> list[tuple[str, str, float]] | None:
    if v is None:
        return v
    for a, b, rho in v:
        if not math.isfinite(rho):
            raise ValueError(f"相関係数が有限ではありません({a},{b},{rho})")
        if not -1.0 <= rho <= 1.0:
            raise ValueError(f"相関係数は -1〜1 の範囲である必要があります({a},{b},{rho})")
    return v


def _reject_non_finite_floats(v: float | None) -> float | None:
    if v is not None and not math.isfinite(v):
        raise ValueError("値が NaN または無限大です(有限の実数のみ許可)")
    return v


class UpdateParameterRequest(BaseModel):
    central: float | None = None
    low: float | None = None
    high: float | None = None
    distribution: DistributionKind | None = None
    note: str = ""

    @field_validator("central", "low", "high")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        return _reject_non_finite_floats(v)


class UpdateEvidenceRequest(BaseModel):
    accepted: bool
    rejection_reason: str = ""


class SelectModelsRequest(BaseModel):
    primary_id: str
    check_id: str | None = None


class RecalculateRequest(BaseModel):
    seed: int | None = None
    iterations: int | None = Field(default=None, ge=1000, le=100000)
    custom_overrides: dict[str, float] | None = None
    correlations: list[tuple[str, str, float]] | None = None

    @field_validator("correlations")
    @classmethod
    def _corr(
        cls, v: list[tuple[str, str, float]] | None
    ) -> list[tuple[str, str, float]] | None:
        return _validate_correlations(v)

    @field_validator("custom_overrides")
    @classmethod
    def _overrides_finite(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is not None:
            for k, val in v.items():
                if not math.isfinite(val):
                    raise ValueError(f"custom_overrides[{k}] が NaN/無限大です")
        return v


class LLMSettingsRequest(BaseModel):
    provider: str | None = None  # noop | openai_compatible | anthropic (mock はテスト用)
    api_base: str | None = None
    model: str | None = None
    api_key: str | None = None  # 空文字は「変更なし」、clear_api_key=true で明示削除
    clear_api_key: bool = False
    proxy: str | None = None
    timeout_seconds: float | None = Field(default=None, ge=1, le=600)


# ---------- ヘルパ ----------


def _get_project(request: Request, project_id: str) -> EstimateProject:
    cache: dict[str, EstimateProject] = request.app.state.projects_cache
    if project_id in cache:
        return cache[project_id]
    project = request.app.state.repo.load(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
    cache[project_id] = project
    return project


def _save(request: Request, project: EstimateProject) -> None:
    request.app.state.repo.save(project)
    request.app.state.projects_cache[project.id] = project


def _ensure_not_running(request: Request, project_id: str) -> None:
    """調査実行中はパラメータ/証拠の編集・再計算を拒否する(状態競合の防止)。"""
    if request.app.state.run_manager.is_running(project_id):
        raise HTTPException(
            status_code=409,
            detail="調査の実行中は編集・再計算できません。完了までお待ちください。",
        )


def _atomic_update(
    request: Request, project_id: str, mutate: Callable[[EstimateProject], Any]
) -> tuple[EstimateProject, Any]:
    """状態変更を原子的に行う。

    キャッシュ上のプロジェクトの deep copy に対して mutate を適用し、検証・再計算が
    すべて成功した場合にのみ、キャッシュとDBを新しい状態へ差し替える。mutate 内で
    HTTPException 等が送出された場合はキャッシュもDBも変更前のまま保たれる。
    """
    current = _get_project(request, project_id)
    working = current.model_copy(deep=True)
    result = mutate(working)
    request.app.state.repo.save(working)
    request.app.state.projects_cache[project_id] = working
    return working, result


async def _atomic_update_async(
    request: Request, project_id: str, mutate: Callable[[EstimateProject], Any]
) -> tuple[EstimateProject, Any]:
    """_atomic_update の非同期版(mutate が await を含む場合に使う)。"""
    current = _get_project(request, project_id)
    working = current.model_copy(deep=True)
    result = await mutate(working)
    request.app.state.repo.save(working)
    request.app.state.projects_cache[project_id] = working
    return working, result


def _discard_derived_state(project: EstimateProject) -> None:
    """スコープ変更時に旧スコープの派生状態(証拠・批判・矛盾・計算結果等)を破棄する。

    モデル・パラメータ自体の扱い(再生成 or 空化)は呼び出し側が決める。
    """
    project.evidence = {}
    project.critiques = {}
    project.contradictions = []
    project.irreducible_assumptions = []
    project.decomposition_attempts = []
    project.simulation_results = []
    project.sensitivity_results = []
    project.scenarios = []
    project.validation = None
    project.overall_confidence = None
    project.confidence_reasons = []
    project.key_caveats = []
    project.search_hits = []


def _validate_param_estimate(param: ParameterEstimate) -> None:
    """パラメータ値の整合性を検証する。不正なら 422 を送出する(500 にしない)。"""
    low, central, high = param.low, param.central, param.high
    # NaN / 無限大 の拒否(誤った値の無警告表示を防ぐ)
    for name, v in (("low", low), ("central", central), ("high", high),
                    ("confidence", param.confidence), ("sensitivity", param.sensitivity)):
        if v is not None and not math.isfinite(v):
            raise HTTPException(
                status_code=422, detail=f"{name} が NaN または無限大です(有限の実数のみ許可)。"
            )
    # 有効範囲メタデータによる範囲外の拒否(割合は0〜1、非負カウントは0以上など)
    vmin, vmax = param.valid_min, param.valid_max
    for name, v in (("low", low), ("central", central), ("high", high)):
        if v is None:
            continue
        if vmin is not None and v < vmin:
            raise HTTPException(
                status_code=422, detail=f"{name}={v} が有効範囲の下限({vmin})を下回っています。"
            )
        if vmax is not None and v > vmax:
            raise HTTPException(
                status_code=422, detail=f"{name}={v} が有効範囲の上限({vmax})を上回っています。"
            )
    # 大小関係(部分更新後の完全な値で検査)
    for lo, hi, msg in (
        (low, central, "low は central 以下である必要があります"),
        (central, high, "central は high 以下である必要があります"),
        (low, high, "low は high 以下である必要があります"),
    ):
        if lo is not None and hi is not None and lo > hi:
            raise HTTPException(status_code=422, detail=msg)
    kind = param.distribution
    if kind in (DistributionKind.LOGNORMAL, DistributionKind.LOGUNIFORM):
        for name, v in (("low", low), ("central", central), ("high", high)):
            if v is not None and v <= 0:
                raise HTTPException(
                    status_code=422,
                    detail=f"{kind.value} 分布は正の値のみ扱えます({name}={v})。",
                )
    if kind == DistributionKind.FIXED and central is None:
        raise HTTPException(status_code=422, detail="固定分布には central(中心値)が必要です。")
    if kind == DistributionKind.TRIANGULAR and central is None and (low is not None or high is not None):
        raise HTTPException(status_code=422, detail="三角分布には central(最頻値)が必要です。")


# ---------- ヘルスチェック・診断 ----------


@router.get("/healthz")
async def healthz():
    """Liveness: プロセスが応答することのみを示す(依存関係は見ない)。"""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    """Readiness: DB・必須リソースが利用可能かを確認する。未準備なら 503。"""
    from fermiscope.diagnostics import collect_diagnostics

    diag = collect_diagnostics(
        request.app.state.settings,
        repo=request.app.state.repo,
        search_provider=request.app.state.search_provider,
        llm=current_llm(request.app),
    )
    status = 200 if diag["ready"] else 503
    return JSONResponse(status_code=status, content={"ready": diag["ready"], "checks": diag["checks"]})


@router.get("/api/health")
async def api_health(request: Request):
    """詳細診断(秘密は隠す)。プロキシは有無・scheme/host のみ、DBはスキームのみ。"""
    from fermiscope.diagnostics import collect_diagnostics

    return collect_diagnostics(
        request.app.state.settings,
        repo=request.app.state.repo,
        search_provider=request.app.state.search_provider,
        llm=current_llm(request.app),
    )


# ---------- 画面 ----------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    settings = request.app.state.settings
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": settings.display_name(),
            "version": __version__,
            "search_provider": request.app.state.search_provider.name,
            "llm_provider": current_llm(request.app).name,
        },
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_page(request: Request, project_id: str):
    settings = request.app.state.settings
    project = _get_project(request, project_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "project.html",
        {
            "app_name": settings.display_name(),
            "version": __version__,
            "project_id": project.id,
            "question": project.question.original_question,
        },
    )


# ---------- 設定 ----------


@router.get("/api/config")
async def get_config(request: Request):
    settings = request.app.state.settings
    return {
        "app_name": settings.display_name(),
        "version": __version__,
        "search_provider": request.app.state.search_provider.name,
        "llm_provider": current_llm(request.app).name,
        "llm_available": current_llm(request.app).available,
        "research_modes": [m.value for m in ResearchMode],
        "config_hash": settings.config_hash,
        "defaults": {
            "max_searches": settings.search.max_searches_per_project,
            "max_cost_usd": settings.search.max_cost_per_project_usd,
            "iterations": settings.simulation.iterations,
        },
    }


# ---------- LLM 接続設定(GUIから編集)----------


_LLM_PROVIDER_CHOICES = [
    {"value": "noop", "label": "使用しない(LLM補助なし)", "needs_key": False, "needs_base": False},
    {
        "value": "openai_compatible",
        "label": "OpenAI / OpenAI互換(ローカルLLM・vLLM・Ollama等)",
        "needs_key": True,
        "needs_base": True,
        "base_hint": "例: https://api.openai.com/v1 、http://localhost:11434/v1",
        "model_hint": "例: gpt-4o-mini 、qwen2.5 等",
    },
    {
        "value": "anthropic",
        "label": "Anthropic API",
        "needs_key": True,
        "needs_base": False,
        "base_hint": "省略可(既定 https://api.anthropic.com)。プロキシ/ゲートウェイ時のみ指定",
        "model_hint": "例: claude-sonnet-5 、claude-opus-4-8 等",
    },
]


def _llm_store_or_409(request: Request):
    store = getattr(request.app.state, "llm_store", None)
    if store is None:
        raise HTTPException(
            status_code=409,
            detail="このインスタンスではLLMプロバイダが固定されており、GUIから変更できません。",
        )
    return store


@router.get("/api/settings/llm")
async def get_llm_settings(request: Request):
    store = getattr(request.app.state, "llm_store", None)
    editable = store is not None
    if store is None:
        provider = current_llm(request.app)
        current = {
            "provider": provider.name,
            "api_base": "",
            "model": "",
            "proxy": "",
            "timeout_seconds": 60.0,
            "key_set": False,
        }
    else:
        current = store.config.public_dict()
    return {"editable": editable, "providers": _LLM_PROVIDER_CHOICES, "current": current}


@router.put("/api/settings/llm")
async def update_llm_settings(request: Request, body: LLMSettingsRequest):
    store = _llm_store_or_409(request)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if body.provider is not None and body.provider not in (
        "noop",
        "mock",
        "openai_compatible",
        "anthropic",
    ):
        raise HTTPException(status_code=400, detail="未知のプロバイダです")
    try:
        config = await store.update(patch)
    except Exception as exc:  # noqa: BLE001 — 構築失敗は 400 で返す
        raise HTTPException(status_code=400, detail=f"設定を適用できません: {exc}") from exc
    return {"current": config.public_dict()}


@router.post("/api/settings/llm/test")
async def test_llm_settings(request: Request):
    store = _llm_store_or_409(request)
    ok, message = await store.test_connection()
    return {"ok": ok, "message": message}


# ---------- プロジェクトCRUD ----------


@router.post("/api/projects")
async def create_project(request: Request, body: CreateProjectRequest):
    settings = request.app.state.settings
    llm = current_llm(request.app)
    from datetime import UTC, datetime

    spec, ai_assisted = await parse_question(
        body.question,
        llm,
        geography_hint=body.geography,
        reference_date_hint=body.reference_date,
        target_unit_hint=body.target_unit,
        known_facts=body.known_facts,
        current_year=datetime.now(UTC).year,
    )
    project = EstimateProject(
        question=spec,
        name=body.name or body.question[:80],
        research_mode=body.research_mode,
        max_searches=body.max_searches,
        max_cost_usd=body.max_cost_usd,
    )
    project.simulation_config = SimulationConfig(
        iterations=body.iterations or settings.simulation.iterations,
        seed=body.seed if body.seed is not None else settings.simulation.default_seed,
    )
    project.audit(
        "project_created",
        "プロジェクトを作成しました",
        question=body.question,
        mode=body.research_mode.value,
        ai_assisted_parse=ai_assisted,
    )
    if ai_assisted:
        project.audit("ai_fallback", "AIフォールバック使用: 問いの構造化")

    models, params, ai_models = await generate_model_candidates(spec, llm)
    project.models = models
    project.parameters = params
    if ai_models:
        project.audit("ai_fallback", "AIフォールバック使用: モデル候補生成")
    project.audit(
        "models_generated",
        f"モデル候補 {len(models)} 件を生成しました",
        primary=next((m.name for m in models if m.role == "primary"), None),
        check=next((m.name for m in models if m.role == "check"), None),
    )
    _save(request, project)
    return build_report(project)


@router.get("/api/projects")
async def list_projects(request: Request):
    return request.app.state.repo.list_projects()


@router.get("/api/projects/{project_id}")
async def get_project(request: Request, project_id: str):
    return build_report(_get_project(request, project_id))


@router.patch("/api/projects/{project_id}/question")
async def update_question(request: Request, project_id: str, body: UpdateQuestionRequest):
    _ensure_not_running(request, project_id)
    from fermiscope.domain.enums import StockOrFlow

    # スコープを定義するフィールド(変更時は旧スコープの派生状態を破棄する)
    scope_fields = ("subject", "geography", "reference_date", "target_unit", "time_period")

    # 値が実際に変わったフィールドだけを検出する(同値保存は暫定解除・再生成しない)
    current_spec = _get_project(request, project_id).question
    changed: list[str] = []
    for field in (*scope_fields, "inclusions", "exclusions", "known_facts"):
        value = getattr(body, field)
        if value is not None and value != getattr(current_spec, field):
            changed.append(field)
    if body.stock_or_flow in ("stock", "flow") and (
        StockOrFlow(body.stock_or_flow) != current_spec.stock_or_flow
    ):
        changed.append("stock_or_flow")

    async def mutate(project: EstimateProject) -> None:
        spec = project.question
        for field in (*scope_fields, "inclusions", "exclusions", "known_facts"):
            if field in changed:
                setattr(spec, field, getattr(body, field))
        if "stock_or_flow" in changed:
            spec.stock_or_flow = StockOrFlow(body.stock_or_flow)  # type: ignore[arg-type]
        if not changed:
            return  # 同値保存: 暫定フラグ・モデルID・派生状態を一切変えない
        # ユーザーが修正した項目は暫定フラグを解除
        spec.provisional = [p for p in spec.provisional if p.field not in changed]
        project.audit("question_updated", f"スコープを更新: {', '.join(changed)}", fields=changed)

        scope_changed = any(f in changed for f in (*scope_fields, "stock_or_flow"))
        if scope_changed:
            # スコープが変われば、regenerate_models の値に関わらず旧スコープの派生状態を
            # 必ず破棄する(旧証拠・旧モデルを新スコープの問いに混ぜない)。
            _discard_derived_state(project)
            if body.regenerate_models:
                models, params, _ = await generate_model_candidates(
                    spec, current_llm(request.app)
                )
                project.models = models
                project.parameters = params
                project.audit(
                    "models_generated",
                    "スコープ変更に伴いモデル・パラメータを再生成し、旧スコープの証拠・"
                    "批判・矛盾・シミュレーション・検算・信頼度を破棄しました",
                )
            else:
                # 再生成しない場合はモデル・パラメータも空にし、未生成状態へ戻す。
                project.models = []
                project.parameters = {}
                project.audit(
                    "scope_reset",
                    "スコープを変更しました。再生成を指定しなかったため、モデル・"
                    "パラメータ・証拠等の派生状態を破棄し未生成状態に戻しました"
                    "(モデルの再生成が必要です)。",
                )

    project, _ = await _atomic_update_async(request, project_id, mutate)
    return build_report(project)


@router.post("/api/projects/{project_id}/models/select")
async def select_models(request: Request, project_id: str, body: SelectModelsRequest):
    _ensure_not_running(request, project_id)
    project0 = _get_project(request, project_id)
    ids = {m.id for m in project0.models}
    if body.primary_id not in ids or (body.check_id and body.check_id not in ids):
        raise HTTPException(status_code=400, detail="指定されたモデルIDが存在しません")
    if body.check_id and body.check_id == body.primary_id:
        raise HTTPException(status_code=400, detail="主モデルと検算モデルに同じIDは指定できません")
    primary_model = next(m for m in project0.models if m.id == body.primary_id)
    if not primary_model.formula.unit_check_passed:
        raise HTTPException(
            status_code=400, detail="単位検査に不合格のモデルは主モデルにできません"
        )

    def mutate(project: EstimateProject) -> None:
        for m in project.models:
            if m.id == body.primary_id:
                m.role = "primary"
                m.selection_reason = "ユーザーが主モデルとして選択。"
            elif body.check_id and m.id == body.check_id:
                m.role = "check"
                m.selection_reason = "ユーザーが検算モデルとして選択。"
            else:
                m.role = "rejected"
        project.audit(
            "models_selected",
            "ユーザーがモデルを選択しました",
            primary=body.primary_id,
            check=body.check_id,
        )
        # モデル変更に伴い、シミュレーション・シナリオ・検算・信頼度を再計算する
        # (recalculate_project が古い派生状態を破棄して再構築する)。
        try:
            recalculate_project(project, request.app.state.settings)
        except DistributionError as exc:
            raise HTTPException(
                status_code=422, detail=f"選択したモデルで再計算できません: {exc}"
            ) from exc

    project, _ = _atomic_update(request, project_id, mutate)
    return build_report(project)


# ---------- 調査実行 ----------


@router.post("/api/projects/{project_id}/research/start")
async def start_research(request: Request, project_id: str, wait: bool = False):
    """調査を開始する。

    通常は即座に返し、進捗はSSE(/events)で配信する。
    `?wait=true` の場合は完了までブロックする(スクリプト・テスト用)。
    """
    project = _get_project(request, project_id)
    manager = request.app.state.run_manager
    if manager.is_running(project_id):
        raise HTTPException(status_code=409, detail="調査は既に実行中です")
    settings = request.app.state.settings

    service = SearchService(
        request.app.state.search_provider,
        settings,
        max_searches=project.max_searches,
        max_cost_usd=project.max_cost_usd,
        # プロジェクト累積を引き継ぎ、再実行で予算がリセットされないようにする
        executed_count=project.searches_spent,
        total_cost_usd=project.cost_spent_usd,
    )
    orchestrator = ResearchOrchestrator(
        settings,
        service,
        request.app.state.fetcher,
        current_llm(request.app),
        emit=lambda et, msg, data: manager.emit(project_id, et, msg, data),
    )

    async def run_and_save():
        try:
            await orchestrator.run_research(project)
        finally:
            # 累積予算を保存し、結果は必ず永続化する(save失敗は監査に残す)
            project.searches_spent = service.executed_count
            project.cost_spent_usd = service.total_cost_usd
            try:
                request.app.state.repo.save(project)
            except Exception as exc:  # noqa: BLE001
                project.audit("persist_error", f"結果の保存に失敗しました: {type(exc).__name__}")
                manager.emit(
                    project_id, "warning", "結果の保存に失敗しました(状態は保持されています)", {}
                )

    # wait=true でも必ず RunManager に登録する。直接 await すると is_running() が
    # False のままになり、実行中ロック(_ensure_not_running / 二重起動防止)が
    # すべて無効化される(同一プロジェクトへの同時実行・実行中編集を許してしまう)。
    manager.start(project_id, run_and_save())
    if wait:
        await manager.wait(project_id)
        run = project.current_run()
        return {
            "status": run.status.value if run else "unknown",
            "project_id": project_id,
        }
    return {"status": "started", "project_id": project_id}


@router.post("/api/projects/{project_id}/research/cancel")
async def cancel_research(request: Request, project_id: str):
    project = _get_project(request, project_id)
    run = project.current_run()
    if run is None or run.status.value != "running":
        raise HTTPException(status_code=409, detail="実行中の調査がありません")
    run.cancel_requested = True
    return {"status": "cancel_requested"}


@router.get("/api/projects/{project_id}/research/status")
async def research_status(request: Request, project_id: str):
    project = _get_project(request, project_id)
    run = project.current_run()
    manager = request.app.state.run_manager
    if run is None:
        return {"status": "idle", "run_id": None}
    return {
        "status": run.status.value,
        "run_id": run.id,
        "stage": run.stage.value,
        "running": manager.is_running(project_id),
        "searches_executed": run.searches_executed,
        "cache_hits": run.search_cache_hits,
        "documents_fetched": run.documents_fetched,
        "evidence_found": run.evidence_found,
        "parameters_verified": run.parameters_verified,
        "warnings": run.warnings_count,
        "ai_fallback_uses": run.ai_fallback_uses,
        "error": run.error,
    }


@router.get("/api/projects/{project_id}/events")
async def event_stream(request: Request, project_id: str):
    _get_project(request, project_id)
    manager = request.app.state.run_manager
    queue = manager.subscribe(project_id)

    async def generator():
        try:
            yield sse_format({"type": "hello", "message": "接続しました", "data": {}})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # 終端イベントがキュー溢れ等で失われても、実行終了を検知して閉じる
                    if not manager.is_running(project_id) and queue.empty():
                        project = request.app.state.projects_cache.get(project_id)
                        run = project.current_run() if project else None
                        status = run.status.value if run else "done"
                        yield sse_format(
                            {"type": status if status in ("done", "failed", "cancelled") else "done",
                             "message": "調査は終了しています", "data": {"stage": status}}
                        )
                        break
                    yield ": keepalive\n\n"
                    continue
                yield sse_format(event)
                if event.get("type") in ("done", "failed", "cancelled"):
                    break
        finally:
            manager.unsubscribe(project_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- パラメータ・証拠の編集と再計算 ----------


@router.patch("/api/projects/{project_id}/parameters/{parameter_id}")
async def update_parameter(
    request: Request, project_id: str, parameter_id: str, body: UpdateParameterRequest
):
    _ensure_not_running(request, project_id)

    def mutate(project: EstimateProject) -> None:
        param = project.parameters.get(parameter_id)
        if param is None:
            raise HTTPException(status_code=404, detail="パラメータが見つかりません")

        for field in ("central", "low", "high"):
            value = getattr(body, field)
            if value is not None:
                old = getattr(param, field)
                setattr(param, field, value)
                param.record_change(field, old, value, actor="user", note=body.note)
        if body.distribution is not None:
            param.record_change(
                "distribution", param.distribution.value, body.distribution.value, actor="user"
            )
            param.distribution = body.distribution
            param.distribution_rationale = "ユーザーが分布を指定しました。"

        # 部分更新後の完全な値を検証する(不正なら 422、500 にしない)
        _validate_param_estimate(param)

        value_provided = any(
            getattr(body, f) is not None for f in ("central", "low", "high")
        )
        if value_provided:
            # 数値が与えられたときのみユーザー上書きとして確定し、未解決を解除する
            param.user_overridden = True
            param.value_basis = ValueBasis.USER_INPUT
            param.status = ParameterStatus.USER_OVERRIDDEN
            param.unresolved_reason = ""
        elif param.central is None:
            # 分布だけ変更し数値が無い場合、未解決状態を誤って解除しない
            param.status = ParameterStatus.UNRESOLVED
            param.value_basis = ValueBasis.UNRESOLVED
        project.audit(
            "value_change",
            f"ユーザーがパラメータを編集: {param.name}",
            parameter_id=parameter_id,
            central=param.central,
            low=param.low,
            high=param.high,
            note=body.note,
        )
        # 編集後は検索を再実行せずローカル再計算(要件§16)
        try:
            recalculate_project(project, request.app.state.settings)
        except DistributionError as exc:
            raise HTTPException(
                status_code=422, detail=f"この値では分布を構成できません: {exc}"
            ) from exc

    project, _ = _atomic_update(request, project_id, mutate)
    return build_report(project)


@router.patch("/api/projects/{project_id}/evidence/{evidence_id}")
async def update_evidence(
    request: Request, project_id: str, evidence_id: str, body: UpdateEvidenceRequest
):
    _ensure_not_running(request, project_id)

    def mutate(project: EstimateProject) -> None:
        evidence = project.evidence.get(evidence_id)
        if evidence is None:
            raise HTTPException(status_code=404, detail="証拠が見つかりません")
        evidence.accepted = body.accepted
        evidence.rejection_reason = body.rejection_reason if not body.accepted else ""
        project.audit(
            "evidence_updated",
            f"証拠の採用状態を変更: {evidence.title}({'採用' if body.accepted else '不採用'})",
            evidence_id=evidence_id,
            reason=body.rejection_reason,
        )
        # 影響を受けるパラメータを再統合(ユーザー上書きは温存)。統合前に旧い
        # incompatible_reason をクリアし、現在の採用状態で判定し直す。
        param = project.parameters.get(evidence.parameter_id)
        if param is not None and not param.user_overridden:
            from fermiscope.domain.enums import SearchPurpose

            for e in project.evidence.values():
                if e.parameter_id == param.id:
                    e.incompatible_reason = ""
            items = [
                e
                for e in project.evidence.values()
                if e.parameter_id == param.id
                and (
                    e.search_purpose
                    in (
                        SearchPurpose.DIRECT_VALUE,
                        SearchPurpose.PRIMARY_SOURCE,
                        SearchPurpose.LATEST_VALUE,
                        SearchPurpose.ALTERNATIVE_VALUE,
                    )
                    or e.search_purpose is None
                )
            ]
            fuse_evidence(
                param,
                items,
                request.app.state.settings,
                reference_year=parse_year(project.question.reference_date),
            )
        # 統合・矛盾・検算・信頼度・注意点を再構築(不採用証拠の古い矛盾を残さない)
        try:
            recalculate_project(project, request.app.state.settings)
        except DistributionError as exc:
            raise HTTPException(
                status_code=422, detail=f"再計算できません: {exc}"
            ) from exc

    project, _ = _atomic_update(request, project_id, mutate)
    return build_report(project)


@router.post("/api/projects/{project_id}/recalculate")
async def recalculate(request: Request, project_id: str, body: RecalculateRequest | None = None):
    _ensure_not_running(request, project_id)
    project = _get_project(request, project_id)
    if body is not None:
        if body.seed is not None:
            project.simulation_config.seed = body.seed
        if body.iterations is not None:
            project.simulation_config.iterations = body.iterations
        if body.correlations is not None:
            project.simulation_config.correlations = body.correlations
    recalculate_project(project, request.app.state.settings)
    if body is not None and body.custom_overrides:
        primary = project.primary_model()
        if primary is not None:
            from fermiscope.estimation.engine import (
                EstimationError,
                compute_scenarios,
            )

            try:
                sim = next(
                    (r for r in project.simulation_results if r.model_id == primary.id), None
                )
                if sim is not None:
                    project.scenarios = compute_scenarios(
                        primary,
                        project.parameters,
                        sim,
                        request.app.state.settings,
                        custom_overrides=body.custom_overrides,
                    )
            except EstimationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    _save(request, project)
    return build_report(project)


@router.post("/api/projects/{project_id}/reverify")
async def reverify(request: Request, project_id: str):
    """既存の証拠に対して敵対的検証を再実行する(追加検索なし)。"""
    _ensure_not_running(request, project_id)
    project = _get_project(request, project_id)
    from fermiscope.adversarial.verifier import verify_parameter

    primary = project.primary_model()
    check = project.check_model()
    reference_year = parse_year(project.question.reference_date)
    # 既存の決定論チェック由来の未解決批判を作り直す
    project.critiques = {
        cid: c
        for cid, c in project.critiques.items()
        if c.resolution_status.value not in ("open",)
    }
    for p in project.parameters.values():
        p.critique_ids = [cid for cid in p.critique_ids if cid in project.critiques]
    count = 0
    for model in [m for m in (primary, check) if m is not None]:
        for pid in model.formula.leaf_parameter_ids():
            param = project.parameters[pid]
            critiques, _ = await verify_parameter(
                param,
                project.evidence,
                model,
                request.app.state.settings,
                reference_year,
                current_llm(request.app),
            )
            for c in critiques:
                project.critiques[c.id] = c
                if c.id not in param.critique_ids:
                    param.critique_ids.append(c.id)
                count += 1
    project.audit("reverify", f"再検証を実行しました({count}件の指摘)")
    _save(request, project)
    return build_report(project)


# ---------- エクスポート ----------


@router.get("/api/projects/{project_id}/report")
async def get_report(request: Request, project_id: str):
    return build_report(_get_project(request, project_id))


@router.get("/api/projects/{project_id}/export/{fmt}")
async def export_project(request: Request, project_id: str, fmt: str):
    project = _get_project(request, project_id)
    settings = request.app.state.settings
    if fmt == "json":
        return JSONResponse(
            content=build_report(project),
            headers={"Content-Disposition": f'attachment; filename="{project.id}.json"'},
        )
    if fmt == "csv":
        files = export_csv(project)
        combined = (
            "# parameters.csv\n"
            + files["parameters.csv"]
            + "\n# evidence.csv\n"
            + files["evidence.csv"]
        )
        return PlainTextResponse(
            combined,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{project.id}.csv"'},
        )
    if fmt == "html":
        return HTMLResponse(
            export_html(project, app_name=settings.display_name()),
            headers={"Content-Disposition": f'attachment; filename="{project.id}.html"'},
        )
    if fmt in ("md", "markdown"):
        return PlainTextResponse(
            export_markdown(project),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{project.id}.md"'},
        )
    raise HTTPException(status_code=400, detail="対応形式: json / csv / html / md")


__all__ = ["router"]
