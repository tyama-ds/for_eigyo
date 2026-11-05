#!/usr/bin/env python3
"""Doc Vault — 文書管理 + RAG 相談アプリ。

    python doc-vault/server.py                 # http://127.0.0.1:8760
    python doc-vault/server.py --port 9600 --open
    python doc-vault/server.py --host 0.0.0.0 --token <合言葉>   # 共有（LAN 公開）時

- 標準ライブラリだけで起動する（PDF は pypdf、Claude は anthropic を pip で追加）
- 既定は 127.0.0.1 にのみ bind。127.0.0.1 以外で待ち受けるときはアクセストークン必須
- 取り込み（抽出 → BM25 → 埋め込み → グラフ → 要約）はバックグラウンドで順に処理
- 相談の回答は Server-Sent Events でストリーミング
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import sys
import threading
import urllib.request
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from docvault import __version__, config, db, graph, rag  # noqa: E402
from docvault.extract import SUPPORTED  # noqa: E402
from docvault.indexer import ALL_STAGES, INDEXER, register_upload  # noqa: E402
from docvault.llm import LLM, LLMError  # noqa: E402
from docvault.retrieval import INDEX, load_chunks  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8760
STATIC = BASE / "static"
MAX_UPLOAD = 200 * 1024 * 1024
TOKEN = ""                     # 空ならトークン不要（127.0.0.1 のみで待ち受け）
STRATEGIES = ("auto", "decision", "classic")   # 検索方式（自動 / DecisionRAG のみ / 従来型のみ）

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


# ---------------------------------------------------------------- API 実装

def api_status() -> dict:
    cfg = config.load_config()
    docs = db.list_docs()
    counts: dict[str, int] = {}
    for d in docs:
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    return {"version": __version__, "docs": len(docs), "by_status": counts,
            "indexer": INDEXER.status(),
            "profiles": {n: {"chat": config.chat_ready(cfg, n), "embed": config.embed_ready(cfg, n),
                             "provider": cfg["profiles"][n]["provider"],
                             "model": cfg["profiles"][n]["model"]}
                         for n in config.PROFILE_NAMES},
            "chat_mode": cfg["policy"]["chat_mode"]}


def api_config_test(body: dict) -> dict:
    name = body.get("profile")
    if name not in config.PROFILE_NAMES:
        return {"error": "profile は local / cloud"}
    cfg = config.load_config()
    llm = LLM(cfg)
    sens = "public" if name == "cloud" else "internal"
    out: dict = {}
    try:
        text = llm.chat(name, "「接続OK」とだけ返答してください。", sensitivity=sens,
                        max_tokens=1024, purpose="router")
        out["chat"] = {"ok": bool(text), "message": (text or "応答が空でした（Max Tokens を増やしてください）")[:120]}
    except LLMError as e:
        out["chat"] = {"ok": False, "message": str(e)}
    if config.embed_ready(cfg, name):
        try:
            vec = llm.embed(name, ["接続テスト"], sensitivity=sens)[0]
            out["embed"] = {"ok": True, "message": f"次元数 {len(vec)}"}
        except LLMError as e:
            out["embed"] = {"ok": False, "message": str(e)}
    else:
        out["embed"] = {"ok": False, "message": "埋め込みモデル未設定（BM25 とグラフで検索します）"}
    return out


def doc_public(d: dict) -> dict:
    out = {k: v for k, v in d.items() if k not in ("sha256",)}
    out["flags"] = {k: v for k, v in (d.get("flags") or {}).items() if k != "extract_warnings"}
    return out


def api_docs(qs: dict) -> dict:
    folder = (qs.get("folder") or [""])[0].strip("/")
    q = (qs.get("q") or [""])[0].strip().lower()
    docs = []
    for d in db.list_docs():
        if folder and not (d["folder"] == folder or d["folder"].startswith(folder + "/")):
            continue
        if q and q not in (d["title"] + " " + d["filename"] + " " + " ".join(d["tags"])).lower():
            continue
        docs.append(doc_public(d))
    folders: dict[str, int] = {}
    for d in db.list_docs():
        f = d["folder"]
        while True:
            folders[f] = folders.get(f, 0) + 1
            if not f:
                break
            f = f.rsplit("/", 1)[0] if "/" in f else ""
    tags = sorted({t for d in db.list_docs() for t in d["tags"]})
    return {"docs": docs, "folders": [{"path": k, "count": v} for k, v in sorted(folders.items())],
            "tags": tags, "supported": sorted(SUPPORTED)}


def api_doc_update(doc_id: str, body: dict) -> dict:
    doc = db.get_doc(doc_id)
    if not doc:
        return {"error": "文書が見つかりません"}
    fields: dict = {}
    if isinstance(body.get("title"), str) and body["title"].strip():
        fields["title"] = body["title"].strip()[:200]
    if isinstance(body.get("folder"), str):
        fields["folder"] = "/".join(p.strip() for p in body["folder"].split("/") if p.strip())[:200]
    if isinstance(body.get("tags"), list):
        fields["tags"] = [str(t).strip()[:30] for t in body["tags"] if str(t).strip()][:20]
    reprocess = False
    if body.get("sensitivity") in ("internal", "public") and body["sensitivity"] != doc["sensitivity"]:
        fields["sensitivity"] = body["sensitivity"]
        reprocess = True
    db.update_doc(doc_id, **fields)
    if "title" in fields:
        INDEX.add_doc(doc_id)             # 検索用テキストにタイトルを含むため
    db.bump_revision()
    if reprocess:
        # 区分が変わったら埋め込み空間を区分に合わせて作り直す
        INDEXER.enqueue(doc_id, ("embed",))
    return {"doc": doc_public(db.get_doc(doc_id))}


def api_doc_delete(doc_id: str) -> dict:
    doc = db.get_doc(doc_id)
    if not doc:
        return {"error": "文書が見つかりません"}
    INDEX.remove_doc(doc_id)
    db.delete_doc_data(doc_id)
    try:
        (db.files_dir() / f"{doc_id}{doc['ext']}").unlink()
    except OSError:
        pass
    return {"ok": True}


def api_search(body: dict) -> dict:
    q = str(body.get("q") or "").strip()[:500]
    if not q:
        return {"results": []}
    cfg = config.load_config()
    pseudo = {"mode": cfg["policy"]["chat_mode"], "scope": {"folder": body.get("folder") or ""},
              "touched_internal": False}
    ctx = rag.Ctx(pseudo, cfg)
    # 文書検索は LLM に本文を送らないので、区分に関係なく全文書が対象
    allowed, _ = rag.allowed_docs(pseudo, "local")
    hits = INDEX.hybrid(q, allowed_docs=allowed, k=60, qvecs=rag._qvecs(ctx, q, allowed))
    chunks = load_chunks([cid for cid, _ in hits])
    grouped: dict[str, dict] = {}
    for cid, score in hits:
        ch = chunks.get(cid)
        if not ch:
            continue
        g = grouped.setdefault(ch["doc_id"], {"doc_id": ch["doc_id"], "title": ch["doc_title"],
                                              "ext": ch["ext"], "sensitivity": ch["sensitivity"],
                                              "score": 0.0, "hits": []})
        g["score"] += score
        if len(g["hits"]) < 3:
            g["hits"].append({"chunk_id": cid, "locator": ch["locator"], "page": ch["page"],
                              "snippet": _snippet(ch["text"], q)})
    results = sorted(grouped.values(), key=lambda g: -g["score"])[:30]
    return {"results": results, "warnings": ctx.warnings}


def _snippet(text: str, q: str, width: int = 160) -> str:
    from docvault.textutil import tokenize
    low = text.lower()
    pos = -1
    for tok in sorted(set(tokenize(q)), key=len, reverse=True):
        pos = low.find(tok)
        if pos != -1:
            break
    start = max(0, pos - width // 3) if pos != -1 else 0
    s = text[start:start + width].replace("\n", " ")
    return ("…" if start else "") + s + ("…" if start + width < len(text) else "")


def api_graph(qs: dict) -> dict:
    try:
        limit = max(20, min(int((qs.get("limit") or ["300"])[0]), 2000))
    except ValueError:
        limit = 300
    doc_id = (qs.get("doc") or [""])[0]
    docs = db.list_docs()
    sens = {d["id"]: d["sensitivity"] for d in docs}
    allowed = {doc_id} if doc_id else {d["id"] for d in docs}
    g = graph.get_graph(allowed, sens)
    if not g.entities:
        cfg = config.load_config()
        hint = ("取り込み時のナレッジグラフ抽出には LLM が必要です（設定タブ）。設定後に"
                "文書の「再処理」を実行してください") if not cfg["rag"]["graph"] or not (
            config.chat_ready(cfg, "local") or config.chat_ready(cfg, "cloud")) \
            else "まだグラフがありません。文書を取り込むと、エンティティと関係が抽出されます"
        return {"error": hint, "nodes": [], "edges": [], "communities": []}
    return g.to_payload(limit=limit)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"DocVault/{__version__}"

    def handle(self) -> None:
        # ThreadingHTTPServer はリクエストごとにスレッドを作るため、スレッドの DB 接続を閉じる
        try:
            super().handle()
        finally:
            db.close()

    # -- 共通 ---------------------------------------------------------
    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        if self.headers.get("X-DocVault-Token") == TOKEN:
            return True
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        if "docvault_token" in cookie and cookie["docvault_token"].value == TOKEN:
            return True
        qs = parse_qs(urlparse(self.path).query)
        return (qs.get("token") or [""])[0] == TOKEN

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str, *, download_name: str = "",
              inline: bool = True) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            disp = "inline" if inline else "attachment"
            self.send_header("Content-Disposition",
                             f"{disp}; filename*=UTF-8''{quote(download_name)}")
        if TOKEN and "token=" in (self.path or ""):
            self.send_header("Set-Cookie", f"docvault_token={TOKEN}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4_000_000)
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def _sse(self, events) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for ev in events:
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001 - ストリーム中の例外はイベントで通知
            import traceback
            traceback.print_exc()
            try:
                self.wfile.write(("data: " + json.dumps({"type": "error", "message": f"内部エラー: {e}"},
                                                        ensure_ascii=False) + "\n\n").encode("utf-8"))
            except OSError:
                pass

    # -- GET ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        if route in ("/", "/index.html"):
            if not self._authorized():
                self._login_page()
                return
            self._file(STATIC / "index.html", STATIC_TYPES[".html"])
            return
        if route.startswith("/static/"):
            name = route[len("/static/"):]
            path = (STATIC / name).resolve()
            if STATIC.resolve() in path.parents and path.suffix in STATIC_TYPES:
                self._file(path, STATIC_TYPES[path.suffix])
            else:
                self.send_error(404)
            return
        if route == "/api/health":
            self._json({"ok": True, "app": "doc-vault", "version": __version__})
            return
        if not self._authorized():
            self._json({"error": "アクセストークンが必要です"}, 401)
            return
        parts = [unquote(p) for p in route.strip("/").split("/")]
        if route == "/api/status":
            self._json(api_status())
        elif route == "/api/config":
            self._json(config.public_config(config.load_config()))
        elif route == "/api/docs":
            self._json(api_docs(qs))
        elif len(parts) == 3 and parts[:2] == ["api", "docs"]:
            d = db.get_doc(parts[2])
            self._json({"doc": doc_public(d)} if d else {"error": "文書が見つかりません"},
                       200 if d else 404)
        elif len(parts) == 4 and parts[:2] == ["api", "docs"] and parts[3] == "chunks":
            d = db.get_doc(parts[2])
            if not d:
                self._json({"error": "文書が見つかりません"}, 404)
            else:
                self._json({"doc": doc_public(d), "chunks": db.doc_chunks(parts[2])})
        elif len(parts) == 4 and parts[:2] == ["api", "docs"] and parts[3] == "file":
            d = db.get_doc(parts[2])
            if not d:
                self.send_error(404)
                return
            ctype = mimetypes.types_map.get(d["ext"], "application/octet-stream")
            if d["ext"] in (".txt", ".md", ".csv", ".log", ".json"):
                ctype = "text/plain; charset=utf-8"
            self._file(db.files_dir() / f"{d['id']}{d['ext']}", ctype,
                       download_name=d["filename"],
                       inline=(qs.get("download") or ["0"])[0] != "1")
        elif route == "/api/graph":
            self._json(api_graph(qs))
        elif route == "/api/chats":
            self._json({"chats": db.list_chats()})
        elif len(parts) == 3 and parts[:2] == ["api", "chats"]:
            chat = db.get_chat(parts[2])
            if not chat:
                self._json({"error": "相談が見つかりません"}, 404)
            else:
                self._json({"chat": chat, "messages": db.chat_messages(parts[2])})
        else:
            self.send_error(404)

    # -- POST ---------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        if not self._authorized():
            self._json({"error": "アクセストークンが必要です"}, 401)
            return
        parts = [unquote(p) for p in route.strip("/").split("/")]
        if route == "/api/docs/upload":
            self._upload(parse_qs(parsed.query))
            return
        body = self._body_json()
        if route == "/api/config":
            self._json(config.public_config(config.save_config(body)))
        elif route == "/api/config/test":
            self._json(api_config_test(body))
        elif route == "/api/search":
            self._json(api_search(body))
        elif route == "/api/samples":
            from docvault.samples import sample_documents
            out = []
            for item in sample_documents():
                doc, created = register_upload(item["data"], item["filename"], folder=item["folder"],
                                               tags=item["tags"], sensitivity=item["sensitivity"])
                out.append({"id": doc["id"], "title": doc["title"], "created": created})
            self._json({"docs": out})
        elif route == "/api/docs/reindex-all":
            stages = tuple(s for s in (body.get("stages") or ALL_STAGES) if s in ALL_STAGES)
            for d in db.list_docs():
                INDEXER.enqueue(d["id"], stages or ALL_STAGES)
            self._json({"ok": True})
        elif len(parts) == 4 and parts[:2] == ["api", "docs"]:
            doc_id, action = parts[2], parts[3]
            if action == "update":
                r = api_doc_update(doc_id, body)
            elif action == "delete":
                r = api_doc_delete(doc_id)
            elif action == "reindex":
                if not db.get_doc(doc_id):
                    r = {"error": "文書が見つかりません"}
                else:
                    stages = tuple(s for s in (body.get("stages") or ALL_STAGES) if s in ALL_STAGES)
                    INDEXER.enqueue(doc_id, stages or ALL_STAGES)
                    r = {"ok": True}
            else:
                r = {"error": "不明な操作です"}
            self._json(r, 400 if "error" in r else 200)
        elif route == "/api/chats":
            mode = body.get("mode") if body.get("mode") in ("auto", "local", "cloud") \
                else config.load_config()["policy"]["chat_mode"]
            scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
            strategy = body.get("strategy") if body.get("strategy") in STRATEGIES else "auto"
            chat = db.create_chat(str(body.get("title") or ""), mode, _clean_scope(scope), strategy)
            beam = _beam(body.get("decision_beam"))
            if beam:
                db.update_chat(chat["id"], decision_beam=beam)
                chat = db.get_chat(chat["id"])
            self._json({"chat": chat})
        elif len(parts) == 4 and parts[:2] == ["api", "chats"]:
            chat_id, action = parts[2], parts[3]
            chat = db.get_chat(chat_id)
            if not chat:
                self._json({"error": "相談が見つかりません"}, 404)
            elif action == "ask":
                question = str(body.get("question") or "").strip()
                if not question:
                    self._json({"error": "質問が空です"}, 400)
                else:
                    self._sse(rag.ask(chat_id, question))
            elif action == "update":
                fields = {}
                if body.get("mode") in ("auto", "local", "cloud"):
                    fields["mode"] = body["mode"]
                if body.get("strategy") in STRATEGIES:
                    fields["strategy"] = body["strategy"]
                beam = _beam(body.get("decision_beam"))
                if beam is not None:
                    fields["decision_beam"] = beam
                if isinstance(body.get("scope"), dict):
                    fields["scope"] = _clean_scope(body["scope"])
                if isinstance(body.get("title"), str) and body["title"].strip():
                    fields["title"] = body["title"].strip()[:80]
                if fields:
                    db.update_chat(chat_id, **fields)
                self._json({"chat": db.get_chat(chat_id)})
            elif action == "delete":
                db.delete_chat(chat_id)
                self._json({"ok": True})
            else:
                self._json({"error": "不明な操作です"}, 400)
        else:
            self.send_error(404)

    def _upload(self, qs: dict) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        name = (qs.get("name") or [""])[0]
        if not name or length <= 0:
            self._json({"error": "name とファイル本体が必要です"}, 400)
            return
        if length > MAX_UPLOAD:
            self._json({"error": f"ファイルが大きすぎます（上限 {MAX_UPLOAD // 1024 // 1024}MB）"}, 413)
            return
        ext = Path(name.lower()).suffix
        if ext not in SUPPORTED:
            self.rfile.read(length)
            self._json({"error": f"未対応の形式です: {ext}"}, 400)
            return
        data = self.rfile.read(length)
        tags = [t for t in (qs.get("tags") or [""])[0].split(",") if t.strip()]
        doc, created = register_upload(
            data, name, folder=(qs.get("folder") or [""])[0], tags=tags,
            sensitivity=(qs.get("sensitivity") or [""])[0] or None,
            replace=(qs.get("replace") or ["0"])[0] == "1")
        self._json({"doc": doc_public(doc), "created": created})

    def _login_page(self) -> None:
        body = ("<!doctype html><meta charset=utf-8><title>Doc Vault</title>"
                "<body style='font-family:sans-serif;background:#0f1420;color:#e6ecf7;padding:40px'>"
                "<h2>Doc Vault</h2><p>アクセストークンを入力してください。</p>"
                "<form method=get action='/'><input name=token type=password autofocus "
                "style='padding:8px'> <button>開く</button></form>").encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("DOCVAULT_QUIET"):
            return
        sys.stderr.write("doc-vault: " + fmt % args + "\n")


def _beam(value) -> int | None:
    """DecisionRAG のビーム幅（各段で残す枝の数）。0 は「設定の値に従う」。不正なら None。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return None


