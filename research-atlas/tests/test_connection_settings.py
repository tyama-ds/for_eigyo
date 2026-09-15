import asyncio
import base64
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import httpx
import pytest

from app import connection_settings as settings, field_llm, insights, sources, foresight_sources


def header(value):
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode("ascii")


def configured(**values):
    return settings.settings_context(settings.ConnectionSettings.model_validate(values))


def test_browser_header_roundtrip_and_secret_repr_redaction():
    value = settings.parse_header(header({"version": 1,
        "openai": {"api_key": "cloud-secret", "model": "test-model"},
        "local": {"backend": "openai_compatible", "url": "http://[::1]:1234/v1", "api_key": "local-secret"},
        "proxy": {"enabled": True, "url": "http://proxy.example:8080", "username": "user-secret", "password": "password-secret"}}))
    assert value.openai.api_key.get_secret_value() == "cloud-secret"
    for secret in ("cloud-secret", "local-secret", "user-secret", "password-secret"):
        assert secret not in repr(value)
        assert secret not in value.model_dump_json()
    with settings.settings_context(value):
        status = settings.connection_status()
        assert status["openai"]["configured"] and status["proxy"]["enabled"]
        assert "secret" not in json.dumps(status)
    assert not insights.configured()


@pytest.mark.parametrize("value", ["", "not-base64-secret", "a" * 16385,
    base64.b64encode(b"\xffsecret").decode(), header([]), header({"version": True}),
    header({"version": 2}), header({"openai": {"api_key": 123}}),
    header({"proxy": {"enabled": "true", "url": "http://proxy"}}),
    header({"private-secret": "value"}), header({"openai": {"api_key": "secret\r\nInjected: x"}})])
def test_malformed_headers_have_one_sanitized_error(value):
    with pytest.raises(ValueError) as caught:
        settings.parse_header(value)
    assert str(caught.value) == settings._INVALID
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("url", ["http://user:password@proxy:8080", "http://proxy/path", "http://proxy?api_key=secret",
    "http://proxy#secret", "socks5://proxy:1080", "http://proxy:0", "http://proxy:90000", "http://pro xy:8080"])
def test_proxy_urls_disallow_embedded_credentials_and_non_proxy_components(url):
    with pytest.raises(ValueError):
        settings.ConnectionSettings(proxy={"url": url})


@pytest.mark.parametrize("url,expected", [
    (" http://192.168.1.20:1234/v1/ ", "http://192.168.1.20:1234/v1"),
    ("http://10.22.33.44:11434/", "http://10.22.33.44:11434"),
    ("https://[fd00::1234]:443/llm/v1", "https://[fd00::1234]:443/llm/v1"),
    ("http://[::1]:1234/v1", "http://[::1]:1234/v1"),
    ("http://llm-workstation:1234/v1", "http://llm-workstation:1234/v1"),
    ("http://lmstudio.local:1234/v1", "http://lmstudio.local:1234/v1"),
    ("https://llm.example.org/v1", "https://llm.example.org/v1"),
    ("https://研究.example/v1", "https://研究.example/v1"),
    ("https://llm.example.org./v1", "https://llm.example.org./v1"),
    ("http://localhost:65535", "http://localhost:65535"),
])
def test_browser_header_accepts_explicit_llm_hosts_and_normalizes_url(url, expected):
    value = settings.parse_header(header({"local": {"url": url, "model": "local-model", "api_key": "local-secret"}}))
    assert value.local.url == expected
    with settings.settings_context(value):
        assert field_llm._local_config() == ("ollama", expected)
        assert settings.local_headers() == {"Authorization": "Bearer local-secret"}
    assert settings.current_settings().local.url == "http://127.0.0.1:11434"


@pytest.mark.parametrize("url", [
    "http://username:password@localhost:11434", "http://localhost:11434/?api_key=secret",
    "http://host#secret", "http://host?", "http://host#", "file:///tmp/model", "ftp://host/model", "http:///v1",
    "http://host:0", "http://host:65536", "http://host:port", "http://host:", "http://host:1:2",
    "http://bad host:1234", "http://host\x00:1234", "http://host\x7f:1234", "http://host\\path",
    "http://[::1]suffix", "http://[not-ip]:1234", "http://::1:1234", "http://999.1.1.1:1234",
    "http://bad..host", "http://-host", "http://host-", "http://host..", "http://host%2fpath",
])
def test_local_urls_reject_malformed_hosts_and_unwanted_components(url):
    with pytest.raises(ValueError, match="HTTP"):
        settings.ConnectionSettings(local={"url": url})


