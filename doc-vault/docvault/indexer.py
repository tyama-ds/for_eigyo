"""取り込みパイプライン（バックグラウンドの2本のワーカー）。

段階:
  1. extract  … 原本 → 構造つきテキスト → チャンク → BM25（ここで検索可能になる。LLM を使わない）
  2. embed    … チャンクの埋め込み（区分に応じてローカル / クラウドの空間へ）
  3. summary  … 文書要約とキーワード（RAPTOR 的な上位ノード。要約・横断質問・DecisionRAG に使う）
  4. graph    … エンティティ・関係・キーワード抽出（GraphRAG / LightRAG 系。チャンクごとに LLM を呼ぶ。
                 高速モード・LLM なしの語の共起グラフも選べる）

2本のワーカー:
  - 抽出ワーカー: 届いた文書を順に抽出する。LLM を待たないので、取り込んだ文書はすぐ検索・相談に使える
  - 後処理ワーカー: LLM を使う段階を、文書をまたいで「全文書の埋め込み → 全文書の要約 → 全文書の
    グラフ抽出」の順に進める（検索に効くもの・軽いものから）
後段が失敗しても文書は「検索可能」のまま残し、警告として表示する。

要約・グラフ・埋め込みは「読み込み結果」（readings.py）として、作った LLM ごとに残す。

ローカル LLM でグラフ抽出が途中で終わってしまう問題への対策（以前は1回の失敗で抽出全体を止めていた。
相談の回答中で LLM が順番待ちになりタイムアウト・モデルのプロセスが落ちて HTTP 500・受信の途中で
止まる、のどれか1回で、24 チャンクの文書でも 2 チャンク分しかグラフにならなかった）:
  - 待てば通るエラー（タイムアウト・接続断・HTTP 5xx / 429）は、間隔を空けて同じチャンクをやり直す
    （待ち時間も毎回延ばす）。その依頼だけの失敗（HTTP 400 など）はそのチャンクを飛ばして続ける
  - 設定の誤り（認証・モデル名）は続けても通らないので止め、理由を出す
  - 接続できない状態が続いたら一時停止し、数分後に自動で続きから再開する
  - チャンクごとの結果を DB（graph_chunks）に残し、再起動・一時停止の後は続きから進める
  - 相談の回答中は次のチャンクを送るのを待つ（同じローカル LLM の取り合いでタイムアウトさせない）
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import heapq
import itertools
import json
import sys
import threading
import time
import traceback
from collections import Counter, deque

from . import db, graph, readings, terms
from .config import chat_ready, embed_profile_for, embed_space, ingest_profile_for, load_config
from .extract import ExtractError, extract
from .llm import LLM, LLMError
from .retrieval import INDEX, index_text
from .textutil import as_str_list, build_chunks, pack_vec

ALL_STAGES = ("extract", "embed", "graph", "summary")
LATER_STAGES = ("embed", "summary", "graph")        # 後処理ワーカーで進める順
STAGE_LABEL = {"extract": "テキスト抽出", "embed": "埋め込み", "summary": "文書要約", "graph": "グラフ抽出"}

RETRY_WAITS = (5.0, 20.0, 60.0)   # 待てば通るエラーのやり直しまでの待ち（秒）。回数 = 要素数
PAUSE_AFTER = 3                   # 接続エラーでこの数のチャンクが続けて失敗したら一時停止
AUTO_RESUME_SEC = 300.0           # 一時停止から自動で再開するまで（秒）
MAX_AUTO_RESUME = 3               # 自動で再開する回数の上限（以後は「続きから再開」ボタン）
CHAT_WAIT_MAX = 240.0             # 相談の回答中は、次のチャンクを送るのを最大この秒数待つ

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


def _log(msg: str) -> None:
    """コンソールへの記録（現場の PC で起きたことを後から確認できるように）。"""
    import os
    if os.environ.get("DOCVAULT_QUIET"):
        return
    try:
        sys.stderr.write(f"doc-vault: {time.strftime('%H:%M:%S')} {msg}\n")
    except (OSError, ValueError):
        pass


def _cfg_for(cfg: dict, profile: str, model: str) -> dict:
    """モデル名を上書きした設定（「別の LLM で読み込む」）。Azure はデプロイ名を替える。"""
    if not model:
        return cfg
    out = copy.deepcopy(cfg)
    p = out["profiles"][profile]
    if p.get("provider") == "azure":
        p["model"] = model
    else:
        p["ingest_model"] = model
    return out


class Stopped(Exception):
    """文書が削除された・抽出し直しになったので、この段階をやめる。"""


class Patient:
    """取り込み用の LLM の包み: 待てば通るエラーは、間隔と待ち時間を延ばしながらやり直す。"""

    def __init__(self, llm: LLM, base_timeout: float, *, waiting=None, check=None):
        self.llm = llm
        self.base_timeout = base_timeout
        self.waiting = waiting or (lambda e, sec, n: None)
        self.check = check or (lambda: None)
        self.retries = 0
        self.attempts = 1

    def _run(self, fn, *, scale_timeout: bool):
        self.attempts = 0
        for i in range(len(RETRY_WAITS) + 1):
            self.attempts += 1
            try:
                if scale_timeout:
                    return fn(self.base_timeout * (i + 1))
                return fn()
            except LLMError as e:
                if e.kind != "transient" or i >= len(RETRY_WAITS):
                    raise
                self.retries += 1
                wait = RETRY_WAITS[i]
                _log(f"LLM の一時的なエラー（{i + 1} 回目・{wait:.0f} 秒後にやり直し）: {e}")
                self.waiting(e, wait, i + 1)
                end = time.time() + wait
                while time.time() < end:
                    self.check()
                    time.sleep(min(0.5, max(0.0, end - time.time())))
        raise LLMError("やり直しの回数を超えました", kind="transient")   # pragma: no cover

    def chat(self, *args, **kwargs):
        return self._run(lambda: self.llm.chat(*args, **kwargs), scale_timeout=False)

    def chat_json(self, *args, **kwargs):
        return self._run(lambda timeout: self.llm.chat_json(*args, timeout=timeout, **kwargs),
                         scale_timeout=True)

    def embed(self, *args, **kwargs):
        return self._run(lambda: self.llm.embed(*args, **kwargs), scale_timeout=False)

    def extract_chunk(self, profile, sens, title, chunk, *, mode, on_progress):
        return self._run(lambda timeout: graph.extract_chunk(self.llm, profile, sens, title, chunk,
                                                             on_progress=on_progress, mode=mode,
                                                             timeout=timeout),
                         scale_timeout=True)


class Indexer:
    """抽出ワーカーと後処理ワーカー。enqueue した段階を、文書ごとに終わるまで追跡する。"""

    def __init__(self):
        self._cv = threading.Condition()
        self.fast: deque[str] = deque()            # 抽出待ちの文書
        self.slow: list[tuple] = []                # (段階の順, 通し番号, 文書, 段階, 世代, 指定) のヒープ
        self.todo: dict[str, set[str]] = {}        # 文書 → 残っている後段
        self.gen: dict[str, int] = {}              # 抽出し直すたびに増える（古い後段の結果を捨てる）
        self.later_opts: dict[str, dict] = {}      # 抽出の後に続ける後段の指定
        self.current: dict[str, dict | None] = {"fast": None, "slow": None}
        self._seq = itertools.count()
        self._threads: dict[str, threading.Thread] = {}
        self.idle = threading.Event()
        self.idle.set()
        self._chat_lock = threading.Lock()
        self.chat_active = 0                       # 回答を生成中の相談の数（取り込みはその間 LLM を待つ）
        self._timers: list[threading.Timer] = []

    # ------------------------------------------------------------ 相談との譲り合い
    @contextlib.contextmanager
    def chat_session(self):
        with self._chat_lock:
            self.chat_active += 1
        try:
            yield
        finally:
            with self._chat_lock:
                self.chat_active -= 1

    def _yield_to_chat(self, notify, check) -> None:
        start = time.time()
        while self.chat_active > 0 and time.time() - start < CHAT_WAIT_MAX:
            notify()
            check()
            time.sleep(0.5)

    # ------------------------------------------------------------ 受付・状態
    def start(self) -> None:
        for name, target in (("fast", self._fast_worker), ("slow", self._slow_worker)):
            t = self._threads.get(name)
            if not (t and t.is_alive()):
                t = threading.Thread(target=target, name=f"docvault-indexer-{name}", daemon=True)
                self._threads[name] = t
                t.start()

    def enqueue(self, doc_id: str, stages: tuple[str, ...] = ALL_STAGES, opts: dict | None = None) -> None:
        """段階を予約する。opts: {profile, model, graph_mode, policy（restart / resume / retry_failed）}。"""
        opts = dict(opts or {})
        later = {s for s in LATER_STAGES if s in stages}
        with self._cv:
            self.idle.clear()
            if "extract" in stages:
                self.gen[doc_id] = self.gen.get(doc_id, 0) + 1     # 古い後段は無効にする
                self.todo[doc_id] = later
                self.later_opts[doc_id] = opts
                if doc_id not in self.fast:
                    self.fast.append(doc_id)
            else:
                self.todo[doc_id] = self.todo.get(doc_id, set()) | later
                self._push_slow(doc_id, later, opts)
            self._cv.notify_all()
        if "extract" in stages:
            db.update_doc(doc_id, status="queued", stage="待機中（テキスト抽出）", progress=0.0)
        else:
            db.update_doc(doc_id, stage=_waiting_label(self.todo.get(doc_id, later)), progress=0.0)
        self.start()

    def _push_slow(self, doc_id: str, stages, opts: dict | None = None) -> None:
        gen = self.gen.get(doc_id, 0)
        for s in stages:
            heapq.heappush(self.slow, (LATER_STAGES.index(s), next(self._seq), doc_id, s, gen,
                                       dict(opts or {})))

    def status(self) -> dict:
        with self._cv:
            running = [dict(c) for c in self.current.values() if c]
            busy = set(self.fast) | {d for d, rest in self.todo.items() if rest} \
                | {c["doc_id"] for c in running}
            pending = [d for d in busy if d not in {c["doc_id"] for c in running}]
            return {"current": running[0] if running else None, "running": running,
                    "pending": pending, "busy": len(busy),
                    "extract_waiting": len(self.fast), "chat_active": self.chat_active,
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

    def cancel_timers(self) -> None:
        for t in self._timers:
            t.cancel()
        self._timers.clear()

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
                            self._push_slow(doc_id, rest, self.later_opts.get(doc_id))
                        else:
                            self.todo.pop(doc_id, None)
                    self._cv.notify_all()
                    self._check_idle()

    def _slow_worker(self) -> None:
        while True:
            with self._cv:
                while not self.slow:
                    self._cv.wait()
                _, _, doc_id, stage, gen, opts = heapq.heappop(self.slow)
                if self.gen.get(doc_id, 0) != gen or stage not in self.todo.get(doc_id, set()):
                    self._check_idle()                          # 抽出し直し・重複の古い仕事
                    continue
                self.current["slow"] = {"doc_id": doc_id, "stage": stage, "started": time.time()}
            try:
                self.run_stage(doc_id, stage, gen, opts)
            except Stopped:
                pass
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
    def _set_stage_warnings(self, doc_id: str, stage: str, warnings: list[str]) -> None:
        """段階ごとの警告を flags に持ち、文書の警告は全段階の分をまとめて作り直す。"""
        with db._write_lock:
            doc = db.get_doc(doc_id)
            if not doc:
                return
            flags = dict(doc["flags"])
            flags[f"{stage}_warnings"] = list(warnings)
            merged = [w for s in ALL_STAGES for w in flags.get(f"{s}_warnings") or []]
            db.update_doc(doc_id, flags=flags, warnings=merged)

    # ------------------------------------------------------------ 1. 抽出
    def extract_doc(self, doc_id: str) -> bool:
        """抽出・チャンク化・BM25。成功したら True（後段へ進める）。

        チャンクの中身が前回と同じなら（同じ原本・同じ設定で抽出し直したとき）、読み込み結果
        （要約・グラフ・埋め込み）はそのまま残す。変わったときは、チャンクと対応しなくなるので消す。
        """
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
        rows = [(f"{doc_id}#{c['ord']}", doc_id, c["ord"], c["locator"][:60], c["heading"][:120],
                 c["page"], c["text"]) for c in chunks]
        old = db.query("SELECT text FROM chunks WHERE doc_id=? ORDER BY ord", (doc_id,))
        same = bool(old) and _signature([r["text"] for r in old]) == _signature([r[6] for r in rows])
        if same:
            db.write("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        else:
            db.delete_doc_data(doc_id, keep_doc=True)
        db.executemany("INSERT INTO chunks(id,doc_id,ord,locator,heading,page,text) "
                       "VALUES(?,?,?,?,?,?,?)", rows)
        title = doc["title"]
        if result["title"] and title == doc["filename"]:     # ユーザーが改名していなければ
            title = result["title"]
        flags = {"bm25": True, "extract_warnings": list(result["warnings"])}
        with self._cv:
            rest = self.todo.get(doc_id, set())
        fields = dict(title=title, pages=result["pages"], chars=sum(len(c["text"]) for c in chunks),
                      status="ready", error="", warnings=list(result["warnings"]), flags=flags,
                      stage=_waiting_label(rest) if chunks else "",
                      progress=0.0 if rest and chunks else 1.0)
        if not same:
            fields.update(summary="", keywords=[])
        db.update_doc(doc_id, **fields)
        INDEX.add_doc(doc_id)
        if same:
            INDEX.load_vectors(doc_id)
            readings.restore_chunk_keywords(doc_id)
        readings.refresh_flags(doc_id)
        db.bump_revision()
        return bool(chunks)

    # ------------------------------------------------------------ 2〜4. 後段
    def run_stage(self, doc_id: str, stage: str, gen: int, opts: dict | None = None) -> None:
        opts = dict(opts or {})
        doc = db.get_doc(doc_id)
        if not doc:
            return
        chunks = db.doc_chunks(doc_id)
        if not chunks:
            return
        cfg = load_config()
        sens = doc["sensitivity"]

        def progress(frac: float | None, text: str) -> None:
            if frac is None:
                db.update_doc(doc_id, stage=text)
            else:
                db.update_doc(doc_id, progress=round(max(0.0, min(1.0, frac)), 3), stage=text)

        def check() -> None:
            if self._stale(doc_id, gen) or not db.get_doc(doc_id):
                raise Stopped()

        if stage == "embed":
            self._embed(doc, chunks, cfg, sens, progress, check, opts)
        elif stage == "summary":
            if not cfg["rag"]["doc_summary"] and not opts.get("profile"):
                return
            self._summary(doc, chunks, cfg, sens, progress, check, opts)
        elif stage == "graph":
            if not cfg["rag"]["graph"] and not opts.get("graph_mode") and not opts.get("profile"):
                return
            self._graph(doc, chunks, cfg, sens, progress, check, opts)

    @staticmethod
    def _profile(cfg: dict, sens: str, opts: dict) -> str | None:
        """取り込みに使うプロファイル。指定があればそれ（社外秘はローカルだけ）。使えなければ None。"""
        prof = opts.get("profile")
        if prof:
            if prof not in ("local", "cloud") or (prof == "cloud" and sens != "public"):
                return None                      # 呼び出し側（API）でも止めている。二重の防御
            return prof if chat_ready(_cfg_for(cfg, prof, opts.get("model") or ""), prof) else None
        return ingest_profile_for(cfg, sens)

    @staticmethod
    def _patient(llm: LLM, cfg: dict, prof: str, progress, check, label) -> Patient:
        base = float(cfg["profiles"][prof].get("timeout") or 180)

        def waiting(e, sec, n):
            progress(None, f"{label()}（LLM の応答でエラー。{sec:.0f} 秒後にやり直し {n}/{len(RETRY_WAITS)}）")
        return Patient(llm, base, waiting=waiting, check=check)

    def _summary(self, doc, chunks, cfg, sens, progress, check, opts) -> None:
        doc_id = doc["id"]
        prof = self._profile(cfg, sens, opts)
        if not prof:
            self._set_stage_warnings(doc_id, "summary", [_no_llm_warning(sens, "文書要約")])
            return
        cfg2 = _cfg_for(cfg, prof, opts.get("model") or "")
        ident = readings.identity(cfg2, prof)
        old = readings.find(doc_id, "summary", readings.key_of(ident, "summary"))
        if opts.get("policy") == "resume" and old and old["status"] == "done":
            return                                       # 再起動後: この LLM の要約は作成済み
        progress(0.0, "文書要約")

        def prog(f, s):
            progress(None if f is None else (f - 0.82) / 0.18, s)
        patient = self._patient(LLM(cfg2), cfg2, prof, prog, check, lambda: "文書要約")
        started = time.time()
        try:
            summary, keywords = summarize_doc(patient, prof, sens, doc["title"], chunks, prog)
        except LLMError as e:
            _log(f"文書要約に失敗（{doc['title']}）: {e}")
            self._set_stage_warnings(doc_id, "summary",
                                     [f"文書要約に失敗（{readings.label_of(ident, 'summary')}）: {e}"])
            return
        check()
        readings.record_done(doc_id, "summary", ident,
                             {"summary": summary, "keywords": keywords, "opts": _opts_record(opts)},
                             seconds=time.time() - started, activate=opts.get("activate", True))
        self._set_stage_warnings(doc_id, "summary", [])

    def _embed(self, doc, chunks, cfg, sens, progress, check, opts) -> None:
        doc_id = doc["id"]
        prof = embed_profile_for(cfg, sens)
        if not prof:
            self._set_stage_warnings(doc_id, "embed", [])
            return
        space = embed_space(cfg, prof)
        old = readings.find(doc_id, "embed", space)
        if opts.get("policy") == "resume" and old and old["status"] == "done" and \
                db.one("SELECT 1 FROM vectors WHERE doc_id=? AND space=? LIMIT 1", (doc_id, space)):
            return
        db.write("DELETE FROM vectors WHERE doc_id=? AND space=?", (doc_id, space))
        INDEX.drop_vectors(doc_id, space)
        patient = self._patient(LLM(cfg), cfg, prof, progress, check, lambda: "埋め込み")
        started = time.time()
        try:
            texts = [index_text(doc["title"], c) for c in chunks]
            vecs: list[list[float]] = []
            for i in range(0, len(texts), 64):
                check()
                progress(i / len(texts), f"埋め込み {i + 1}/{len(texts)}")
                vecs.extend(patient.embed(prof, texts[i:i + 64], sensitivity=sens))
            db.executemany("INSERT OR REPLACE INTO vectors(chunk_id,space,doc_id,vec) VALUES(?,?,?,?)",
                           [(c["id"], space, doc_id, pack_vec(v)) for c, v in zip(chunks, vecs)])
            INDEX.add_vectors(space, [(c["id"], v) for c, v in zip(chunks, vecs)])
            readings.record_done(doc_id, "embed", readings.embed_identity(cfg, prof, space),
                                 {"space": space, "dims": len(vecs[0]) if vecs else 0, "chunks": len(vecs)},
                                 seconds=time.time() - started)
            self._set_stage_warnings(doc_id, "embed", [])
        except LLMError as e:
            _log(f"埋め込みに失敗（{doc['title']}）: {e}")
            self._set_stage_warnings(doc_id, "embed", [f"埋め込みに失敗（BM25 とグラフで検索します）: {e}"])

    # ------------------------------------------------------------ グラフ抽出
    def _graph(self, doc, chunks, cfg, sens, progress, check, opts) -> None:
        """チャンクごとにエンティティ・関係を抽出する。1チャンクの失敗では止めない。"""
        doc_id = doc["id"]
        mode = opts.get("graph_mode") or cfg["rag"].get("graph_mode") or "standard"
        if mode not in readings.GRAPH_MODES:
            mode = "standard"
        policy = opts.get("policy") or "restart"
        prof = patient = None
        cfg2 = cfg
        if mode == "terms":
            ident = readings.terms_identity()
        else:
            prof = self._profile(cfg, sens, opts)
            if not prof:
                self._set_stage_warnings(doc_id, "graph", [
                    _no_llm_warning(sens, "ナレッジグラフ")
                    + "。設定の「グラフの作り方」を「語の共起（LLM なし）」にすると、LLM なしでグラフを作れます"])
                return
            cfg2 = _cfg_for(cfg, prof, opts.get("model") or "")
            ident = readings.identity(cfg2, prof)
        reading = readings.start(doc_id, "graph", ident, mode, opts={**opts, "graph_mode": mode},
                                 restart=policy == "restart")
        rid = reading["id"]
        records = {r["chunk_id"]: r["status"] for r in db.query(
            "SELECT chunk_id,status FROM graph_chunks WHERE reading_id=?", (rid,))}
        skip = {cid for cid, st in records.items()
                if st == "done" or (st == "failed" and policy != "retry_failed")}
        targets = _sample(chunks, int(cfg["rag"]["max_graph_chunks"]))
        todo = [c for c in targets if c["id"] not in skip]
        base = len(targets) - len(todo)
        n = len(targets)
        cur = {"label": ""}
        if prof:
            patient = self._patient(LLM(cfg2), cfg2, prof, progress, check, lambda: cur["label"])
        started = time.time()
        consecutive = 0
        stopped_reason = ""
        _log(f"グラフ抽出を開始: {doc['title']}（{reading['label']}・{n} チャンク"
             f"{f'・{base} チャンクは済み' if base else ''}）")
        for j, ch in enumerate(todo):
            check()
            i = base + j
            cur["label"] = f"グラフ抽出 {i + 1}/{n}" + ("（続きから）" if base else "")
            progress(i / n, cur["label"])
            if mode == "terms":
                readings.save_chunk(rid, doc_id, ch["id"], "done", finish="terms",
                                    data=graph._clean_extraction(terms.extract_terms(ch["text"])))
                if j % 20 == 19:
                    db.bump_revision()
                continue

            self._yield_to_chat(lambda: progress(None, f"{cur['label']}（相談の回答が終わるのを待っています）"),
                                check)

            def note(size, sec, frac=i / n):   # 受信中（止まっていないことが分かる）
                progress(frac, f"{cur['label']}（受信 {size:,} 字・{sec:.0f} 秒）")
            t_chunk = time.time()
            try:
                data, info = patient.extract_chunk(prof, sens, doc["title"], ch, mode=mode, on_progress=note)
            except LLMError as e:
                secs = time.time() - t_chunk
                if e.kind == "config":
                    stopped_reason = f"設定を確認してください: {e}"
                    readings.save_chunk(rid, doc_id, ch["id"], "error", reason=str(e),
                                        attempts=patient.attempts, seconds=secs)
                    _log(f"グラフ抽出を中止（{doc['title']}）: {e}")
                    break
                if e.kind == "transient":
                    consecutive += 1
                    readings.save_chunk(rid, doc_id, ch["id"], "error", reason=str(e),
                                        attempts=patient.attempts, seconds=secs)
                    _log(f"グラフ抽出 {ch['id']}: やり直しても LLM に届きませんでした: {e}")
                    if consecutive >= PAUSE_AFTER:
                        self._pause_graph(doc_id, rid, opts, mode, n, str(e))
                        return
                    continue
                consecutive = 0                           # その依頼だけの失敗（HTTP 400 など）は飛ばす
                readings.save_chunk(rid, doc_id, ch["id"], "failed", reason=str(e),
                                    attempts=patient.attempts, seconds=secs)
                _log(f"グラフ抽出 {ch['id']}: 失敗（次のチャンクへ）: {e}")
                continue
            consecutive = 0
            secs = time.time() - t_chunk
            if data is None:
                readings.save_chunk(rid, doc_id, ch["id"], "failed", finish=info["finish"],
                                    reason=info["reason"], attempts=patient.attempts, seconds=secs)
                _log(f"グラフ抽出 {ch['id']}: JSON を取り出せず（{info['reason']}）")
                continue
            readings.save_chunk(rid, doc_id, ch["id"], "done", data=data, finish=info["finish"],
                                reason="出力が途中で切れたため、書けた分までを使用" if info["salvaged"] else "",
                                attempts=patient.attempts, seconds=secs)
            if j % 5 == 4:
                db.bump_revision()                       # 途中までの関係もグラフ・検索に出す
        self._finish_graph(doc_id, rid, targets, chunks, mode, started, opts,
                           retries=patient.retries if patient else 0, stopped_reason=stopped_reason)

    def _finish_graph(self, doc_id, rid, targets, chunks, mode, started, opts, *, retries: int,
                      stopped_reason: str) -> None:
        counts = readings.chunk_counts(rid)
        n = len(targets)
        done, failed, err = counts.get("done", 0), counts.get("failed", 0), counts.get("error", 0)
        warnings = []
        if n < len(chunks):
            warnings.append(f"グラフ抽出は {len(chunks)} チャンク中 {n} 件に限定（設定の上限）")
        if stopped_reason:
            warnings.append(f"グラフ抽出を中止しました（{done}/{n} チャンク済み）。{stopped_reason}"
                            "。直したら文書の「続きから再開」で残りを抽出できます")
        reasons = Counter(r["reason"] for r in db.query(
            "SELECT reason FROM graph_chunks WHERE reading_id=? AND status IN ('failed','error')", (rid,)))
        if (failed or err) and not stopped_reason:
            why = "／".join(f"{r[:80]}（{c}件）" for r, c in reasons.most_common(3))
            warnings.append(f"グラフ抽出: {failed + err}/{n} チャンクを抽出できませんでした（理由: {why}）。"
                            "文書の「失敗したチャンクをやり直す」で再挑戦できます")
        salvaged = db.one("SELECT COUNT(*) n FROM graph_chunks WHERE reading_id=? AND status='done' "
                          "AND reason<>''", (rid,))["n"]
        if salvaged:
            warnings.append(f"グラフ抽出: {salvaged} チャンクは出力が途中で切れたため、書けた分までを使いました")
        if retries:
            warnings.append(f"グラフ抽出: LLM の一時的なエラーで {retries} 回やり直しました（すべて記録済み）")
        if stopped_reason or (n and done == 0):
            status = "error"
        elif done + failed + err >= n:
            status = "done"
        else:
            status = "partial"
        prev = readings.get(rid) or {}
        readings.finish(rid, status, data={"chunks_total": n, "done": done, "failed": failed + err,
                                           "mode": mode, "retries": retries, "opts": _opts_record(opts)},
                        warnings=warnings, seconds=time.time() - started + float(prev.get("seconds") or 0),
                        activate=opts.get("activate", True))
        _log(f"グラフ抽出を終了: {doc_id}（{done}/{n} チャンク・失敗 {failed + err}）")
        self._set_stage_warnings(doc_id, "graph", warnings)

    def _pause_graph(self, doc_id, rid, opts, mode, n, error) -> None:
        r = readings.get(rid) or {"data": {}}
        pauses = int(r["data"].get("pauses", 0)) + 1
        counts = readings.chunk_counts(rid)
        auto = pauses <= MAX_AUTO_RESUME
        msg = (f"LLM に接続できない状態が続いたため、グラフ抽出を一時停止しました（{counts.get('done', 0)}/{n} "
               f"チャンク済み・最後のエラー: {error}）。"
               + (f"{AUTO_RESUME_SEC / 60:.0f} 分後に自動で続きから再開します" if auto
                  else "LLM の状態を確認してから、文書の「続きから再開」を押してください"))
        readings.finish(rid, "paused", data={"pauses": pauses, "chunks_total": n, "mode": mode,
                                             "opts": _opts_record(opts)}, warnings=[msg], activate=False)
        self._set_stage_warnings(doc_id, "graph", [msg])
        _log(msg)
        if auto:
            t = threading.Timer(AUTO_RESUME_SEC, self.enqueue,
                                args=(doc_id, ("graph",), {**_opts_record(opts), "graph_mode": mode,
                                                          "policy": "resume"}))
            t.daemon = True
            self._timers.append(t)
            t.start()


def _signature(texts: list[str]) -> str:
    return hashlib.sha1("\n\x00".join(texts).encode("utf-8")).hexdigest()


def _opts_record(opts: dict) -> dict:
    return {k: v for k, v in (opts or {}).items() if k in ("profile", "model", "graph_mode") and v}


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


def resume_pending() -> int:
    """起動時: 中断された取り込みを続きから再開する（済んだチャンク・要約・埋め込みはやり直さない）。"""
    n = 0
    graph_opts: dict[str, dict] = {}
    for r in db.query("SELECT doc_id,data FROM readings WHERE stage='graph' AND status IN ('running','paused')"):
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            data = {}
        graph_opts[r["doc_id"]] = {**(data.get("opts") or {}), "policy": "resume"}
    for d in db.list_docs():
        if d["status"] in ("queued", "processing") or (d["status"] == "ready" and not d["chars"]):
            INDEXER.enqueue(d["id"], ALL_STAGES)
            n += 1
        elif d["status"] == "ready" and (d["stage"] or d["id"] in graph_opts):
            INDEXER.enqueue(d["id"], ("embed", "summary"), {"policy": "resume"})
            INDEXER.enqueue(d["id"], ("graph",), graph_opts.get(d["id"], {"policy": "resume"}))
            n += 1
    return n


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
    folder = clean_folder(folder)
    if dup:
        db.update_doc(doc_id, filename=name, size=len(data), status="queued")
    else:
        db.write("INSERT INTO documents(id,title,filename,ext,size,sha256,folder,tags,sensitivity,"
                 "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (doc_id, name, name, ext, len(data), digest, folder,
                  json.dumps(as_str_list(tags or [], limit=20, maxlen=30), ensure_ascii=False),
                  sens, "queued", t, t))
    if folder:
        ensure_folder(folder)
    INDEXER.enqueue(doc_id)
    return db.get_doc(doc_id), True


# ---------------------------------------------------------------- フォルダ

def clean_folder(path: str) -> str:
    """フォルダのパスを正規化する（"/" 区切り・前後の空白と空の階層を除く・各階層 60 字まで）。"""
    parts = [p.strip()[:60] for p in str(path or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p and p not in (".", ".."))[:200]


def ensure_folder(path: str) -> None:
    """フォルダ（と上位の階層）を登録する（文書が無くても表示されるように）。"""
    path = clean_folder(path)
    parts = path.split("/") if path else []
    t = db.now()
    db.executemany("INSERT OR IGNORE INTO folders(path,created_at) VALUES(?,?)",
                   [("/".join(parts[:i + 1]), t) for i in range(len(parts))])
