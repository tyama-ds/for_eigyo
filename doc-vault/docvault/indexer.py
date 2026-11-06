"""取り込みパイプライン（バックグラウンドの2本のワーカー）。

段階:
  1. extract  … 原本 → 構造つきテキスト → チャンク → BM25（ここで検索可能になる。LLM を使わない）
  2. embed    … チャンクの埋め込み（区分に応じてローカル / クラウドの空間へ）
  3. summary  … 文書要約とキーワード（RAPTOR 的な上位ノード。要約・横断質問・DecisionRAG に使う）
  4. graph    … エンティティ・関係・キーワード抽出（GraphRAG / LightRAG 系。チャンクごとに LLM を呼ぶ）

2本のワーカー:
  - 抽出ワーカー: 届いた文書を順に抽出する。LLM を待たないので、取り込んだ文書はすぐ検索・相談に使える
  - 後処理ワーカー: LLM を使う段階を、文書をまたいで「全文書の埋め込み → 全文書の要約 → 全文書の
    グラフ抽出」の順に進める（検索に効くもの・軽いものから）。以前は1本のワーカーが1文書ずつ最後まで
    処理していたため、遅いローカル LLM のグラフ抽出の間、後ろの文書は抽出すらされず「待機中」のままだった
後段が失敗しても文書は「検索可能」のまま残し、警告として表示する。
"""
from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import threading
import time
import traceback
from collections import deque

from . import db, graph
from .config import embed_profile_for, embed_space, ingest_profile_for, load_config
from .extract import ExtractError, extract
from .llm import LLM, LLMError
from .retrieval import INDEX, index_text
from .textutil import as_str_list, build_chunks, pack_vec

ALL_STAGES = ("extract", "embed", "graph", "summary")
LATER_STAGES = ("embed", "summary", "graph")        # 後処理ワーカーで進める順
STAGE_LABEL = {"extract": "テキスト抽出", "embed": "埋め込み", "summary": "文書要約", "graph": "グラフ抽出"}

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


def _waiting_label(stages) -> str:
    names = [STAGE_LABEL[s] for s in LATER_STAGES if s in stages]
    return f"順番待ち（{'・'.join(names)}）" if names else ""