@pytest.mark.parametrize("rule,url,expected", [
    ("localhost,127.0.0.1,::1", "http://localhost:1234/v1", True),
    ("localhost,127.0.0.1,::1", "http://[::1]:1234/v1", True),
    ("localhost,127.0.0.1,::1", "https://api.openai.com/v1", False),
    (".example.org", "https://sub.example.org/x", True),
    ("*.example.org", "https://example.org/x", True),
    ("example.org", "https://badexample.org/x", False),
    ("api.openai.com:443", "https://api.openai.com/v1", True),
    ("api.openai.com:80", "https://api.openai.com/v1", False),
    ("[::1]:1234", "http://[::1]:1234/v1", True),
    ("[::1]:1234", "http://[::1]:11434/v1", False),
    ("10.0.0.0/8", "http://10.22.33.44", True),
    ("10.0.0.0/8", "http://11.22.33.44", False),
    ("*", "https://any.example/path", True),
])
def test_no_proxy_host_domain_port_and_cidr(rule, url, expected):
    assert settings.bypass_proxy(url, rule) is expected


@pytest.mark.parametrize("value", ["http://proxy/path", "host:0", "host:99999", "host:port", "bad host", "host?secret", "[not-ip]:80"])
def test_no_proxy_invalid_rules_are_rejected(value):
    with pytest.raises(ValueError):
        settings.ConnectionSettings(proxy={"no_proxy": value})


def test_environment_credentials_urls_and_proxy_are_ignored(monkeypatch):
    for key, value in {"OPENAI_API_KEY": "environment-secret", "OPENAI_MODEL": "env-model",
        "OPENAI_BASE_URL": "https://evil.example", "ATLAS_LOCAL_LLM_URL": "https://evil.example",
        "ATLAS_LOCAL_LLM_BACKEND": "invalid", "ATLAS_LOCAL_LLM_MODEL": "env-model",
        "HTTP_PROXY": "http://environment-proxy:9999", "HTTPS_PROXY": "http://environment-proxy:9999",
        "ALL_PROXY": "http://environment-proxy:9999", "NO_PROXY": "*"}.items():
        monkeypatch.setenv(key, value)
    assert not insights.configured()
    assert field_llm._local_config() == ("ollama", "http://127.0.0.1:11434")
    calls = []
    monkeypatch.setattr(settings.httpx, "Client", lambda **kwargs: calls.append(kwargs) or object())
    settings.http_client("https://api.openai.com/v1")
    assert calls[-1]["trust_env"] is False and calls[-1]["proxy"] is None
    with configured(proxy={"enabled": True, "url": "http://browser-proxy:8080", "no_proxy": ""}):
        settings.http_client("https://api.openai.com/v1")
    assert str(calls[-1]["proxy"].url) == "http://browser-proxy:8080"


def test_real_sdk_does_not_inherit_org_project_custom_headers_or_other_credentials(monkeypatch):
    environment = {"OPENAI_API_KEY": "environment-key", "OPENAI_BASE_URL": "https://evil.example",
        "OPENAI_ORG_ID": "environment-org", "OPENAI_PROJECT_ID": "environment-project",
        "OPENAI_ADMIN_KEY": "environment-admin", "OPENAI_WEBHOOK_SECRET": "environment-webhook",
        "OPENAI_CUSTOM_HEADERS": "Authorization: Bearer environment-override\nX-Private-Header: environment-private\nOpenAI-Project: environment-project"}
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    calls = []
    real_client = httpx.Client
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "browser-model", "created": 1, "object": "model", "owned_by": "test"})
    monkeypatch.setattr(settings, "http_client", lambda target_url, **kwargs: real_client(
        transport=httpx.MockTransport(handler), trust_env=False, **kwargs))
    with configured(openai={"api_key": "browser-key", "model": "browser-model"}):
        with settings.openai_client(timeout=2) as client:
            assert client.models.retrieve("browser-model").id == "browser-model"
            assert client.organization is None and client.project is None
            assert client.admin_api_key is None and client.webhook_secret is None
    assert len(calls) == 1 and calls[0].url.host == "api.openai.com"
    assert calls[0].headers["Authorization"] == "Bearer browser-key"
    assert "OpenAI-Organization" not in calls[0].headers and "OpenAI-Project" not in calls[0].headers
    assert "X-Private-Header" not in calls[0].headers
    assert "environment-" not in str(calls[0].headers)
    import os
    assert all(os.environ[key] == value for key, value in environment.items())


def test_local_llm_bypasses_proxy_and_receives_only_local_authorization(monkeypatch):
    calls, constructor = [], []
    real_client = httpx.Client
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"models": [{"name": "local-model"}]})
    def client(**kwargs):
        constructor.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(settings.httpx, "Client", client)
    with configured(openai={"api_key": "cloud-secret", "model": "cloud-model"},
        local={"api_key": "local-secret", "model": "local-model"},
        proxy={"enabled": True, "url": "http://proxy.example:8080", "no_proxy": ""}):
        assert field_llm.local_status()["available"]
    assert calls[0].headers["Authorization"] == "Bearer local-secret"
    assert "cloud-secret" not in str(calls[0].headers)
    assert constructor[0]["proxy"] is None and constructor[0]["trust_env"] is False


