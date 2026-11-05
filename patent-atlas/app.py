"""Patent Atlas — single-user, local Python application."""

import copy
import csv
import io
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from analysis_engine import ALIASES, demo_rows, map_patents, parse_csv, refinement_terms, train
from catalog import CATALOG, keywords, suggest
from llm import complete, validate_timeout
from prompt_templates import SELECTION_SYSTEM
import llm_judgment
from query_formats import formats as query_formats, export_query
from refinement_classifications import classification_feedback
import classification_explorer as explorer
import classification_translations
import candidate_service
import job_runner
import judgment_service
import query_service
from candidate_service import (
    display_candidate_proposal,
    display_discovery,
    display_labels,
    llm_candidate_rows,
    llm_classification_text,
    normalize_llm_classification,
    rejected_candidate_guidance,
)
from judgment_service import LLM_CHECKPOINT_BATCHES
from discovery_routes import register_discovery_routes
from discovery_services import new_discovery
from orchestration_routes import register_orchestration_routes
from convergence_routes import register_convergence_routes
from convergence_services import display_convergence, display_search_result, new_observation
from workspace_import import merge_patents
from workspace import Workspace
from research_routes import register_research_routes
from research_services import ResearchServices
from invention_routes import register_invention_routes
from services import Services

ROOT = Path(__file__).parent
DEFAULT_SETTINGS = dict(
    provider="offline",
    base_url="http://localhost:11434/v1",
    model="qwen3:8b",
    api_key="",
    proxy="",
    ca_bundle="",
    bypass_local=True,
    transformer_model="",
    allow_model_download=False,
    classification_layout="semantic",
    ops_key="",
    ops_secret="",
    llm_timeout=120,
)
SECRET_SETTINGS = ("api_key", "proxy", "ops_key", "ops_secret")


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
    state=dict(
        keywords="",
        candidates=[],
        selected=[],
        patents=[],
        clusters=[],
        queries=[],
        training=None,
        agent_log=[],
        demo=False,
        import_info=None,
        classification_view=None,
        candidate_proposal=None,
        discovery=None,
    ),
    settings=DEFAULT_SETTINGS.copy(),
    job=dict(
        id=None,
        status="idle",
        progress=0,
        message="",
        error=None,
        kind=None,
        mode=None,
        started_at=None,
        completed_at=None,
        stop_requested=False,
    ),
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


def save():
    return ws.save()


def idle():
    return ws.idle()



def _public_workspace(state):
    return {
        **{key: value for key, value in state.items() if key != "convergence_tracking"},
        "convergence": display_convergence(state),
        "search_result": display_search_result(state),
        "selected": normalize_selection(state["selected"]),
        "candidate_proposal": display_candidate_proposal(state.get("candidate_proposal")),
        "discovery": display_discovery(state["discovery"]),
        "candidates": [
            {**display_labels(c, selection=True), "key": classification_key(c)} for c in state["candidates"]
        ],
        "classification_catalog": {
            "ipc": explorer.ipc_catalog.metadata(),
            "fterm": explorer.fterm_catalog.metadata(),
            "fi": explorer.fi_catalog.metadata(),
        },
    }


def public_state():
    return ws.public_state(_public_workspace, cache_token=classification_translations.cache_token)


def classification_key(candidate):
    return query_service.classification_key(candidate)


def normalize_selection(values, candidates=None):
    return query_service.normalize_selection(ws, values, candidates)


def set_classification_view(items, **metadata):
    return query_service.set_classification_view(ws, items, **metadata)


def classification_overview(items=None):
    return query_service.classification_overview(ws, items)


def previous_query_for_context(ignore_classifications=False):
    return query_service.previous_query_for_context(ws, ignore_classifications)


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


