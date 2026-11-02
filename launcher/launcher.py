#!/usr/bin/env python3
"""App Portal — for_eigyo / claudecode のアプリを呼び出す窓口.

    python launcher/launcher.py              # http://127.0.0.1:8770
    python launcher/launcher.py --port 9200

- 標準ライブラリのみ。127.0.0.1 にのみ bind し外部公開しない
- アプリの登録は同じフォルダの apps.json（UI の「＋ アプリを追加」からも可）
- 起動はサブプロセスで行い、ポートが応答するまで監視して UI に配信する
- カードの並び・グループ・非表示は layout.json（git 管理外）に保存する
- 各アプリのバージョンはパッケージフォルダから読み取る（detect_version）
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

__version__ = "1.1.0"

BASE = Path(__file__).resolve().parent
APPS_FILE = BASE / "apps.json"
LAYOUT_FILE = BASE / "layout.json"
UI_FILE = BASE / "index.html"
PREVIEW_DIR = BASE / "previews"
REPO_ROOT = BASE.parent
PROGRESS_DIR = REPO_ROOT / "progress" / "projects"
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


def app_cwd(app: dict) -> Path:
    return (BASE / app.get("cwd", ".")).resolve()


# ---------------------------------------------------------------- layout.json

MAIN_GROUP = "_main"
_GROUP_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_GROUPS = 50
MAX_IDS = 1000


def clean_layout(data) -> dict:
    """UI から来た配置を検証して正規化する（壊れた値は捨てる）。"""
    if not isinstance(data, dict):
        data = {}
    groups, seen_groups, seen_apps = [], set(), set()
    for g in data.get("groups") or []:
        if not isinstance(g, dict) or len(groups) >= MAX_GROUPS:
            continue
        gid = str(g.get("id") or "")
        if not _GROUP_ID.match(gid) or gid in seen_groups:
            continue
        seen_groups.add(gid)
        ids = []
        for app_id in g.get("apps") or []:
            if isinstance(app_id, str) and app_id and app_id not in seen_apps \
                    and len(seen_apps) < MAX_IDS:
                seen_apps.add(app_id)
                ids.append(app_id[:120])
        name = str(g.get("name") or "").strip()[:40]
        groups.append({"id": gid, "name": name, "apps": ids})
    hidden = []
    for app_id in data.get("hidden") or []:
        if isinstance(app_id, str) and app_id and app_id not in hidden \
                and len(hidden) < MAX_IDS:
            hidden.append(app_id[:120])
    return {"groups": groups, "hidden": hidden}


def load_layout() -> dict:
    try:
        return clean_layout(json.loads(LAYOUT_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return clean_layout({})


def save_layout(data) -> dict:
    layout = clean_layout(data)
    tmp = LAYOUT_FILE.with_name(LAYOUT_FILE.name + ".tmp")
    tmp.write_text(json.dumps(layout, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, LAYOUT_FILE)
    return layout


# ---------------------------------------------------------------- バージョン

VERSION_TTL = 60.0
_READ_LIMIT = 512_000
_VERSION_TEXT = re.compile(r"^v?\d+(\.\d+)*([.+-][0-9A-Za-z.+-]+)?$")
# 優先順に試す（__version__ を最優先）
_PY_PATTERNS = [
    re.compile(r"""^\s*__version__\s*(?::\s*str\s*)?=\s*['"]([^'"\s]+)['"]""", re.M),
    re.compile(r"""^\s*APP_VERSION\s*(?::\s*str\s*)?=\s*['"]([^'"\s]+)['"]""", re.M),
    re.compile(r"""^\s*VERSION\s*(?::\s*str\s*)?=\s*['"]([^'"\s]+)['"]""", re.M),
    re.compile(r"""FastAPI\([^)]*?\bversion\s*=\s*['"]([^'"\s]+)['"]""", re.S),
]
_META_FILES = ("pyproject.toml", "setup.cfg", "setup.py", "package.json",
               "VERSION", "VERSION.txt", "version.txt")
_ENTRY_FILES = ("server.py", "run.py", "app.py", "main.py", "app/main.py")
_SKIP_DIRS = ("test", "tests", "docs", "build", "dist", "node_modules", "static")

_version_lock = threading.Lock()
_version_cache: dict[str, tuple[float, dict]] = {}


def _read_text(path: Path) -> str | None:
    try:
        if path.is_file() and path.stat().st_size <= _READ_LIMIT:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return None


def _valid(version) -> str | None:
    version = str(version or "").strip()
    return version if _VERSION_TEXT.match(version) else None


def _section_value(text: str, sections: tuple[str, ...], key: str = "version") -> str | None:
    """TOML / INI の [section] 内の key = "..." を拾う（tomllib 不要の簡易版）。"""
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        header = re.match(r"^\[([^\]]+)\]\s*$", stripped)
        if header:
            current = header.group(1).strip()
            continue
        if current in sections:
            m = re.match(rf"""^{key}\s*[=:]\s*['"]?([^'"\s#]+)['"]?""", stripped)
            if m:
                return m.group(1)
    return None


def version_in_file(path: Path) -> str | None:
    """1つのファイルからバージョン文字列を読み取る。見つからなければ None。"""
    text = _read_text(path)
    if text is None:
        return None
    name = path.name.lower()
    if name == "pyproject.toml":
        return _valid(_section_value(text, ("project", "tool.poetry")))
    if name == "setup.cfg":
        return _valid(_section_value(text, ("metadata",)))
    if name == "package.json":
        try:
            return _valid(json.loads(text).get("version"))
        except (ValueError, AttributeError):
            return None
    if name in ("version", "version.txt"):
        return _valid(text.strip().splitlines()[0] if text.strip() else "")
    if name == "setup.py":
        m = re.search(r"""\bversion\s*=\s*['"]([^'"\s]+)['"]""", text)
        return _valid(m.group(1)) if m else None
    for pattern in _PY_PATTERNS:
        m = pattern.search(text)
        if m and _valid(m.group(1)):
            return m.group(1)
    return None


def _command_packages(command) -> list[str]:
    """`-m pkg.mod` や `-c "from pkg import ..."` から起動パッケージ名を取り出す。"""
    names: list[str] = []
    command = list(command or [])
    for i, part in enumerate(command):
        nxt = command[i + 1] if i + 1 < len(command) else ""
        if part == "-m" and nxt:
            names.append(nxt.split(".")[0])
        elif part == "-c" and nxt:
            names += re.findall(r"(?:^|[\s;])(?:from|import)\s+([A-Za-z_]\w*)", nxt)
    return [n for n in dict.fromkeys(names)]


def _package_files(package_dir: Path) -> list[Path]:
    return [package_dir / "__init__.py", package_dir / "_version.py",
            package_dir / "version.py"]


def _candidates(app: dict, cwd: Path):
    """(ファイル, パッケージフォルダ) を優先順に返す。"""
    if app.get("version_from"):
        target = Path(app["version_from"])
        yield (target if target.is_absolute() else cwd / target), None
    # 1) 起動コマンドのパッケージ（claudecode のように cwd がリポジトリ直下でも正しく当たる）
    for name in _command_packages(app.get("command")):
        for root in (cwd, cwd / "src"):
            pkg = root / name
            if (pkg / "__init__.py").is_file():
                for f in _package_files(pkg):
                    yield f, pkg
    # 2) フォルダのパッケージ情報
    for name in _META_FILES:
        yield cwd / name, None
    # 3) フォルダ直下・src 直下のパッケージ
    for root in (cwd, cwd / "src"):
        try:
            dirs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for pkg in dirs:
            if pkg.name.startswith((".", "_")) or pkg.name.lower() in _SKIP_DIRS:
                continue
            if (pkg / "__init__.py").is_file():
                for f in _package_files(pkg):
                    yield f, pkg
    # 4) 起動スクリプト・よくある入口ファイル
    scripts = [p for p in (app.get("command") or []) if str(p).endswith(".py")]
    for name in [*scripts, *_ENTRY_FILES]:
        yield cwd / name, None


def _progress_version(cwd: Path) -> tuple[str | None, Path | None]:
    """for_eigyo の progress/projects/<フォルダ名>.json の version（最後の手段）。"""
    try:
        # 両方を resolve() してから比べる（Windows の 8.3 短縮名やジャンクションで表記が食い違うため）
        slug = cwd.resolve().relative_to(REPO_ROOT.resolve()).parts[0]
    except (ValueError, IndexError, OSError):
        return None, None
    path = PROGRESS_DIR / f"{slug}.json"
    try:
        version = _valid(json.loads(path.read_text(encoding="utf-8")).get("version"))
    except (OSError, ValueError, AttributeError):
        return None, None
    return (version, path) if version else (None, None)


def git_last_updated(directory: Path) -> str | None:
    """そのフォルダを最後に変更したコミットの日付（YYYY-MM-DD）。git が無ければ None。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(directory), "log", "-1", "--format=%cd",
             "--date=short", "--", "."],
            capture_output=True, text=True, timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    date = out.stdout.strip()
    return date if out.returncode == 0 and re.match(r"^\d{4}-\d{2}-\d{2}$", date) else None


