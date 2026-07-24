"""SSRF対策 — 通信先URLの由来ベース検証。

- ユーザー入力・Web本文由来のURL (`origin="untrusted"`):
  localhost / private / link-local / metadata endpoint / 非http(s) scheme を拒否。
  redirect先も同じ検証を通す (httpx event hookで適用)。
- 管理者がSettingsで登録したLocal LLM endpoint (`origin="admin"`):
  llm_endpoint_allowlist に (host, port) が登録されている場合のみ private を許可。
  調査入力からallowlistは変更できない (settings APIのみが書き込む)。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata",
    "100.100.100.200",  # Alibaba
}


class SsrfBlockedError(ValueError):
    pass


def _is_private_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def resolve_host_ips(host: str) -> list[str]:
    """hostの全A/AAAAレコードを解決する。DNS rebinding対策で全アドレスを検証する。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SsrfBlockedError(f"DNS解決に失敗しました: {host}") from e
    return sorted({info[4][0] for info in infos})


def validate_url(
    url: str,
    *,
    origin: str = "untrusted",
    allowlist: set[tuple[str, int]] | None = None,
    resolver=resolve_host_ips,
) -> None:
    """URLを検証し、policy違反なら SsrfBlockedError を送出する。

    origin="untrusted": ユーザー調査入力・Web本文由来。private一律拒否。
    origin="admin": 管理者登録エンドポイント。allowlistに載るhost:portのみprivate許可。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SsrfBlockedError(f"許可されないscheme: {parsed.scheme or '(なし)'}")
    host = parsed.hostname
    if not host:
        raise SsrfBlockedError("hostがありません")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if host.lower() in METADATA_HOSTS:
        raise SsrfBlockedError(f"metadata endpointへのアクセスは拒否されます: {host}")

    # literal IPか判定
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        ips = resolver(host)

    private_ips = [ip for ip in ips if _is_private_ip(ip)]
    if not private_ips:
        return  # 全てpublic — 許可

    if origin == "admin" and allowlist is not None and (host.lower(), port) in allowlist:
        return
    raise SsrfBlockedError(
        f"private/loopback/link-localアドレスへのアクセスは拒否されます: "
        f"{host} -> {','.join(private_ips)}"
    )