def checked_text(value, limit=3000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"入力は{limit}文字以内の文字列にしてください。")
    return value.strip()


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
    with LOCK:
        query_id = body.get("query_id")
        item = (
            next((q for q in STATE["queries"] if q["id"] == query_id), None)
            if query_id
            else (STATE["queries"][-1] if STATE["queries"] else None)
        )
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
    with LOCK:
        idle()
        text = checked_text(body.get("keywords", ""))
        if not text:
            raise ValueError("調べたい技術のキーワードを入力してください。")
        use_llm = body.get("use_llm", False)
        if not isinstance(use_llm, bool):
            raise ValueError("LLM利用の指定は true / false で指定してください。")
        before = {name: copy.deepcopy(STATE.get(name)) for name in watched_fields}
        selected = normalize_selection(STATE["selected"])
        connection = copy.deepcopy(SETTINGS)
    # Slow model requests must not prevent state/progress reads or other UI
    # interactions. Commit only if the classification workspace is unchanged.
    notices = []
    proposal = dict(
        id=uuid.uuid4().hex,
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        keywords=text,
        llm_used=use_llm,
        returned_count=0,
        accepted_count=0,
        new_count=0,
        existing_count=0,
        rejected_count=0,
        duplicate_count=0,
        items=[],
        rejected=[],
        notices=notices,
    )
    items = candidate_list(text, use_llm, notices=notices, proposal=proposal, connection=connection)
    old_keys = {classification_key(row) for row in before["candidates"]}
    for row in proposal["items"]:
        row["is_new"] = classification_key(row) not in old_keys
        row["status"] = "new" if row["is_new"] else "existing"
    proposal["new_count"] = sum(row["is_new"] for row in proposal["items"])
    proposal["existing_count"] = len(proposal["items"]) - proposal["new_count"]
    pool = explorer.merge_candidates(before["candidates"], items)
    rows = explorer.merge_candidates(explorer.overview(items, text), [c for c in pool if c.get("origins")])
    # Show the exact model proposals alongside broad groups, including repeated
    # codes. Users can inspect and select what the model actually suggested.
    rows = explorer.merge_candidates(rows, proposal["items"])
    pool = explorer.merge_candidates(pool, rows)
    note = "材料・製造・界面などの観点から推薦しています。コードの存在と主題への適合性は別です。下位へ進んで定義を確認してください。"
    if use_llm:
        note += f" LLM提案{proposal['returned_count']}件のうち、候補に{proposal['accepted_count']}件を反映（新規{proposal['new_count']}件・既存{proposal['existing_count']}件）。提案コードを上位分類と一緒に表示しています。"
    if notices:
        note += " " + " ".join(notices)
    with LOCK:
        idle()
        if any(STATE.get(name) != before[name] for name in watched_fields) or (use_llm and SETTINGS != connection):
            raise HTTPException(
                409,
                "候補の生成中に分類・選択・キーワードまたはLLM設定が変更されました。現在の操作を保持しました。内容を確認して再実行してください。",
            )
        with ws.transaction(watched_fields, save=save):
            STATE.update(
                keywords=text,
                candidates=pool,
                selected=selected,
                classification_view=dict(
                    keys=[classification_key(c) for c in rows],
                    direction="overview",
                    note=note,
                    total=len(rows),
                    next_offset=None,
                    trail=[],
                ),
            )
            STATE["candidate_proposal"] = proposal
    return public_state()


@app.post("/api/selection")
def selection(body: dict):
    with LOCK:
        idle()
        selected = body.get("selected", [])
        STATE["selected"] = normalize_selection(selected)
        save()
    return public_state()


@app.post("/api/candidates/broader")
def add_rejected_broader_candidate(body: dict):
    """Explicitly add a verified broader scope, without selecting it for a query."""
    with LOCK:
        idle()
        proposal = STATE.get("candidate_proposal") or {}
        if (
            body.get("proposal_id") != proposal.get("id")
            or not proposal.get("llm_used")
            or proposal.get("keywords") != STATE["keywords"]
        ):
            raise HTTPException(409, "候補の生成結果またはテーマが変わりました。最新の候補を確認してください。")
        index = body.get("rejected_index")
        if type(index) is not int:
            raise ValueError("確認する除外候補を指定してください。")
        rejected = next(
            (
                item
                for position, item in enumerate(proposal.get("rejected", []), 1)
                if item.get("index", position) == index
            ),
            None,
        )
        if rejected is None:
            raise ValueError("この生成結果に指定された除外候補はありません。")
        item = rejected_candidate_guidance(rejected).get("broader_candidate")
        if not item or not item.get("verified") or not item.get("selectable"):
            raise ValueError("この候補から確認できる上位IPCはありません。分類指定から確認してください。")
        with ws.transaction(("candidates", "selected", "classification_view", "candidate_proposal"), save=save):
            old = next((row for row in STATE["candidates"] if classification_key(row) == item["key"]), None)
            row = copy.deepcopy(old or item)
            origin = dict(
                type="reviewed_broader",
                label="除外候補から利用者が確認した上位IPC",
                from_code=rejected["code"],
                proposal_id=proposal["id"],
            )
            row.setdefault("origins", [])
            if origin not in row["origins"]:
                row["origins"].append(origin)
            set_classification_view(
                [row],
                direction="seed",
                focus=row,
                total=1,
                next_offset=None,
                trail=[],
                note=f"{rejected['code']} を置換せず、確認済みの広い探索起点 {item['code']} を候補に追加しました。検索式に使うには選択してください。",
            )
            rejected["broader_added"] = True
    return public_state()


