"""Copilot Bridge サーバ（OpenAI 互換 API）。標準ライブラリのみ。

  python server.py                 # config.json があれば読む。既定 http://127.0.0.1:8800
  python server.py --backend fake  # ブラウザ無しで動作確認
  python server.py --windows 3     # Edge を 3 ウインドウ開く

エンドポイント:
  POST /v1/chat/completions   OpenAI Chat Completions 形式（tools / stream 対応）
  GET  /v1/models             モデル一覧（model_name 1 件）
  GET  /status                ウインドウと会話の状態
  GET  /health
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows のコンソール（cp932 / cp1252）で出せない文字があっても落ちないようにする
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

from bridge import __version__  # noqa: E402
from bridge.config import load_config  # noqa: E402
from bridge.pool import BridgeError, WorkerPool, WorkerRestarted  # noqa: E402
from bridge.protocol import normalize_message, parse_reply, render_delta, render_opening  # noqa: E402
from bridge.session import SessionStore  # noqa: E402


class BadRequest(ValueError):
    """要求の形が不正（HTTP 400）。"""


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = SessionStore()
        if cfg.backend == "fake":
            from bridge.fake import FakeWorker as W
        else:
            from bridge.edge import EdgeWorker as W
        self.pool = WorkerPool([W(i, cfg) for i in range(max(1, int(cfg.windows)))])
        self.started = time.time()
        self._log_lock = threading.Lock()

    def start(self) -> None:
        self.pool.start_all()

    def close(self) -> None:
        self.pool.close_all()

    # ------------------------------------------------------------ 1 要求の処理
    def complete(self, body: dict) -> dict:
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise BadRequest("messages が空です")
        tools = body.get("tools") or []
        session, norm, delta = self.store.resolve(messages)
        with session.lock:
            worker = self.pool.acquire(session.id, self.cfg.acquire_timeout_sec)
            detach = False
            try:
                # 差分が無い／担当が別の会話に使われた後／まだ何も送っていない → 会話を作り直す
                fresh = not delta or not worker.resumed or not session.sent
                if fresh:
                    worker.new_chat()
                    text = render_opening(norm, tools,
                                          max_tool_result_chars=self.cfg.max_tool_result_chars,
                                          max_chars=self.cfg.max_resend_chars)
                else:
                    text = render_delta(delta, max_tool_result_chars=self.cfg.max_tool_result_chars)
                t0 = time.time()
                try:
                    try:
                        reply = worker.send(text)
                    except WorkerRestarted as e:
                        # ブラウザが再起動された → 会話を作り直して 1 回だけやり直す
                        print(f"{e} → 会話を作り直してやり直します", flush=True)
                        fresh = True
                        worker.new_chat()
                        text = render_opening(norm, tools,
                                              max_tool_result_chars=self.cfg.max_tool_result_chars,
                                              max_chars=self.cfg.max_resend_chars)
                        reply = worker.send(text)
                except BridgeError as e:
                    print(f"{e}", flush=True)
                    detach = True
                    raise
                content, calls = parse_reply(reply, id_prefix=f"call_{session.id}_{session.turns + 1}")
                assistant = {"role": "assistant", "content": content or None}
                if calls:
                    assistant["tool_calls"] = calls
                session.sent = norm + [normalize_message(assistant)]
                session.worker_id = worker.id
                session.turns += 1
                self._log({"session": session.id, "worker": worker.id, "fresh": fresh,
                           "sent": text, "reply": reply, "latency_sec": round(time.time() - t0, 1)})
            finally:
                self.pool.release(worker, detach=detach)
        return self._completion(body, assistant, calls)

    def _completion(self, body: dict, assistant: dict, calls: list) -> dict:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or self.cfg.model_name,
            "choices": [{"index": 0, "message": assistant,
                         "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def _log(self, rec: dict) -> None:
        if not self.cfg.log_dir:
            return
        rec["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        path = Path(self.cfg.log_dir) / (time.strftime("%Y%m%d") + ".jsonl")
        with self._log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def status(self) -> dict:
        return {"version": __version__, "backend": self.cfg.backend, "model": self.cfg.model_name,
                "uptime_sec": round(time.time() - self.started),
                "windows": self.pool.describe(),
                "sessions": [s.to_dict() for s in self.store.all()]}


# ---------------------------------------------------------------- HTTP

def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: D401
            sys.stderr.write("%s - %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def _json(self, code: int, obj) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, code: int, message: str, kind: str = "server_error") -> None:
            self._json(code, {"error": {"message": message, "type": kind, "code": code}})

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/"):
                self._json(200, {"ok": True, "version": __version__})
            elif path == "/v1/models":
                self._json(200, {"object": "list", "data": [
                    {"id": bridge.cfg.model_name, "object": "model", "created": int(bridge.started),
                     "owned_by": "copilot-bridge"}]})
            elif path == "/status":
                self._json(200, bridge.status())
            else:
                self._error(404, f"not found: {path}", "invalid_request_error")

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in ("/v1/chat/completions", "/chat/completions"):
                return self._error(404, f"not found: {path}", "invalid_request_error")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, f"JSON を読めません: {e}", "invalid_request_error")
            try:
                result = bridge.complete(body)
            except BadRequest as e:
                return self._error(400, str(e), "invalid_request_error")
            except BridgeError as e:
                return self._error(503, str(e))
            except Exception as e:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                return self._error(500, f"{type(e).__name__}: {e}")
            if body.get("stream"):
                return self._stream(result)
            self._json(200, result)

        def _stream(self, result: dict) -> None:
            """回答が出そろってから SSE で流す（途中経過は無い）。"""
            msg = result["choices"][0]["message"]
            base = {"id": result["id"], "object": "chat.completion.chunk",
                    "created": result["created"], "model": result["model"]}
            chunks = [{"role": "assistant", "content": ""}]
            if msg.get("content"):
                chunks.append({"content": msg["content"]})
            if msg.get("tool_calls"):
                chunks.append({"tool_calls": [dict(tc, index=i) for i, tc in enumerate(msg["tool_calls"])]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for delta in chunks:
                self._sse(dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]))
            self._sse(dict(base, choices=[{"index": 0, "delta": {},
                                           "finish_reason": result["choices"][0]["finish_reason"]}],
                           usage=result["usage"]))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def _sse(self, obj: dict) -> None:
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

    return Handler


def serve(cfg, *, ready=None):
    bridge = Bridge(cfg)
    bridge.start()
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(bridge))
    httpd.daemon_threads = True
    if ready is not None:
        ready(httpd, bridge)
    print(f"Copilot Bridge v{__version__}  http://{cfg.host}:{httpd.server_address[1]}/v1  "
          f"backend={cfg.backend} windows={len(bridge.pool.workers)}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        bridge.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Copilot Bridge: M365 Copilot を OpenAI 互換 API にする")
    p.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.json"))
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--backend", choices=["edge", "fake"])
    p.add_argument("--windows", type=int)
    p.add_argument("--driver-path", dest="driver_path")
    p.add_argument("--chat-url", dest="chat_url")
    p.add_argument("--profile-mode", dest="profile_mode",
                   choices=["auto", "separate", "temp", "bare", "manager"])
    p.add_argument("--input-mode", dest="input_mode", choices=["auto", "js", "paste", "keys", "cdp"])
    p.add_argument("--log-dir", dest="log_dir")
    a = p.parse_args(argv)
    cfg = load_config(a.config, {k: v for k, v in vars(a).items() if k != "config"})
    serve(cfg)


if __name__ == "__main__":
    main()
