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

# ipaddress の is_private 等では捕捉されないが到達させるべきでないネットワーク。
# 100.64.0.0/10 は CGNAT 共有アドレス空間(RFC 6598)、198.18.0.0/15 はベンチマーク用、
# 192.0.0.0/24 は IETF プロトコル割当。いずれも内部ネットワークで実在し得る。
_EXTRA_BLOCKED_NETWORKS = [
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
]


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
    if any(ip in net for net in _EXTRA_BLOCKED_NETWORKS):
        raise UrlGuardError(f"到達を許可しないネットワークのIPです: {ip_text}")


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
    # 不正なURL(壊れたIPv6リテラル・不正ポート等)で urlparse / hostname 参照が
    # 素の ValueError を投げると呼び出し側(FetchError/UrlGuardError のみ捕捉)を
    # 貫通して調査全体を落とす。ここで UrlGuardError に正規化する。
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        username, password = parsed.username, parsed.password
    except ValueError as exc:
        raise UrlGuardError(f"URLを解釈できません: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise UrlGuardError(f"http/https 以外のスキームは拒否します: {parsed.scheme or '(なし)'}")
    if not host:
        raise UrlGuardError("ホスト名がありません")
    host_lower = host.lower().rstrip(".")
    if host_lower in _BLOCKED_HOSTNAMES or host_lower.endswith(".localhost"):
        raise UrlGuardError(f"拒否対象のホスト名です: {host}")
    if username or password:
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

    # 数値のみ/16進/8進/混在ドット表記の「IPリテラルもどき」を拒否する。
    # ipaddress.ip_address は "2130706433"・"0x7f000001"・"0177.0.0.1"・"127.1"・
    # "0x7f.0.0.1" を IP として解釈しないが、OS の getaddrinfo はこれらを
    # 127.0.0.1 等へ解決し得る。skip_dns=True(オフライン検査)でも確実にブロックする保険。
    labels = bare_host.split(".")
    all_numeric = re.fullmatch(r"[0-9]+(\.[0-9]+)*", bare_host) is not None
    any_hex_label = any(lbl.lower().startswith("0x") for lbl in labels)
    if all_numeric or any_hex_label:
        raise UrlGuardError(f"数値形式のホストは許可しません: {host}")

    if not skip_dns:
        ips = (resolver or default_resolver)(host_lower)
        if not ips:
            raise UrlGuardError(f"ホスト名を解決できません: {host}")
        for ip in ips:
            validate_ip(ip)
    return url
