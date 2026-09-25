#!/usr/bin/env python3
"""Mycel — ローカル Markdown を、つながる知識に変えるノートアプリ。

    python mycel/server.py                    # http://127.0.0.1:8795
    python mycel/server.py --vault D:/notes   # Vault を指定して起動
    python mycel/server.py --port 9000 --open

- 標準ライブラリのみ（pip install 不要）。127.0.0.1 にだけ bind し外部公開しない
- ノートはただの .md ファイル。インデックスは <vault>/.mycel/ に置くキャッシュ
- LLM は OpenAI 互換 API / Azure OpenAI（設定画面から登録）
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from mycelcore import __version__  # noqa: E402
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.config import chat_configured, embed_configured, public_config  # noqa: E402
from mycelcore.llm import LLMClient, LLMError  # noqa: E402
from mycelcore.plugins import PluginVeto  # noqa: E402
from mycelcore.vault import ConflictError, VaultError  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8795
STATIC = BASE / "static"
MAX_BODY = 20 * 1024 * 1024
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _str(params: dict, key: str, default: str = "") -> str:
    val = params.get(key, default)
    if isinstance(val, list):
        val = val[0] if val else default
    return val if isinstance(val, str) else default


# ---------------------------------------------------------------- API ルーティング

def api_state(app: MycelApp, _p: dict) -> dict:
    cfg = app.config()
    return {"version": __version__, "vault": str(app.vault.root), "user": cfg["user_name"],
            "llm": {"chat": chat_configured(cfg), "embed": embed_configured(cfg)},
            "daily_folder": cfg["daily_folder"], "template_folder": cfg["template_folder"],
            "fts": app.index.has_fts}


def api_config_test(app: MycelApp, _p: dict) -> dict:
    cfg = app.config()
    client = LLMClient(cfg)
    out = {}
    try:
        text = client.chat("「接続OK」とだけ返答してください。", max_tokens=256, temperature=0)
        out["chat"] = {"ok": bool(text), "message": text[:120] or "応答が空でした（Max tokens を増やしてください）"}
    except LLMError as e:
        out["chat"] = {"ok": False, "message": str(e)}
    if embed_configured(cfg):
        try:
            out["embed"] = {"ok": True, "message": f"次元数 {len(client.embed(['接続テスト'])[0])}"}
        except LLMError as e:
            out["embed"] = {"ok": False, "message": str(e)}
    else:
        out["embed"] = {"ok": None, "message": "未設定（AI 検索はキーワード方式で動きます）"}
    return out


GET_ROUTES = {
    "/api/state": api_state,
    "/api/tree": lambda app, p: app.tree(),
    "/api/note": lambda app, p: app.note(_str(p, "path")),
    "/api/links": lambda app, p: app.links_info(_str(p, "path")),
    "/api/resolve": lambda app, p: {"path": app.index.resolve(_str(p, "target"))},
    "/api/search": lambda app, p: {"results": app.index.search(_str(p, "q"))},
    "/api/graph": lambda app, p: app.index.graph(_str(p, "path") or None,
                                                 int(_str(p, "depth", "1") or 1)),
    "/api/tags": lambda app, p: {"tags": app.index.tags()},
    "/api/tag": lambda app, p: {"notes": app.index.notes_with_tag(_str(p, "name"))},
    "/api/templates": lambda app, p: {"templates": app.templates()},
    "/api/config": lambda app, p: public_config(app.config()),
    "/api/plugins": lambda app, p: {"plugins": app.plugins.available()},
    "/api/ai/status": lambda app, p: app.ai.status(),
    "/api/ai/suggest": lambda app, p: {"suggestions": app.ai.suggest_links(_str(p, "path"))},
}

POST_ROUTES = {
    "/api/note/save": lambda app, b: app.save(_str(b, "path"), b.get("text"), b.get("base_version")),
    "/api/note/create": lambda app, b: app.create(_str(b, "path") or None, _str(b, "title"),
                                                  _str(b, "folder"), b.get("text") if isinstance(b.get("text"), str) else None,
                                                  _str(b, "template")),
    "/api/note/rename": lambda app, b: app.rename(_str(b, "path"), _str(b, "new_path"),
                                                  b.get("update_links", True) is not False),
    "/api/note/delete": lambda app, b: app.delete(_str(b, "path")),
    "/api/daily": lambda app, b: app.daily(_str(b, "date") or None),
    "/api/template/render": lambda app, b: {"text": app.render_template(_str(b, "template"), _str(b, "title"))},
    "/api/reindex": lambda app, b: {"updated": app.index.rebuild()},
    "/api/config": lambda app, b: public_config(app.update_config(b)),
    "/api/config/test": api_config_test,
    "/api/ai/ask": lambda app, b: app.ai.ask(_str(b, "question"), _str(b, "path") or None,
                                            b.get("history") if isinstance(b.get("history"), list) else None),
    "/api/ai/summarize": lambda app, b: app.ai.summarize(_str(b, "path")),
    "/api/ai/transform": lambda app, b: {"text": app.ai.transform(_str(b, "text"), _str(b, "preset"),
                                                                  _str(b, "instruction"))},
    "/api/ai/reindex": lambda app, b: app.ai.build_embeddings_async(),
}


def make_handler(app: MycelApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"Mycel/{__version__}"

        def log_message(self, fmt, *args):  # noqa: D401 - 静かにする
            if args and isinstance(args[0], str) and args[0].startswith(("GET /api", "POST /api")):
                return
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

        # ---- 応答
        def _json(self, obj, status: int = 200) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, e: Exception) -> None:
            if isinstance(e, ConflictError):
                self._json({"error": str(e), "conflict": True, "current_text": e.current_text,
                            "current_version": e.current_version}, 409)
            elif isinstance(e, (ApiError, VaultError)):
                self._json({"error": str(e)}, e.status)
            elif isinstance(e, PluginVeto):
                self._json({"error": str(e) or "プラグインにより保存が止められました"}, 423)
            elif isinstance(e, LLMError):
                self._json({"error": str(e)}, 502)
            elif isinstance(e, (ValueError, TypeError)):
                self._json({"error": str(e)}, 400)
            else:
                traceback.print_exc()
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        # ---- 安全確認
        def _guard(self, write: bool) -> None:
            """DNS リバインディングとクロスサイト要求（CSRF）を拒否する。"""
            raw = (self.headers.get("Host") or "").strip().lower()
            host = raw.split("]")[0] + "]" if raw.startswith("[") else raw.split(":")[0]
            if host and host not in _ALLOWED_HOSTS:
                raise ApiError("この要求は許可されていません（Host）", 403)
            if write:
                origin = self.headers.get("Origin")
                if origin:
                    o = urlparse(origin)
                    if o.scheme != "http" or (o.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
                        raise ApiError("この要求は許可されていません（Origin）", 403)
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    raise ApiError("Content-Type は application/json にしてください", 415)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError("リクエストが大きすぎます", 413)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                raise ApiError("JSON の解釈に失敗しました") from e
            if not isinstance(obj, dict):
                raise ApiError("JSON オブジェクトを送ってください")
            return obj

        # ---- 静的ファイル
        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                self._json({"error": "見つかりません"}, 404)
                return
            data = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data: http: https:; "
                             "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        # ---- メソッド
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            try:
                self._guard(write=False)
                if not url.path.startswith("/api/"):
                    self._static(url.path)
                    return
                params = parse_qs(url.query)
                if url.path.startswith("/api/plugins/"):
                    self._json(self._plugin_call(url.path, {k: v[0] for k, v in params.items()}, "GET"))
                    return
                fn = GET_ROUTES.get(url.path)
                if not fn:
                    raise ApiError("API が見つかりません", 404)
                self._json(fn(app, params))
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            try:
                self._guard(write=True)
                body = self._body()
                if url.path.startswith("/api/plugins/"):
                    self._json(self._plugin_call(url.path, body, "POST"))
                    return
                fn = POST_ROUTES.get(url.path)
                if not fn:
                    raise ApiError("API が見つかりません", 404)
                self._json(fn(app, body))
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def _plugin_call(self, path: str, params: dict, method: str) -> dict:
            """params["_method"] に GET / POST を入れて渡す。状態を変える処理は POST に限ること。"""
            parts = path.split("/")          # ["", "api", "plugins", id, name]
            if len(parts) != 5:
                raise ApiError("API が見つかりません", 404)
            fn = app.plugins.route(parts[3], parts[4])
            if not fn:
                raise ApiError("プラグインの API が見つかりません（有効になっていますか）", 404)
            return fn({**params, "_method": method})

    return Handler


def make_server(app: MycelApp, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((HOST, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Mycel — ローカル Markdown ノートアプリ")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--vault", help="Vault（ノートを置くフォルダ）。省略時は設定値か mycel/vault")
    ap.add_argument("--no-sample", action="store_true", help="空の Vault にサンプルノートを入れない")
    ap.add_argument("--config", help="設定ファイルの場所（省略時は mycel/mycel.config.json）")
    ap.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    args = ap.parse_args(argv)

    app = MycelApp(config_path=Path(args.config) if args.config else None,
                   vault_override=args.vault, seed_sample=not args.no_sample)
    httpd = make_server(app, args.port)
    url = f"http://{HOST}:{args.port}"
    print(f"Mycel {__version__}  {url}  (vault: {app.vault.root})")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.plugins.unload()
        httpd.server_close()


if __name__ == "__main__":
    main()
