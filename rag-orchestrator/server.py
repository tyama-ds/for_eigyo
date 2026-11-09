#!/usr/bin/env python3
"""RAG Orchestrator — 複数の RAG 実装へ同一コーパス・同一質問を並列実行して比較する。

    python rag-orchestrator/server.py            # http://127.0.0.1:8750
    python rag-orchestrator/server.py --port 9500 --open

- 標準ライブラリのみ（pip install 不要）。127.0.0.1 にのみ bind し外部公開しない
- LLM はローカルの OpenAI 互換エンドポイント（Ollama / LM Studio / vLLM / llama.cpp）
- 組み込みエンジン: GraphRAG（Microsoft 方式の再実装）/ Vector / BM25 / Hybrid / DecisionRAG /
  組み合わせ（選んだ方式を RRF で統合）
- 外部エンジン: nano-graphrag / LightRAG（pip 導入で有効化・実験的）
- BM25 は LLM 未設定でも抜粋モードで動作する
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from ragcore import __version__  # noqa: E402
from ragcore import decision_api, store  # noqa: E402
from ragcore.config import load_config, public_config, save_config  # noqa: E402
from ragcore.engines import all_engines, external, get_engine  # noqa: E402
from ragcore.engines.base import build_chunks  # noqa: E402
from ragcore.graphio import GRAPHML_FILENAME, graphml_graph_payload  # noqa: E402
from ragcore.llm import LLMClient, LLMError  # noqa: E402
from ragcore.orchestrator import Orchestrator  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8750
SAMPLE_DIR = BASE / "sample_docs"
MAX_GRAPH_NODES = 300

ORCH = Orchestrator()


# ---------------------------------------------------------------- API 実装

def api_engines() -> dict:
    cfg = load_config()
    corpus = store.load_corpus()
    engines = []
    for eng in all_engines():
        info = eng.info(cfg)
        info["index"] = store.index_status(eng.id, corpus["rev"])
        if info["index"]["built"]:
            # 構築済みインデックスの方式（GraphRAG の 標準 / 高速 など。記録の無い古いものは 標準）
            info["index"]["variant"] = eng.variant(stats=info["index"].get("stats") or {}, cfg=cfg)
        engines.append(info)
    # チャンク数（GraphRAG の構築方式ごとの LLM 呼び出し回数の目安に使う）
    chunks = len(build_chunks(corpus["docs"]))
    return {"engines": engines, "corpus_rev": corpus["rev"], "corpus_chunks": chunks}


def api_config_test(body: dict | None = None) -> dict:
    """チャットと埋め込みの疎通試験。judge_api を付けると判定 API（Decisions API）だけを試す。"""
    cfg = load_config()
    if (body or {}).get("judge_api"):
        return decision_api.connection_test(cfg)
    llm = LLMClient(cfg)
    out: dict = {}
    try:
        # 推論モデルは思考にトークンを使うため、余裕を持たせる
        text = llm.chat("「接続OK」とだけ返答してください。", max_tokens=512)
        if text:
            out["chat"] = {"ok": True, "message": text[:100]}
        else:
            out["chat"] = {"ok": False,
                           "message": "応答が空でした。思考トークンで max_tokens を使い切った"
                                      "可能性があります（Max Tokens を増やしてください）"}
    except LLMError as e:
        out["chat"] = {"ok": False, "message": str(e)}
    try:
        vecs = llm.embed(["接続テスト"])
        out["embed"] = {"ok": True, "message": f"次元数 {len(vecs[0])}"}
    except LLMError as e:
        out["embed"] = {"ok": False, "message": str(e)}
    return out


def api_corpus_add(body: dict) -> dict:
    docs_in = body.get("docs")
    if not isinstance(docs_in, list):
        docs_in = [body]
    corpus = store.load_corpus()
    new_docs = []
    for doc in docs_in:
        text = str(doc.get("text", "")).strip()
        if not text:
            continue
        title = str(doc.get("title", "")).strip() or text.splitlines()[0][:40]
        doc_id = str(doc.get("id", "")).strip() or store.next_doc_id(corpus)
        entry = {"id": doc_id, "title": title[:120], "text": text[:400_000]}
        new_docs.append(entry)
        corpus["docs"].append(entry)          # next_doc_id の連番を進めるため
    if not new_docs:
        return {"error": "text が空です"}
    return {"corpus": store.add_docs(new_docs)}


def api_corpus_sample() -> dict:
    docs = []
    for path in sorted(SAMPLE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        title = text.splitlines()[0].lstrip("# ").strip() if text else path.stem
        # id を固定して、再読み込みしても重複せず上書きになるようにする
        docs.append({"id": f"sample-{path.stem}", "title": title, "text": text})
    if not docs:
        return {"error": "サンプル文書が見つかりません"}
    return api_corpus_add({"docs": docs})


# GraphML（NetworkX ストレージ）を出力する外部エンジン
# （HippoRAG は igraph/parquet 保存のため GraphML 表示は非対応）
GRAPHML_ENGINES = ("lightrag", "nano-graphrag", "minirag", "rag-anything")


def api_graph(engine_id: str, limit: int = MAX_GRAPH_NODES) -> dict:
    """グラフ可視化用データ。

    - graphrag（組み込み）: インデックス JSON から生成
    - lightrag / nano-graphrag 等（外部）: 作業ディレクトリの GraphML から生成
    ノードは次数（関係の数）の多い順に ``limit`` 件まで返す。
    """
    limit = max(20, min(int(limit or MAX_GRAPH_NODES), 2000))
    if engine_id in GRAPHML_ENGINES:
        # external.DATA_DIR を呼び出し時に参照する（テストで差し替え可能にするため）
        path = external.DATA_DIR / engine_id / GRAPHML_FILENAME
        return graphml_graph_payload(path, max_nodes=limit)
    index = store.load_index(engine_id)
    if index is None:
        return {"error": "インデックス未構築です。「エンジン」タブでこのエンジンのインデックスを構築してください"}
    if "entities" not in index:
        return {"error": "このエンジンのインデックスはグラフ表示に対応していません"}
    payload = builtin_graph_payload(index, max_nodes=limit)
    engine = get_engine(engine_id)
    payload["variant"] = engine.variant(stats=index.get("stats") or {}) if engine else None
    return payload


def builtin_graph_payload(index: dict, *, max_nodes: int = MAX_GRAPH_NODES) -> dict:
    """組み込み GraphRAG のインデックスからグラフ表示用ペイロードを作る。"""
    com_of = {}
    for com in index.get("communities") or []:
        for key in com["entity_keys"]:
            com_of[key] = com["id"]
    relations = index.get("relations") or []
    # 次数は関係リストから数え直す（古いインデックスや端点補完後でも整合させる）
    degree: dict[str, int] = {}
    for r in relations:
        degree[r["source"]] = degree.get(r["source"], 0) + 1
        degree[r["target"]] = degree.get(r["target"], 0) + 1
    doc_title = {c["id"]: c.get("doc_title", "") for c in index.get("chunks") or []}
    entities = sorted(index["entities"],
                      key=lambda e: (-degree.get(e["key"], 0), e["key"]))[:max_nodes]
    keys = {e["key"] for e in entities}
    nodes = []
    for e in entities:
        docs = []
        for cid in e.get("chunks") or []:
            title = doc_title.get(cid)
            if title and title not in docs:
                docs.append(title)
        nodes.append({"id": e["key"], "name": e["name"], "type": e["type"],
                      "degree": degree.get(e["key"], 0), "community": com_of.get(e["key"], ""),
                      "description": e["description"][:300], "docs": docs[:8]})
    edges = [{"source": r["source"], "target": r["target"],
              "strength": r["strength"], "description": r["description"][:200]}
             for r in relations
             if r["source"] in keys and r["target"] in keys]
    # summary_source: llm（LLM の要約）/ auto（LLM なしで機械的に作った要約）。記録の無い古いものは空
    communities = [{"id": c["id"], "title": c["title"], "size": c["size"],
                    "summary": c["summary"][:500], "summary_source": c.get("summary_source", "")}
                   for c in index.get("communities") or []]
    return {"nodes": nodes, "edges": edges, "communities": communities,
            "total_nodes": len(index["entities"]), "total_edges": len(relations),
            "truncated": len(index["entities"]) > max_nodes}


def _valid_engine_ids(body: dict) -> list[str]:
    ids = body.get("engines")
    if not isinstance(ids, list):
        return []
    return [e for e in ids if isinstance(e, str) and get_engine(e)]


# ---------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = f"RAGOrchestrator/{__version__}"

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body_json(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 8_000_000)
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    # -- GET ------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._send_file(BASE / "index.html", "text/html; charset=utf-8")
        elif route == "/style.css":
            self._send_file(BASE / "style.css", "text/css; charset=utf-8")
        elif route == "/app.js":
            self._send_file(BASE / "app.js", "text/javascript; charset=utf-8")
        elif route == "/graphview.js":
            self._send_file(BASE / "graphview.js", "text/javascript; charset=utf-8")
        elif route == "/api/health":
            self._send_json({"ok": True, "app": "rag-orchestrator",
                             "version": __version__})
        elif route == "/api/config":
            self._send_json(public_config(load_config()))
        elif route == "/api/engines":
            self._send_json(api_engines())
        elif route == "/api/corpus":
            self._send_json(store.load_corpus())
        elif route == "/api/jobs":
            self._send_json({"jobs": ORCH.list_jobs()})
        elif route.startswith("/api/jobs/"):
            job = ORCH.get_job(route.rsplit("/", 1)[-1])
            if job is None:
                self._send_json({"error": "ジョブが見つかりません"}, 404)
            else:
                self._send_json(job.to_dict())
        elif route == "/api/graph":
            qs = parse_qs(parsed.query)
            engine_id = (qs.get("engine") or ["graphrag"])[0]
            try:
                limit = int((qs.get("limit") or [MAX_GRAPH_NODES])[0])
            except ValueError:
                limit = MAX_GRAPH_NODES
            if not get_engine(engine_id):
                self._send_json({"error": f"未知のエンジンです: {engine_id}"}, 400)
            else:
                self._send_json(api_graph(engine_id, limit))
        else:
            self.send_error(404)

    # -- POST -----------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        body = self._read_body_json()
        if route == "/api/config":
            cfg = save_config(body)
            self._send_json(public_config(cfg))
        elif route == "/api/config/test":
            self._send_json(api_config_test(body))
        elif route == "/api/corpus/add":
            result = api_corpus_add(body)
            self._send_json(result, 400 if "error" in result else 200)
        elif route == "/api/corpus/sample":
            result = api_corpus_sample()
            self._send_json(result, 400 if "error" in result else 200)
        elif route == "/api/corpus/delete":
            doc_id = str(body.get("id", ""))
            self._send_json({"corpus": store.delete_doc(doc_id)})
        elif route == "/api/ingest":
            engine_ids = _valid_engine_ids(body)
            if not engine_ids:
                self._send_json({"error": "エンジンを選択してください"}, 400)
            else:
                job = ORCH.start_ingest(engine_ids)
                self._send_json({"job_id": job.id})
        elif route == "/api/query":
            engine_ids = _valid_engine_ids(body)
            question = str(body.get("question", "")).strip()[:4000]
            mode = str(body.get("mode", "auto"))
            if mode not in ("auto", "global", "local"):
                mode = "auto"
            if not engine_ids:
                self._send_json({"error": "エンジンを選択してください"}, 400)
            elif not question:
                self._send_json({"error": "質問が空です"}, 400)
            else:
                options = {}
                beam = body.get("beam")
                if beam not in (None, "") and not isinstance(beam, bool):
                    try:
                        options["decision_beam"] = max(1, min(10, int(beam)))
                    except (TypeError, ValueError):
                        pass
                job = ORCH.start_query(engine_ids, question, mode, options)
                self._send_json({"job_id": job.id})
        else:
            self.send_error(404)

    def log_message(self, fmt: str, *args) -> None:  # 静かに
        sys.stderr.write("rag-orchestrator: " + fmt % args + "\n")


class Server(ThreadingHTTPServer):
    # Windows の SO_REUSEADDR は、他のプロセスが待ち受け中のポートにも重ねて待ち受けることを許す。
    # start.bat を2回ダブルクリックすると2つ目も起動したように見え、要求が古い方に届くため、Windows では付けない
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
    """待ち受けに失敗したとき: RAG Orchestrator がもう動いていればそれを開いて 0、それ以外は理由を表示して 1。"""
    app = running_app(url)
    if app == "rag-orchestrator":
        print(f"RAG Orchestrator はすでに起動しています: {url}")
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
    _safe_stdio()
    parser = argparse.ArgumentParser(description="RAG Orchestrator server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    args = parser.parse_args()

    url = f"http://{HOST}:{args.port}"
    try:
        server = Server((HOST, args.port), Handler)
    except OSError as e:
        sys.exit(explain_busy_port(url, args.port, e, open_browser=args.open))
    print(f"RAG Orchestrator: {url}  (Ctrl+C で終了)")
    if args.open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
