"""Runner API v1 クライアント (Control Plane側)。

Runnerは同一compose network内のinternal serviceなのでorigin=internal
(SSRFガード対象外・NO_PROXY)。RUNNER_SHARED_TOKENがあれば送る。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


class RunnerError(RuntimeError):
    pass


class RunnerUnavailableError(RunnerError):
    pass


class RunnerClient:
    def __init__(self, base_url: str, settings: Settings, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        headers = {}
        if settings.runner_shared_token:
            headers["X-Runner-Token"] = settings.runner_shared_token
        self._client = httpx.Client(timeout=timeout, headers=headers)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RunnerClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise RunnerUnavailableError(f"Runnerへ接続できません ({self.base_url}): {e}") from e
        return resp

    def capabilities(self) -> list[dict[str, Any]]:
        resp = self._request("GET", "/v1/capabilities")
        if resp.status_code != 200:
            raise RunnerError(f"capabilities取得失敗: HTTP {resp.status_code}")
        return resp.json()

    def create_run(self, request_body: dict[str, Any]) -> dict[str, Any]:
        resp = self._request("POST", "/v1/runs", json=request_body)
        if resp.status_code == 409:
            raise RunnerError(f"engine利用不可: {resp.json().get('detail')}")
        if resp.status_code not in (200, 201):
            raise RunnerError(f"run作成失敗: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def get_run(self, run_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"/v1/runs/{run_id}")
        if resp.status_code == 404:
            raise RunnerError(f"run {run_id} がRunnerに存在しません (Runner再起動の可能性)")
        if resp.status_code != 200:
            raise RunnerError(f"run取得失敗: HTTP {resp.status_code}")
        return resp.json()

    def get_events(self, run_id: str, after: int = 0) -> dict[str, Any]:
        resp = self._request("GET", f"/v1/runs/{run_id}/events", params={"after": after})
        if resp.status_code != 200:
            raise RunnerError(f"events取得失敗: HTTP {resp.status_code}")
        return resp.json()

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        resp = self._request("DELETE", f"/v1/runs/{run_id}")
        if resp.status_code == 404:
            return {"state": "unknown"}
        if resp.status_code != 200:
            raise RunnerError(f"cancel失敗: HTTP {resp.status_code}")
        return resp.json()

    def get_result(self, run_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"/v1/runs/{run_id}/result")
        if resp.status_code == 404:
            raise RunnerError(f"result未存在: {resp.json().get('detail', '')}")
        if resp.status_code == 409:
            raise RunnerError("runが未完了のためresultを取得できません")
        if resp.status_code != 200:
            raise RunnerError(f"result取得失敗: HTTP {resp.status_code}")
        return resp.json()

    def healthz(self) -> bool:
        try:
            return self._request("GET", "/healthz").status_code == 200
        except RunnerError:
            return False