@app.post("/api/classification")
def add_classification(body: dict):
    with LOCK:
        idle()
        kind = body.get("kind")
        code = explorer.normalize(kind, checked_text(body.get("code", ""), 32))
        STATE["selected"] = normalize_selection(STATE["selected"])
        item = explorer.enrich(
            dict(
                code=code,
                kind=kind,
                title=checked_text(body.get("title", code), 120),
                origins=[{"type": "manual", "label": "利用者の推薦"}],
                reason="利用者が推薦した分類",
            )
        )
        set_classification_view(
            [item], direction="seed", note="推薦した分類を起点に探索できます。", total=1, next_offset=None, trail=[]
        )
        save()
    return public_state()


@app.post("/api/classifications/browse")
def browse_classifications(body: dict):
    with LOCK:
        idle()
        direction = body.get("direction", "children")
        if direction == "overview":
            classification_overview(candidate_list(STATE["keywords"], False) if STATE["keywords"].strip() else None)
        elif direction == "seeds":
            items = [c for c in STATE["candidates"] if c.get("origins")]
            set_classification_view(
                items,
                direction="seed",
                note="利用者の推薦・ターゲット由来の分類です。",
                total=len(items),
                next_offset=None,
                trail=[],
            )
        else:
            view = explorer.browse(
                kind=body.get("kind", "IPC"),
                code=body.get("code"),
                direction=direction,
                text=body.get("text", ""),
                offset=body.get("offset", 0),
            )
            items = view.pop("items")
            set_classification_view(items, **view)
        save()
    return public_state()


@app.post("/api/classifications/translations")
def classification_translations_update(body: dict):
    # Public classification codes only; network requests do not hold the workspace
    # lock or replace concurrent manual selections, query history, or lab plans.
    with LOCK:
        settings = copy.deepcopy(SETTINGS)
    return classification_translations.fetch_translations(body.get("codes"), settings, cache_dir=DATA)


@app.post("/api/classifications/seeds")
def classification_seeds(body: dict):
    with LOCK:
        idle()
        select = body.get("select", False)
        if not isinstance(select, bool):
            raise ValueError("分類の選択指定を確認してください。")
        items = explorer.seed_items(body, STATE["patents"])
        set_classification_view(
            items,
            direction="seed",
            note="推薦した分類を表示しています。分類の出典と、どの特許から来たかを確認できます。",
            total=len(items),
            next_offset=None,
            trail=[],
        )
        if select:
            STATE["selected"] = list(
                dict.fromkeys(STATE["selected"] + [classification_key(c) for c in items if c["selectable"]])
            )
        save()
    return public_state()


@app.post("/api/keywords")
def update_keywords(body: dict):
    with LOCK:
        idle()
        text = checked_text(body.get("keywords", ""))
        keywords(text)
        STATE["keywords"] = text
        save()
    return public_state()


@app.post("/api/query")
def query(body: dict):
    with LOCK:
        idle()
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
    with LOCK:
        result = refinement_terms(STATE["patents"])
        previous = previous_query_for_context(ignore_classifications=True)
        for kind in ("include", "exclude"):
            result[kind] = list(dict.fromkeys(result[kind] + previous.get(kind + "_terms", [])))
        result["active_include"] = previous.get("include_terms", [])
        result["active_exclude"] = previous.get("exclude_terms", [])
        result["classifications"] = classification_feedback(STATE["patents"], normalize_selection(STATE["selected"]))
        return result


