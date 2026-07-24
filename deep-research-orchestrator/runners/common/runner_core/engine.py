"""Engine interface と実行コンテキスト。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from runner_core.models import EngineCapabilities, RunEvent, RunRequest, RunResult


class CancelledByUser(Exception):
    """ユーザー起点のキャンセル。"""


class RunContext:
    """エンジン実装がイベントを発行し、キャンセルを検知するためのコンテキスト。"""

    def __init__(self, run_id: str, request: RunRequest):
        self.run_id = run_id
        self.request = request
        self._events: list[RunEvent] = []
        self._seq = 0
        self._cancel_event = asyncio.Event()
        self._lock = asyncio.Lock()

    def emit(self, type: str, payload: dict[str, Any] | None = None) -> None:
        self._seq += 1
        self._events.append(
            RunEvent(
                seq=self._seq,
                type=type,
                payload=payload or {},
                ts=datetime.now(UTC).isoformat(),
            )
        )

    @property
    def events(self) -> list[RunEvent]:
        return self._events

    @property
    def last_seq(self) -> int:
        return self._seq

    def request_cancel(self) -> None:
        self._cancel_event.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise CancelledByUser()

    async def sleep(self, seconds: float) -> None:
        """キャンセル可能なsleep。"""
        try:
            await asyncio.wait_for(self._cancel_event.wait(), timeout=seconds)
            raise CancelledByUser()
        except TimeoutError:
            return


class Engine(ABC):
    engine_id: str

    @abstractmethod
    def capabilities(self) -> EngineCapabilities: ...

    @abstractmethod
    async def run(self, ctx: RunContext) -> RunResult: ...
