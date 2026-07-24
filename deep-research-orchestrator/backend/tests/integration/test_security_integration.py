"""セキュリティ統合テスト — 受入 13,17,18,19。"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import requires_infra
from tests.fixtures.servers import ExternalSite, ForwardProxy, OpenAiCompatServer
from tests.integration.helpers import (
    clear_roles,
    create_job,
    event_history,
    setup_llm_profile,
    wait_for_job,
)

pytestmark = requires_infra

SECRET_KEY_VALUE = "sk-local-test-key-abcdef123456"


@pytest.fixture(autouse=True)
def _stack(api_client, celery_worker_proc):
    yield


class TestLocalLlmFixture:
    def test_endpoint_key_model_passed_and_key_never_leaks(self, api_client):
        """受入17: OpenAI互換local fixtureへendpoint/API key/modelが正しく渡り、
        接続試験と生成が成功する。keyはAPI/log/SSEへ出ない。"""
        server = OpenAiCompatServer(required_key=SECRET_KEY_VALUE).start()
        try:
            profile_id = setup_llm_profile(
                api_client, server.url + "/v1", api_key=SECRET_KEY_VALUE
            )

            # profile応答にkeyが含まれない (masked placeholderのみ)
            profiles = api_client.get("/api/settings/llm-profiles").json()
            me = next(p for p in profiles if p["id"] == profile_id)
            assert me["has_api_key"] is True
            assert me["api_key_masked"] == "••••••••"
            assert SECRET_KEY_VALUE not in json.dumps(profiles)

            # 接続試験: 到達性・認証・model有無・最小生成
            result = api_client.post(
                f"/api/settings/llm-profiles/{profile_id}/test"
            ).json()
            assert result["reachable"] is True
            assert result["authenticated"] is True
            assert result["model_available"] is True
            assert result["generation_ok"] is True
            assert SECRET_KEY_VALUE not in json.dumps(result)

            # fixture側でBearer keyとmodelを受信している
            assert any(
                r["authorization"] == f"Bearer {SECRET_KEY_VALUE}" for r in server.requests
            )
            chat = [r for r in server.requests if r["path"].endswith("/chat/completions")]
            assert chat and chat[-1]["body"]["model"] == "test-model"

            # 統合を実行し、SSE/イベントへkeyが漏れないこと (受入13の一部)
            job = create_job(api_client, ["mock-fast", "mock-slow"], auto_synthesize=True)
            wait_for_job(api_client, job["id"], timeout=120)
            events = event_history(api_client, job["id"])
            assert SECRET_KEY_VALUE not in json.dumps(events, ensure_ascii=False)
            synthesis = api_client.get(f"/api/jobs/{job['id']}/synthesis").json()
            assert synthesis["status"] == "succeeded"
            assert SECRET_KEY_VALUE not in json.dumps(synthesis, ensure_ascii=False)
        finally:
            clear_roles(api_client)
            server.stop()

    def test_wrong_key_reported_as_auth_failure(self, api_client):
        server = OpenAiCompatServer(required_key="sk-correct-key-000111").start()
        try:
            profile_id = setup_llm_profile(
                api_client, server.url + "/v1", api_key="sk-wrong-key-999888"
            )
            result = api_client.post(
                f"/api/settings/llm-profiles/{profile_id}/test"
            ).json()
            assert result["generation_ok"] is False
            assert result["error"]
            assert "sk-wrong-key-999888" not in json.dumps(result)
        finally:
            clear_roles(api_client)
            server.stop()


class TestProxyIntegration:
    def test_external_via_auth_proxy_local_bypassed(self, api_client, db_session):
        """受入18: 認証付きforward proxy経由で外部HTTPが成功し、
        Local LLM/internal serviceはNO_PROXYでbypassされる。"""
        external = ExternalSite(body=b"reached-through-proxy").start()
        proxy = ForwardProxy().start()
        llm = OpenAiCompatServer().start()
        try:
            # global explicit proxyを設定 (外部fixtureのIPはNO_PROXY既定の
            # privateレンジに入るため、検証はpolicyの経路決定とtransport実挙動で行う)
            from app.config import get_settings
            from app.security.http_client import build_client
            from app.security.proxy import EffectiveProxyPolicy

            policy = EffectiveProxyPolicy(
                mode="explicit",
                http_proxy=proxy.url,
                https_proxy=proxy.url,
                no_proxy=[],
            )
            # 外部URL (公開hostに見せるためexample.testを使い、mountを直接検証)
            assert policy.proxy_for_url("http://external.example.test/") == proxy.url
            assert policy.proxy_for_url("http://localhost:9999/") is None
            assert policy.proxy_for_url(llm.url + "/v1") is None  # 127.0.0.1

            # 実通信: httpx transportがproxyへ絶対URIで到達することを検証
            with httpx.Client(
                transport=httpx.HTTPTransport(proxy=proxy.url), timeout=10
            ) as client:
                resp = client.get(external.url + "/page")
                assert resp.status_code == 200
                assert resp.content == b"reached-through-proxy"
            assert any("/page" in r for r in proxy.requests)

            # 認証なしだと407になる (proxyが認証を要求している証明)
            with httpx.Client(
                transport=httpx.HTTPTransport(proxy=proxy.url_noauth), timeout=10
            ) as client:
                resp = client.get(external.url + "/page")
                assert resp.status_code == 407

            # proxy設定API: 認証情報は書き込み専用でHTML/APIへ出ない
            resp = api_client.put(
                "/api/settings/proxy",
                json={"scope": "global", "mode": "explicit",
                      "http_proxy": proxy.url, "https_proxy": proxy.url,
                      "no_proxy": ["extra.internal"]},
            )
            assert resp.status_code == 200
            view = resp.json()
            assert view["has_http_proxy"] is True
            assert "proxypass" not in json.dumps(view)

            # effective policyの解決 (DB経由)
            from app.llm.profiles import effective_proxy_policy

            db_session.expire_all()
            eff = effective_proxy_policy(db_session, get_settings())
            assert eff.mode == "explicit"
            assert eff.http_proxy == proxy.url
            assert eff.proxy_for_url("http://external.example.test/") == proxy.url
            assert eff.proxy_for_url("http://extra.internal/x") is None

            # Test proxy API
            resp = api_client.post(
                "/api/settings/proxy/test",
                json={"scope": "global",
                      "external_url": "http://external.example.test/never-called",
                      "internal_url": llm.url},
            )
            body = resp.json()
            assert body["external"]["via_proxy"] is True
            assert body["internal_bypassed"] is True
            assert "proxypass" not in json.dumps(body)
        finally:
            api_client.put("/api/settings/proxy",
                           json={"scope": "global", "mode": "off", "no_proxy": []})
            external.stop()
            proxy.stop()
            llm.stop()

    def test_proxy_env_injected_to_runner_request(self, api_client, db_session):
        """Runnerへrun単位でproxy環境変数が注入される。"""
        from app.config import get_settings
        from app.db.models import EngineRun, ResearchJob
        from app.orchestrator.tasks import _build_run_request

        api_client.put(
            "/api/settings/proxy",
            json={"scope": "global", "mode": "explicit",
                  "https_proxy": "http://u:pw@proxy.corp:3128", "no_proxy": ["corp.jp"]},
        )
        try:
            job = ResearchJob(topic="t", options={})
            db_session.add(job)
            db_session.flush()
            run = EngineRun(job_id=job.id, engine_id="mock-fast", options={})
            db_session.add(run)
            db_session.flush()
            body = _build_run_request(db_session, get_settings(), run, job)
            env = body["proxy_env"]
            assert env["HTTPS_PROXY"] == "http://u:pw@proxy.corp:3128"
            assert "corp.jp" in env["NO_PROXY"]
            assert "localhost" in env["NO_PROXY"]
        finally:
            api_client.put("/api/settings/proxy",
                           json={"scope": "global", "mode": "off", "no_proxy": []})


class TestSsrfIntegration:
    def test_private_input_url_rejected_allowlisted_llm_endpoint_allowed(self, api_client):
        """受入19: ユーザー入力由来private URLは拒否。管理者allowlist済み
        Local LLM endpointだけ許可。"""
        # ユーザー入力のprivate URLは400
        resp = api_client.post(
            "/api/jobs",
            json={"topic": "t", "language": "ja", "engines": ["mock-fast"],
                  "auto_synthesize": False,
                  "input_urls": ["http://169.254.169.254/latest/meta-data/"]},
        )
        assert resp.status_code == 400
        assert "拒否" in resp.json()["detail"]

        resp = api_client.post(
            "/api/jobs",
            json={"topic": "t", "language": "ja", "engines": ["mock-fast"],
                  "auto_synthesize": False,
                  "input_urls": ["http://127.0.0.1:8800/internal"]},
        )
        assert resp.status_code == 400

        # 管理者がprofile登録したprivate endpointはallowlist化され、接続試験が通る
        server = OpenAiCompatServer().start()
        try:
            profile_id = setup_llm_profile(api_client, server.url + "/v1")
            allowlist = api_client.get("/api/settings/llm-endpoint-allowlist").json()
            assert any(a["port"] == server.port for a in allowlist)
            result = api_client.post(
                f"/api/settings/llm-profiles/{profile_id}/test"
            ).json()
            assert result["generation_ok"] is True
        finally:
            clear_roles(api_client)
            server.stop()

    def test_untrusted_fetch_blocks_redirect_to_private(self):
        """redirect先がprivateの場合も拒否される。"""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from tests.fixtures.servers import free_port

        port = free_port()

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/")
                self.end_headers()

        httpd = ThreadingHTTPServer(("127.0.0.1", port), RedirectHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            from app.security.http_client import fetch_untrusted
            from app.security.ssrf import SsrfBlockedError

            # 入口URL自体がprivate (127.0.0.1) なので即拒否される
            with pytest.raises(SsrfBlockedError):
                fetch_untrusted(f"http://127.0.0.1:{port}/start")

            # redirect検証: validate_urlをmockして入口だけ通しても、
            # redirect先のmetadata endpointで拒否される
            from app.security import http_client as hc

            original = hc.validate_url
            calls = []

            def spy(url, **kw):
                calls.append(url)
                if url.startswith(f"http://127.0.0.1:{port}"):
                    return  # 入口fixtureのみ許可 (テスト用バイパス)
                return original(url, **kw)

            hc.validate_url = spy
            try:
                with pytest.raises(SsrfBlockedError):
                    hc.fetch_untrusted(f"http://127.0.0.1:{port}/start")
                assert any("169.254.169.254" in u for u in calls)
            finally:
                hc.validate_url = original
        finally:
            httpd.shutdown()


class TestSecretsNeverInApiOrLogs:
    def test_proxy_credentials_not_in_events_or_job_api(self, api_client):
        """受入13: proxy認証情報等がjob API/イベントへ出ない (redaction)。"""
        api_client.put(
            "/api/settings/proxy",
            json={"scope": "global", "mode": "explicit",
                  "https_proxy": "http://secretuser:secretpw999@proxy.corp:3128"},
        )
        try:
            job = create_job(api_client, ["mock-fast"])
            wait_for_job(api_client, job["id"])
            dump = json.dumps(event_history(api_client, job["id"]), ensure_ascii=False)
            assert "secretpw999" not in dump
            job_dump = json.dumps(api_client.get(f"/api/jobs/{job['id']}").json(),
                                  ensure_ascii=False)
            assert "secretpw999" not in job_dump
        finally:
            api_client.put("/api/settings/proxy",
                           json={"scope": "global", "mode": "off", "no_proxy": []})