@app.post("/api/upload")
async def upload(request: Request):
    # Stream with a hard cap so an absent/incorrect Content-Length cannot bypass it.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("CSVは20MB以下にしてください。")
    mapping = json.loads(request.query_params.get("mapping", "{}"))
    if not isinstance(mapping, dict):
        raise ValueError("列の割り当てを確認してください。")
    merge_mode = request.query_params.get("merge", "false")
    if merge_mode not in ("true", "false"):
        raise ValueError("追加・置換の指定を確認してください。")
    with LOCK:
        idle()
        parsed = parse_csv(bytes(raw), mapping)
        if parsed["needs_mapping"]:
            return parsed
        rows = parsed.pop("rows")
        observation = new_observation(rows, STATE)
        if merge_mode == "true":
            rows, report = merge_patents(STATE["patents"], rows)
            parsed.update(import_mode="merge", merge=report)
        else:
            parsed["import_mode"] = "replace"
        clusters = map_patents(rows, STATE["keywords"])
        training_metadata = copy.deepcopy(STATE["training"]) if merge_mode == "true" else None
        if training_metadata and report["added"]:
            training_metadata.update(dataset_changed=True, added_since_training=report["added"])
        with ws.transaction(save=save):
            STATE.update(
                patents=rows,
                clusters=clusters,
                demo=False,
                training=training_metadata,
                import_info=parsed,
                search_result=observation,
            )
    return public_state()


@app.post("/api/classifications/enrich-import")
async def enrich_classification_import(request: Request):
    """Fill FI for matching existing rows without reimporting or retraining."""
    from analysis_engine import backfill_fi

    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("CSVは20MB以下にしてください。")
    mapping = json.loads(request.query_params.get("mapping", "{}"))
    if not isinstance(mapping, dict):
        raise ValueError("列の割り当てを確認してください。")
    parsed = parse_csv(bytes(raw), mapping)
    if parsed["needs_mapping"] or not parsed["mapping"].get("fi"):
        raise ValueError("公報番号・発明の名称・FIを含むCSVを指定してください。")
    with LOCK:
        idle()
        rows, report = backfill_fi(STATE["patents"], parsed["rows"])
        if report["conflicts"]:
            raise ValueError(
                "公報番号と内容の不一致、または既存FIとの不一致があるため補完を中止しました。既存判定は変更していません。"
            )
        with ws.transaction(("patents", "import_info"), save=save):
            STATE["patents"] = rows
            STATE["import_info"] = {**(STATE.get("import_info") or {}), "fi_enrichment": report}
    return public_state()


