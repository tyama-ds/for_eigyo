"""Celery application。

at-least-once配信を前提に全タスクを冪等に実装する (tasks.py参照)。
"""

from __future__ import annotations

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "dro",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.orchestrator.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    beat_schedule={
        "reconcile-stuck-runs": {
            "task": "app.orchestrator.tasks.reconcile_stuck_runs",
            "schedule": 30.0,
        },
        "retention-cleanup": {
            "task": "app.orchestrator.tasks.retention_cleanup",
            "schedule": 3600.0,
        },
    },
)
