"""SSEイベント配信。

全イベントはPostgreSQL (job_events) に連番付きで永続化済み。
`Last-Event-ID` header または `?after=` から再送する (acceptance 4)。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Header, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.db.events import events_after
from app.db.models import JOB_TERMINAL, JobStatus, ResearchJob
from app.db.session import get_session_factory

router = APIRouter(prefix="/api", tags=["events"])

POLL_INTERVAL = 0.4
KEEPALIVE_SECONDS = 15.0
# job終了後もこの猶予だけ配信を続けてから閉じる (末尾イベントの取りこぼし防止)
LINGER_AFTER_TERMINAL = 2.0


def _fetch_batch(job_id: str, after: int) -> tuple[list[dict], bool]:
    session = get_session_factory()()
    try:
        events = events_after(session, job_id, after)
        job = session.get(ResearchJob, job_id)
        terminal = job is not None and JobStatus(job.status) in JOB_TERMINAL
        return (
            [
                {
                    "id": str(e.seq),
                    "event": e.type,
                    "data": json.dumps(
                        {
                            "seq": e.seq,
                            "type": e.type,
                            "run_id": e.run_id,
                            "engine_id": e.engine_id,
                            "payload": e.payload,
                            "created_at": e.created_at.isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                }
                for e in events
            ],
            terminal,
        )
    finally:
        session.close()


def _job_exists(job_id: str) -> bool:
    session = get_session_factory()()
    try:
        return session.get(ResearchJob, job_id) is not None
    finally:
        session.close()


@router.get("/jobs/{job_id}/events")
async def stream_events(
    job_id: str,
    request: Request,
    after: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    if not await asyncio.to_thread(_job_exists, job_id):
        raise HTTPException(status_code=404, detail="job not found")

    start_after = 0
    if last_event_id is not None:
        try:
            start_after = int(last_event_id)
        except ValueError:
            start_after = 0
    if after is not None:
        start_after = after

    async def generator():
        cursor = start_after
        idle = 0.0
        terminal_linger: float | None = None
        while True:
            if await request.is_disconnected():
                return
            batch, terminal = await asyncio.to_thread(_fetch_batch, job_id, cursor)
            for item in batch:
                cursor = int(item["id"])
                idle = 0.0
                yield item
            if terminal:
                if terminal_linger is None:
                    terminal_linger = 0.0
                elif terminal_linger >= LINGER_AFTER_TERMINAL and not batch:
                    yield {"event": "stream_end", "data": json.dumps({"reason": "job_finished"})}
                    return
                terminal_linger += POLL_INTERVAL
            if idle >= KEEPALIVE_SECONDS:
                idle = 0.0
                yield {"comment": "keepalive"}
            await asyncio.sleep(POLL_INTERVAL)
            idle += POLL_INTERVAL

    return EventSourceResponse(generator())


@router.get("/jobs/{job_id}/events/history")
async def event_history(
    job_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
):
    """SSEを使わない取得 (デバッグ・エクスポート用)。"""

    def _fetch() -> list[dict]:
        session = get_session_factory()()
        try:
            if session.get(ResearchJob, job_id) is None:
                return []
            return [
                {
                    "seq": e.seq,
                    "type": e.type,
                    "run_id": e.run_id,
                    "engine_id": e.engine_id,
                    "payload": e.payload,
                    "created_at": e.created_at.isoformat(),
                }
                for e in events_after(session, job_id, after, limit)
            ]
        finally:
            session.close()

    return await asyncio.to_thread(_fetch)
