"""RunManager — 調査タスクの実行・進捗イベント配信・キャンセル。

外部キュー製品を使わず、asyncioタスク+プロジェクトごとのイベントキューで
SSE配信する(要件§17: HTTPを無期限ブロックしない)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import Any


class RunManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._last_events: dict[str, list[dict]] = {}

    def is_running(self, project_id: str) -> bool:
        task = self._tasks.get(project_id)
        return task is not None and not task.done()

    def emit(self, project_id: str, event_type: str, message: str, data: dict[str, Any]) -> None:
        event = {
            "type": event_type,
            "message": message,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        history = self._last_events.setdefault(project_id, [])
        history.append(event)
        if len(history) > 200:
            del history[: len(history) - 200]
        for queue in self._subscribers.get(project_id, []):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    def start(self, project_id: str, coro) -> None:
        if self.is_running(project_id):
            raise RuntimeError("この プロジェクトの調査は既に実行中です")
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks[project_id] = task

    async def wait(self, project_id: str) -> None:
        task = self._tasks.get(project_id)
        if task is not None:
            await task

    def subscribe(self, project_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.setdefault(project_id, []).append(queue)
        # 直近のイベント履歴を再生(途中参加でも状況が分かるように)
        for event in self._last_events.get(project_id, [])[-30:]:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        return queue

    def unsubscribe(self, project_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(project_id, [])
        if queue in subs:
            subs.remove(queue)


def sse_format(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
