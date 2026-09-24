#!/usr/bin/env python3
"""Tensorium — Transformer / BERT / SentenceBERT で表データ（CSV / XLSX）の回帰・分類を行う GUI アプリ。

    python tensorium/server.py            # http://127.0.0.1:8740
    python tensorium/server.py --port 9300 --open

- サーバは標準ライブラリのみ。127.0.0.1 にのみ bind し外部公開しない
- 学習には PyTorch（+ transformers / sentence-transformers）が必要。無くても
  データ確認・ベースライン・環境診断は動く
- モデルは Hugging Face Hub からダウンロード、またはローカルフォルダを指定（オフライン可）
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from tcore import (  # noqa: E402
    __version__,
    augment,  # noqa: E402
    dataio,
)
from tcore import runs as runstore  # noqa: E402
from tcore.catalog import catalog_payload  # noqa: E402
from tcore.catalog import family as family_info  # noqa: E402
from tcore.config import apply_env, load_settings, public_settings, save_settings  # noqa: E402
from tcore.dataio import DataError  # noqa: E402
from tcore.env import env_payload  # noqa: E402
from tcore.jobs import JobBusy, JobManager  # noqa: E402
from tcore.llm import LLMClient  # noqa: E402
from tcore.prep import PrepError, build_spec, make_examples  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8740
SAMPLE_DIR = BASE / "sample_data"
MAX_UPLOAD = 300 * 1024 * 1024
STATIC = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css", "/app.js": "app.js",
          "/charts.js": "charts.js"}

SAMPLES = [
    {"file": "reviews_ja.csv", "name": "商品レビュー → 評価（分類）",
     "desc": "レビュー本文＋カテゴリ・価格から 高評価/普通/低評価 を予測。600 行", "task": "classification"},
    {"file": "used_cars_ja.csv", "name": "中古車の説明文 → 価格（回帰）",
     "desc": "車両説明テキスト＋年式・走行距離などから価格（万円）を予測。520 行", "task": "regression"},
    {"file": "sales_memo_ja.xlsx", "name": "商談メモ → 結果（分類・XLSX）",
     "desc": "営業の商談メモ＋提案金額・商談回数から 受注/失注/継続 を予測。420 行", "task": "classification"},
]

JOBS = JobManager()
_state_lock = threading.Lock()
STATE: dict = {"raw": None, "filename": None, "table": None, "summary": None, "augment": None}
AUGMENT_PREVIEW = 200
_pred_lock = threading.Lock()
PREDICTORS: dict = {}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- データセット

def set_dataset(raw: bytes, filename: str, sheet: str | None = None) -> dict:
    if len(raw) > MAX_UPLOAD:
        raise ApiError("ファイルが大きすぎます（300MB まで）")
    table = dataio.read_table_bytes(raw, filename, sheet)
    summary = dataio.table_summary(table)
    with _state_lock:
        STATE.update({"raw": raw, "filename": filename, "table": table, "summary": summary, "augment": None})
    return summary


def current_table() -> dict:
    with _state_lock:
        table = STATE["table"]
    if table is None:
        raise ApiError("データが読み込まれていません。CSV / XLSX をアップロードしてください")
    return table


def api_dataset_sample(body: dict) -> dict:
    name = str(body.get("name", ""))
    if not any(s["file"] == name for s in SAMPLES):
        raise ApiError("不明なサンプル名")
    path = SAMPLE_DIR / name
    if not path.exists():
        raise ApiError("サンプルファイルが見つかりません")
    return set_dataset(path.read_bytes(), name)


def api_dataset_sheet(body: dict) -> dict:
    with _state_lock:
        raw, filename = STATE["raw"], STATE["filename"]
    if raw is None:
        raise ApiError("データが読み込まれていません")
    return set_dataset(raw, filename, str(body.get("sheet") or "") or None)


def api_spec_validate(body: dict) -> dict:
    """spec を正規化し、目的変数の分布と有効行数を返す（タスク設定画面用）。"""
    table = current_table()
    spec = build_spec(table, body.get("spec") or {})
    examples = make_examples(table, spec)
    out: dict = {"spec": spec, "n_valid": len(examples), "n_dropped": table["n_rows"] - len(examples)}
    ys = [e["y"] for e in examples]
    if spec["task"] == "regression":
        srt = sorted(ys)
        out["target_hist"] = dataio.histogram(srt, 24)
        out["target_stats"] = {"min": srt[0], "max": srt[-1], "mean": sum(srt) / len(srt),
                               "median": srt[len(srt) // 2]}
    else:
        from collections import Counter
        cnt = Counter(ys)
        out["class_counts"] = [{"value": k, "count": c} for k, c in cnt.most_common()]
        out["n_classes"] = len(cnt)
        if len(cnt) > 50:
            out["warning"] = f"クラス数が {len(cnt)} と多いです。回帰の方が適切かもしれません"
        elif min(cnt.values()) < 5:
            out["warning"] = "件数が 5 未満のクラスがあります。層化分割でも検証に出ない場合があります"
    n = len(examples)
    sp = spec["split"]
    out["split_counts"] = {"test": int(round(n * sp["test"])), "val": int(round(n * sp["val"]))}
    out["split_counts"]["train"] = n - out["split_counts"]["test"] - out["split_counts"]["val"]
    return out


# ---------------------------------------------------------------- 学習

def api_train(body: dict) -> dict:
    table = current_table()
    fam_id = body.get("family") or "baseline"
    fam = family_info(fam_id)
    if fam is None:
        raise ApiError(f"不明なモデルファミリー: {fam_id}")
    spec = build_spec(table, body.get("spec") or {})       # 早めに検証してエラーを返す
    if fam_id == "baseline":
        from tcore.engine.baseline import train_baseline as fn
    else:
        try:
            import torch  # noqa: F401
        except ImportError as e:
            raise ApiError("PyTorch が見つかりません。「環境」タブのコマンドでインストールしてください") from e
        if fam_id in ("hf", "sbert"):
            try:
                import transformers  # noqa: F401
            except ImportError as e:
                raise ApiError("transformers が見つかりません: pip install transformers") from e
        apply_env(load_settings())
        from tcore.engine.pipeline import train_run as fn
    params = {"family": fam_id, "family_name": fam["name"], "model": body.get("model"),
              "target": spec["target"], "task": spec["task"], "name": body.get("name")}
    if body.get("use_synthetic"):
        table = table_with_synthetic(table, spec)
        if "synthetic_from" in table:
            params["n_synthetic"] = len(table["rows"]) - table["synthetic_from"]
    try:
        job = JOBS.start("train", params, lambda job: fn(table, body, job))
    except JobBusy as e:
        raise ApiError(str(e), 409) from e
    return {"job_id": job.id, "job": job.snapshot()}


def table_with_synthetic(table: dict, spec: dict) -> dict:
    """採用済みの合成行を末尾に足した表（synthetic_from 以降が合成）。目的変数が違えば元の表のまま。"""
    with _state_lock:
        aug = STATE["augment"]
    if not aug or not aug.get("enabled") or not aug.get("rows"):
        return table
    if aug["target"] != spec["target"] or aug["task"] != spec["task"] or aug["columns"] != table["columns"]:
        return table
    rows = list(table["rows"]) + [list(r) for r in aug["rows"]]
    return {**table, "rows": rows, "n_rows": len(rows), "synthetic_from": len(table["rows"]),
            "name": f"{table.get('name')} (+合成 {len(aug['rows'])} 行)"}


# ---------------------------------------------------------------- データ拡張（LLM 知識蒸留）

def _augment_summary(aug: dict | None, full: bool = False) -> dict | None:
    if not aug:
        return None
    out = {k: aug.get(k) for k in ("target", "task", "columns", "dataset_name", "n_real", "groups", "rejected",
                                    "rejected_samples", "stats_before", "stats_after", "params", "provider", "model",
                                    "llm_stats", "duration_sec", "created", "enabled")}
    out["n_rows"] = len(aug.get("rows") or [])
    limit = None if full else AUGMENT_PREVIEW
    out["rows"] = aug["rows"][:limit] if limit else aug["rows"]
    out["meta"] = aug["meta"][:limit] if limit else aug["meta"]
    return out


def api_augment_profile(body: dict) -> dict:
    table = current_table()
    spec = build_spec(table, body.get("spec") or {})
    tg = augment.target_groups(table, spec)
    groups = [{k: g[k] for k in ("key", "label", "count", "range")} for g in tg["groups"]]
    with _state_lock:
        aug = STATE["augment"]
    return {"task": spec["task"], "target": spec["target"], "groups": groups, "n_valid": len(tg["examples"]),
            "stats": augment.balance_stats([g["count"] for g in groups]), "presets": augment.PRESET_COUNTS,
            "max_total": augment.MAX_TOTAL,
            "current": (_augment_summary(aug)
                        if aug and aug["target"] == spec["target"] and aug["task"] == spec["task"] else None)}


def api_augment_plan(body: dict) -> dict:
    table = current_table()
    spec = build_spec(table, body.get("spec") or {})
    tg = augment.target_groups(table, spec)
    p = augment.coerce_params(body)
    counts = {g["key"]: g["count"] for g in tg["groups"]}
    alloc = augment.plan_allocation(counts, p["n_total"], p["mode"], p["cap"], p["custom"])
    out = augment.plan_payload(tg["groups"], alloc)
    out["groups"] = [{k: g[k] for k in ("key", "label", "count", "range")} for g in tg["groups"]]
    return out


def api_augment_start(body: dict) -> dict:
    table = current_table()
    spec = build_spec(table, body.get("spec") or {})
    cfg = load_settings()
    params = augment.coerce_params(body.get("params") or {})
    if params["n_total"] <= 0 and params["mode"] != "custom":
        raise ApiError("生成件数を 1 以上にしてください")
    if cfg.get("llm_provider", "openai") != "builtin" and not cfg.get("llm_model"):
        raise ApiError("LLM のモデル名が未設定です。「LLM 接続」で設定を保存するか、内蔵生成を選んでください")

    def fn(job):
        result = augment.run_augmentation(table, body.get("spec") or {}, params, job, cfg)
        with _state_lock:
            STATE["augment"] = result
        return {"n_rows": len(result["rows"]), "stats_before": result["stats_before"],
                "stats_after": result["stats_after"], "rejected": result["rejected"]}

    job_params = {"kind": "augment", "target": spec["target"], "task": spec["task"], "n_total": params["n_total"],
                  "provider": cfg.get("llm_provider", "openai"), "model": cfg.get("llm_model")}
    try:
        job = JOBS.start("augment", job_params, fn)
    except JobBusy as e:
        raise ApiError(str(e), 409) from e
    return {"job_id": job.id, "job": job.snapshot()}


def api_augment_get(query: dict) -> dict:
    with _state_lock:
        aug = STATE["augment"]
    return {"augment": _augment_summary(aug, full=query.get("full", ["0"])[0] == "1")}


def api_augment_enable(body: dict) -> dict:
    with _state_lock:
        if STATE["augment"] is None:
            raise ApiError("合成データがありません")
        STATE["augment"]["enabled"] = bool(body.get("enabled", True))
        return {"enabled": STATE["augment"]["enabled"]}


def augment_export_csv(kind: str) -> bytes:
    """合成データ（synthetic）または 実データ + 合成データ（all）を CSV（UTF-8 BOM）で返す。"""
    with _state_lock:
        aug = STATE["augment"]
        table = STATE["table"]
    if not aug:
        raise ApiError("合成データがありません")
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(list(aug["columns"]) + ["_source", "_group"])
    if kind == "all" and table is not None and table["columns"] == aug["columns"]:
        for r in table["rows"]:
            w.writerow(list(r) + ["real", ""])
    for r, m in zip(aug["rows"], aug["meta"], strict=True):
        w.writerow(list(r) + ["synthetic", m.get("label", "")])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def api_llm_test() -> dict:
    return LLMClient(load_settings()).test()


def api_job(job_id: str, query: dict) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise ApiError("ジョブが見つかりません", 404)
    try:
        log_from = int(query.get("log_from", ["0"])[0])
    except ValueError:
        log_from = 0
    return job.snapshot(log_from)


def api_job_current() -> dict:
    job = JOBS.current()
    return {"job": job.snapshot() if job else None}


# ---------------------------------------------------------------- 予測

def get_predictor(run_id: str):
    with _pred_lock:
        p = PREDICTORS.get(run_id)
    if p is not None:
        return p
    apply_env(load_settings())
    from tcore.engine.predictor import Predictor
    p = Predictor(run_id)
    with _pred_lock:
        PREDICTORS[run_id] = p
        if len(PREDICTORS) > 4:                      # 重いモデルを抱え込みすぎない
            PREDICTORS.pop(next(iter(PREDICTORS)))
    return p


def _predict_with_table(run_id: str, table: dict, preview: bool) -> dict:
    if not run_id:
        raise ApiError("run_id を指定してください")
    try:
        p = get_predictor(run_id)
    except ValueError as e:
        raise ApiError(str(e)) from e
    if not table["rows"]:
        raise ApiError("予測する行がありません")
    out = p.predict_table(table)
    out["columns"] = table["columns"]
    out["n_rows"] = len(table["rows"])
    out["rows"] = table["rows"]
    out["required_columns"] = p.required_columns()
    return out


def api_predict(body: dict) -> dict:
    run_id = str(body.get("run_id") or "")
    if body.get("table"):
        t = body["table"]
        table = {"name": "manual", "columns": list(t.get("columns") or []),
                 "rows": [list(r) for r in t.get("rows") or []]}
    elif body.get("rows"):
        rows_in = body["rows"]
        cols = list(body.get("columns") or (list(rows_in[0].keys()) if rows_in else []))
        table = {"name": "manual", "columns": cols,
                 "rows": [[("" if r.get(c) is None else str(r.get(c))) for c in cols] for r in rows_in]}
    else:
        raise ApiError("rows または table を指定してください")
    table["n_rows"] = len(table["rows"])
    return _predict_with_table(run_id, table, preview=False)


def api_predict_upload(raw: bytes, filename: str, run_id: str) -> dict:
    table = dataio.read_table_bytes(raw, filename)
    return _predict_with_table(run_id, table, preview=True)


# ---------------------------------------------------------------- runs

def api_run(run_id: str) -> dict:
    try:
        meta = runstore.load_meta(run_id)
    except ValueError as e:
        raise ApiError(str(e)) from e
    if meta is None:
        raise ApiError("run が見つかりません", 404)
    return meta


def api_run_delete(run_id: str) -> dict:
    try:
        ok = runstore.delete_run(run_id)
    except ValueError as e:
        raise ApiError(str(e)) from e
    with _pred_lock:
        PREDICTORS.pop(run_id, None)
    return {"deleted": ok}


def api_run_rename(run_id: str, body: dict) -> dict:
    try:
        meta = runstore.rename_run(run_id, str(body.get("name") or ""))
    except ValueError as e:
        raise ApiError(str(e)) from e
    if meta is None:
        raise ApiError("run が見つかりません", 404)
    return {"id": run_id, "name": meta["name"]}


# ---------------------------------------------------------------- 状態・設定

def api_status() -> dict:
    with _state_lock:
        summary = STATE["summary"]
    job = JOBS.current()
    cfg = load_settings()
    dataset = None
    if summary:
        dataset = {k: summary.get(k) for k in ("name", "n_rows", "n_cols", "sheet", "sheets")}
    with _state_lock:
        aug = STATE["augment"]
    return {"version": __version__, "dataset": dataset,
            "job": ({"id": job.id, "kind": job.kind, "status": job.status, "progress": job.progress,
                     "params": job.params} if job else None),
            "augment": ({"target": aug["target"], "task": aug["task"], "n_rows": len(aug["rows"]),
                         "enabled": aug["enabled"]} if aug else None),
            "settings": public_settings(cfg), "n_runs": len(runstore.list_runs())}


def api_settings_save(body: dict) -> dict:
    cfg = save_settings(body or {})
    apply_env(cfg)
    with _pred_lock:
        PREDICTORS.clear()
    return public_settings(cfg)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"Tensorium/{__version__}"

    def log_message(self, fmt, *args):     # ポーリングで騒がしくならないように API エラーのみ表示
        msg = fmt % args
        if '"GET /api/jobs/' in msg or '"GET /api/status' in msg or '"GET /api/augment' in msg:
            return
        if " 200 " in msg or " 304 " in msg:
            return
        sys.stderr.write(f"{self.address_string()} - {msg}\n")

    # ---- helpers
    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False, allow_nan=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, e: Exception) -> None:
        if isinstance(e, ApiError):
            self._json({"error": str(e)}, e.status)
        elif isinstance(e, (DataError, PrepError, ValueError)):
            self._json({"error": str(e)}, 400)
        else:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ApiError("リクエストが大きすぎます（300MB まで）", 413)
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ApiError("JSON の解釈に失敗しました") from e
        return obj if isinstance(obj, dict) else {"value": obj}

    def _filename(self) -> str:
        name = unquote(self.headers.get("X-Filename") or "upload.csv")
        return Path(name.replace("\\", "/")).name or "upload.csv"

    def _static(self, path: str) -> None:
        file = BASE / STATIC[path]
        if not file.exists():
            self.send_error(404)
            return
        data = file.read_bytes()
        ctype = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---- routes
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path, query = u.path, parse_qs(u.query)
        try:
            if path in STATIC:
                return self._static(path)
            if path == "/api/status":
                return self._json(api_status())
            if path == "/api/env":
                return self._json(env_payload(load_settings()))
            if path == "/api/settings":
                return self._json(public_settings())
            if path == "/api/catalog":
                return self._json(catalog_payload())
            if path == "/api/samples":
                return self._json({"samples": [s for s in SAMPLES if (SAMPLE_DIR / s["file"]).exists()]})
            if path == "/api/dataset":
                with _state_lock:
                    return self._json({"dataset": STATE["summary"]})
            if path == "/api/jobs/current":
                return self._json(api_job_current())
            if path == "/api/augment":
                return self._json(api_augment_get(query))
            if path == "/api/augment/export":
                kind = query.get("kind", ["synthetic"])[0]
                data = augment_export_csv("all" if kind == "all" else "synthetic")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(data)
            if path.startswith("/api/jobs/"):
                return self._json(api_job(path.split("/")[3], query))
            if path == "/api/runs":
                return self._json({"runs": runstore.list_runs()})
            if path.startswith("/api/runs/"):
                return self._json(api_run(path.split("/")[3]))
            self.send_error(404)
        except Exception as e:  # noqa: BLE001
            self._error(e)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/dataset/upload":
                return self._json({"dataset": set_dataset(self._body(), self._filename(),
                                                          unquote(self.headers.get("X-Sheet") or "") or None)})
            if path == "/api/predict/upload":
                run_id = self.headers.get("X-Run-Id") or ""
                return self._json(api_predict_upload(self._body(), self._filename(), run_id))
            body = self._json_body()
            if path == "/api/dataset/sample":
                return self._json({"dataset": api_dataset_sample(body)})
            if path == "/api/dataset/sheet":
                return self._json({"dataset": api_dataset_sheet(body)})
            if path == "/api/dataset/clear":
                with _state_lock:
                    STATE.update({"raw": None, "filename": None, "table": None, "summary": None, "augment": None})
                return self._json({"ok": True})
            if path == "/api/spec/validate":
                return self._json(api_spec_validate(body))
            if path == "/api/train":
                return self._json(api_train(body))
            if path == "/api/augment/profile":
                return self._json(api_augment_profile(body))
            if path == "/api/augment/plan":
                return self._json(api_augment_plan(body))
            if path == "/api/augment/start":
                return self._json(api_augment_start(body))
            if path == "/api/augment/enable":
                return self._json(api_augment_enable(body))
            if path == "/api/augment/clear":
                with _state_lock:
                    STATE["augment"] = None
                return self._json({"ok": True})
            if path == "/api/llm/test":
                return self._json(api_llm_test())
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                return self._json({"cancelled": JOBS.cancel(path.split("/")[3])})
            if path == "/api/predict":
                return self._json(api_predict(body))
            if path.startswith("/api/runs/") and path.endswith("/delete"):
                return self._json(api_run_delete(path.split("/")[3]))
            if path.startswith("/api/runs/") and path.endswith("/rename"):
                return self._json(api_run_rename(path.split("/")[3], body))
            if path == "/api/settings":
                return self._json(api_settings_save(body))
            self.send_error(404)
        except Exception as e:  # noqa: BLE001
            self._error(e)

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/runs/"):
                return self._json(api_run_delete(path.split("/")[3]))
            self.send_error(404)
        except Exception as e:  # noqa: BLE001
            self._error(e)


def make_server(port: int = 0) -> ThreadingHTTPServer:
    apply_env(load_settings())
    srv = ThreadingHTTPServer((HOST, port), Handler)
    srv.daemon_threads = True
    return srv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tensorium — Transformer 回帰・分類スタジオ")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    args = ap.parse_args(argv)
    srv = make_server(args.port)
    url = f"http://{HOST}:{srv.server_address[1]}"
    print(f"Tensorium v{__version__}  {url}  (Ctrl+C で終了)")
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
