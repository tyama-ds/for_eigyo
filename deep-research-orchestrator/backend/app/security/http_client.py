"""外向きHTTPクライアントfactory — proxy policyとSSRF policyを一元適用する。

- origin="untrusted": ユーザー入力・Web本文由来URL。SSRFガードをrequest/redirect毎に適用。
- origin="admin":     管理者登録endpoint (Local LLM等)。allowlist照合の上でprivate許可。
- origin="internal":  同一compose内サービス (runner, searxng)。SSRFガード対象外・NO_PROXY。
"""

from __future__ import annotations

import httpx

from app.security.proxy import EffectiveProxyPolicy
from app.security.ssrf import SsrfBlockedError, validate_url


def _make_mounts(policy: EffectiveProxyPolicy | None, sample_url: str | None) -> dict | None:
    if policy is None or policy.mode == "off":
        return None
    mounts: dict[str, httpx.BaseTransport | None] = {}
    verify: bool | str = policy.ca_bundle_path or True
    http_p = policy.http_proxy or policy.all_proxy
    https_p = policy.https_proxy or policy.all_proxy
    if http_p:
        mounts["http://"] = httpx.HTTPTransport(proxy=http_p)
    if https_p:
        mounts["https://"] = httpx.HTTPTransport(proxy=https_p, verify=verify)
    # NO_PROXY該当hostは直接接続 (per-hostのmountはhttpxではhost単位で指定)
    for entry in policy.merged_no_proxy():
        e = entry.strip()
        if not e or "/" in e:
            continue  # CIDRはproxy_for_urlで判定される用途。mountはhost名のみ。
        host = e.lstrip("*.").lstrip(".")
        if host:
            mounts[f"all://{host}"] = None
            mounts[f"all://*.{host}"] = None
    return mounts


class SsrfGuardTransport(httpx.BaseTransport):
    """全request (redirect含む) のURLをSSRF policyで検証するtransport wrapper。"""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        origin: str,
        allowlist: set[tuple[str, int]] | None = None,
    ):
        self._inner = inner
        self._origin = origin
        self._allowlist = allowlist

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        validate_url(str(request.url), origin=self._origin, allowlist=self._allowlist)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def build_client(
    *,
    origin: str = "untrusted",
    policy: EffectiveProxyPolicy | None = None,
    allowlist: set[tuple[str, int]] | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
) -> httpx.Client:
    """policy適用済みhttpx.Clientを構築する。"""
    verify: bool | str = True
    if policy is not None and policy.ca_bundle_path:
        verify = policy.ca_bundle_path

    mounts_cfg = _make_mounts(policy, None)
    if origin == "internal":
        client = httpx.Client(
            timeout=timeout, headers=headers, follow_redirects=follow_redirects
        )
        return client

    if mounts_cfg:
        mounts = {
            k: SsrfGuardTransport(
                v if v is not None else httpx.HTTPTransport(verify=verify),
                origin=origin,
                allowlist=allowlist,
            )
            for k, v in mounts_cfg.items()
        }
        client = httpx.Client(
            mounts=mounts,
            timeout=timeout,
            headers=headers,
            verify=verify,
            follow_redirects=follow_redirects,
            transport=SsrfGuardTransport(
                httpx.HTTPTransport(verify=verify), origin=origin, allowlist=allowlist
            ),
        )
    else:
        client = httpx.Client(
            timeout=timeout,
            headers=headers,
            verify=verify,
            follow_redirects=follow_redirects,
            transport=SsrfGuardTransport(
                httpx.HTTPTransport(verify=verify), origin=origin, allowlist=allowlist
            ),
        )
    return client


def fetch_untrusted(
    url: str,
    *,
    policy: EffectiveProxyPolicy | None = None,
    timeout: float = 30.0,
    max_redirects: int = 5,
    max_bytes: int = 10 * 1024 * 1024,
) -> httpx.Response:
    """ユーザー入力/Web本文由来URLの安全な取得。redirect先も毎回SSRF検証する。"""
    current = url
    with build_client(origin="untrusted", policy=policy, timeout=timeout) as client:
        for _ in range(max_redirects + 1):
            validate_url(current, origin="untrusted")
            resp = client.get(current)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    return resp
                current = str(httpx.URL(current).join(location))
                continue
            if len(resp.content) > max_bytes:
                raise ValueError(f"応答サイズが上限を超えました: {len(resp.content)} bytes")
            return resp
    raise SsrfBlockedError(f"redirectが多すぎます: {url}")
