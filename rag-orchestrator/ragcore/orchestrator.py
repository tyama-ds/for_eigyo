"""オーケストレーター: 複数エンジンへの並列 ingest / query とジョブ管理・統合レポート。

Deep Research Orchestrator と同じ発想:
- 同じ入力（コーパス / 質問）を選択された全エンジンへ並列に送る
- 部分失敗を許容する（失敗エンジンはエラー表示、成功分だけで比較・統合を続行）
- 全エンジン完了後、LLM で各回答を突き合わせた統合レポート（一致点・相違点）を作る
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid

from . import store
from .config import chat_configured, load_config
from .engines import get_engine
from .engines.base import EngineContext
from .llm import LLMClient, LLMError

MAX_JOBS_KEPT = 40

SYNTHESIS_SYSTEM = (
    "あなたは複数の RAG エンジンの回答を比較・統合する分析者です。"
    "与えられた回答だけを根拠にし、日本語で答えてください。"
)

SYNTHESIS_PROMPT = """[TASK:synthesis]
同じ質問を複数の RAG エンジンに投げた結果です。以下の構成で統合レポートを作ってください。

## 統合回答
（各エンジンの回答を突き合わせた、最も確からしい回答。どのエンジンの回答に基づくかを
 （GraphRAG） のように付記する）

## 一致点
（複数エンジンが一致して述べている内容の箇条書き）

## 相違点・片方にしかない情報
（エンジン間で食い違う、または特定エンジンのみが述べた内容。無ければ「なし」）

# 質問
{question}

