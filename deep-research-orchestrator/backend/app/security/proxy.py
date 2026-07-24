"""Proxy / Network Profile。

effective policyの決定順: engine別override > global explicit > environment inherit > off。
localhost・PostgreSQL・Redis・API・Runner・Local LLM等のlocal endpointは既定NO_PROXY。
proxy URL (認証情報含む) はsecret storeで暗号化保存し、ログ・API・SSEへ出さない。
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

DEFAULT_NO_PROXY = [
    "localhost",
    "127.0.0.1",
    "::1",
    "postgres",
    "redis",
    "api",
    "worker",
    "searxng",
    "runner-mock",
    "runner-gptr",
    "runner-odr",
    "host.docker.internal",
    "ollama",
    "*.local",
    "*.internal",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
]


@dataclass
class EffectiveProxyPolicy:
    mode: str = "off"  # off | inherit | explicit
    http_proxy: str | None = None
    https_proxy: str | None = None
    all_proxy: str | None = None
    no_proxy: list[str] = field(default_factory=list)
    ca_bundle_path: str | None = None
    source_scope: str = "off"  # off | inherit | global | engine:<id>

    def merged_no_proxy(self) -> list[str]:
        merged = list(DEFAULT_NO_PROXY)
        for entry in self.no_proxy:
            if entry and entry not in merged:
                merged.append(entry)
        return merged

    def proxy_for_url(self, url: str) -> str | None:
        """URLに適用するproxyを返す。NO_PROXY該当・mode=offならNone。"""
        if self.mode == "off":
            return None
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if self._host_bypassed(host):
            return None
        if parsed.scheme == "https":
            return self.https_proxy or self.all_proxy
        return self.http_proxy or self.all_proxy

    def _host_bypassed(self, host: str) -> bool:
        if not host:
            return True
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            addr = None
        for entry in self.merged_no_proxy():
            entry = entry.strip().lower()
            if not entry:
                continue
            if "/" in entry and addr is not None:
                try:
                    if addr in ipaddress.ip_network(entry, strict=False):
                        return True
                except ValueError:
                    continue
            elif entry.startswith("*."):
                if fnmatch.fnmatch(host, entry) or host == entry[2:]:
                    return True
            elif entry.startswith("."):
                if host.endswith(entry) or host == entry[1:]:
                    return True
            elif host == entry:
                return True
        return False

    def to_env(self) -> dict[str, str]:
        """Runner containerやsubprocessへ注入する環境変数表現。"""
        env: dict[str, str] = {}
        if self.mode == "off":
            # 明示的に無効化 (親環境のproxyを継承させない)
            env["NO_PROXY"] = "*"
            env["no_proxy"] = "*"
            return env
        if self.http_proxy:
            env["HTTP_PROXY"] = self.http_proxy
            env["http_proxy"] = self.http_proxy
        if self.https_proxy:
            env["HTTPS_PROXY"] = self.https_proxy
            env["https_proxy"] = self.https_proxy
        if self.all_proxy:
            env["ALL_PROXY"] = self.all_proxy
            env["all_proxy"] = self.all_proxy
        no_proxy = ",".join(self.merged_no_proxy())
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
        if self.ca_bundle_path:
            env["SSL_CERT_FILE"] = self.ca_bundle_path
            env["REQUESTS_CA_BUNDLE"] = self.ca_bundle_path
            env["NODE_EXTRA_CA_CERTS"] = self.ca_bundle_path
        return env


def policy_from_environment(ca_bundle_path: str | None = None) -> EffectiveProxyPolicy:
    """inherit mode: 標準環境変数 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY を利用。"""

    def _get(*names: str) -> str | None:
        for n in names:
            v = os.environ.get(n)
            if v:
                return v
        return None

    no_proxy_env = _get("NO_PROXY", "no_proxy") or ""
    return EffectiveProxyPolicy(
        mode="inherit",
        http_proxy=_get("HTTP_PROXY", "http_proxy"),
        https_proxy=_get("HTTPS_PROXY", "https_proxy"),
        all_proxy=_get("ALL_PROXY", "all_proxy"),
        no_proxy=[e.strip() for e in no_proxy_env.split(",") if e.strip()],
        ca_bundle_path=ca_bundle_path or _get("PROXY_CA_BUNDLE"),
        source_scope="inherit",
    )
