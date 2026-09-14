"""学習ジョブの管理（同時実行は 1 本。進捗はポーリングで取得）。"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections import deque

LOG_LINES = 400
MAX_STEP_POINTS = 600


class JobCancelled(Exception):
    """ユーザーによる中止。"""


class JobBusy(Exception):
    """別のジョブが実行中。"""


class Job:
    def __init__(self, kind: str, params: dict):
        self.id = time.strftime("j%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.kind = kind
        self.params = params
        self.status = "queued"          # queued / running / done / failed / cancelled
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.progress: dict = {"pct": 0.0, "phase": "待機中"}
        self.curves: list[dict] = []            # エポック毎の指標
        self.step_losses: list[list[float]] = []  # [step, loss]（間引き済み）
        self._step_seen = 0
        self._step_every = 1
        self.log_lines: deque = deque(maxlen=LOG_LINES)
        self.log_total = 0
        self.result: dict | None = None
        self.error: str | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ---- 学習側から呼ぶ
    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self._lock:
            self.log_lines.append(line)
            self.log_total += 1
        print(f"  {self.id} {line}", flush=True)

    def update(self, **kw) -> None:
        with self._lock:
            self.progress.update(kw)

    def epoch_done(self, record: dict) -> None:
        with self._lock:
            self.curves.append(record)

    def step_loss(self, step: int, loss: float) -> None:
        with self._lock:
            self._step_seen += 1
            if self._step_seen % self._step_every:
                return
            self.step_losses.append([step, loss])
            if len(self.step_losses) > MAX_STEP_POINTS:      # 半分に間引く
                self.step_losses = self.step_losses[::2]
                self._step_every *= 2

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ---- API 側
    def snapshot(self, log_from: int = 0) -> dict:
        with self._lock:
            lines = list(self.log_lines)
            first_index = self.log_total - len(lines)
            skip = max(0, log_from - first_index)
            now = time.time()
            elapsed = ((self.finished or now) - self.started) if self.started else 0.0
            return {
                "id": self.id, "kind": self.kind, "status": self.status, "params": self.params,
                "progress": dict(self.progress), "curves": list(self.curves),
                "step_losses": list(self.step_losses),
                "log": lines[skip:], "log_next": self.log_total,
                "elapsed": elapsed, "result": self.result, "error": self.error,
            }


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def current(self) -> Job | None:
        with self._lock:
            for jid in reversed(self._order):
                j = self._jobs[jid]
                if j.status in ("queued", "running"):
                    return j
        return None

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def start(self, kind: str, params: dict, fn) -> Job:
        """fn(job) を別スレッドで実行する。実行中ジョブがあれば JobBusy。"""
        with self._lock:
            for jid in self._order:
                if self._jobs[jid].status in ("queued", "running"):
                    raise JobBusy("別の学習が実行中です。完了または中止を待ってください")
            job = Job(kind, params)
            self._jobs[job.id] = job
            self._order.append(job.id)
            if len(self._order) > 50:
                old = self._order.pop(0)
                self._jobs.pop(old, None)

        def runner():
            job.status = "running"
            job.started = time.time()
            try:
                job.result = fn(job)
                job.status = "cancelled" if job.cancelled else "done"
            except JobCancelled:
                job.status = "cancelled"
                job.log("中止しました")
            except Exception as e:  # noqa: BLE001 — 失敗理由は UI に出す
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                job.log("エラー: " + job.error)
                for line in traceback.format_exc().rstrip().splitlines()[-12:]:
                    job.log("  " + line)
            finally:
                job.finished = time.time()
                job.update(pct=100.0 if job.status == "done" else job.progress.get("pct", 0.0))

        threading.Thread(target=runner, name=f"job-{job.id}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job._cancel.set()
        job.log("中止要求を受け付けました（現在のステップ終了後に停止）")
        return True

    def list(self) -> list[dict]:
        return [self._jobs[j].snapshot() for j in self._order]
