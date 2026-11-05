"""Patent Atlas — single-user, local Python application.

This module composes the Workspace and services, registers routes and keeps
each handler thin. Domain logic lives in the *_service modules.
"""

import copy
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from analysis_engine import demo_rows, map_patents, parse_csv, refinement_terms, train
from catalog import keywords, suggest
from llm import complete, validate_timeout
import llm_judgment
from query_formats import formats as query_formats, export_query
from refinement_classifications import classification_feedback
import classification_explorer as explorer
import classification_translations
import agent_service
import candidate_service
import import_service
import job_runner
import judgment_service
import query_service
from candidate_service import (
    display_candidate_proposal,
    display_discovery,
    display_labels,
    llm_candidate_rows,
    rejected_candidate_guidance,
)
from common import checked_text
from judgment_service import LLM_CHECKPOINT_BATCHES
from discovery_routes import register_discovery_routes
from discovery_services import new_discovery
from orchestration_routes import register_orchestration_routes
from convergence_routes import register_convergence_routes
from convergence_services import display_convergence, display_search_result
from workspace import Workspace
from workspace_defaults import DEFAULT_SETTINGS, SECRET_SETTINGS, checked_settings, new_job, new_state
from research_routes import register_research_routes
from research_services import ResearchServices
from invention_routes import register_invention_routes
from services import workspace_services

ROOT = Path(__file__).parent


def _recover_workspace(workspace):
    if workspace.settings["classification_layout"] not in ("semantic", "grid"):
        workspace.settings["classification_layout"] = DEFAULT_SETTINGS["classification_layout"]
    try:
        workspace.settings["llm_timeout"] = validate_timeout(workspace.settings.get("llm_timeout", 120))
    except ValueError:
        workspace.settings["llm_timeout"] = DEFAULT_SETTINGS["llm_timeout"]
    if not isinstance(workspace.state.get("discovery"), dict):
        workspace.state["discovery"] = new_discovery()
    elif workspace.state["discovery"].get("status") == "running":
        workspace.state["discovery"].update(
            status="stopped", last_error="前回の探索はアプリの終了で中断しました。保存済みの結果から再開できます。"
        )
    if (workspace.state.get("training") or {}).get("mode") == "llm" and workspace.state["training"].get(
        "status"
    ) == "running":
        workspace.state["training"].update(
            status="cancelled",
            stage="中断",
            message="前回のLLM判定はアプリの終了で中断しました。保存済みの判定を保持しています。",
        )


ws = Workspace(
    Path(os.environ.get("PATENT_ATLAS_DATA", str(ROOT / "data"))),
    state=new_state(),
    settings=DEFAULT_SETTINGS.copy(),
    job=new_job(),
    secret_settings=SECRET_SETTINGS,
)
ws.load(after_load=_recover_workspace, initialize_workflows=True)
# Compatibility references for callers; routes receive ws and explicit services.
STATE, SETTINGS, LOCK, JOB, DATA = ws.state, ws.settings, ws.lock, ws.job, ws.data_dir
classification_translations.configure_cache(DATA)
STOP = ws.stop
app = FastAPI(title="Patent Atlas", docs_url="/api/docs")


