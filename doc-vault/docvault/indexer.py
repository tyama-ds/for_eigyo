"""取り込みパイプライン（バックグラウンドの1本のワーカーで順に処理）。

段階:
  1. extract  … 原本 → 構造つきテキスト → チャンク → BM25（ここで検索可能になる）
  2. embed    … チャンクの埋め込み（区分に応じてローカル / クラウドの空間へ）
  3. graph    … エンティティ・関係・キーワード抽出（GraphRAG / LightRAG 系）
  4. summary  … 文書要約とキーワード（RAPTOR 的な上位ノード。要約・横断質問に使う）
後段（2〜4）が失敗しても文書は「検索可能」のまま残し、警告として表示する。
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import traceback

from . import db, graph
from .config import embed_profile_for, embed_space, ingest_profile_for, load_config
from .extract import ExtractError, extract
from .llm import LLM, LLMError
from .retrieval import INDEX, index_text
from .textutil import as_str_list, build_chunks, extract_json, pack_vec

ALL_STAGES = ("extract", "embed", "graph", "summary")

SUMMARY_SYSTEM = "あなたは社内文書の要約者です。本文に書かれていることだけを使い、日本語で簡潔にまとめます。"
SUMMARY_PROMPT = """[TASK:doc_summary]
以下は文書「{title}」の{part}です。この文書の要約とキーワードを作ってください。

出力は次の JSON のみ:
{{"summary": "目的・主な内容・結論や数値を含む 5〜8 文の要約", "keywords": ["主題を表すキーワード（5〜10個）"]}}

# 本文
{text}
"""
PART_PROMPT = """[TASK:doc_part_summary]
以下は文書「{title}」の一部（{locator}）です。重要な事実・数値・結論を落とさずに 3〜5 文で要約してください。

