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

from docvault import __version__, config, db, decision_api, folders, graph, rag, ragopts, readings, speech  # noqa: E402
from docvault.extract import SUPPORTED  # noqa: E402
from docvault.indexer import ALL_STAGES, INDEXER, register_upload, resume_pending  # noqa: E402
from docvault.llm import LLM, LLMError  # noqa: E402
from docvault.retrieval import INDEX, load_chunks  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8760
STATIC = BASE / "static"
MAX_UPLOAD = 200 * 1024 * 1024
TOKEN = ""                     # 空ならトークン不要（127.0.0.1 のみで待ち受け）
STRATEGIES = config.STRATEGIES   # 検索方式（自動 / 速さ優先 / 選んで使う / DecisionRAG のみ / 従来型）

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


# ---------------------------------------------------------------- API 実装

def public_config_with_catalog(cfg: dict) -> dict:
    """画面へ返す設定（API キーは伏せる）＋ 相談で選べる RAG の一覧と呼び出し回数の目安。"""
    out = config.public_config(cfg)
    out["rag_methods"] = ragopts.catalog(cfg)
    return out


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
    if body.get("judge_api"):                      # 判定 API（Jev / OpenAI / Azure）の接続テスト
        return decision_api.connection_test(config.load_config())
    if body.get("speech"):                         # 文字起こし（Whisper 互換）の接続テスト
        try:
            r = speech.transcribe(config.load_config(), speech.test_tone())
        except speech.SpeechError as e:
            return {"ok": False, "message": str(e)}
        return {"ok": True, "message": f"応答あり（{r['seconds']} 秒・テスト音の認識結果: "
                                       f"「{r['text'][:30] or '（なし）'}」）", "url": r["url"]}
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
    out["flags"] = {k: v for k, v in (d.get("flags") or {}).items() if not k.endswith("_warnings")}
    return out


def api_docs(qs: dict) -> dict:
    folder = (qs.get("folder") or [""])[0].strip("/")
    q = (qs.get("q") or [""])[0].strip().lower()
    docs = []
    all_docs = db.list_docs()
    acts = readings.actives_by_doc()
    for d in all_docs:
        if folder and not (d["folder"] == folder or d["folder"].startswith(folder + "/")):
            continue
        if q and q not in (d["title"] + " " + d["filename"] + " " + " ".join(d["tags"])).lower():
            continue
        out = doc_public(d)
        out["readings"] = acts.get(d["id"], {})
        docs.append(out)
    tags = sorted({t for d in all_docs for t in d["tags"]})
    return {"docs": docs, "folders": folders.tree(), "tags": tags, "supported": sorted(SUPPORTED)}


def api_doc_read(doc_id: str, body: dict) -> dict:
    """「この LLM で読み込む」: 指定の LLM（プロファイル・モデル）・方式で要約・グラフを作る（前の結果は残す）。"""
    doc = db.get_doc(doc_id)
    if not doc:
        return {"error": "文書が見つかりません"}
    stages = tuple(s for s in (body.get("stages") or ["summary", "graph"]) if s in ("embed", "summary", "graph"))
    if not stages:
        return {"error": "読み込む段階（要約・グラフ）を選んでください"}
    mode = body.get("graph_mode") if body.get("graph_mode") in readings.GRAPH_MODES else ""
    prof = body.get("profile") if body.get("profile") in config.PROFILE_NAMES else ""
    model = str(body.get("model") or "").strip()[:120]
    cfg = config.load_config()
    needs_llm = "summary" in stages or ("graph" in stages and mode != "terms")
    if needs_llm:
        if not prof:
            return {"error": "使う LLM（ローカル / クラウド）を選んでください"}
        if prof == "cloud" and doc["sensitivity"] != "public":
            return {"error": "社外秘の文書はクラウド LLM で読み込めません（ローカル LLM を選んでください）"}
        from docvault.indexer import _cfg_for
        if not config.chat_ready(_cfg_for(cfg, prof, model), prof):
            return {"error": f"{'ローカル' if prof == 'local' else 'クラウド'} LLM が未設定です（設定タブ）"}
    opts = {"profile": prof if needs_llm else "", "model": model if needs_llm else "",
            "graph_mode": mode, "policy": "restart"}
    INDEXER.enqueue(doc_id, stages, opts)
    return {"ok": True}


def api_doc_reindex(doc_id: str, body: dict) -> dict:
    if not db.get_doc(doc_id):
        return {"error": "文書が見つかりません"}
    stages = tuple(s for s in (body.get("stages") or ALL_STAGES) if s in ALL_STAGES) or ALL_STAGES
    opts: dict = {}
    if body.get("policy") in ("resume", "retry_failed", "restart"):
        opts["policy"] = body["policy"]
    rid = str(body.get("reading_id") or "")
    if rid:                                          # その読み込み結果（LLM・方式）の続き・やり直し
        r = readings.get(rid)
        if not r or r["doc_id"] != doc_id:
            return {"error": "読み込み結果が見つかりません"}
        opts.update(r["data"].get("opts") or {})
        if r["stage"] == "graph":
            opts["graph_mode"] = r["mode"] or "standard"
        stages = (r["stage"],)
    INDEXER.enqueue(doc_id, stages, opts)
    return {"ok": True}