def _display_path(path: Path) -> str:
    """for_eigyo/... や claudecode/... のように親フォルダからの相対で見せる。"""
    try:
        return path.resolve().relative_to(REPO_ROOT.parent.resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def detect_version(app: dict) -> dict:
    """{"version", "source", "updated"}。読み取れない項目は None。"""
    if not app.get("command") and not app.get("cwd"):
        return {"version": None, "source": None, "updated": None}
    cwd = app_cwd(app)
    if not cwd.is_dir():
        return {"version": None, "source": None, "updated": None}
    version = source = package_dir = None
    for path, pkg in _candidates(app, cwd):
        found = version_in_file(path)
        if found:
            version, source, package_dir = found, path, pkg
            break
    if not version:
        version, source = _progress_version(cwd)
    # cwd がリポジトリ直下（claudecode）ならパッケージ単位、そうでなければアプリのフォルダ単位で日付を取る
    date_dir = package_dir if (package_dir and (cwd / ".git").exists()) else cwd
    return {
        "version": version,
        "source": _display_path(source) if source else None,
        "updated": git_last_updated(date_dir),
    }


def app_version(app: dict, fresh: bool = False) -> dict:
    key = json.dumps([app.get("id"), app.get("cwd"), app.get("command"),
                      app.get("version_from")], ensure_ascii=False)
    now = time.monotonic()
    with _version_lock:
        hit = _version_cache.get(key)
        if hit and not fresh and now - hit[0] < VERSION_TTL:
            return hit[1]
    info = detect_version(app)
    with _version_lock:
        _version_cache[key] = (now, info)
    return info


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


def resolve_command(command: list[str], cwd: Path) -> list[str]:
    """Resolve Python placeholders without invoking a shell or wrapper process."""
    command = [part.replace("{python}", sys.executable) for part in command]
    requires_venv = any("{venv_python}" in part for part in command)
    if requires_venv or any("{app_python}" in part for part in command):
        relative_python = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        venv_python = cwd / ".venv" / relative_python
        venv_exists = venv_python.is_file()
        if requires_venv and not venv_exists:
            raise FileNotFoundError(
                f"アプリ専用の Python が見つかりません: {venv_python}。"
                "アプリの README に従って .venv を作成し、依存パッケージを"
                "インストールしてから起動してください。"
            )
        app_python = str(venv_python) if venv_exists else sys.executable
        command = [
            part.replace("{app_python}", app_python).replace("{venv_python}", str(venv_python))
            for part in command
        ]
    return command


def launch_app(app: dict) -> dict:
    state = app_state(app)
    if state in ("running", "link"):
        return {"ok": True, "state": state}

    with _lock:
        entry = _procs.get(app["id"])
        if entry and entry["proc"].poll() is None:
            return {"ok": True, "state": "starting"}

    cwd = (BASE / app.get("cwd", ".")).resolve()
    if not cwd.exists():
        return {"ok": False, "error": f"作業フォルダが見つかりません: {cwd}"}
    try:
        cmd = resolve_command(app["command"], cwd)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
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

def app_payload(app: dict, fresh: bool = False) -> dict:
    return {
        "id": app.get("id"),
        "name": app.get("name"),
        "description": app.get("description", ""),
        "icon": app.get("icon", "terminal"),
        "url": app.get("url"),
        "cwd": app.get("cwd"),
        "command": app.get("command"),
        "previews": app_previews(app),
        "version": app_version(app, fresh=fresh),
        "state": app_state(app),
    }


def app_previews(app: dict) -> list[str]:
    """apps.json の previews 指定、なければ previews/<id>-*.jpg|png を自動検出。"""
    if app.get("previews"):
        return list(app["previews"])
    found = []
    if PREVIEW_DIR.is_dir():
        for f in sorted(PREVIEW_DIR.iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp") and (
                f.stem == app["id"] or f.stem.startswith(app["id"] + "-")
            ):
                found.append(f"/previews/{f.name}")
    return found


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
            fresh = (parse_qs(u.query).get("fresh") or [""])[0] == "1"
            self._json({
                "apps": [app_payload(a, fresh=fresh) for a in load_apps()],
                "layout": load_layout(),
                "portal": {"version": __version__},
            })
            return
        if u.path == "/api/layout":
            self._json(load_layout())
            return
        if u.path.startswith("/previews/"):
            name = Path(u.path).name  # パス区切りを落として previews/ 直下に限定
            f = PREVIEW_DIR / name
            if not f.is_file():
                self._json({"error": "not found"}, 404)
                return
            ctype = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp",
            }.get(f.suffix.lower(), "application/octet-stream")
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=60")
            self.end_headers()
            self.wfile.write(body)
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

        if u.path == "/api/layout":  # カードの並び・グループ・非表示を保存
            # application/json を必須にして、他サイトからの単純フォーム送信では書き換えられないようにする
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._json({"ok": False, "error": "Content-Type は application/json"}, 415)
                return
            try:
                layout = save_layout(self._read_body())
            except OSError as e:
                self._json({"ok": False, "error": f"保存できませんでした: {e}"}, 500)
                return
            self._json({"ok": True, "layout": layout})
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
            version_from = (body.get("version_from") or "").strip()
            if version_from:
                app["version_from"] = version_from
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
    print(f"App Portal v{__version__}: {url}  (Ctrl+C で終了)")
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