def _clean_scope(scope: dict) -> dict:
    return {"folder": str(scope.get("folder") or "").strip("/")[:200],
            "doc_ids": [str(x) for x in (scope.get("doc_ids") or []) if str(x)][:50],
            "tags": [str(x) for x in (scope.get("tags") or []) if str(x)][:10]}


def startup() -> None:
    """起動時: 検索インデックスの読み込みと、中断された取り込みの再開。"""
    INDEX.ensure_loaded()
    for d in db.list_docs():
        if d["status"] in ("queued", "processing") or (d["status"] == "ready" and d["stage"]):
            INDEXER.enqueue(d["id"], ALL_STAGES if not d["chars"] else ("embed", "graph", "summary"))


class Server(ThreadingHTTPServer):
    # Windows の SO_REUSEADDR は、他のプロセスが待ち受け中のポートにも重ねて待ち受けることを許す。
    # start.bat を2回ダブルクリックすると2つ目も起動したように見え、2つの取り込み処理が同じデータを
    # 書き換えてしまうため、Windows では付けない（付けなくても、終了した直後の同じ番号で起動し直せる）
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def running_app(url: str, timeout: float = 2.0) -> str | None:
    """url で待ち受けている相手のアプリ名（/api/health の app）。応答が無い・別物なら None。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1 をプロキシに送らない
    try:
        with opener.open(url + "/api/health", timeout=timeout) as r:
            app = json.loads(r.read().decode("utf-8")).get("app")
    except (OSError, ValueError, AttributeError):
        return None
    return str(app) if app else None


def explain_busy_port(url: str, port: int, error: OSError, *, open_browser: bool) -> int:
    """待ち受けに失敗したとき: Doc Vault がもう動いていればそれを開いて 0、それ以外は理由を表示して 1。"""
    app = running_app(url)
    if app == "doc-vault":
        print(f"Doc Vault はすでに起動しています: {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    print(f"[エラー] ポート {port} で待ち受けられません（{error}）")
    if app:
        print(f"  別のアプリ（{app}）が使っています。")
    print(f"  別の番号で起動するには --port を付けてください（例: --port {port + 1}）")
    return 1


def _safe_stdio() -> None:
    # 出力をファイルやパイプに送ると、英語版 Windows では日本語を書けずに落ちるため、書けない文字は置き換える
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    global TOKEN
    _safe_stdio()
    parser = argparse.ArgumentParser(description="Doc Vault server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", default=os.environ.get("DOCVAULT_DATA", ""),
                        help="データ保存先（既定: doc-vault/data）")
    parser.add_argument("--config", default=os.environ.get("DOCVAULT_CONFIG", ""),
                        help="設定ファイル（既定: doc-vault/docvault.config.json）")
    parser.add_argument("--token", default=os.environ.get("DOCVAULT_TOKEN", ""),
                        help="アクセストークン（127.0.0.1 以外で待ち受けるときは必須。省略時は自動生成）")
    parser.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    args = parser.parse_args()

    if args.data_dir:
        db.set_data_dir(Path(args.data_dir).expanduser().resolve())
    if args.config:
        config.CONFIG_FILE = Path(args.config).expanduser().resolve()
    TOKEN = args.token
    if args.host not in ("127.0.0.1", "localhost", "::1") and not TOKEN:
        TOKEN = secrets.token_urlsafe(12)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    try:
        server = Server((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(explain_busy_port(url, args.port, e, open_browser=args.open))
    threading.Thread(target=startup, daemon=True).start()     # 待ち受けられてから取り込みを再開する
    print(f"Doc Vault {__version__}: {url}  (Ctrl+C で終了)")
    print(f"  データ: {db.DATA_DIR}")
    if TOKEN:
        print(f"  アクセストークン: {TOKEN}   （{url}/?token={TOKEN} で開けます）")
    if args.open:
        threading.Timer(0.8, webbrowser.open,
                        args=(url + (f"/?token={TOKEN}" if TOKEN else ""),)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