# 各エンジンの回答
{answers}
"""


class Job:
    """1回の ingest / query 実行。UI はこれをポーリングする。"""

    def __init__(self, job_type: str, engine_ids: list[str], question: str = "",
                 mode: str = "auto"):
        self.id = uuid.uuid4().hex[:12]
        self.type = job_type                  # ingest | query
        self.question = question
        self.mode = mode
        self.status = "running"               # running | done | error
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.lock = threading.Lock()
        self.engines: dict[str, dict] = {
            eid: {"engine": eid, "status": "pending", "progress": 0.0, "message": "",
                  "result": None, "error": "", "elapsed": 0.0, "llm_stats": None}
            for eid in engine_ids
        }
        self.synthesis: dict = {"status": "pending", "text": "", "error": ""}

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "id": self.id, "type": self.type, "status": self.status,
                "question": self.question, "mode": self.mode,
                "created_at": self.created_at, "finished_at": self.finished_at,
                "engines": {k: dict(v) for k, v in self.engines.items()},
                "synthesis": dict(self.synthesis),
            }


class Orchestrator:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ jobs
    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: -j.created_at)
        return [j.to_dict() for j in jobs]

    def _register(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            if len(self._jobs) > MAX_JOBS_KEPT:       # 古い完了ジョブから捨てる
                for jid in sorted(self._jobs, key=lambda j: self._jobs[j].created_at):
                    if len(self._jobs) <= MAX_JOBS_KEPT:
                        break
                    if self._jobs[jid].status != "running":
                        del self._jobs[jid]

    # ------------------------------------------------------------ ingest
    def start_ingest(self, engine_ids: list[str]) -> Job:
        job = Job("ingest", engine_ids)
        self._register(job)
        threading.Thread(target=self._run_ingest, args=(job,), daemon=True).start()
        return job

    def _run_ingest(self, job: Job) -> None:
        cfg = load_config()
        corpus = store.load_corpus()
        threads = []
        for eid in job.engines:
            t = threading.Thread(target=self._ingest_one, args=(job, eid, cfg, corpus),
                                 daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        with job.lock:
            job.status = "done" if any(e["status"] == "done"
                                       for e in job.engines.values()) else "error"
            job.finished_at = time.time()

    def _ingest_one(self, job: Job, engine_id: str, cfg: dict, corpus: dict) -> None:
        entry = job.engines[engine_id]
        start = time.time()

        def progress(frac: float, msg: str) -> None:
            with job.lock:
                entry["progress"] = round(max(0.0, min(1.0, frac)), 3)
                entry["message"] = msg

        with job.lock:
            entry["status"] = "running"
        try:
            engine = get_engine(engine_id)
            if engine is None:
                raise ValueError(f"未知のエンジンです: {engine_id}")
            ok, reason = engine.availability(cfg)
            if not ok:
                raise LLMError(reason)
            if not corpus["docs"]:
                raise ValueError("コーパスが空です。先に文書を追加してください")
            ctx = EngineContext(llm=LLMClient(cfg), cfg=cfg, progress=progress)
            index = engine.ingest(corpus["docs"], ctx)
            store.save_index(engine_id, index, corpus["rev"])
            with job.lock:
                entry["status"] = "done"
                entry["progress"] = 1.0
                entry["result"] = {"stats": index.get("stats") or {}, "logs": ctx.logs}
                entry["llm_stats"] = ctx.llm.stats
        except Exception as e:  # noqa: BLE001  部分失敗を許容し UI へ表示する
            with job.lock:
                entry["status"] = "error"
                entry["error"] = f"{type(e).__name__}: {e}"
                entry["message"] = "失敗"
            traceback.print_exc()
        finally:
            with job.lock:
                entry["elapsed"] = round(time.time() - start, 2)

    # ------------------------------------------------------------ query
    def start_query(self, engine_ids: list[str], question: str, mode: str) -> Job:
        job = Job("query", engine_ids, question=question, mode=mode)
        self._register(job)
        threading.Thread(target=self._run_query, args=(job,), daemon=True).start()
        return job

    def _run_query(self, job: Job) -> None:
        cfg = load_config()
        threads = []
        for eid in job.engines:
            t = threading.Thread(target=self._query_one, args=(job, eid, cfg),
                                 daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        self._synthesize(job, cfg)
        with job.lock:
            job.status = "done" if any(e["status"] == "done"
                                       for e in job.engines.values()) else "error"
            job.finished_at = time.time()

    def _query_one(self, job: Job, engine_id: str, cfg: dict) -> None:
        entry = job.engines[engine_id]
        start = time.time()

        def progress(frac: float, msg: str) -> None:
            with job.lock:
                entry["progress"] = round(max(0.0, min(1.0, frac)), 3)
                entry["message"] = msg

        with job.lock:
            entry["status"] = "running"
        try:
            engine = get_engine(engine_id)
            if engine is None:
                raise ValueError(f"未知のエンジンです: {engine_id}")
            index = store.load_index(engine_id)
            if index is None:
                raise ValueError("インデックス未構築です。先に「インデックス構築」を実行してください")
            corpus = store.load_corpus()
            if index.get("corpus_rev") != corpus["rev"]:
                with job.lock:
                    entry["message"] = "注意: インデックスが古いコーパスで構築されています"
            ctx = EngineContext(llm=LLMClient(cfg), cfg=cfg, progress=progress)
            result = engine.query(index, job.question, job.mode, ctx)
            with job.lock:
                entry["status"] = "done"
                entry["progress"] = 1.0
                entry["result"] = result
                entry["result"]["logs"] = ctx.logs
                entry["llm_stats"] = ctx.llm.stats
        except Exception as e:  # noqa: BLE001
            with job.lock:
                entry["status"] = "error"
                entry["error"] = f"{type(e).__name__}: {e}"
                entry["message"] = "失敗"
            traceback.print_exc()
        finally:
            with job.lock:
                entry["elapsed"] = round(time.time() - start, 2)

    # ------------------------------------------------------------ synthesis
    def _synthesize(self, job: Job, cfg: dict) -> None:
        with job.lock:
            answers = [(eid, e["result"]["answer"]) for eid, e in job.engines.items()
                       if e["status"] == "done" and e.get("result")]
        if len(answers) < 2:
            with job.lock:
                job.synthesis = {"status": "skipped", "text": "",
                                 "error": "成功したエンジンが2つ未満のため統合をスキップしました"}
            return
        if not chat_configured(cfg):
            with job.lock:
                job.synthesis = {"status": "skipped", "text": "",
                                 "error": "LLM 未設定のため統合をスキップしました"}
            return
        with job.lock:
            job.synthesis = {"status": "running", "text": "", "error": ""}
        try:
            blocks = []
            for eid, answer in answers:
                engine = get_engine(eid)
                name = engine.name if engine else eid
                blocks.append(f"## {name}\n{answer[:4000]}")
            llm = LLMClient(cfg)
            text = llm.chat(
                SYNTHESIS_PROMPT.format(question=job.question,
                                        answers="\n\n".join(blocks)),
                system=SYNTHESIS_SYSTEM, temperature=0.0)
            with job.lock:
                job.synthesis = {"status": "done", "text": text, "error": ""}
        except Exception as e:  # noqa: BLE001
            with job.lock:
                job.synthesis = {"status": "error", "text": "",
                                 "error": f"{type(e).__name__}: {e}"}