def api_doc_readings(doc_id: str, qs: dict) -> dict:
    doc = db.get_doc(doc_id)
    if not doc:
        return {"error": "文書が見つかりません"}
    cfg = config.load_config()
    current = {}
    for name in config.PROFILE_NAMES:
        if config.chat_ready(cfg, name):
            current[name] = readings.label_of(readings.identity(cfg, name), "summary")
    out = {"readings": readings.list_for(doc_id), "current": current,
           "embed_spaces": {name: config.embed_space(cfg, name) for name in config.PROFILE_NAMES
                            if config.embed_ready(cfg, name)},
           "graph_mode": cfg["rag"].get("graph_mode", "standard")}
    rid = (qs.get("log") or [""])[0]
    if rid:
        out["log"] = readings.chunk_log(rid)
    return out


def api_bulk(body: dict) -> dict:
    """文書の一括操作（移動・区分の変更・再処理・削除）。"""
    ids = [str(x) for x in (body.get("doc_ids") or []) if db.get_doc(str(x))]
    if not ids:
        return {"error": "文書が選ばれていません"}
    action = body.get("action")
    if action == "move":
        return {"ok": True, "moved": folders.move_docs(ids, str(body.get("folder") or ""))}
    if action == "sensitivity":
        if body.get("sensitivity") not in ("internal", "public"):
            return {"error": "区分は internal / public"}
        for i in ids:
            api_doc_update(i, {"sensitivity": body["sensitivity"]})
        return {"ok": True}
    if action == "reindex":
        for i in ids:
            INDEXER.enqueue(i, ALL_STAGES)
        return {"ok": True}
    if action == "delete":
        for i in ids:
            api_doc_delete(i)
        return {"ok": True}
    return {"error": "不明な操作です"}


