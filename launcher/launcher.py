#!/usr/bin/env python3
"""App Portal — for_eigyo / claudecode のアプリを呼び出す窓口.

    python launcher/launcher.py              # http://127.0.0.1:8770
    python launcher/launcher.py --port 9200

- 標準ライブラリのみ。127.0.0.1 にのみ bind し外部公開しない
- アプリの登録は同じフォルダの apps.json（UI の「＋ アプリを追加」からも可）
- 起動はサブプロセスで行い、ポートが応答するまで監視して UI に配信する
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
APPS_FILE = BASE / "apps.json"
UI_FILE = BASE / "index.html"
DEFAULT_PORT = 8770
HOST = "127.0.0.1"
LOG_LINES = 200

_lock = threading.Lock()
_procs: dict[str, dict] = {}  # app_id -> {proc, log(deque), started(float)}


# ---------------------------------------------------------------- apps.json

def load_apps() -> list[dict]:
    try:
        data = json.loads(APPS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    apps = data.get("apps", [])
    for app in apps:
        app.setdefault("icon", "terminal")
        app.setdefault("description", "")
    return apps


def save_apps(apps: list[dict]) -> None:
    APPS_FILE.write_text(
        json.dumps({"apps": apps}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def find_app(app_id: str) -> dict | None:
    for app in load_apps():
        if app.get("id") == app_id:
            return app
    return None


# ---------------------------------------------------------------- 状態判定

def port_open(url_or_port) -> bool:
    """URL または ポート番号の待ち受けを確認する。"""
    host, port = HOST, None
    if isinstance(url_or_port, int):
        port = url_or_port
    else:
        u = urlparse(str(url_or_port))
        host = u.hostname or HOST
        port = u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def app_state(app: dict) -> str:
    """link / running / starting / stopped"""
    if not app.get("command"):
        return "link"
    probe = app.get("wait_port") or app.get("url")
    if probe and port_open(probe):
        return "running"
    with _lock:
        entry = _procs.get(app["id"])
    if entry and entry["proc"].poll() is None:
        return "starting"
    return "stopped"


def log_tail(app_id: str, n: int = 30) -> list[str]:
    with _lock:
        entry = _procs.get(app_id)
        if not entry:
            return []
        return list(entry["log"])[-n:]


# ---------------------------------------------------------------- 起動 / 停止

def _reader(app_id: str, proc: subprocess.Popen) -> None:
    for line in iter(proc.stdout.readline, b""):
        with _lock:
            entry = _procs.get(app_id)
            if entry:
                entry["log"].append(line.decode("utf-8", "replace").rstrip())
    proc.stdout.close()


def launch_app(app: dict) -> dict:
    state = app_state(app)
    if state in ("running", "link"):
        return {"ok": True, "state": state}

    with _lock:
        entry = _procs.get(app["id"])
        if entry and entry["proc"].poll() is None:
            return {"ok": True, "state": "starting"}

    cmd = [c.replace("{python}", sys.executable) for c in app["command"]]
    cwd = (BASE / app.get("cwd", ".")).resolve()
    if not cwd.exists():
        return {"ok": False, "error": f"作業フォルダが見つかりません: {cwd}"}
    env = {**os.environ, **{k: str(v) for k, v in app.get("env", {}).items()}}

    kwargs: dict = dict(
        cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # Windows
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError as e:
        return {"ok": False, "error": f"コマンドが見つかりません: {e}"}
    except OSError as e:
        return {"ok": False, "error": str(e)}

    with _lock:
        _procs[app["id"]] = {
            "proc": proc,
            "log": deque(maxlen=LOG_LINES),
            "started": time.time(),
        }
    threading.Thread(target=_reader, args=(app["id"], proc), daemon=True).start()
    return {"ok": True, "state": "starting"}


def stop_app(app: dict) -> dict:
    with _lock:
        entry = _procs.get(app["id"])
    if not entry or entry["proc"].poll() is not None:
        if app_state(app) == "running":
            return {"ok": False,
                    "error": "このポータル以外で起動されたプロセスのため停止できません"}
        return {"ok": True, "state": "stopped"}
    proc = entry["proc"]
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        proc.kill()
    return {"ok": True, "state": "stopped"}


# ---------------------------------------------------------------- HTTP

def app_payload(app: dict) -> dict:
    return {
        "id": app.get("id"),
        "name": app.get("name"),
        "description": app.get("description", ""),
        "icon": app.get("icon", "terminal"),
        "url": app.get("url"),
        "cwd": app.get("cwd"),
        "command": app.get("command"),
        "state": app_state(app),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静かに
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = UI_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/apps":
            self._json({"apps": [app_payload(a) for a in load_apps()]})
            return
        if u.path == "/api/status":
            app_id = (parse_qs(u.query).get("id") or [""])[0]
            app = find_app(app_id)
            if not app:
                self._json({"error": "unknown app"}, 404)
                return
            with _lock:
                entry = _procs.get(app_id)
                started = entry["started"] if entry else None
            self._json({
                "state": app_state(app),
                "elapsed": round(time.time() - started, 1) if started else None,
                "log": log_tail(app_id),
            })
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        app_id = (parse_qs(u.query).get("id") or [""])[0]

        if u.path in ("/api/launch", "/api/stop"):
            app = find_app(app_id)
            if not app:
                self._json({"error": "unknown app"}, 404)
                return
            result = launch_app(app) if u.path == "/api/launch" else stop_app(app)
            code = 200 if result.get("ok") else 400
            self._json(result, code)
            return

        if u.path == "/api/apps":  # アプリの追加登録
            body = self._read_body()
            name = (body.get("name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "名前は必須です"}, 400)
                return
            apps = load_apps()
            base_id = "".join(
                c if c.isalnum() else "-" for c in name.lower()
            ).strip("-") or "app"
            new_id, n = base_id, 2
            while any(a.get("id") == new_id for a in apps):
                new_id, n = f"{base_id}-{n}", n + 1
            app = {"id": new_id, "name": name,
                   "description": (body.get("description") or "").strip(),
                   "icon": body.get("icon") or "terminal"}
            if body.get("url"):
                app["url"] = body["url"].strip()
            command = (body.get("command") or "").split()
            if command:
                app["command"] = command
                app["cwd"] = (body.get("cwd") or ".").strip() or "."
                try:
                    app["wait_port"] = int(body.get("port"))
                except (TypeError, ValueError):
                    pass
            if not app.get("url") and not app.get("command"):
                self._json({"ok": False,
                            "error": "URL か起動コマンドのどちらかは必須です"}, 400)
                return
            apps.append(app)
            save_apps(apps)
            self._json({"ok": True, "app": app_payload(app)})
            return

        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path == "/api/apps":
            app_id = (parse_qs(u.query).get("id") or [""])[0]
            apps = load_apps()
            remain = [a for a in apps if a.get("id") != app_id]
            if len(remain) == len(apps):
                self._json({"error": "unknown app"}, 404)
                return
            save_apps(remain)
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, 404)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="App Portal — アプリ呼び出しの窓口")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="起動時にブラウザを開く")
    args = ap.parse_args(argv)

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}"
    print(f"App Portal: {url}  (Ctrl+C で終了)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止します")
        for app in load_apps():
            stop_app(app)


if __name__ == "__main__":
    main()
