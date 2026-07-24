"""統合テスト共通ヘルパー。"""

from __future__ import annotations

import time
import uuid
from typing import Any

FAST = {"speed_factor": 0.1, "seed": 42}


def create_job(
    api_client,
    engines: list[str],
    *,
    topic: str = "テスト用の調査テーマ",
    auto_synthesize: bool = False,
    engine_options: dict[str, dict[str, Any]] | None = None,
    idempotency_key: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    body = {
        "topic": topic,
        "language": "ja",
        "engines": engines,
        "auto_synthesize": auto_synthesize,
        "engine_options": engine_options or {e: dict(FAST) for e in engines},
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        **kwargs,
    }
    resp = api_client.post("/api/jobs", json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def get_job(api_client, job_id: str) -> dict[str, Any]:
    resp = api_client.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def wait_for_job(api_client, job_id: str, *, timeout: float = 60.0,
                 statuses: tuple[str, ...] = ("completed", "partial", "failed", "cancelled")
                 ) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = get_job(api_client, job_id)
        if last["status"] in statuses:
            return last
        time.sleep(0.3)
    raise AssertionError(
        f"job {job_id} が {timeout}s 以内に {statuses} へ到達しません。"
        f"現状: {last.get('status')} runs="
        f"{[(r['engine_id'], r['status']) for r in last.get('runs', [])]}"
    )


def wait_for_run_status(api_client, job_id: str, engine_id: str, statuses: tuple[str, ...],
                        timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_job(api_client, job_id)
        for run in job["runs"]:
            if run["engine_id"] == engine_id and run["status"] in statuses:
                return run
        time.sleep(0.2)
    raise AssertionError(f"run {engine_id} が {statuses} になりません")


def event_history(api_client, job_id: str, after: int = 0) -> list[dict[str, Any]]:
    resp = api_client.get(f"/api/jobs/{job_id}/events/history",
                          params={"after": after, "limit": 2000})
    assert resp.status_code == 200
    return resp.json()


def setup_llm_profile(api_client, endpoint: str, *, name: str | None = None,
                      api_key: str | None = None, model: str = "test-model") -> str:
    """OpenAI互換fixtureへのlocal profileを作成し全roleへ割り当てる。"""
    name = name or f"test-profile-{uuid.uuid4().hex[:6]}"
    resp = api_client.post(
        "/api/settings/llm-profiles",
        json={
            "name": name,
            "provider": "local",
            "api": "openai-compatible",
            "endpoint": endpoint,
            "model": model,
            "api_key": api_key,
            "timeout_seconds": 30,
            "max_concurrency": 4,
            "enabled": True,
        },
    )
    assert resp.status_code == 201, resp.text
    profile_id = resp.json()["id"]
    resp = api_client.put(
        "/api/settings/roles",
        json={"assignments": {role: profile_id for role in
                              ("research", "summarization", "normalization", "synthesis")}},
    )
    assert resp.status_code == 200, resp.text
    return profile_id


def clear_roles(api_client) -> None:
    api_client.put(
        "/api/settings/roles",
        json={"assignments": {role: None for role in
                              ("research", "summarization", "normalization", "synthesis")}},
    )
