"""バックグラウンド処理（読み込み・確認・意味検索の索引作り）。同時に 1 つだけ動かす。"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Callable

from .index import Cancelled


class JobBusy(Exception):
    pass


class Job:
    def __init__(self, kind: str, label: str, prefixes: list[str] | None):
        self.kind = kind
        self.label = label
        self.prefixes = prefixes
        self.cancel = threading.Event()
        self.state = "running"            # running / done / error / cancelled
        self.phase = "準備中"
        self.done = 0
        self.total = 0
        self.current = ""
        self.message = ""
        self.result: dict = {}
        self.started = time.time()
        self.finished: float | None = None

    def progress(self, phase: str, done: int, total: int, current: str = "") -> None:
        self.phase, self.done, self.total, self.current = phase, done, total, current

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "prefixes": self.prefixes, "state": self.state,
                "phase": self.phase, "done": self.done, "total": self.total, "current": self.current,
                "message": self.message, "result": self.result, "started": self.started,
                "finished": self.finished}


class JobRunner:
    def __init__(self):
        self.job: Job | None = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self.job is not None and self.job.state == "running"

    def start(self, kind: str, label: str, prefixes: list[str] | None,
              fn: Callable[[Job], dict], wait: bool = False) -> dict:
        with self._lock:
            if self.running():
                raise JobBusy(f"「{self.job.label}」を実行中です。終わるまで待つか中断してください")
            job = Job(kind, label, prefixes)
            self.job = job

        def run():
            try:
                job.result = fn(job) or {}
                job.state = "done"
            except Cancelled:
                job.state = "cancelled"
                job.message = "中断しました（ここまでの読み込みは保存されています）"
            except Exception as e:  # noqa: BLE001 - 画面に出す
                traceback.print_exc()
                job.state = "error"
                job.message = str(e)
            finally:
                job.finished = time.time()

        if wait:
            run()
        else:
            threading.Thread(target=run, daemon=True).start()
        return job.to_dict()

    def status(self) -> dict | None:
        return self.job.to_dict() if self.job else None

    def cancel(self) -> bool:
        if self.running():
            self.job.cancel.set()
            return True
        return False

    def wait(self, timeout: float = 60.0) -> dict | None:
        end = time.time() + timeout
        while self.running() and time.time() < end:
            time.sleep(0.05)
        return self.status()