@app.middleware("http")
async def local_security(request: Request, call_next):
    host = (request.url.hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1", "testserver"):
        return JSONResponse({"detail": "ローカル接続のみ利用できます。"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "異なるオリジンからの変更は許可されません。"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(ValueError)
async def value_error(request, error):
    return JSONResponse({"detail": str(error)}, status_code=400)


# --- Service seams -----------------------------------------------------------
# These module-level names are the replaceable seams (tests patch save, complete,
# candidate_list, make_query, train, refinement_terms, ...). Each delegates to a
# *_service function with the Workspace and the current seam values.


def save():
    return ws.save()


def idle():
    return ws.idle()


def _public_workspace(state):
    return {
        **{key: value for key, value in state.items() if key != "convergence_tracking"},
        "convergence": display_convergence(state),
        "search_result": display_search_result(state),
        "selected": query_service.normalize_selection(ws, state["selected"], state["candidates"]),
        "candidate_proposal": display_candidate_proposal(state.get("candidate_proposal")),
        "discovery": display_discovery(state["discovery"]),
        "candidates": [
            {**display_labels(c, selection=True), "key": query_service.classification_key(c)}
            for c in state["candidates"]
        ],
        "classification_catalog": {
            "ipc": explorer.ipc_catalog.metadata(),
            "fterm": explorer.fterm_catalog.metadata(),
            "fi": explorer.fi_catalog.metadata(),
        },
    }


def public_state():
    return ws.public_state(_public_workspace, cache_token=classification_translations.cache_token)


def make_query(refine=False, include=None, exclude=None, preserve_conditions=False, classification_keys=None):
    return query_service.make_query(
        ws,
        refine,
        include,
        exclude,
        preserve_conditions,
        classification_keys,
        refinement_terms=refinement_terms,
    )


def candidate_list(text, use_llm=False, *, notices=None, proposal=None, connection=None, research_context=None):
    return candidate_service.candidate_list(
        ws,
        text,
        use_llm,
        notices=notices,
        proposal=proposal,
        connection=connection,
        research_context=research_context,
        complete=complete,
    )


def progress(value, message):
    return job_runner.progress(ws, value, message)


def begin_job(action, *, kind=None, mode=None):
    return job_runner.begin_job(ws, action, kind=kind, mode=mode, save=save)


def assessment_options(body, *, allow_empty=False):
    return judgment_service.assessment_options(ws, body, allow_empty=allow_empty)


def run_llm_assessment(body, progress_callback=None, *, _prepared=None):
    return judgment_service.run_llm_assessment(
        ws,
        body,
        progress_callback,
        _prepared=_prepared,
        complete=complete,
        save=save,
        progress=progress,
        checkpoint_batches=LLM_CHECKPOINT_BATCHES,
    )


def run_model_training(mode, include_agent=False, progress_callback=None):
    return judgment_service.run_model_training(
        ws, mode, include_agent, progress_callback, train=train, save=save, progress=progress
    )


# --- Routes ------------------------------------------------------------------


@app.get("/api/state")
def get_state():
    return Response(
        ws.public_state_json(_public_workspace, cache_token=classification_translations.cache_token),
        media_type="application/json",
    )


@app.get("/api/query/formats")
def output_formats():
    return query_formats()


@app.post("/api/query/export")
def output_query(body: dict):
    with ws.lock:
        item = query_service.find_query(ws, body.get("query_id"))
        if item is None:
            raise HTTPException(404, "出力する検索式が見つかりません。まず初案を作成してください。")
        snapshot = copy.deepcopy(item)
    return export_query(
        snapshot,
        body.get("format", "jplatpat"),
        replacements=body.get("replacements"),
        omit_unsupported=body.get("omit_unsupported", False),
        allow_unverified=body.get("allow_unverified", False),
        descendants=body.get("descendants", False),
        language=body.get("language", "auto"),
    )


@app.post("/api/candidates")
def candidates(body: dict):
    watched_fields = ("keywords", "candidates", "selected", "classification_view", "candidate_proposal")
    with ws.lock:
        ws.idle()
        text, use_llm = candidate_service.checked_candidate_request(body)
        before = ws.snapshot(watched_fields, include_settings=True)
        selected = query_service.normalize_selection(ws, ws.state["selected"])
    # Slow model requests must not prevent state/progress reads or other UI
    # interactions. Commit only if the classification workspace is unchanged.
    proposal, rows, pool, note = candidate_service.build_candidate_proposal(
        text, use_llm, before.state["candidates"], before.settings, candidate_list=candidate_list
    )
    with ws.lock:
        ws.idle()
        changed = any(ws.state.get(name) != before.state.get(name) for name in watched_fields)
        if changed or (use_llm and ws.settings != before.settings):
            raise HTTPException(
                409,
                "候補の生成中に分類・選択・キーワードまたはLLM設定が変更されました。現在の操作を保持しました。内容を確認して再実行してください。",
            )
        with ws.transaction(watched_fields, save=save):
            candidate_service.apply_candidate_proposal(ws, text, selected, proposal, rows, pool, note)
    return public_state()


@app.post("/api/selection")
def selection(body: dict):
    with ws.transaction(("selected",), save=save):
        ws.state["selected"] = query_service.normalize_selection(ws, body.get("selected", []))
    return public_state()


@app.post("/api/candidates/broader")
def add_rejected_broader_candidate(body: dict):
    """Explicitly add a verified broader scope, without selecting it for a query."""
    with ws.lock:
        ws.idle()
        proposal, rejected, item = candidate_service.reviewed_broader_candidate(ws, body)
        with ws.transaction(("candidates", "selected", "classification_view", "candidate_proposal"), save=save):
            candidate_service.add_broader_candidate(ws, proposal, rejected, item)
    return public_state()


@app.post("/api/classification")
def add_classification(body: dict):
    with ws.transaction(("candidates", "selected", "classification_view"), save=save):
        query_service.add_manual_classification(ws, body)
    return public_state()


@app.post("/api/classifications/browse")
def browse_classifications(body: dict):
    with ws.transaction(("candidates", "selected", "classification_view"), save=save):
        query_service.browse_view(ws, body, candidate_list=candidate_list)
    return public_state()


@app.post("/api/classifications/translations")
def classification_translations_update(body: dict):
    # Public classification codes only; network requests do not hold the workspace
    # lock or replace concurrent manual selections, query history, or lab plans.
    with ws.lock:
        settings = copy.deepcopy(ws.settings)
    return classification_translations.fetch_translations(body.get("codes"), settings, cache_dir=ws.data_dir)


@app.post("/api/classifications/seeds")
def classification_seeds(body: dict):
    with ws.transaction(("candidates", "selected", "classification_view"), save=save):
        query_service.seed_view(ws, body)
    return public_state()


@app.post("/api/keywords")
def update_keywords(body: dict):
    with ws.transaction(("keywords",), save=save):
        text = checked_text(body.get("keywords", ""))
        keywords(text)
        ws.state["keywords"] = text
    return public_state()


@app.post("/api/query")
def query(body: dict):
    with ws.lock:
        ws.idle()
        refine = body.get("refine", False)
        if not isinstance(refine, bool):
            raise ValueError("改善案の指定は true / false にしてください。")
        if "classification_keys" in body and (not refine or not isinstance(body["classification_keys"], list)):
            raise ValueError("改善案の分類は体系付きキーのリストで指定してください。")
        with ws.transaction(save=save, restore_pending=True):
            make_query(
                refine, body.get("include"), body.get("exclude"), classification_keys=body.get("classification_keys")
            )
            result = public_state()
    return result


@app.get("/api/refinement")
def refinement():
    with ws.lock:
        return query_service.refinement_options(ws, refinement_terms=refinement_terms)


@app.post("/api/upload")
async def upload(request: Request):
    raw, mapping = await import_service.read_csv_request(request)
    merge_mode = import_service.checked_merge_mode(request)
    with ws.lock:
        ws.idle()
        parsed = parse_csv(raw, mapping)
        if parsed["needs_mapping"]:
            return parsed
        import_service.import_patents(ws, parsed, merge_mode, map_patents=map_patents, save=save)
    return public_state()


@app.post("/api/classifications/enrich-import")
async def enrich_classification_import(request: Request):
    """Fill FI for matching existing rows without reimporting or retraining."""
    raw, mapping = await import_service.read_csv_request(request)
    parsed = import_service.parsed_fi_import(raw, mapping)
    with ws.lock:
        ws.idle()
        import_service.enrich_fi(ws, parsed, save=save)
    return public_state()


@app.post("/api/demo")
def demo():
    words = "全固体電池 固体電解質 界面抵抗"
    rows = demo_rows()
    with ws.transaction(save=save):
        ws.state.update(
            keywords=words,
            candidates=[explorer.enrich(c) for c in suggest(words)],
            selected=["H01M10/0562", "5H029AJ06"],
            patents=rows,
            clusters=map_patents(rows, words),
            training=None,
            queries=[],
            agent_log=[],
            demo=True,
            import_info=None,
            candidate_proposal=None,
            search_result=None,
            convergence_tracking=None,
        )
        make_query()
        query_service.classification_overview(ws)
    return public_state()


@app.post("/api/labels")
def labels(body: dict):
    with ws.transaction(("patents", "training"), save=save):
        query_service.apply_labels(ws, body.get("ids", []), body.get("label"))
    return public_state()


@app.post("/api/train")
def training(body: dict):
    mode = body.get("mode", "lightweight")
    if mode not in ("lightweight", "transformer", "llm"):
        raise ValueError("学習モードを確認してください。")
    with ws.lock:
        ws.idle()
        if mode == "llm":
            options = assessment_options(body)
            prepared = options, copy.deepcopy(ws.state["patents"]), copy.deepcopy(ws.settings)
            action = lambda: run_llm_assessment(body, _prepared=prepared)
        else:
            action = lambda: run_model_training(mode, bool(body.get("include_agent")))
        return begin_job(action, kind="training", mode=mode)


@app.post("/api/job/stop")
def stop():
    return job_runner.request_stop(ws)


@app.post("/api/settings")
def settings(body: dict):
    with ws.transaction((), include_settings=True, save=save):
        ws.settings.update(checked_settings(ws.settings, body))
    return public_state()


@app.post("/api/settings/test")
def test_connection():
    result = complete(ws.settings, '接続テストです。{"ok":true} と返してください。', {"test": True})
    if result.get("ok") is not True:
        raise ValueError("APIに接続しましたがJSON応答を検証できませんでした。")
    return {"ok": True, "message": "接続とJSON応答を確認しました。"}


@app.post("/api/agent")
def agent(body: dict):
    criteria = agent_service.checked_agent_request(ws, body)
    action = lambda: agent_service.run_agent(ws, services, body, criteria, refinement_terms=refinement_terms)
    return begin_job(action, kind="agent", mode=body.get("mode", "lightweight") if body.get("train") else None)


@app.get("/api/export")
def export():
    with ws.lock:
        data = json.dumps(ws.state, ensure_ascii=False, indent=2)
    return Response(
        data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="patent-atlas-project.json"'},
    )


@app.get("/api/export/labels")
def export_labels():
    with ws.lock:
        data = import_service.labels_csv(ws.state["patents"])
    return Response(
        data,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="patent-labels.csv"'},
    )


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


# --- Composition ---------------------------------------------------------------

query_service.refresh_candidates(ws)

# Explicit closures preserve the existing replaceable service seams without passing globals().
services = workspace_services(
    ws,
    save=lambda *args, **kwargs: save(*args, **kwargs),
    public_state=lambda *args, **kwargs: public_state(*args, **kwargs),
    checked_text=lambda *args, **kwargs: checked_text(*args, **kwargs),
    complete=lambda *args, **kwargs: complete(*args, **kwargs),
    progress=lambda *args, **kwargs: progress(*args, **kwargs),
    begin_job=lambda *args, **kwargs: begin_job(*args, **kwargs),
    make_query=lambda *args, **kwargs: make_query(*args, **kwargs),
    map_patents=lambda *args, **kwargs: map_patents(*args, **kwargs),
    classification_feedback=lambda *args, **kwargs: classification_feedback(*args, **kwargs),
    run_llm_assessment=lambda *args, **kwargs: run_llm_assessment(*args, **kwargs),
    run_model_training=lambda *args, **kwargs: run_model_training(*args, **kwargs),
    candidate_list=lambda *args, **kwargs: candidate_list(*args, **kwargs),
)
research_services = ResearchServices(ws, services)
register_discovery_routes(app, ws, services)
register_orchestration_routes(app, ws, services)
register_convergence_routes(app, ws, services)
register_invention_routes(app, ws, research_services)
register_research_routes(app, ws, research_services)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