class Indexer:
    """抽出ワーカーと後処理ワーカー。enqueue した段階を、文書ごとに終わるまで追跡する。"""

    def __init__(self):
        self._cv = threading.Condition()
        self.fast: deque[str] = deque()            # 抽出待ちの文書
        self.slow: list[tuple] = []                # (段階の順, 通し番号, 文書, 段階, 世代) のヒープ
        self.todo: dict[str, set[str]] = {}        # 文書 → 残っている後段
        self.gen: dict[str, int] = {}              # 抽出し直すたびに増える（古い後段の結果を捨てる）
        self.current: dict[str, dict | None] = {"fast": None, "slow": None}
        self._seq = itertools.count()
        self._threads: dict[str, threading.Thread] = {}
        self.idle = threading.Event()
        self.idle.set()

    # ------------------------------------------------------------ 受付・状態
    def start(self) -> None:
        for name, target in (("fast", self._fast_worker), ("slow", self._slow_worker)):
            t = self._threads.get(name)
            if not (t and t.is_alive()):
                t = threading.Thread(target=target, name=f"docvault-indexer-{name}", daemon=True)
                self._threads[name] = t
                t.start()

    def enqueue(self, doc_id: str, stages: tuple[str, ...] = ALL_STAGES) -> None:
        later = {s for s in LATER_STAGES if s in stages}
        with self._cv:
            self.idle.clear()
            if "extract" in stages:
                self.gen[doc_id] = self.gen.get(doc_id, 0) + 1     # 古い後段は無効にする
                self.todo[doc_id] = later
                if doc_id not in self.fast:
                    self.fast.append(doc_id)
            else:
                self.todo[doc_id] = self.todo.get(doc_id, set()) | later
                self._push_slow(doc_id, later)
            self._cv.notify_all()
        if "extract" in stages:
            db.update_doc(doc_id, status="queued", stage="待機中（テキスト抽出）", progress=0.0)
        else:
            db.update_doc(doc_id, stage=_waiting_label(self.todo.get(doc_id, later)), progress=0.0)
        self.start()

    def _push_slow(self, doc_id: str, stages) -> None:
        gen = self.gen.get(doc_id, 0)
        for s in stages:
            heapq.heappush(self.slow, (LATER_STAGES.index(s), next(self._seq), doc_id, s, gen))

    def status(self) -> dict:
        with self._cv:
            running = [dict(c) for c in self.current.values() if c]
            busy = set(self.fast) | {d for d, rest in self.todo.items() if rest} \
                | {c["doc_id"] for c in running}
            pending = [d for d in busy if d not in {c["doc_id"] for c in running}]
            return {"current": running[0] if running else None, "running": running,
                    "pending": pending, "busy": len(busy),
                    "extract_waiting": len(self.fast),
                    "later_waiting": {s: sum(1 for j in self.slow if j[3] == s) for s in LATER_STAGES}}

    def wait_idle(self, timeout: float = 60.0) -> bool:
        return self.idle.wait(timeout)

    def _check_idle(self) -> None:          # self._cv を持った状態で呼ぶ
        if not self.fast and not self.slow and not any(self.current.values()) \
                and not any(self.todo.values()):
            self.idle.set()

    def _stale(self, doc_id: str, gen: int) -> bool:
        with self._cv:
            return self.gen.get(doc_id, 0) != gen

    # ------------------------------------------------------------ ワーカー
    def _fast_worker(self) -> None:
        while True:
            with self._cv:
                while not self.fast:
                    self._cv.wait()
                doc_id = self.fast.popleft()
                gen = self.gen.get(doc_id, 0)
                self.current["fast"] = {"doc_id": doc_id, "stage": "extract", "started": time.time()}
            ok = False
            try:
                ok = self.extract_doc(doc_id)
            except Exception as e:  # noqa: BLE001 - ワーカーは止めない
                traceback.print_exc()
                if db.get_doc(doc_id):
                    db.update_doc(doc_id, status="error", error=f"内部エラー: {e}", stage="")
            finally:
                with self._cv:
                    self.current["fast"] = None
                    if self.gen.get(doc_id, 0) == gen:          # 抽出中に再登録されていなければ
                        rest = self.todo.get(doc_id, set()) if ok else set()
                        if rest:
                            self._push_slow(doc_id, rest)
                        else:
                            self.todo.pop(doc_id, None)
                    self._cv.notify_all()
                    self._check_idle()

    def _slow_worker(self) -> None:
        while True:
            with self._cv:
                while not self.slow:
                    self._cv.wait()
                _, _, doc_id, stage, gen = heapq.heappop(self.slow)
                if self.gen.get(doc_id, 0) != gen or stage not in self.todo.get(doc_id, set()):
                    self._check_idle()                          # 抽出し直し・重複の古い仕事
                    continue
                self.current["slow"] = {"doc_id": doc_id, "stage": stage, "started": time.time()}
            try:
                self.run_stage(doc_id, stage, gen)
            except Exception as e:  # noqa: BLE001 - ワーカーは止めない
                traceback.print_exc()
                if db.get_doc(doc_id):
                    self._set_stage_warnings(doc_id, stage, [f"{STAGE_LABEL[stage]}で内部エラー: {e}"])
            finally:
                with self._cv:
                    rest = None
                    if self.gen.get(doc_id, 0) == gen and doc_id in self.todo:
                        if not any(j[2] == doc_id and j[3] == stage and j[4] == gen for j in self.slow):
                            self.todo[doc_id].discard(stage)
                        rest = set(self.todo[doc_id])
                    # 表示（残りの段階）を書いてから「終わった」ことにする（終わった後に古い表示を残さない）
                    if rest is not None and db.get_doc(doc_id):
                        db.update_doc(doc_id, stage=_waiting_label(rest), progress=0.0 if rest else 1.0)
                        db.bump_revision()
                    if rest is not None and not rest:
                        self.todo.pop(doc_id, None)
                    self.current["slow"] = None
                    self._cv.notify_all()
                    self._check_idle()

    # ------------------------------------------------------------ 警告
    def _set_stage_warnings(self, doc_id: str, stage: str, warnings: list[str],
                            flags_update: dict | None = None, drop_flags: tuple = ()) -> None:
        """段階ごとの警告を flags に持ち、文書の警告は全段階の分をまとめて作り直す。"""
        doc = db.get_doc(doc_id)
        if not doc:
            return
        flags = dict(doc["flags"])
        for k in drop_flags:
            flags.pop(k, None)
        flags.update(flags_update or {})
        flags[f"{stage}_warnings"] = list(warnings)
        merged = [w for s in ALL_STAGES for w in flags.get(f"{s}_warnings") or []]
        db.update_doc(doc_id, flags=flags, warnings=merged)

    # ------------------------------------------------------------ 1. 抽出
    def extract_doc(self, doc_id: str) -> bool:
        """抽出・チャンク化・BM25。成功したら True（後段へ進める）。"""
        doc = db.get_doc(doc_id)
        if not doc:
            return False
        cfg = load_config()
        db.update_doc(doc_id, status="processing", progress=0.02, stage="テキスト抽出")
        path = db.files_dir() / f"{doc_id}{doc['ext']}"
        try:
            result = extract(path.read_bytes(), doc["filename"])
        except (ExtractError, OSError) as e:
            db.update_doc(doc_id, status="error", error=str(e), stage="", progress=0.0)
            return False
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
        flags = {"bm25": True, "extract_warnings": list(result["warnings"])}
        with self._cv:
            rest = self.todo.get(doc_id, set())
        db.update_doc(doc_id, title=title, pages=result["pages"],
                      chars=sum(len(c["text"]) for c in chunks), status="ready",
                      error="", warnings=list(result["warnings"]), flags=flags, summary="",
                      keywords=[], stage=_waiting_label(rest) if chunks else "",
                      progress=0.0 if rest and chunks else 1.0)
        INDEX.add_doc(doc_id)
        db.bump_revision()
        return bool(chunks)

    # ------------------------------------------------------------ 2〜4. 後段
    def run_stage(self, doc_id: str, stage: str, gen: int) -> None:
        doc = db.get_doc(doc_id)
        if not doc:
            return
        chunks = db.doc_chunks(doc_id)
        if not chunks:
            return
        cfg = load_config()
        llm = LLM(cfg)
        sens = doc["sensitivity"]

        def progress(frac: float, text: str) -> None:
            db.update_doc(doc_id, progress=round(frac, 3), stage=text)

        if stage == "embed":
            self._embed(doc, chunks, cfg, llm, sens, progress)
        elif stage == "summary":
            if not cfg["rag"]["doc_summary"]:
                return
            prof = ingest_profile_for(cfg, sens)
            if not prof:
                self._set_stage_warnings(doc_id, "summary", [_no_llm_warning(sens, "文書要約")],
                                         drop_flags=("summary",))
                return
            progress(0.0, "文書要約")
            try:
                summary, keywords = summarize_doc(llm, prof, sens, doc["title"], chunks,
                                                  lambda f, s: progress((f - 0.82) / 0.18, s))
            except LLMError as e:
                self._set_stage_warnings(doc_id, "summary", [f"文書要約に失敗: {e}"],
                                         drop_flags=("summary",))
                return
            if self._stale(doc_id, gen):
                return
            db.update_doc(doc_id, summary=summary, keywords=keywords)
            self._set_stage_warnings(doc_id, "summary", [], {"summary": prof})
        elif stage == "graph":
            if not cfg["rag"]["graph"]:
                return
            self._graph(doc, chunks, cfg, llm, sens, progress, gen)

    def _embed(self, doc, chunks, cfg, llm, sens, progress) -> None:
        doc_id = doc["id"]
        prof = embed_profile_for(cfg, sens)
        db.write("DELETE FROM vectors WHERE doc_id=?", (doc_id,))
        INDEX.drop_vectors(doc_id)
        if not prof:
            self._set_stage_warnings(doc_id, "embed", [], drop_flags=("vector",))
            return
        try:
            space = embed_space(cfg, prof)
            texts = [index_text(doc["title"], c) for c in chunks]
            vecs: list[list[float]] = []
            for i in range(0, len(texts), 64):
                progress(i / len(texts), f"埋め込み {i + 1}/{len(texts)}")
                vecs.extend(llm.embed(prof, texts[i:i + 64], sensitivity=sens))
            db.executemany("INSERT OR REPLACE INTO vectors(chunk_id,space,doc_id,vec) VALUES(?,?,?,?)",
                           [(c["id"], space, doc_id, pack_vec(v)) for c, v in zip(chunks, vecs)])
            INDEX.add_vectors(space, [(c["id"], v) for c, v in zip(chunks, vecs)])
            self._set_stage_warnings(doc_id, "embed", [], {"vector": space})
        except LLMError as e:
            self._set_stage_warnings(doc_id, "embed", [f"埋め込みに失敗（BM25 とグラフで検索します）: {e}"],
                                     drop_flags=("vector",))

    def _graph(self, doc, chunks, cfg, llm, sens, progress, gen) -> None:
        doc_id = doc["id"]
        prof = ingest_profile_for(cfg, sens)
        db.write_many([("DELETE FROM entity_mentions WHERE doc_id=?", (doc_id,)),
                       ("DELETE FROM relation_mentions WHERE doc_id=?", (doc_id,))])
        if not prof:
            self._set_stage_warnings(doc_id, "graph", [_no_llm_warning(sens, "ナレッジグラフ")],
                                     drop_flags=("graph",))
            return
        warnings = []
        targets = _sample(chunks, int(cfg["rag"]["max_graph_chunks"]))
        if len(targets) < len(chunks):
            warnings.append(f"グラフ抽出は {len(chunks)} チャンク中 {len(targets)} 件に限定（設定の上限）")
        failures = salvaged = 0
        reasons: dict[str, int] = {}
        for i, ch in enumerate(targets):
            if self._stale(doc_id, gen) or not db.get_doc(doc_id):
                return                                   # 抽出し直し・削除された
            label = f"グラフ抽出 {i + 1}/{len(targets)}"
            progress(i / len(targets), label)

            def note(size, sec, frac=i / len(targets), label=label):   # 受信中（止まっていないことが分かる）
                progress(frac, f"{label}（受信 {size:,} 字・{sec:.0f} 秒）")
            try:
                data, info = graph.extract_chunk(llm, prof, sens, doc["title"], ch, on_progress=note)
            except LLMError as e:
                warnings.append(f"グラフ抽出を中断: {e}")
                break
            salvaged += int(info["salvaged"])
            if data is None:
                failures += 1
                reasons[info["reason"]] = reasons.get(info["reason"], 0) + 1
                continue
            graph.save_extraction(doc_id, ch["id"], data)
            if i % 10 == 9:
                db.bump_revision()                       # 途中までの関係もグラフ・検索に出す
        if failures:
            why = "／".join(f"{r}（{n}件）" for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:3])
            warnings.append(f"グラフ抽出: {failures} チャンクで JSON を取り出せませんでした（理由: {why}）")
        if salvaged:
            warnings.append(f"グラフ抽出: {salvaged} チャンクは出力が途中で切れたため、書けた分までを使いました")
        self._set_stage_warnings(doc_id, "graph", warnings, {"graph": prof})


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
    data, info = llm.chat_json(prof, SUMMARY_PROMPT.format(title=title, part=part, text=body[:14000]),
                               sensitivity=sens, system=SUMMARY_SYSTEM, max_tokens=2048, purpose="ingest")
    if isinstance(data, dict) and str(data.get("summary", "")).strip():
        return (str(data["summary"]).strip()[:2000],
                as_str_list(data.get("keywords"), limit=10, maxlen=40))
    if info.get("text", "").strip() and info["finish"] != "repetition":
        return info["text"].strip()[:2000], []         # JSON でなくても要約の文章なら使う
    raise LLMError(info["reason"] or "要約を作れませんでした")


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
