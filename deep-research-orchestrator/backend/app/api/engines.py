"""エンジン一覧・capabilities・health API。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import db_session, settings_dep
from app.api.schemas import EngineView
from app.config import Settings
from app.db.models import EngineConfig
from app.runners.client import RunnerClient, RunnerError

router = APIRouter(prefix="/api", tags=["engines"])

# capabilitiesの短期キャッシュ (runner_url -> (expires, data))
_caps_cache: dict[str, tuple[float, list[dict[str, Any]] | None]] = {}
_CAPS_TTL = 15.0


def _fetch_capabilities(runner_url: str, settings: Settings) -> list[dict[str, Any]] | None:
    import time

    cached = _caps_cache.get(runner_url)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    data: list[dict[str, Any]] | None
    try:
        with RunnerClient(runner_url, settings, timeout=5.0) as client:
            data = client.capabilities()
    except RunnerError:
        data = None
    _caps_cache[runner_url] = (time.monotonic() + _CAPS_TTL, data)
    return data


@router.get("/engines", response_model=list[EngineView])
async def list_engines(
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> list[EngineView]:
    configs = list(session.scalars(select(EngineConfig).order_by(EngineConfig.engine_id)))
    runner_urls = sorted({c.runner_url for c in configs if c.runner_url})
    caps_by_url: dict[str, list[dict[str, Any]] | None] = {}
    results = await asyncio.gather(
        *[asyncio.to_thread(_fetch_capabilities, url, settings) for url in runner_urls]
    )
    for url, caps in zip(runner_urls, results, strict=True):
        caps_by_url[url] = caps

    views: list[EngineView] = []
    for cfg in configs:
        runner_caps = caps_by_url.get(cfg.runner_url)
        engine_caps = None
        healthy: bool | None = None
        if runner_caps is not None:
            healthy = False
            for c in runner_caps:
                if c.get("engine_id") == cfg.engine_id:
                    engine_caps = c
                    healthy = c.get("health") == "available"
                    break
        availability = cfg.availability
        reason = cfg.unavailable_reason
        if cfg.enabled and availability == "available":
            if runner_caps is None:
                availability = "unhealthy"
                reason = "Runnerへ接続できません"
            elif engine_caps is None:
                availability = "unhealthy"
                reason = "Runnerがこのエンジンを提供していません"
            elif engine_caps.get("health") != "available":
                availability = engine_caps.get("health", "unhealthy")
                reason = engine_caps.get("health_reason")
        views.append(
            EngineView(
                engine_id=cfg.engine_id,
                display_name=cfg.display_name,
                enabled=cfg.enabled,
                availability=availability,
                unavailable_reason=reason,
                max_concurrency=cfg.max_concurrency,
                capabilities=engine_caps,
                healthy=healthy,
            )
        )
    return views
