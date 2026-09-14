#!/usr/bin/env python3
"""Patent Query Builder — 特許検索式自動作成システム（セミオート版／完全自動版）の Web UI サーバ。

    python patent-query-builder/server.py            # http://127.0.0.1:8740
    python patent-query-builder/server.py --port 9600 --open

- 標準ライブラリのみ（pip install 不要）。127.0.0.1 にのみ bind し外部公開しない
- LLM なしでも動く（mock モード）。⚙️ 設定で manual（プロンプトを貼る）／api（OpenAI 互換・Anthropic）に切替
- J-PlatPat 等 Web サイトへの自動アクセスは行わない。検索式はコピーして人が DB に貼り、CSV を取り込む
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from pqb import __version__  # noqa: E402
from pqb import config as cfgmod  # noqa: E402
from pqb.core.document import Document  # noqa: E402
from pqb.db.adapter import DBError  # noqa: E402
from pqb.db.csv_import import import_csv_bytes  # noqa: E402
from pqb.gates.base import PendingHuman  # noqa: E402
from pqb.llm.adapter import LLMAdapter, LLMError, PendingConfirmation, PendingManualResponse, load_prompt_template  # noqa: E402
from pqb.orchestrator import Orchestrator, OrchestratorError  # noqa: E402
from pqb.report.build import build_html, build_markdown  # noqa: E402
from pqb.store.db import Store  # noqa: E402
from pqb.ui.excel import design_workbook  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8740

_ORC: Orchestrator | None = None
_ORC_LOCK = threading.Lock()


def get_orc() -> Orchestrator:
    global _ORC
    with _ORC_LOCK:
        if _ORC is None:
            _ORC = Orchestrator(Store(cfgmod.data_dir() / "pqb.sqlite"))
        return _ORC


def set_orc(orc: Orchestrator) -> None:
    global _ORC
    _ORC = orc


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.status, self.extra = status, extra or {}


def _b64(body: dict, key: str) -> bytes | None:
    raw = body.get(key)
    if not raw:
        return None
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    return base64.b64decode(raw)


# ---------------------------------------------------------------- API 実装

def api_case_list(orc: Orchestrator) -> dict:
    return {"cases": [{k: c.get(k) for k in ("case_id", "name", "purpose", "status", "iteration", "created_at", "updated_at", "seeds")}
                      for c in orc.store.list_cases()]}


def api_case_create(orc: Orchestrator, body: dict) -> dict:
    seeds_raw = body.get("seeds")
    seeds: list = []
    if isinstance(seeds_raw, list):
        seeds = seeds_raw
    elif isinstance(seeds_raw, str):
        text = seeds_raw.strip()
        if text.startswith("["):
            try:
                seeds = json.loads(text)
            except ValueError as e:
                raise ApiError(f"既知文献 JSON が不正です: {e}") from e
        else:
            seeds = [s.strip() for s in text.replace(",", "\n").splitlines() if s.strip()]
    settings = {}
    mask = body.get("mask_terms")
    if isinstance(mask, str):
        mask = [m.strip() for m in mask.replace(",", "\n").splitlines() if m.strip()]
    if mask:
        settings["mask_terms"] = mask
    case = orc.create_case(name=str(body.get("name") or "").strip(), purpose=str(body.get("purpose") or "prior_art"),
                           input_text=str(body.get("input_text") or ""), seeds=seeds,
                           date_from=str(body.get("date_from") or ""), date_to=str(body.get("date_to") or ""),
                           countries=body.get("countries") or ["JP"], dialect=body.get("dialect") or None,
                           csv_dialect=body.get("csv_dialect") or None, settings=settings)
    return {"case": case}


def api_runs(orc: Orchestrator, case_id: str, body: dict) -> dict:
    variant = body.get("variant") or "standard"
    data = _b64(body, "csv_base64")
    text = body.get("csv_text")
    hits = body.get("hit_count")
    hits = int(hits) if hits not in (None, "") else None
    if data is None and not text:
        if hits is None:
            raise ApiError("CSV か件数のどちらかを入力してください")
        # 件数のみ（CSV なし）: 空の実行結果として記録する
        return orc.import_run(case_id, variant, docs=[], hit_count=hits, filename=body.get("filename") or "", source="count_only")
    return orc.import_run(case_id, variant, data=data, text=text, hit_count=hits, filename=body.get("filename") or "")


def api_local_index(orc: Orchestrator, body: dict) -> dict:
    data = _b64(body, "csv_base64")
    text = body.get("csv_text")
    if data is None and not text:
        raise ApiError("CSV が空です")
    docs, info = import_csv_bytes(data if data is not None else text.encode("utf-8"), cfgmod.load_csv_dialect(orc.cfg.get("csv_dialect") or "jplatpat"))
    if not docs:
        raise ApiError("CSV から文献を読み取れませんでした（列名を確認）")
    n = orc.load_local_index(docs, replace=bool(body.get("replace")))
    return {"local_index_size": n, "imported": len(docs), "info": info}


def api_config_test(orc: Orchestrator) -> dict:
    try:
        return LLMAdapter(orc.cfg, None, "").test_connection()
    except LLMError as e:
        return {"ok": False, "message": str(e)}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"PatentQueryBuilder/{__version__}"

    # -- 送信
    def _send(self, body: bytes, ctype: str, status: int = 200, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8", status)

    def _file(self, path: Path, ctype: str) -> None:
        try:
            self._send(path.read_bytes(), ctype)
        except OSError:
            self.send_error(404)

    def _body(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 64_000_000)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def _handle(self, fn) -> None:
        try:
            result = fn()
            self._json(result if result is not None else {"ok": True})
        except PendingManualResponse as e:
            self._json({"pending": True, "kind": "manual", "prompts": e.prompts,
                        "message": "LLM 返答待ち: プロンプトを LLM 画面に貼り、返答 JSON を貼り戻してください"}, 409)
        except PendingConfirmation as e:
            self._json({"pending": True, "kind": "confirm", "prompt_id": e.prompt_id, "prompt_text": e.prompt_text, "masked": e.masked,
                        "message": "外部送信前の確認が必要です（production）"}, 409)
        except PendingHuman as e:
            self._json({"pending": True, "kind": "human", "gate": e.gate, "context": e.context, "message": str(e)}, 409)
        except ApiError as e:
            self._json({"error": str(e), **e.extra}, e.status)
        except (OrchestratorError, LLMError, DBError, KeyError, ValueError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"内部エラー: {e}"}, 500)

    # -- GET
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        route, qs = u.path, parse_qs(u.query)
        orc = get_orc()
        if route in ("/", "/index.html"):
            return self._file(BASE / "index.html", "text/html; charset=utf-8")
        if route == "/style.css":
            return self._file(BASE / "style.css", "text/css; charset=utf-8")
        if route == "/app.js":
            return self._file(BASE / "app.js", "text/javascript; charset=utf-8")
        if route == "/api/health":
            return self._json({"ok": True, "app": "patent-query-builder", "version": __version__})
        if route == "/api/config":
            return self._json({"config": cfgmod.public_config(orc.cfg), "purposes": cfgmod.load_purposes(),
                               "dialects": cfgmod.list_dialects(), "csv_dialects": cfgmod.list_csv_dialects(),
                               "local_index_size": orc.store.local_index_size(), "dictionary": orc.store.dictionary_summary() | {"degraded": orc.dictionary.degraded}})
        if route == "/api/cases":
            return self._handle(lambda: api_case_list(orc))
        if route == "/api/dictionary":
            return self._json(orc.store.dictionary_summary() | {"degraded": orc.dictionary.degraded})
        if route == "/api/documents":
            ids = [i for i in (qs.get("ids") or [""])[0].split(",") if i]
            return self._json({"documents": [d.to_dict() for d in orc.store.get_documents(ids).values()]})
        if route.startswith("/api/prompts/"):
            pid = route.rsplit("/", 1)[-1]
            return self._handle(lambda: {"prompt_id": pid, "template": load_prompt_template(pid)})
        if route.startswith("/api/cases/"):
            parts = route[len("/api/cases/"):].split("/")
            case_id = parts[0]
            action = parts[1] if len(parts) > 1 else ""
            if not action:
                return self._handle(lambda: orc.case_bundle(case_id))
            if action == "report":
                fmt = (qs.get("format") or ["md"])[0]
                def rep():
                    b = orc.case_bundle(case_id)
                    return build_html(b) if fmt == "html" else build_markdown(b)
                try:
                    text = rep()
                except OrchestratorError as e:
                    return self._json({"error": str(e)}, 404)
                ctype = "text/html; charset=utf-8" if fmt == "html" else "text/markdown; charset=utf-8"
                fname = f"{case_id}_report.{'html' if fmt == 'html' else 'md'}" if qs.get("download") else None
                return self._send(text.encode("utf-8"), ctype, 200, fname)
            if action == "excel":
                try:
                    data = design_workbook(orc.case_bundle(case_id))
                except OrchestratorError as e:
                    return self._json({"error": str(e)}, 404)
                return self._send(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 200, f"{case_id}_design.xlsx")
            if action == "summary":
                return self._handle(lambda: orc.summary(case_id))
        self.send_error(404)

    # -- POST
    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        body = self._body()
        orc = get_orc()
        if route == "/api/config":
            def save():
                cfg = cfgmod.save_runtime(body)
                orc.reload_config(cfg)
                return {"config": cfgmod.public_config(cfg)}
            return self._handle(save)
        if route == "/api/config/test":
            return self._handle(lambda: api_config_test(orc))
        if route == "/api/cases":
            return self._handle(lambda: api_case_create(orc, body))
        if route == "/api/demo":
            return self._handle(lambda: {"case": orc.load_sample_case()})
        if route == "/api/dictionary":
            return self._handle(lambda: {"imported": orc.import_code_dictionary(str(body.get("csv_text") or "")),
                                         "dictionary": orc.store.dictionary_summary() | {"degraded": orc.dictionary.degraded}})
        if route == "/api/local_index":
            return self._handle(lambda: api_local_index(orc, body))
        if route.startswith("/api/cases/"):
            parts = route[len("/api/cases/"):].split("/")
            case_id, action = parts[0], (parts[1] if len(parts) > 1 else "")
            confirmed = bool(body.get("confirmed"))
            table = {
                "structure": lambda: orc.structure(case_id, confirmed=confirmed),
                "g1": lambda: orc.decide_g1(case_id, body.get("axes") or []),
                "expand": lambda: orc.expand(case_id, confirmed=confirmed),
                "g2": lambda: orc.decide_g2(case_id, body.get("decisions") or [], body.get("extra"), body.get("broad_drop_axis")),
                "build": lambda: orc.build_queries(case_id, actor="human"),
                "runs": lambda: api_runs(orc, case_id, body),
                "runs_local": lambda: orc.run_local(case_id, body.get("variants"), actor="human"),
                "score": lambda: orc.score(case_id, confirmed=confirmed, max_docs=body.get("max_docs")),
                "analyze": lambda: orc.analyze(case_id, confirmed=confirmed),
                "g4": lambda: orc.decide_g4(case_id, body.get("judgments"), body.get("transforms"), body.get("action") or "finalize", confirmed=confirmed),
                "auto": lambda: orc.run_auto(case_id, max_iterations=body.get("max_iterations")),
                "manual": lambda: orc.answer_manual(case_id, str(body.get("call_key") or ""), str(body.get("text") or ""), bool(body.get("apply_all"))),
                "excel": lambda: orc.apply_excel(case_id, _b64(body, "xlsx_base64") or b""),
                "delete": lambda: (orc.delete_case(case_id), {"deleted": case_id})[1],
            }
            fn = table.get(action)
            if fn:
                return self._handle(fn)
        self.send_error(404)

    def do_DELETE(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route.startswith("/api/cases/"):
            case_id = route[len("/api/cases/"):].split("/")[0]
            orc = get_orc()
            return self._handle(lambda: (orc.delete_case(case_id), {"deleted": case_id})[1])
        self.send_error(404)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("pqb: " + fmt % args + "\n")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Patent Query Builder server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    args = ap.parse_args(argv)
    get_orc()
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}"
    print(f"Patent Query Builder: {url}  (Ctrl+C で終了)")
    if args.open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
