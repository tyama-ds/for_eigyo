"""SSRFガード・proxy policy・redactionの単体テスト。"""

from __future__ import annotations

import pytest

from app.security.proxy import EffectiveProxyPolicy, policy_from_environment
from app.security.redaction import Redactor
from app.security.ssrf import SsrfBlockedError, validate_url


class TestSsrf:
    def test_public_url_allowed(self):
        validate_url("https://example.com/page", origin="untrusted",
                     resolver=lambda h: ["93.184.216.34"])

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://localhost:8080/",
            "http://[::1]/",
            "http://10.0.0.5/",
            "http://172.16.3.4/",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
        ],
    )
    def test_private_and_loopback_blocked(self, url):
        with pytest.raises(SsrfBlockedError):
            validate_url(url, origin="untrusted", resolver=lambda h: ["127.0.0.1"])

    def test_metadata_hostname_blocked(self):
        with pytest.raises(SsrfBlockedError):
            validate_url("http://metadata.google.internal/computeMetadata",
                         origin="untrusted", resolver=lambda h: ["8.8.8.8"])

    def test_dns_to_private_blocked(self):
        """public風hostnameがprivateへ解決される場合 (DNS rebinding) も拒否。"""
        with pytest.raises(SsrfBlockedError):
            validate_url("http://evil.example.com/", origin="untrusted",
                         resolver=lambda h: ["93.184.216.34", "192.168.0.10"])

    def test_non_http_scheme_blocked(self):
        with pytest.raises(SsrfBlockedError):
            validate_url("file:///etc/passwd", origin="untrusted")
        with pytest.raises(SsrfBlockedError):
            validate_url("gopher://example.com/", origin="untrusted")

    def test_admin_allowlist_permits_private(self):
        allowlist = {("192.168.10.20", 11434)}
        validate_url(
            "http://192.168.10.20:11434/v1", origin="admin", allowlist=allowlist
        )

    def test_admin_without_allowlist_entry_blocked(self):
        with pytest.raises(SsrfBlockedError):
            validate_url("http://192.168.10.21:11434/v1", origin="admin",
                         allowlist={("192.168.10.20", 11434)})

    def test_untrusted_ignores_allowlist(self):
        """調査入力由来 (untrusted) はallowlistがあってもprivate拒否。"""
        with pytest.raises(SsrfBlockedError):
            validate_url("http://192.168.10.20:11434/v1", origin="untrusted",
                         allowlist={("192.168.10.20", 11434)})


class TestProxyPolicy:
    def test_off_mode_no_proxy(self):
        policy = EffectiveProxyPolicy(mode="off")
        assert policy.proxy_for_url("https://example.com/") is None
        assert policy.to_env()["NO_PROXY"] == "*"

    def test_explicit_routes_external_and_bypasses_local(self):
        policy = EffectiveProxyPolicy(
            mode="explicit",
            http_proxy="http://user:pass@proxy.internal:3128",
            https_proxy="http://user:pass@proxy.internal:3128",
            no_proxy=["corp.example.jp"],
        )
        assert policy.proxy_for_url("https://api.openai.com/v1") is not None
        # 既定NO_PROXY: localhost / private CIDR / compose service名
        assert policy.proxy_for_url("http://localhost:8800/") is None
        assert policy.proxy_for_url("http://127.0.0.1:5432/") is None
        assert policy.proxy_for_url("http://searxng:8080/search") is None
        assert policy.proxy_for_url("http://10.1.2.3:11434/v1") is None
        assert policy.proxy_for_url("http://192.168.0.7/") is None
        # 追加NO_PROXY
        assert policy.proxy_for_url("https://corp.example.jp/page") is None

    def test_wildcard_and_suffix_no_proxy(self):
        policy = EffectiveProxyPolicy(
            mode="explicit", https_proxy="http://p:3128", no_proxy=["*.example.com"]
        )
        assert policy.proxy_for_url("https://api.example.com/") is None
        assert policy.proxy_for_url("https://example.org/") == "http://p:3128"

    def test_to_env_contains_credentials_and_no_proxy(self):
        policy = EffectiveProxyPolicy(
            mode="explicit", https_proxy="http://u:p@proxy:3128", no_proxy=["extra.host"]
        )
        env = policy.to_env()
        assert env["HTTPS_PROXY"] == "http://u:p@proxy:3128"
        assert "localhost" in env["NO_PROXY"]
        assert "extra.host" in env["NO_PROXY"]

    def test_inherit_reads_environment(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://envproxy:8080")
        monkeypatch.setenv("NO_PROXY", "internal.example")
        policy = policy_from_environment()
        assert policy.https_proxy == "http://envproxy:8080"
        assert "internal.example" in policy.no_proxy


class TestRedaction:
    def test_known_secret_removed(self):
        r = Redactor()
        r.register("sk-verysecretkey12345")
        out = r.redact("error calling llm with key sk-verysecretkey12345 failed")
        assert "sk-verysecretkey12345" not in out
        assert "[REDACTED]" in out

    def test_authorization_header_pattern(self):
        r = Redactor()
        out = r.redact("Authorization: Bearer abcdef1234567890abcdef")
        assert "abcdef1234567890" not in out

    def test_url_userinfo_redacted(self):
        r = Redactor()
        out = r.redact("proxy=http://alice:supersecret@proxy.corp:3128")
        assert "supersecret" not in out
        assert "alice" in out  # userは残る

    def test_api_key_kv_pattern(self):
        r = Redactor()
        out = r.redact("api_key=abcd1234efgh5678 and X-Api-Key: zzzz9999yyyy8888")
        assert "abcd1234efgh5678" not in out
        assert "zzzz9999yyyy8888" not in out
