"""共通HTTP(S)プロキシ設定のテスト(検索・取得・LLMへの配線)。"""

from __future__ import annotations

from fermiscope.config import load_settings
from fermiscope.llm.settings_store import _common_proxy
from fermiscope.research.fetcher import DocumentFetcher

PROXY = "http://user:pass@proxy.example:8080"


def test_dedicated_env_sets_common_proxy():
    s = load_settings(env={"FERMISCOPE_HTTP_PROXY": PROXY})
    assert s.http_proxy == PROXY


def test_standard_proxy_env_is_picked_up():
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy"):
        s = load_settings(env={key: PROXY})
        assert s.http_proxy == PROXY, key


def test_dedicated_env_takes_precedence_over_standard():
    s = load_settings(
        env={"FERMISCOPE_HTTP_PROXY": PROXY, "HTTPS_PROXY": "http://other:1"}
    )
    assert s.http_proxy == PROXY


def test_no_proxy_by_default():
    assert load_settings(env={}).http_proxy == ""


def test_fetcher_skips_ip_pinning_when_proxied():
    """プロキシ利用時は IP 固定(DNS再解決)を行わず、DNSリゾルバも呼ばない。"""
    s = load_settings(env={"FERMISCOPE_HTTP_PROXY": PROXY})
    calls: list[str] = []

    def resolver(host: str) -> list[str]:
        calls.append(host)
        return ["93.184.216.34"]

    fetcher = DocumentFetcher(s, resolver=resolver, skip_dns=False)
    assert fetcher._proxy == PROXY
    connect_url, host_header, sni = fetcher._pin_connection("https://example.com/x")
    assert connect_url == "https://example.com/x"
    assert host_header is None and sni is None
    assert calls == []  # リゾルバは呼ばれない(接続はプロキシが担う)


def test_fetcher_pins_ip_without_proxy():
    """プロキシ未設定時は従来どおり検証済みIPへ接続を固定する。"""
    s = load_settings(env={})
    fetcher = DocumentFetcher(s, resolver=lambda host: ["93.184.216.34"], skip_dns=False)
    assert fetcher._proxy is None
    connect_url, host_header, sni = fetcher._pin_connection("https://example.com/x")
    assert "93.184.216.34" in connect_url
    assert host_header == "example.com" and sni == "example.com"


def test_llm_common_proxy_fallback():
    # LLM_PROXY 未設定なら共通プロキシを引き継ぐ
    assert _common_proxy({"FERMISCOPE_HTTP_PROXY": PROXY}) == PROXY
    assert _common_proxy({"HTTPS_PROXY": PROXY}) == PROXY
    # LLM_PROXY があれば最優先
    assert _common_proxy({"LLM_PROXY": "http://llm:9", "HTTPS_PROXY": PROXY}) == "http://llm:9"
    assert _common_proxy({}) == ""


def test_brave_and_ddg_accept_proxy():
    import httpx

    from fermiscope.research.search.brave import BraveSearchProvider
    from fermiscope.research.search.duckduckgo import DuckDuckGoSearchProvider

    # transport 指定時は proxy を適用しない(モック互換)。構築が通ることを確認。
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    b = BraveSearchProvider(api_key="k", proxy=PROXY, transport=t)
    d = DuckDuckGoSearchProvider(proxy=PROXY, transport=t)
    assert b is not None and d is not None
