"""ブラウザ無しで動く作業担当（テスト・開発用）。

``FakeWorker.responder`` に「送信文 → 回答文」の関数を入れ替えると、テストから
Copilot の返事を好きに作れる。既定は送信文の末尾を返すだけ。
"""

from __future__ import annotations

import threading
import time

from .pool import Worker


def default_responder(text: str, worker: "FakeWorker") -> str:
    tail = text.strip().splitlines()[-1] if text.strip() else ""
    return f"（fake {worker.id}）{tail[:200]}"


class FakeWorker(Worker):
    responder = staticmethod(default_responder)
    delay_sec = 0.0

    def __init__(self, index: int, cfg):
        super().__init__(index, cfg)
        self.chats = 0
        self.models_selected: list = []
        self.sent_texts: list[str] = []
        self._lock = threading.Lock()

    def new_chat(self, model: str | None = None) -> None:
        self.chats += 1
        self.models_selected.append(model)

    def send(self, text: str) -> str:
        with self._lock:
            self.sent_texts.append(text)
            self.turns += 1
            if self.delay_sec:
                time.sleep(self.delay_sec)
            return type(self).responder(text, self)

    def describe(self) -> dict:
        d = super().describe()
        d["chats"] = self.chats
        return d