def api_doc_update(doc_id: str, body: dict) -> dict:
    doc = db.get_doc(doc_id)
    if not doc:
        return {"error": "文書が見つかりません"}
    fields: dict = {}
    if isinstance(body.get("title"), str) and body["title"].strip():
        fields["title"] = body["title"].strip()[:200]
    if isinstance(body.get("folder"), str):
        from docvault.indexer import clean_folder, ensure_folder
        fields["folder"] = clean_folder(body["folder"])
        if fields["folder"]:
            ensure_folder(fields["folder"])
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
        if fields["sensitivity"] == "internal":
            # 社外秘にしたら、クラウドで作った埋め込みは使わない（質問をクラウドで埋め込むことになるため）
            for r in readings.list_for(doc_id):
                if r["stage"] == "embed" and r["profile"] == "cloud":
                    readings.delete(r["id"])
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
    folder = (qs.get("folder") or [""])[0].strip("/")
    docs = db.list_docs()
    sens = {d["id"]: d["sensitivity"] for d in docs}
    if doc_id:
        allowed = {doc_id}
    elif folder:                                   # フォルダ（配下を含む）の文書だけのグラフ
        allowed = {d["id"] for d in docs if d["folder"] == folder or d["folder"].startswith(folder + "/")}
    else:
        allowed = {d["id"] for d in docs}
    g = graph.get_graph(allowed, sens)
    if not g.entities:
        cfg = config.load_config()
        no_llm = not (config.chat_ready(cfg, "local") or config.chat_ready(cfg, "cloud"))
        if not cfg["rag"]["graph"]:
            hint = "取り込み時のグラフ作成が OFF です（設定タブ → RAG の設定）"
        elif no_llm and cfg["rag"].get("graph_mode") != "terms":
            hint = ("グラフの抽出には LLM が必要です（設定タブ）。LLM が無くても、設定の「グラフの作り方」を"
                    "「語の共起（LLM なし）」にして文書を再処理すると、語のつながりのグラフを作れます")
        else:
            hint = "まだグラフがありません。文書を取り込むと、エンティティと関係が抽出されます"
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
            self._json(public_config_with_catalog(config.load_config()))
        elif route == "/api/docs":
            self._json(api_docs(qs))
        elif len(parts) == 3 and parts[:2] == ["api", "docs"]:
            d = db.get_doc(parts[2])
            self._json({"doc": doc_public(d)} if d else {"error": "文書が見つかりません"},
                       200 if d else 404)
        elif len(parts) == 4 and parts[:2] == ["api", "docs"] and parts[3] == "readings":
            r = api_doc_readings(parts[2], qs)
            self._json(r, 404 if "error" in r else 200)
        elif route == "/api/folders":
            self._json({"folders": folders.tree()})
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
            docs = db.list_docs()
            chats = db.list_chats()
            for c in chats:
                c["scope_info"] = folders.describe_scope(c["scope"], docs)
            self._json({"chats": chats})
        elif len(parts) == 3 and parts[:2] == ["api", "chats"]:
            chat = db.get_chat(parts[2])
            if not chat:
                self._json({"error": "相談が見つかりません"}, 404)
            else:
                chat["scope_info"] = folders.describe_scope(chat["scope"])
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
        if route == "/api/transcribe":
            self._transcribe(parse_qs(parsed.query))
            return
        body = self._body_json()
        if route == "/api/config":
            self._json(public_config_with_catalog(config.save_config(body)))
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
        elif route == "/api/folders":
            try:
                self._json({"ok": True, "path": folders.create(str(body.get("path") or ""))})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
        elif route in ("/api/folders/rename", "/api/folders/delete"):
            try:
                r = folders.rename(str(body.get("path") or ""), str(body.get("to") or "")) \
                    if route.endswith("rename") else folders.delete(str(body.get("path") or ""))
                self._json({"ok": True, **r})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
        elif route == "/api/docs/move":
            self._json({"ok": True, "moved": folders.move_docs(body.get("doc_ids") or [],
                                                               str(body.get("folder") or ""))})
        elif route == "/api/docs/bulk":
            r = api_bulk(body)
            self._json(r, 400 if "error" in r else 200)
        elif len(parts) == 6 and parts[:2] == ["api", "docs"] and parts[3] == "readings":
            r = readings.get(parts[4])
            if not r or r["doc_id"] != parts[2]:
                self._json({"error": "読み込み結果が見つかりません"}, 404)
            elif parts[5] == "activate":
                if r["status"] not in ("done", "partial", "paused", "running"):
                    self._json({"error": "完了していない読み込み結果は使えません"}, 400)
                else:
                    readings.set_active(r["id"])
                    self._json({"ok": True})
            elif parts[5] == "delete":
                readings.delete(r["id"])
                self._json({"ok": True})
            else:
                self._json({"error": "不明な操作です"}, 400)
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
                r = api_doc_reindex(doc_id, body)
            elif action == "read":
                r = api_doc_read(doc_id, body)
            else:
                r = {"error": "不明な操作です"}
            self._json(r, 400 if "error" in r else 200)
        elif route == "/api/chats":
            mode = body.get("mode") if body.get("mode") in ("auto", "local", "cloud") \
                else config.load_config()["policy"]["chat_mode"]
            scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
            cfg = config.load_config()
            strategy = body.get("strategy") if body.get("strategy") in STRATEGIES \
                else cfg["rag"].get("default_strategy", "auto")
            methods = config.clean_methods(body["methods"]) if isinstance(body.get("methods"), list) \
                else list(cfg["rag"].get("custom_methods") or [])
            chat = db.create_chat(str(body.get("title") or ""), mode, folders.clean_scope(scope), strategy,
                                  methods)
            beam = _beam(body.get("decision_beam"))
            if beam:
                db.update_chat(chat["id"], decision_beam=beam)
                chat = db.get_chat(chat["id"])
            chat["scope_info"] = folders.describe_scope(chat["scope"])
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
                if isinstance(body.get("methods"), list):
                    fields["methods"] = config.clean_methods(body["methods"])
                beam = _beam(body.get("decision_beam"))
                if beam is not None:
                    fields["decision_beam"] = beam
                if isinstance(body.get("scope"), dict):
                    fields["scope"] = folders.clean_scope(body["scope"])
                if isinstance(body.get("title"), str) and body["title"].strip():
                    fields["title"] = body["title"].strip()[:80]
                if fields:
                    db.update_chat(chat_id, **fields)
                chat = db.get_chat(chat_id)
                chat["scope_info"] = folders.describe_scope(chat["scope"])
                self._json({"chat": chat})
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

    def _transcribe(self, qs: dict) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > speech.MAX_AUDIO:
            if length > 0:
                self.rfile.read(length)
            self._json({"error": "音声が空か、長すぎます"}, 400)
            return
        audio = self.rfile.read(length)
        try:
            r = speech.transcribe(config.load_config(), audio,
                                  content_type=self.headers.get("Content-Type") or "audio/wav",
                                  language=(qs.get("lang") or [""])[0])
        except speech.SpeechError as e:
            self._json({"error": str(e)}, 400)
            return
        self._json(r)

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


def startup() -> None:
    """起動時: 検索インデックスの読み込みと、中断された取り込みの再開（済んだ分はやり直さない）。"""
    INDEX.ensure_loaded()
    n = resume_pending()
    if n:
        print(f"  中断していた取り込みを続きから再開します（{n} 文書）")


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
