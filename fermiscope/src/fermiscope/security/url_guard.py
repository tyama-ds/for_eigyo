"""URL取得前の安全性検査(SSRF対策)。

- http/https 以外のスキームを拒否
- localhost・プライベートIP・リンクローカル・メタデータサービスを拒否
- DNS解決後の全IPアドレスも検査(DNSリバインディング対策)
- リダイレクト先はフェッチャ側で本モジュールにより再検査される
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from urllib.parse import urlparse

Resolver = Callable[[str], list[str]]


class UrlGuardError(ValueError):
    """安全でないURL。"""


_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}

_METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}


def default_resolver(host: str) -> list[str]:
    """socket.getaddrinfo による全アドレス解決。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UrlGuardError(f"ホスト名を解決できません: {host}") from exc
    return list({str(info[4][0]) for info in infos})


def validate_ip(ip_text: str) -> None:
    """IPアドレスがプライベート・予約・リンクローカル等でないことを検査。"""
    try:
        ip = ipaddress.ip_address(ip_text.split("%")[0])
    except ValueError as exc:
        raise UrlGuardError(f"IPアドレスとして不正です: {ip_text}") from exc
    if ip_text in _METADATA_IPS:
        raise UrlGuardError(f"メタデータサービスへのアクセスは拒否します: {ip_text}")
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise UrlGuardError(f"プライベート/予約IPへのアクセスは拒否します: {ip_text}")


def validate_url(
    url: str,
    resolver: Resolver | None = None,
    skip_dns: bool = False,
) -> str:
    """URLの安全性を検査し、正規化したURLを返す。

    Args:
        url: 検査対象URL
        resolver: DNS解決関数(テスト用に差し替え可能)
        skip_dns: モック環境等でDNS解決を省略する場合 True
    """
    if not url or len(url) > 4096:
        raise UrlGuardError("URLが空または長すぎます")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlGuardError(f"http/https 以外のスキームは拒否します: {parsed.scheme or '(なし)'}")
    host = parsed.hostname
    if not host:
        raise UrlGuardError("ホスト名がありません")
    host_lower = host.lower().rstrip(".")
    if host_lower in _BLOCKED_HOSTNAMES or host_lower.endswith(".localhost"):
        raise UrlGuardError(f"拒否対象のホスト名です: {host}")
    if parsed.username or parsed.password:
        raise UrlGuardError("URL内の認証情報は拒否します")

    # ホスト名が直接IPの場合
    # 注意: UrlGuardError は ValueError の派生のため、IP判定と安全性検査の
    # 例外処理を分離する(検査失敗を「非IP」と誤解釈しない)
    bare_host = host_lower.strip("[]")
    is_ip_literal = True
    try:
        ipaddress.ip_address(bare_host)
    except ValueError:
        is_ip_literal = False
    if is_ip_literal:
        validate_ip(bare_host)
        return url

    # 数値のみ/16進/8進ドット表記の「IPリテラルもどき」を拒否する。
    # ipaddress.ip_address は "2130706433" や "0x7f000001"、"0177.0.0.1"、"127.1" を
    # IPとして解釈しないが、OS の getaddrinfo はこれらを 127.0.0.1 等へ解決し得る。
    # skip_dns=True(オフライン検査)でも確実にブロックするための保険。
    if re.fullmatch(r"0x[0-9a-fA-F]+|[0-9]+|[0-9]+(\.[0-9]+){1,3}", bare_host):
        raise UrlGuardError(f"数値形式のホストは許可しません: {host}")

    if not skip_dns:
        ips = (resolver or default_resolver)(host_lower)
        if not ips:
            raise UrlGuardError(f"ホスト名を解決できません: {host}")
        for ip in ips:
            validate_ip(ip)
    return url
