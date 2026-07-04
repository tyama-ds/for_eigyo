"""llmlab Studio — 索引を横断して 検索/要約/レポート/数値抽出/グラフ するワンストップUI。

起動::

    python -m llmlab.app                 # http://127.0.0.1:8765
    python -m llmlab.app --port 9000 --root ./storage

JupyterLab からは::

    import llmlab
    llmlab.launch_app()                  # バックグラウンドで起動して URL を表示

設計:
- 標準ライブラリのみ（http.server）。追加インストール不要。
- 127.0.0.1 のみに bind（外部公開しない）。
- 接続情報（API キー等）は UI から入力し **プロセスメモリのみ** に保持
  （configure() と同じ方針。ファイルには保存しない）。
- 長時間処理はスレッドで実行し、進捗は SSE (/api/events) でリアルタイム配信。
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8765
DEFAULT_ROOT = "./storage"

_UI_PATH = Path(__file__).parent / "app_ui.html"

# タスクの進捗イベント置き場: task_id -> Queue[dict]
_tasks: dict[str, Queue] = {}
_tasks_lock = threading.Lock()

# 質問履歴（~/.llmlab/history.json に永続化。回答は先頭のみ保存）
_HISTORY_MAX = 50
_history_lock = threading.Lock()


def _history_path() -> Path:
    from .workspace import LLMLAB_DIR

    return LLMLAB_DIR / "history.json"


def _history_read() -> list[dict]:
    from .workspace import _read_json_file

    raw = _read_json_file(_history_path(), [])
    return raw if isinstance(raw, list) else []


def _history_append(entry: dict) -> None:
    from .workspace import _write_json_file

    with _history_lock:
        hist = [entry] + _history_read()
        _write_json_file(_history_path(), hist[:_HISTORY_MAX])


def _run_task(task_id: str, payload: dict) -> None:
    """バックグラウンドスレッドで MultiRAG のアクションを実行し、進捗を Queue へ流す。"""
    from .workspace import MultiRAG

    q = _tasks[task_id]

    def emit(evt: dict) -> None:
        q.put({"type": "progress", **evt})

    try:
        action = payload.get("action", "ask")
        question = (payload.get("question") or "").strip()
        indexes = payload.get("indexes") or []
        if not indexes:
            raise ValueError("索引が選択されていません")
        ws = MultiRAG(indexes, top_k=int(payload.get("top_k", 5)), progress=emit)

        def _partials(r):
            return [{"index": p.index, "kind": p.kind, "text": p.text,
                     "sources": p.sources, "refs": p.refs} for p in r.partials]

        import time

        t0 = time.time()
        if action == "extract":
            r = ws.extract(question or "文書中の主要な数値")
            preview = f"{len(r.rows)} 件の数値を抽出"
            q.put({"type": "result", "kind": "extract",
                   "rows": r.rows, "note": r.note, "partials": _partials(r)})
        else:
            if action == "summarize":
                r = ws.summarize(question or None)
            elif action == "report":
                r = ws.report(question or "全体レポート")
            else:  # ask
                if not question:
                    raise ValueError("質問を入力してください")
                r = ws.ask(question)
            preview = r.text.replace("\n", " ")[:200]
            q.put({"type": "result", "kind": "text", "text": r.text,
                   "partials": _partials(r)})
        # 質問履歴に記録（同じ調査を後からワンクリックで再現できるように）
        _history_append({
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "action": payload.get("ui_action") or payload.get("action", "ask"),
            "question": question,
            "indexes": indexes,
            "elapsed_sec": round(time.time() - t0, 1),
            "preview": preview,
        })
    except Exception as e:  # noqa: BLE001  失敗は UI に表示する
        q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        q.put({"type": "done"})


class _Handler(BaseHTTPRequestHandler):
    server_version = "llmlabStudio"
    root_dir = DEFAULT_ROOT  # serve() が差し替える

    # ---- 応答ヘルパ ----------------------------------------------------------

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):  # 静かに（アクセスログでノートを汚さない）
        pass

    # ---- ルーティング --------------------------------------------------------

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            try:
                body = _UI_PATH.read_bytes()
            except OSError:
                self._json({"error": "app_ui.html が見つかりません"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/status":
            self._api_status()
        elif url.path == "/api/indexes":
            qs = parse_qs(url.query)
            root = (qs.get("root") or [self.root_dir])[0]
            from .workspace import discover

            self._json({"root": root, "indexes": [i.to_dict() for i in discover(root)]})
        elif url.path == "/api/events":
            qs = parse_qs(url.query)
            self._api_events((qs.get("id") or [""])[0])
        elif url.path == "/api/history":
            self._json({"history": _history_read()})
        elif url.path == "/api/test":
            self._api_test()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/api/configure":
            self._api_configure()
        elif url.path == "/api/run":
            self._api_run()
        elif url.path == "/api/pin":
            self._api_pin()
        elif url.path == "/api/history/clear":
            from .workspace import _write_json_file

            with _history_lock:
                _write_json_file(_history_path(), [])
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    # ---- API 実装 ------------------------------------------------------------

    def _api_status(self) -> None:
        from .config import is_configured

        info = {"configured": is_configured(), "root": self.root_dir}
        if is_configured():
            from .config import get_settings

            s = get_settings()
            info.update(base_url=s.base_url, model=s.model, embed_model=s.embed_model,
                        use_proxy=s.use_proxy,
                        embed_base_url=s.embed_base_url or "")
        self._json(info)

    def _api_configure(self) -> None:
        from .config import configure

        p = self._read_json()
        try:
            configure(
                base_url=str(p.get("base_url", "")).strip(),
                api_key=str(p.get("api_key", "")),
                model=str(p.get("model", "")).strip(),
                embed_model=str(p.get("embed_model", "")).strip() or None,
                use_proxy=bool(p.get("use_proxy", False)),
                proxy_url=str(p.get("proxy_url", "")).strip() or None,
                embed_base_url=str(p.get("embed_base_url", "")).strip() or None,
                embed_api_key=str(p.get("embed_api_key", "")) or None,
                request_timeout=float(p.get("request_timeout") or 120.0),
            )
            self._json({"ok": True})
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 400)

    def _api_run(self) -> None:
        from .config import is_configured

        if not is_configured():
            self._json({"error": "接続設定が未入力です（右上の CONNECT から設定）"}, 400)
            return
        payload = self._read_json()
        task_id = uuid.uuid4().hex[:12]
        with _tasks_lock:
            _tasks[task_id] = Queue()
        threading.Thread(target=_run_task, args=(task_id, payload), daemon=True).start()
        self._json({"task_id": task_id})

    def _api_pin(self) -> None:
        """ピン留めの付け外し。"""
        from .workspace import pin_index, unpin_index

        p = self._read_json()
        path = str(p.get("path", "")).strip()
        if not path:
            self._json({"ok": False, "error": "path がありません"}, 400)
            return
        try:
            pins = pin_index(path) if p.get("pinned") else unpin_index(path)
            self._json({"ok": True, "pins": pins})
        except OSError as e:
            self._json({"ok": False, "error": f"ピンの保存に失敗: {e}"}, 500)

    def _api_test(self) -> None:
        """接続テスト: /v1/models を短いタイムアウトで叩いて応答を確認する。"""
        from .config import is_configured

        if not is_configured():
            self._json({"ok": False, "error": "接続設定が未入力です"})
            return
        try:
            from .client import get_client

            client = get_client().with_options(timeout=15)
            models = [m.id for m in client.models.list().data][:8]
            self._json({"ok": True, "models": models})
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _api_events(self, task_id: str) -> None:
        """SSE: タスクの進捗/結果を流す。done で切断。"""
        q = _tasks.get(task_id)
        if q is None:
            self._json({"error": "unknown task"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                try:
                    evt = q.get(timeout=30)
                except Empty:  # keep-alive（プロキシ/ブラウザの切断防止）
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(evt, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
                if evt.get("type") == "done":
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass  # クライアントが閉じた
        finally:
            with _tasks_lock:
                _tasks.pop(task_id, None)


def serve(port: int = DEFAULT_PORT, root: str = DEFAULT_ROOT, *, open_browser: bool = False):
    """UI サーバを起動する（ブロッキング）。Ctrl+C で終了。"""
    handler = type("Handler", (_Handler,), {"root_dir": root})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}"
    print(f"llmlab Studio: {url}  （索引ルート: {root} / Ctrl+C で終了）")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def launch_app(port: int = DEFAULT_PORT, root: str = DEFAULT_ROOT) -> str:
    """ノートブックからバックグラウンドで起動して URL を返す。"""
    handler = type("Handler", (_Handler,), {"root_dir": root})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"llmlab Studio を起動しました: {url}\n"
          "（ブラウザで開いてください。接続設定は UI 右上の CONNECT から。"
          "ノートブックで configure 済みならそのまま使えます）")
    return url


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description="llmlab Studio（索引横断のワンストップUI）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--root", default=DEFAULT_ROOT, help="索引を探すルートフォルダ")
    ap.add_argument("--open", action="store_true", help="起動時にブラウザを開く")
    args = ap.parse_args(argv)
    serve(port=args.port, root=args.root, open_browser=args.open)


if __name__ == "__main__":
    main()
