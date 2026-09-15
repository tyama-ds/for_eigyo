"""Browser headers remain request-scoped, including every queued API operation."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connection_settings as connections, main, foresight_api, storage


def headers(key="browser-A", model="model-A", **extra):
    payload = {"openai": {"api_key": key, "model": model}, **extra}
    return {"X-Atlas-Connection": base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    with TestClient(main.app) as client:
        yield client


def test_browser_settings_are_isolated_redacted_and_have_no_env_fallback(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "old-env-secret")
    monkeypatch.setenv("OPENAI_MODEL", "old-env-model")
    monkeypatch.setenv("HTTPS_PROXY", "http://old-env-proxy:9999")
    before = client.get("/api/connections/status").json()
    assert before["openai"] == {"configured": False, "model": None}
    with TestClient(main.app) as other_browser:
        saved = client.get("/api/connections/status", headers=headers(proxy={"enabled": True,
            "url": "http://proxy.example:8080", "username": "利用者", "password": "proxy-secret"}))
        assert saved.status_code == 200
        assert saved.json()["openai"] == {"configured": True, "model": "model-A"}
        assert saved.json()["proxy"]["enabled"] is True
        assert not other_browser.get("/api/connections/status").json()["openai"]["configured"]
        assert other_browser.get("/api/connections/status", headers=headers("browser-B", "model-B")).json()["openai"]["model"] == "model-B"
    assert client.get("/api/connections/status").json() == before
    assert connections.current_settings().openai.api_key.get_secret_value() == ""
    for secret in ("browser-A", "proxy-secret", "old-env-secret", "利用者", "proxy.example"):
        assert secret not in saved.text
    assert saved.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("value", ["not base64 secret", "A" * 16385,
    base64.b64encode(b'{"openai":{"api_key":"bad-secret","unknown":"value"}}').decode(),
    base64.b64encode(b'{"proxy":{"url":"http://user:private-secret@proxy.example"}}').decode()])
def test_malformed_browser_headers_are_sanitized(client, value):
    response = client.get("/api/connections/status", headers={"X-Atlas-Connection": value})
    assert response.status_code == 422
    assert "secret" not in response.text and value not in response.text
    assert client.get("/api/connections/status").status_code == 200


def test_draft_test_is_request_scoped_and_never_saved(client, monkeypatch, tmp_path):
    observed = []
    def test_connection(target):
        observed.append((target, connections.current_settings().openai.api_key.get_secret_value()))
        return {"ok": True, "target": target, "message": "接続を確認しました。"}
    monkeypatch.setattr(connections, "test_connection", test_connection)
    response = client.post("/api/connections/test", json={"target": "openai"}, headers=headers())
    assert response.status_code == 200 and response.json()["ok"]
    assert observed == [("openai", "browser-A")]
    assert "browser-A" not in response.text
    assert not client.get("/api/connections/status").json()["openai"]["configured"]
    assert not list(tmp_path.rglob("*"))
    assert client.post("/api/connections/test", json={"target": "arbitrary-url"}).status_code == 422
    assert client.post("/api/connections/test", json={"target": "openai"},
                       headers={**headers(), "Origin": "https://external.example"}).status_code == 403
    assert len(observed) == 1


@pytest.mark.parametrize("base_url", ["http://192.168.10.20:1234/v1", "http://[fd00::20]:1234/v1",
                                     "https://llm-server.example/v1"])
def test_remote_llm_connection_check_uses_browser_header_without_persistence(client, monkeypatch, tmp_path, base_url):
    requests, transports = [], []
    real_client = httpx.Client
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "remote-model"}]})
    def transport(**kwargs):
        transports.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(connections.httpx, "Client", transport)
    response = client.post("/api/connections/test", json={"target": "local"}, headers=headers(
        local={"backend": "openai_compatible", "url": base_url, "model": "remote-model", "api_key": "local-secret"},
        proxy={"enabled": True, "url": "http://proxy.example:8080", "no_proxy": ""}))
    assert response.status_code == 200 and response.json()["ok"]
    assert len(requests) == 1 and requests[0].method == "GET"
    assert str(requests[0].url) == base_url + "/models" and not requests[0].content
    assert requests[0].headers["Authorization"] == "Bearer local-secret"
    assert "browser-A" not in str(requests[0].headers)
    assert transports[0]["proxy"] is None and transports[0]["trust_env"] is False and transports[0]["follow_redirects"] is False
    assert "secret" not in response.text and base_url not in response.text
    assert client.get("/api/connections/status").json()["local"]["model"] is None
    assert connections.current_settings().local.url == "http://127.0.0.1:11434"
    assert not list(tmp_path.rglob("*"))


@pytest.mark.parametrize("kind", ["discover", "analyze", "field", "authors", "assessment", "explore", "commentary"])
def test_all_queued_endpoints_keep_starting_browser_context(client, monkeypatch, tmp_path, kind):
    result_id, dataset_id, assessment_id = "a" * 32, "b" * 32, "c" * 32
    storage.save("datasets", {"id": dataset_id, "papers": [], "is_demo": False})
    storage.save("results", {"id": result_id, "topics": [{"id": "t1"}]})
    storage.save("assessments", {"id": assessment_id, "candidates": [{"id": "t1", "paper_ids": ["p1"]}],
        "papers": [{"id": "p1", "abstract": "We measured the tensile strength of stainless steel."}], "meta": {"is_demo": False}})
    cases = {
        "discover": (main, "SOURCE_EXECUTOR", "run_discovery", "/api/discover", {"provider": "crossref", "query": "steel"}),
        "analyze": (main, "EXECUTOR", "run_analysis", "/api/analyze", {"dataset_id": dataset_id}),
        "field": (main, "REPORT_EXECUTOR", "run_field_report", "/api/field-reports", {"result_id": result_id, "topic_id": "t1", "provider": "openai"}),
        "authors": (main, "AUTHOR_EXECUTOR", "run_author_network", "/api/author-networks", {"result_id": result_id}),
        "assessment": (foresight_api, "EXECUTOR", "run_create", "/api/assessments", {"result_id": result_id}),
        "explore": (foresight_api, "EXECUTOR", "run_explore", f"/api/assessments/{assessment_id}/explore", {"candidate_id": "t1", "mode": "external"}),
        "commentary": (foresight_api, "EXECUTOR", "run_commentary", f"/api/assessments/{assessment_id}/commentaries", {"candidate_id": "t1", "provider": "openai"}),
    }
    module, executor_name, worker_name, url, payload = cases[kind]
    gate, done = Event(), Event()
    observed = []
    def worker(job_id, *args):
        # This runs after the request that submitted it has already ended.
        value = connections.current_settings()
        observed.append((value.openai.api_key.get_secret_value(), value.openai.model,
                         value.proxy.password.get_secret_value(), args))
        with main.JOBS_LOCK:
            main.JOBS[job_id].update(status="completed", stage="検証完了")
        done.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(gate.wait, 10)
        monkeypatch.setattr(module, executor_name, executor)
        monkeypatch.setattr(module, worker_name, worker)
        try:
            response = client.post(url, json=payload, headers=headers(proxy={"enabled": True,
                "url": "http://proxy.example:8080", "username": "test-user", "password": "proxy-secret"}))
            assert response.status_code == 200, response.text
            assert client.get("/api/connections/status", headers=headers("browser-B", "model-B")).json()["openai"]["model"] == "model-B"
            assert not client.get("/api/connections/status").json()["openai"]["configured"]
        finally:
            gate.set()
        assert done.wait(10), "worker did not complete"
        blocker.result(timeout=10)
        # Context is restored when the pool worker is reused outside this request.
        assert executor.submit(lambda: connections.current_settings().openai.api_key.get_secret_value()).result(timeout=10) == ""
    assert observed[0][:3] == ("browser-A", "model-A", "proxy-secret")
    job = client.get("/api/jobs/" + response.json()["job_id"])
    serialized = json.dumps(observed[0][3]) + job.text
    serialized += "".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    assert "browser-A" not in serialized and "proxy-secret" not in serialized
