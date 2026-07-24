"""Runner API v1 — FastAPI application factory。

全Runner (mock / gpt-researcher / open_deep_research) が同じAPIを実装する:

    GET    /v1/capabilities
    POST   /v1/runs
    GET    /v1/runs/{run_id}
    GET    /v1/runs/{run_id}/events?after={sequence}
    DELETE /v1/runs/{run_id}
    GET    /v1/runs/{run_id}/result

- POST /v1/runs は client_run_id で冪等 (同じIDの再送は既存runを返す)
- Runner内の状態はin-memory。永続化と再試行はControl Planeの責務。
- RUNNER_SHARED_TOKEN が設定されている場合、X-Runner-Token headerを検証する。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from runner_core.engine import CancelledByUser, Engine, RunContext
from runner_core.models import (
    TERMINAL_STATES,
    RunRequest,
    RunResult,
    RunStatusResponse,
)


class _RunRecord:
    def __init__(self, run_id: str, request: RunRequest, ctx: RunContext):
        self.run_id = run_id
        self.request = request
        self.ctx = ctx
        self.state: str = "queued"
        self.stage: str | None = None
        self.error: str | None = None
        self.result: RunResult | None = None
        self.created_at = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.task: asyncio.Task | None = None

    def status(self) -> RunStatusResponse:
        return RunStatusResponse(
            run_id=self.run_id,
            engine_id=self.request.engine_id,
            state=self.state,  # type: ignore[arg-type]
            stage=self.stage,
            error=self.error,
            created_at=self.created_at.isoformat(),
            started_at=self.started_at.isoformat() if self.started_at else None,
            finished_at=self.finished_at.isoformat() if self.finished_at else None,
            last_seq=self.ctx.last_seq,
        )


class CreateRunResponse(BaseModel):
    run_id: str
    state: str


def create_runner_app(engines: dict[str, Engine], *, title: str) -> FastAPI:
    app = FastAPI(title=title, version="1.0.0")
    runs: dict[str, _RunRecord] = {}
    shared_token = os.environ.get("RUNNER_SHARED_TOKEN") or None

    def _auth(x_runner_token: str | None = Header(default=None)) -> None:
        if shared_token and x_runner_token != shared_token:
            raise HTTPException(status_code=401, detail="invalid runner token")

    def _get_run(run_id: str) -> _RunRecord:
        record = runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        return record

    async def _execute(record: _RunRecord, engine: Engine) -> None:
        ctx = record.ctx
        record.state = "starting"
        record.started_at = datetime.now(UTC)
        ctx.emit("status", {"state": "starting"})
        try:
            record.state = "researching"
            ctx.emit("status", {"state": "researching"})
            timeout = record.request.max_time_seconds
            if timeout:
                result = await asyncio.wait_for(engine.run(ctx), timeout=timeout)
            else:
                result = await engine.run(ctx)
            record.result = result
            record.state = "succeeded"
            ctx.emit("result_ready", {"output_kind": result.output_kind})
            ctx.emit("status", {"state": "succeeded"})
        except CancelledByUser:
            record.state = "cancelled"
            ctx.emit("status", {"state": "cancelled"})
        except TimeoutError:
            record.state = "timed_out"
            record.error = "Runner内タイムアウト"
            ctx.emit("status", {"state": "timed_out"})
        except asyncio.CancelledError:
            record.state = "cancelled"
            ctx.emit("status", {"state": "cancelled"})
            raise
        except Exception as e:  # noqa: BLE001 - engine例外は結果へ変換
            record.state = "failed"
            record.error = f"{type(e).__name__}: {e}"
            ctx.emit("error", {"message": record.error})
            ctx.emit("status", {"state": "failed"})
        finally:
            record.finished_at = datetime.now(UTC)

    @app.get("/v1/capabilities", dependencies=[Depends(_auth)])
    async def capabilities() -> list[dict[str, Any]]:
        return [e.capabilities().model_dump() for e in engines.values()]

    @app.post("/v1/runs", dependencies=[Depends(_auth)], status_code=201)
    async def create_run(request: RunRequest) -> CreateRunResponse:
        existing = runs.get(request.client_run_id)
        if existing is not None:
            # 冪等: 同じclient_run_idの再送は既存runを返す (重複実行防止)
            return CreateRunResponse(run_id=existing.run_id, state=existing.state)
        engine = engines.get(request.engine_id)
        if engine is None:
            raise HTTPException(status_code=400, detail=f"unknown engine: {request.engine_id}")
        caps = engine.capabilities()
        if caps.health != "available":
            raise HTTPException(
                status_code=409,
                detail=f"engine {request.engine_id} is {caps.health}: {caps.health_reason}",
            )
        record = _RunRecord(request.client_run_id, request, RunContext(request.client_run_id, request))
        runs[record.run_id] = record
        record.task = asyncio.create_task(_execute(record, engine))
        return CreateRunResponse(run_id=record.run_id, state=record.state)

    @app.get("/v1/runs/{run_id}", dependencies=[Depends(_auth)])
    async def get_run(run_id: str) -> RunStatusResponse:
        return _get_run(run_id).status()

    @app.get("/v1/runs/{run_id}/events", dependencies=[Depends(_auth)])
    async def get_events(run_id: str, after: int = 0) -> dict[str, Any]:
        record = _get_run(run_id)
        events = [e.model_dump() for e in record.ctx.events if e.seq > after]
        return {"run_id": run_id, "events": events, "last_seq": record.ctx.last_seq}

    @app.delete("/v1/runs/{run_id}", dependencies=[Depends(_auth)])
    async def cancel_run(run_id: str) -> RunStatusResponse:
        record = _get_run(run_id)
        if record.state not in TERMINAL_STATES:
            record.ctx.request_cancel()
            # engineが協調キャンセルしない場合に備え、猶予後にtask cancel
            if record.task is not None:
                task = record.task

                async def _force_cancel() -> None:
                    await asyncio.sleep(10)
                    if not task.done():
                        task.cancel()

                asyncio.create_task(_force_cancel())
        return record.status()

    @app.get("/v1/runs/{run_id}/result", dependencies=[Depends(_auth)])
    async def get_result(run_id: str) -> dict[str, Any]:
        record = _get_run(run_id)
        if record.state not in TERMINAL_STATES:
            raise HTTPException(status_code=409, detail="run is not finished")
        if record.result is None:
            raise HTTPException(
                status_code=404, detail=f"no result (state={record.state}, error={record.error})"
            )
        return record.result.model_dump()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
