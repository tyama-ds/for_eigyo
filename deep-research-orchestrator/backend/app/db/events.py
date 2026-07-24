"""ジョブイベントの永続化 — SSE配信の正本。

seqはjob単位で単調増加。PostgreSQLの行ロックで採番の競合を防ぐ。
イベントpayloadは保存前にredactionを通す。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import JobEvent
from app.security.redaction import redact


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return json.loads(redact(text))


def append_event(
    session: Session,
    *,
    job_id: str,
    type: str,
    payload: dict[str, Any] | None = None,
    run_id: str | None = None,
    engine_id: str | None = None,
) -> JobEvent:
    """イベントを追記する。呼び出し側のtransaction内で実行される。"""
    # job内seq採番: 同時書き込みに対しadvisory lockで直列化
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(job_id)))
    )
    max_seq = session.scalar(
        select(func.coalesce(func.max(JobEvent.seq), 0)).where(JobEvent.job_id == job_id)
    )
    event = JobEvent(
        job_id=job_id,
        seq=(max_seq or 0) + 1,
        run_id=run_id,
        engine_id=engine_id,
        type=type,
        payload=_redact_payload(payload or {}),
    )
    session.add(event)
    session.flush()
    return event


def events_after(session: Session, job_id: str, after_seq: int, limit: int = 500) -> list[JobEvent]:
    return list(
        session.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.seq > after_seq)
            .order_by(JobEvent.seq)
            .limit(limit)
        )
    )