def test_all_public_source_clients_receive_browser_proxy_and_bypass(monkeypatch):
    calls = []
    monkeypatch.setattr(settings.httpx, "Client", lambda **kwargs: calls.append(kwargs) or object())
    with configured(proxy={"enabled": True, "url": "http://proxy.example:8080", "no_proxy": "api.crossref.org"}):
        for provider in ("europepmc", "arxiv", "crossref"):
            sources._make_client(provider)
        foresight_sources._make_scopus_client()
    assert all(item["trust_env"] is False and item["follow_redirects"] is False for item in calls)
    assert [item["proxy"] is not None for item in calls] == [True, True, False, True]


def test_actual_http_proxy_routing_and_proxy_authorization():
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append({"path": self.path, "proxy_auth": self.headers.get("Proxy-Authorization"),
                             "authorization": self.headers.get("Authorization")})
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with configured(proxy={"enabled": True, "url": f"http://127.0.0.1:{server.server_port}",
                               "username": "proxy-user", "password": "p@ss:word", "no_proxy": ""}):
            with settings.http_client("http://atlas-target.invalid/check", timeout=2) as client:
                assert client.get("http://atlas-target.invalid/check").text == "OK"
        expected = "Basic " + base64.b64encode(b"proxy-user:p@ss:word").decode()
        assert observed == [{"path": "http://atlas-target.invalid/check", "proxy_auth": expected, "authorization": None}]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_actual_https_sdk_proxy_connect_sends_only_proxy_auth_and_redacts_failure():
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            observed.append({"path": self.path, "proxy_auth": self.headers.get("Proxy-Authorization"),
                             "authorization": self.headers.get("Authorization")})
            self.send_response(407)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with configured(openai={"api_key": "cloud-secret", "model": "test-model"},
            proxy={"enabled": True, "url": f"http://127.0.0.1:{server.server_port}",
                   "username": "proxy-user", "password": "proxy-secret", "no_proxy": ""}):
            with pytest.raises(RuntimeError) as caught:
                field_llm.structured_output({"papers": []}, field_llm.NarrativeOutput, "test", "openai")
        expected = "Basic " + base64.b64encode(b"proxy-user:proxy-secret").decode()
        assert observed == [{"path": "api.openai.com:443", "proxy_auth": expected, "authorization": None}]
        assert "secret" not in str(caught.value) and "127.0.0.1" not in str(caught.value)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_contexts_isolate_concurrent_browser_settings_and_thread_copy():
    async def run():
        gate = asyncio.Event()
        async def work(key):
            with configured(openai={"api_key": key, "model": key + "-model"}):
                await gate.wait()
                await asyncio.sleep(0)
                return settings.current_settings().openai.api_key.get_secret_value()
        tasks = [asyncio.create_task(work(key)) for key in ("browser-a", "browser-b")]
        gate.set()
        return await asyncio.gather(*tasks)
    assert asyncio.run(run()) == ["browser-a", "browser-b"]
    with ThreadPoolExecutor(max_workers=1) as executor:
        with configured(openai={"api_key": "browser-job", "model": "model"}):
            context = copy_context()
            result = executor.submit(context.run, lambda: settings.current_settings().openai.api_key.get_secret_value()).result()
        assert result == "browser-job"
        assert executor.submit(lambda: settings.current_settings().openai.api_key.get_secret_value()).result() == ""
    assert not insights.configured()


def test_read_only_openai_check_fixed_endpoint_no_generation_and_no_secret_response(monkeypatch):
    calls = []
    real_client = httpx.Client
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "configured/model"})
    monkeypatch.setattr(settings.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    with configured(openai={"api_key": "check-secret", "model": "configured/model"}):
        value = settings.test_connection("openai")
    assert value["ok"] and "secret" not in json.dumps(value)
    assert len(calls) == 1 and calls[0].method == "GET"
    assert calls[0].url.host == "api.openai.com" and b"configured%2Fmodel" in calls[0].url.raw_path
    assert calls[0].headers["Authorization"] == "Bearer check-secret"
    assert not calls[0].content


def test_connection_checks_sanitize_upstream_failures(monkeypatch):
    real_client = httpx.Client
    def handler(request):
        raise httpx.ConnectError("private-secret and http://user:password@proxy", request=request)
    monkeypatch.setattr(settings.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    with configured(openai={"api_key": "private-secret", "model": "test-model"}):
        value = settings.test_connection("openai")
    assert not value["ok"]
    assert all(secret not in json.dumps(value) for secret in ("private-secret", "password", "http://"))
    assert not settings.test_connection("proxy")["ok"]
    assert not settings.test_connection("openai")["ok"]
    with pytest.raises(ValueError):
        settings.test_connection("http://arbitrary.example")


def test_local_connection_check_detects_missing_configured_model(monkeypatch):
    monkeypatch.setattr(field_llm, "local_status", lambda: {"available": True, "default_model": None,
        "error": "設定されたモデルがありません。"})
    with configured(local={"model": "missing-model"}):
        value = settings.test_connection("local")
    assert not value["ok"] and "モデル" in value["message"]