@app.post("/api/demo")
def demo():
    with LOCK:
        idle()
        words = "全固体電池 固体電解質 界面抵抗"
        rows = demo_rows()
        STATE.update(
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
        classification_overview()
        save()
    return public_state()


@app.post("/api/labels")
def labels(body: dict):
    with LOCK:
        idle()
        ids = body.get("ids", [])
        label = body.get("label")
        if label not in ("keep", "exclude", None) or not isinstance(ids, list):
            raise ValueError("判定を確認してください。")
        if not set(ids) <= {r["id"] for r in STATE["patents"]}:
            raise ValueError("存在しない特許IDです。")
        for row in STATE["patents"]:
            if row["id"] in ids:
                row.update(
                    label=label,
                    label_source="human" if label else None,
                    label_reason="利用者による判断" if label else "",
                )
                row["score"] = None
        STATE["training"] = None
        for row in STATE["patents"]:
            row["score"] = None
        save()
    return public_state()


@app.post("/api/train")
def training(body: dict):
    mode = body.get("mode", "lightweight")
    if mode not in ("lightweight", "transformer", "llm"):
        raise ValueError("学習モードを確認してください。")
    with LOCK:
        idle()
        if mode == "llm":
            options = assessment_options(body)
            prepared = options, copy.deepcopy(STATE["patents"]), copy.deepcopy(SETTINGS)
            action = lambda: run_llm_assessment(body, _prepared=prepared)
        else:
            action = lambda: run_model_training(mode, bool(body.get("include_agent")))
        return begin_job(action, kind="training", mode=mode)


@app.post("/api/job/stop")
def stop():
    return job_runner.request_stop(ws)


@app.post("/api/settings")
def settings(body: dict):
    with LOCK:
        idle()
        proposed = SETTINGS.copy()
        for k in DEFAULT_SETTINGS:
            if k not in body:
                continue
            if k in SECRET_SETTINGS and not body[k]:
                continue
            if k == "llm_timeout":
                proposed[k] = validate_timeout(body[k])
            elif isinstance(DEFAULT_SETTINGS[k], bool):
                if not isinstance(body[k], bool):
                    raise ValueError("設定値の形式を確認してください。")
                proposed[k] = body[k]
            else:
                proposed[k] = checked_text(body[k], 2000)
        old_url, new_url = urlparse(SETTINGS["base_url"]), urlparse(proposed["base_url"])
        if (old_url.scheme, old_url.netloc) != (new_url.scheme, new_url.netloc) and not body.get("api_key"):
            proposed["api_key"] = ""
        if body.get("clear_secrets"):
            proposed.update(api_key="", proxy="")
        if body.get("clear_ops"):
            proposed.update(ops_key="", ops_secret="")
        if proposed["provider"] not in ("offline", "openai", "local"):
            raise ValueError("接続種別を確認してください。")
        if proposed["classification_layout"] not in ("semantic", "grid"):
            raise ValueError("分類の配置方式は「意味の近さ」または「整列」を選んでください。")
        if (
            new_url.scheme not in ("http", "https")
            or not new_url.hostname
            or new_url.username
            or new_url.password
            or new_url.query
            or new_url.fragment
        ):
            raise ValueError("API URLは認証情報やクエリを含まない http(s):// のURLにしてください。")
        SETTINGS.update(proposed)
        save()
    return public_state()


@app.post("/api/settings/test")
def test_connection():
    result = complete(SETTINGS, '接続テストです。{"ok":true} と返してください。', {"test": True})
    if result.get("ok") is not True:
        raise ValueError("APIに接続しましたがJSON応答を検証できませんでした。")
    return {"ok": True, "message": "接続とJSON応答を確認しました。"}


@app.post("/api/agent")
def agent(body: dict):
    criteria = checked_text(body.get("criteria", ""), 5000)
    if not criteria or not STATE["keywords"]:
        raise ValueError("先に探索キーワードと判断基準を入力してください。")
    if SETTINGS["provider"] == "offline":
        raise ValueError("判断エージェントにはLLMが必要です。設定タブから接続してください。")
    max_items = body.get("max_items", 100)
    threshold = body.get("threshold", 0.8)
    if body.get("mode", "lightweight") not in ("lightweight", "transformer") or not isinstance(
        body.get("train", False), bool
    ):
        raise ValueError("学習方式・学習実行の指定を確認してください。")
    with LOCK:
        idle()
        llm_judgment.prepare(
            dict(criteria=criteria, max_items=max_items, threshold=threshold, fresh_only=True),
            STATE["patents"],
            STATE["keywords"],
            SETTINGS["provider"],
            allow_empty=True,
        )

    def log(message):
        with LOCK:
            STATE["agent_log"].append(dict(time=time.strftime("%H:%M:%S"), message=message))
            STATE["agent_log"] = STATE["agent_log"][-100:]
            save()

    def action():
        log("開始。人の確定ラベルを保護し、1サイクルで停止します。")
        if not STATE["candidates"]:
            items = candidate_list(STATE["keywords"], True)
            with LOCK:
                STATE["candidates"] = items
        progress(10, "判断基準から分類候補を選んでいます")
        result = complete(
            SETTINGS,
            SELECTION_SYSTEM,
            {
                "keywords": STATE["keywords"],
                "criteria": criteria,
                "candidates": [{**c, "key": classification_key(c)} for c in STATE["candidates"]],
            },
        )
        chosen = result.get("selected")
        chosen = normalize_selection(chosen)
        if STOP.is_set():
            return
        with LOCK:
            STATE["selected"] = chosen
            make_query(bool(STATE["queries"]), preserve_conditions=True)
        log("分類を選択し初案を作成: " + str(result.get("reason", ""))[:500])
        result = run_llm_assessment(
            dict(criteria=criteria, max_items=max_items, threshold=threshold, fresh_only=True),
            lambda value, message: progress(20 + int(0.55 * value), message),
        )
        if STOP.is_set():
            return
        log(
            f"特許判定: {result.get('completed_count', 0)}件を処理、採用{result.get('judged_count', 0)}件・保留{result.get('deferred_count', 0)}件。既存の判定は保持しました。"
        )
        if body.get("train"):
            run_model_training(
                body.get("mode", "lightweight"), True, lambda value, message: progress(75 + int(0.15 * value), message)
            )
            if STOP.is_set():
                return
            log("学習完了。エージェント判断を含むため独立評価は表示しません。")
        if STATE["patents"]:
            with LOCK:
                terms = refinement_terms(STATE["patents"])
                addition = terms["include"][:2]
                kept = [r for r in STATE["patents"] if r.get("label") == "keep"]
                covered = kept and all(
                    any(w.casefold() in (r["title"] + " " + r.get("abstract", "")).casefold() for w in addition)
                    for r in kept
                )
                # Do not introduce a mandatory vocabulary clause that drops known positives.
                previous_include = previous_query_for_context().get("include_terms", [])
                current_keys = normalize_selection(STATE["selected"])
                feedback = classification_feedback(STATE["patents"], current_keys)
                next_keys = list(dict.fromkeys(current_keys + feedback["recommended_keys"]))
                make_query(
                    True,
                    None if previous_include else addition if covered else None,
                    None,
                    classification_keys=next_keys,
                )
            log(
                "判定済み公報から必要側の支持が多いIPCを追加し、次の検索式を提案しました。既存分類は保持し、分類のNOTは作りません。"
            )
        log("次のCSVを待っています。外部の特許検索・ダウンロードは接続されていません。")

    return begin_job(action, kind="agent", mode=body.get("mode", "lightweight") if body.get("train") else None)


@app.get("/api/export")
def export():
    with LOCK:
        data = json.dumps(STATE, ensure_ascii=False, indent=2)
    return Response(
        data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="patent-atlas-project.json"'},
    )


@app.get("/api/export/labels")
def export_labels():
    fields = [
        "id",
        "title",
        "abstract",
        "applicant",
        "ipc",
        "fi",
        "fterm",
        "cpc",
        "year",
        "label",
        "label_source",
        "label_reason",
        "score",
    ]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    with LOCK:
        for r in STATE["patents"]:
            writer.writerow(
                {
                    k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v
                    for k, v in r.items()
                }
            )
    return Response(
        stream.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="patent-labels.csv"'},
    )


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


if STATE["candidates"]:
    STATE["candidates"] = [explorer.enrich(c) for c in STATE["candidates"]]
    STATE["selected"] = normalize_selection(STATE["selected"])
    if (STATE.get("classification_view") or {}).get("focus"):
        STATE["classification_view"]["focus"] = explorer.enrich(STATE["classification_view"]["focus"])
    if not STATE.get("classification_view"):
        classification_overview()

# Explicit closures preserve the existing replaceable service seams without passing globals().
services = Services(
    save=lambda *args, **kwargs: save(*args, **kwargs),
    public_state=lambda *args, **kwargs: public_state(*args, **kwargs),
    checked_text=lambda *args, **kwargs: checked_text(*args, **kwargs),
    complete=lambda *args, **kwargs: complete(*args, **kwargs),
    progress=lambda *args, **kwargs: progress(*args, **kwargs),
    begin_job=lambda *args, **kwargs: begin_job(*args, **kwargs),
    set_classification_view=lambda *args, **kwargs: set_classification_view(*args, **kwargs),
    make_query=lambda *args, **kwargs: make_query(*args, **kwargs),
    map_patents=lambda *args, **kwargs: map_patents(*args, **kwargs),
    normalize_selection=lambda *args, **kwargs: normalize_selection(*args, **kwargs),
    classification_feedback=lambda *args, **kwargs: classification_feedback(*args, **kwargs),
    run_llm_assessment=lambda *args, **kwargs: run_llm_assessment(*args, **kwargs),
    run_model_training=lambda *args, **kwargs: run_model_training(*args, **kwargs),
    classification_overview=lambda *args, **kwargs: classification_overview(*args, **kwargs),
    candidate_list=lambda *args, **kwargs: candidate_list(*args, **kwargs),
)
research_services = ResearchServices(ws, services)
register_discovery_routes(app, ws, services)
register_orchestration_routes(app, ws, services)
register_convergence_routes(app, ws, services)
register_invention_routes(app, ws, research_services)
register_research_routes(app, ws, research_services)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
