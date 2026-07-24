"""Control API — FastAPI application。"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api import artifacts, engines, events, jobs, settings_routes
from app.bootstrap import bootstrap
from app.config import get_settings
from app.db.session import session_scope
from app.security.redaction import redact


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")

    def _redact_processor(logger, method_name, event_dict):
        for key, value in list(event_dict.items()):
            if isinstance(value, str):
                event_dict[key] = redact(value)
        return event_dict

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            _redact_processor,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    with session_scope() as session:
        bootstrap(session, settings)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Deep Research Orchestrator API",
        version="0.1.0",
        lifespan=lifespan,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # --- 簡易rate limit (per-instance, per-client-IP token bucket) ---
    buckets: dict[str, deque[float]] = defaultdict(deque)
    RATE_LIMIT = 120  # requests / 60s
    WINDOW = 60.0

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        # SSEは長時間接続なので対象外
        if request.url.path.endswith("/events"):
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = buckets[client_ip]
        while bucket and bucket[0] < now - WINDOW:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT:
            return Response(status_code=429, content="rate limit exceeded")
        bucket.append(now)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(jobs.router)
    app.include_router(events.router)
    app.include_router(engines.router)
    app.include_router(artifacts.router)
    app.include_router(settings_routes.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        import asyncio

        from sqlalchemy import text as sql_text

        from app.db.session import get_session_factory

        def _check_db() -> bool:
            try:
                s = get_session_factory()()
                try:
                    s.execute(sql_text("SELECT 1"))
                    return True
                finally:
                    s.close()
            except Exception:
                return False

        ok = await asyncio.to_thread(_check_db)
        if not ok:
            return Response(status_code=503, content='{"status":"db_unavailable"}',
                            media_type="application/json")
        return Response(status_code=200, content='{"status":"ready"}',
                        media_type="application/json")

    return app


app = create_app()