# 本文
{text}
"""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Indexer:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.pending: list[dict] = []
        self.current: dict | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.idle = threading.Event()
        self.idle.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name="docvault-indexer", daemon=True)
        self._thread.start()

    def enqueue(self, doc_id: str, stages: tuple[str, ...] = ALL_STAGES) -> None:
        job = {"doc_id": doc_id, "stages": tuple(s for s in ALL_STAGES if s in stages)}
        with self._lock:
            self.pending.append(job)
            self.idle.clear()
        db.update_doc(doc_id, status="queued" if "extract" in stages else "processing",
                      stage="待機中", progress=0.0)
        self.q.put(job)
        self.start()

    def status(self) -> dict:
        with self._lock:
            return {"current": dict(self.current) if self.current else None,
                    "pending": [j["doc_id"] for j in self.pending]}

    def wait_idle(self, timeout: float = 60.0) -> bool:
        return self.idle.wait(timeout)

    def _worker(self) -> None:
        while True:
            job = self.q.get()
            with self._lock:
                if job in self.pending:
                    self.pending.remove(job)
                self.current = {"doc_id": job["doc_id"], "started": time.time()}
            try:
                self.process(job["doc_id"], job["stages"])
            except Exception as e:  # noqa: BLE001 - ワーカーは止めない
                traceback.print_exc()
                if db.get_doc(job["doc_id"]):
                    db.update_doc(job["doc_id"], status="error", error=f"内部エラー: {e}", stage="")
            finally:
                with self._lock:
                    self.current = None
                    if not self.pending:
                        self.idle.set()

    # ------------------------------------------------------------ 本体
    def process(self, doc_id: str, stages: tuple[str, ...]) -> None:
        doc = db.get_doc(doc_id)
        if not doc:
            return
        cfg = load_config()
        llm = LLM(cfg)
        sens = doc["sensitivity"]
        # 再処理（抽出以外の段階だけ）では抽出時の警告だけ残し、他は作り直す
        flags = dict(doc["flags"]) if "extract" not in stages else {}
        warnings = list(flags.get("extract_warnings") or [])

        def progress(frac: float, stage: str) -> None:
            db.update_doc(doc_id, progress=round(frac, 3), stage=stage)

        # 1. 抽出・チャンク化
        if "extract" in stages:
            progress(0.02, "テキスト抽出")
            path = db.files_dir() / f"{doc_id}{doc['ext']}"
            try:
                result = extract(path.read_bytes(), doc["filename"])
            except (ExtractError, OSError) as e:
                db.update_doc(doc_id, status="error", error=str(e), stage="", progress=0.0)
                return
            chunks = build_chunks(result["sections"], size=int(cfg["rag"]["chunk_size"]),
                                  overlap=min(150, int(cfg["rag"]["chunk_size"]) // 8))
            db.delete_doc_data(doc_id, keep_doc=True)
            rows = [(f"{doc_id}#{c['ord']}", doc_id, c["ord"], c["locator"][:60], c["heading"][:120],
                     c["page"], c["text"]) for c in chunks]
            db.executemany("INSERT INTO chunks(id,doc_id,ord,locator,heading,page,text) "
                           "VALUES(?,?,?,?,?,?,?)", rows)
            title = doc["title"]
            if result["title"] and title == doc["filename"]:     # ユーザーが改名していなければ
                title = result["title"]
            warnings.extend(result["warnings"])
            flags["bm25"] = True
            flags["extract_warnings"] = result["warnings"]
            db.update_doc(doc_id, title=title, pages=result["pages"],
                          chars=sum(len(c["text"]) for c in chunks), status="ready",
                          error="", warnings=warnings, flags=flags, summary="", keywords=[])
            INDEX.add_doc(doc_id)
            db.bump_revision()
            doc = db.get_doc(doc_id)
        chunks = db.doc_chunks(doc_id)
        if not chunks:
            db.update_doc(doc_id, stage="", progress=1.0)
            return

        # 2. 埋め込み
        if "embed" in stages:
            prof = embed_profile_for(cfg, sens)
            db.write("DELETE FROM vectors WHERE doc_id=?", (doc_id,))
            INDEX.drop_vectors(doc_id)
            flags.pop("vector", None)
            if prof:
                try:
                    space = embed_space(cfg, prof)
                    texts = [index_text(doc["title"], c) for c in chunks]
                    vecs: list[list[float]] = []
                    for i in range(0, len(texts), 64):
                        progress(0.1 + 0.25 * i / len(texts), f"埋め込み {i + 1}/{len(texts)}")
                        vecs.extend(llm.embed(prof, texts[i:i + 64], sensitivity=sens))
                    db.executemany("INSERT OR REPLACE INTO vectors(chunk_id,space,doc_id,vec) "
                                   "VALUES(?,?,?,?)",
                                   [(c["id"], space, doc_id, pack_vec(v)) for c, v in zip(chunks, vecs)])
                    INDEX.add_vectors(space, [(c["id"], v) for c, v in zip(chunks, vecs)])
                    flags["vector"] = space
                except LLMError as e:
                    warnings.append(f"埋め込みに失敗（BM25 とグラフで検索します）: {e}")
            db.update_doc(doc_id, flags=flags, warnings=warnings)

        # 3. グラフ抽出
        if "graph" in stages and cfg["rag"]["graph"]:
            prof = ingest_profile_for(cfg, sens)
            db.write_many([("DELETE FROM entity_mentions WHERE doc_id=?", (doc_id,)),
                           ("DELETE FROM relation_mentions WHERE doc_id=?", (doc_id,))])
            flags.pop("graph", None)
            if not prof:
                warnings.append(_no_llm_warning(sens, "ナレッジグラフ"))
            else:
                targets = _sample(chunks, int(cfg["rag"]["max_graph_chunks"]))
                if len(targets) < len(chunks):
                    warnings.append(f"グラフ抽出は {len(chunks)} チャンク中 {len(targets)} 件に限定"
                                    "（設定の上限）")
                failures = 0
                for i, ch in enumerate(targets):
                    progress(0.35 + 0.45 * i / len(targets), f"グラフ抽出 {i + 1}/{len(targets)}")
                    try:
                        data = graph.extract_chunk(llm, prof, sens, doc["title"], ch)
                    except LLMError as e:
                        warnings.append(f"グラフ抽出を中断: {e}")
                        break
                    if data is None:
                        failures += 1
                        continue
                    graph.save_extraction(doc_id, ch["id"], data)
                if failures:
                    warnings.append(f"グラフ抽出: {failures} チャンクで JSON を解析できませんでした")
                flags["graph"] = prof
                db.bump_revision()
            db.update_doc(doc_id, flags=flags, warnings=warnings)

        # 4. 文書要約
        if "summary" in stages and cfg["rag"]["doc_summary"]:
            prof = ingest_profile_for(cfg, sens)
            if not prof:
                warnings.append(_no_llm_warning(sens, "文書要約"))
            else:
                progress(0.82, "文書要約")
                try:
                    summary, keywords = summarize_doc(llm, prof, sens, doc["title"], chunks,
                                                      progress)
                    db.update_doc(doc_id, summary=summary, keywords=keywords)
                    flags["summary"] = prof
                except LLMError as e:
                    warnings.append(f"文書要約に失敗: {e}")
            db.update_doc(doc_id, flags=flags, warnings=warnings)

        db.update_doc(doc_id, stage="", progress=1.0, status="ready")
        db.bump_revision()


def _no_llm_warning(sens: str, what: str) -> str:
    if sens == "public":
        return f"{what}を作れませんでした（LLM が未設定）"
    return f"{what}を作れませんでした（社外秘文書はローカル LLM で処理します。ローカル LLM が未設定）"


def _sample(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def summarize_doc(llm, prof: str, sens: str, title: str, chunks: list[dict],
                  progress=lambda f, s: None) -> tuple[str, list[str]]:
    """短い文書は一発要約、長い文書は部分要約 → 統合（RAPTOR の2層版）。"""
    full = "\n\n".join(c["text"] for c in chunks)
    if len(full) <= 12000:
        body, part = full, "全文"
    else:
        groups: list[list[dict]] = []
        cur: list[dict] = []
        size = 0
        for c in chunks:
            if cur and size + len(c["text"]) > 10000:
                groups.append(cur)
                cur, size = [], 0
            cur.append(c)
            size += len(c["text"])
        if cur:
            groups.append(cur)
        groups = _sample(groups, 8)
        partials = []
        for i, g in enumerate(groups):
            progress(0.82 + 0.15 * i / len(groups), f"文書要約（部分 {i + 1}/{len(groups)}）")
            loc = g[0]["locator"] + ("〜" + g[-1]["locator"] if g[-1]["locator"] != g[0]["locator"] else "")
            text = llm.chat(prof, PART_PROMPT.format(title=title, locator=loc,
                                                     text="\n\n".join(c["text"] for c in g)[:11000]),
                            sensitivity=sens, system=SUMMARY_SYSTEM, max_tokens=2048,
                            purpose="ingest")
            partials.append(f"（{loc}）{text.strip()}")
        body, part = "\n\n".join(partials), "部分要約の一覧"
    text = llm.chat(prof, SUMMARY_PROMPT.format(title=title, part=part, text=body[:14000]),
                    sensitivity=sens, system=SUMMARY_SYSTEM, max_tokens=2048, purpose="ingest")
    data = extract_json(text)
    if isinstance(data, dict) and str(data.get("summary", "")).strip():
        return (str(data["summary"]).strip()[:2000],
                as_str_list(data.get("keywords"), limit=10, maxlen=40))
    return text.strip()[:2000], []


INDEXER = Indexer()


def register_upload(data: bytes, filename: str, *, folder: str = "", tags=None,
                    sensitivity: str | None = None, replace: bool = False) -> tuple[dict, bool]:
    """アップロードを登録してキューへ入れる。(文書, 新規か) を返す。同一内容は重複登録しない。"""
    from pathlib import PurePosixPath

    cfg = load_config()
    digest = sha256(data)
    dup = db.one("SELECT id FROM documents WHERE sha256=?", (digest,))
    if dup and not replace:
        return db.get_doc(dup["id"]), False
    name = PurePosixPath(filename.replace("\\", "/")).name or "document"
    ext = PurePosixPath(name.lower()).suffix
    doc_id = dup["id"] if dup else db.new_id("d")
    (db.files_dir() / f"{doc_id}{ext}").write_bytes(data)
    sens = sensitivity if sensitivity in ("internal", "public") \
        else cfg["policy"]["default_sensitivity"]
    t = db.now()
    if dup:
        db.update_doc(doc_id, filename=name, size=len(data), status="queued")
    else:
        db.write("INSERT INTO documents(id,title,filename,ext,size,sha256,folder,tags,sensitivity,"
                 "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (doc_id, name, name, ext, len(data), digest, folder.strip("/ "),
                  json.dumps(as_str_list(tags or [], limit=20, maxlen=30), ensure_ascii=False),
                  sens, "queued", t, t))
    INDEXER.enqueue(doc_id)
    return db.get_doc(doc_id), True
