"""ウインドウ（作業担当）の割り当て。

- 担当は会話に「貼り付く」（同じ会話は同じウインドウで続きを送れる）
- 空きが無いときは、使用中でない担当のうち一番長く使われていないものを取り上げる
  （取り上げられた会話は、次に来たとき新しい Copilot 会話として全履歴を送り直す）
- それも無ければ空くまで待つ
- クリップボードはパソコン全体で 1 つなので、読み書きは ``clipboard_lock`` で順番待ちにする
"""

from __future__ import annotations

import threading
import time


class BridgeError(RuntimeError):
    """担当側の失敗（制限・画面の異常など）。HTTP 503 で返す。"""


class WorkerRestarted(BridgeError):
    """担当がブラウザを起動し直した（接続切れ）。Copilot 側の会話は失われているので、
    呼び出し側は会話を作り直して 1 回だけやり直す。"""


class Worker:
    """作業担当の共通インタフェース。裏方（edge / fake）は start/new_chat/send/close を実装する。"""

    def __init__(self, index: int, cfg):
        self.index = index
        self.id = f"w{index}"
        self.cfg = cfg
        self.session_id: str | None = None
        self.busy = False
        self.last_used = 0.0
        self.turns = 0
        self.error: str = ""
        # acquire 時に設定: True なら前回と同じ会話に貼り付いたまま（Copilot 側の会話を続けられる）
        self.resumed = False

    def start(self) -> None:  # noqa: D401
        pass

    def new_chat(self) -> None:
        raise NotImplementedError

    def send(self, text: str) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def describe(self) -> dict:
        return {"id": self.id, "session": self.session_id, "busy": self.busy,
                "turns": self.turns, "last_used": round(self.last_used, 1), "error": self.error}


class WorkerPool:
    clipboard_lock = threading.Lock()

    def __init__(self, workers: list[Worker]):
        self.workers = workers
        self._cv = threading.Condition()

    def start_all(self) -> None:
        for w in self.workers:
            w.start()

    def close_all(self) -> None:
        for w in self.workers:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    def acquire(self, session_id: str, timeout: float) -> Worker:
        """会話に担当を割り当てて返す。返った担当は busy。"""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                mine = next((w for w in self.workers if w.session_id == session_id), None)
                if mine is not None:
                    if not mine.busy:
                        return self._take(mine, session_id)
                else:
                    free = [w for w in self.workers if not w.busy and w.session_id is None]
                    if free:
                        return self._take(free[0], session_id)
                    idle = [w for w in self.workers if not w.busy]
                    if idle:
                        victim = min(idle, key=lambda w: w.last_used)
                        return self._take(victim, session_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError(f"空きウインドウを {timeout:.0f} 秒待ちましたが取れませんでした")
                self._cv.wait(timeout=min(remaining, 5.0))

    def _take(self, w: Worker, session_id: str) -> Worker:
        w.resumed = (w.session_id == session_id)
        w.busy = True
        w.session_id = session_id
        w.last_used = time.time()
        return w

    def release(self, w: Worker, *, detach: bool = False) -> None:
        with self._cv:
            w.busy = False
            w.last_used = time.time()
            if detach:
                w.session_id = None
            self._cv.notify_all()

    def describe(self) -> list[dict]:
        with self._cv:
            return [w.describe() for w in self.workers]
