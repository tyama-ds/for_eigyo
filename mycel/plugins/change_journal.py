"""変更ジャーナル: 誰が・いつ・どのノートを・どの版にしたかを追記していく。

``<vault>/.mycel/plugins/change_journal/journal.jsonl`` に 1 行 1 イベントで書く。
個人利用では「最近変えたノート」の一覧に使い、将来の共有・同期では
相手に送る差分の元（どこまで同期したかの目印）として使える。
"""
from __future__ import annotations

import json
import threading
from collections import deque

from mycelcore.plugins import NoteEvent, Plugin


class ChangeJournal(Plugin):
    name = "変更ジャーナル"
    description = "ノートの作成・保存・削除・名前変更を作成者付きで記録します（共有・同期の基盤）"

    def setup(self, ctx) -> None:
        super().setup(ctx)
        self.file = ctx.data_dir("change_journal") / "journal.jsonl"
        self._lock = threading.Lock()

    def _write(self, event: NoteEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with self._lock, self.file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    on_created = on_saved = on_deleted = on_renamed = _write

    def recent(self, limit: int = 50) -> list[dict]:
        if not self.file.exists():
            return []
        with self._lock, self.file.open(encoding="utf-8") as f:
            tail = deque(f, maxlen=limit * 5)
        out, seen = [], set()
        for line in reversed(tail):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            # 自動保存の連続はノートごとに最新の 1 件にまとめる
            if ev.get("path") in seen:
                continue
            seen.add(ev.get("path"))
            out.append(ev)
            if len(out) >= limit:
                break
        return out

    def routes(self):
        return {"recent": lambda p: {"events": self.recent(int(p.get("limit") or 50))}}

    def status(self) -> dict:
        size = self.file.stat().st_size if self.file.exists() else 0
        return {"file": str(self.file), "bytes": size}
