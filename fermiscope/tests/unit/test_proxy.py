"""共通HTTP(S)プロキシ設定のテスト(検索・取得・LLMへの配線)。"""

from __future__ import annotations

from fermiscope.config import load_settings
from fermiscope.research.fetcher import DocumentFetcher

PROXY = "http://user:pass@proxy.example:8080"


def test_dedicated_env_sets_common_proxy():
    s = load_settings(env={"FERMISCOPE_HTTP_PROXY": PROXY})
    assert s.http_proxy == PROXY


def test_standard_proxy_env_populates_correct_field():
    """HTTP と HTTPS は潰さず、それぞれの専用フィールドに入る。"""
    assert load_settings(env={"HTTP_PROXY": PROXY}).http_proxy == PROXY
    assert load_settings(env={"HTTPS_PROXY": PROXY}).https_proxy == PROXY
    assert load_settings(env={"ALL_PROXY": PROXY}).all_proxy == PROXY
    assert load_settings(env={"https_proxy": PROXY}).https_proxy == PROXY
    # HTTP と HTTPS を別々に指定でき、混ざらない
    s = load_settings(env={"HTTP_PROXY": "http://h:1", "HTTPS_PROXY": "http://s:2"})
    assert s.http_proxy == "http://h:1"
    assert s.https_proxy == "http://s:2"
    assert s.effective_proxy("http") == "http://h:1"
    assert s.effective_proxy("https") == "http://s:2"


def test_no_proxy_bypass_for_localhost_and_docker():
    s = load_settings(
        env={"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost,127.0.0.1,host.docker.internal"}
    )
    assert s.proxy_for_url("https://example.com/x") == PROXY  # 公開先はプロキシ経由
    assert s.proxy_for_url("http://localhost:8080/x") is None  # バイパス
    assert s.proxy_for_url("http://127.0.0.1:11434/x") is None
    assert s.proxy_for_url("http://host.docker.internal:11434/x") is None


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
    assert fetcher._any_proxy is True
    connect_url, host_header, sni = fetcher._pin_connection("https://example.com/x")
    assert connect_url == "https://example.com/x"
    assert host_header is None and sni is None
    assert calls == []  # リゾルバは呼ばれない(接続はプロキシが担う)


def test_fetcher_pins_ip_without_proxy():
    """プロキシ未設定時は従来どおり検証済みIPへ接続を固定する。"""
    s = load_settings(env={})
    s.http_proxy = s.https_proxy = s.all_proxy = ""  # ホスト環境の proxy を無視
    fetcher = DocumentFetcher(s, resolver=lambda host: ["93.184.216.34"], skip_dns=False)
    assert fetcher._any_proxy is False
    connect_url, host_header, sni = fetcher._pin_connection("https://example.com/x")
    assert "93.184.216.34" in connect_url
    assert host_header == "example.com" and sni == "example.com"


def test_fetcher_pins_ip_for_no_proxy_host_even_with_proxy():
    """NO_PROXY 対象で直接接続になるURLでは IP 固定(SSRF対策)を維持する。"""
    s = load_settings(env={"HTTPS_PROXY": PROXY, "NO_PROXY": "example.com"})
    fetcher = DocumentFetcher(s, resolver=lambda host: ["93.184.216.34"], skip_dns=False)
    connect_url, host_header, sni = fetcher._pin_connection("https://example.com/x")
    assert "93.184.216.34" in connect_url  # プロキシ有でもバイパス先は IP 固定
    assert host_header == "example.com"


def test_llm_proxy_resolution_per_url():
    """LLM のプロキシは接続先URLごとに解決し、NO_PROXY を最優先する。"""
    from fermiscope.config import resolve_proxy_for_url

    env = {"HTTPS_PROXY": PROXY, "HTTP_PROXY": "http://h:1", "NO_PROXY": "localhost,127.0.0.1"}
    # localhost のローカルLLMは、プロキシ設定があっても直接接続(バイパス)
    p, note = resolve_proxy_for_url("http://127.0.0.1:11434/v1", env=env)
    assert p is None and "NO_PROXY" in note
    # 明示プロキシですら NO_PROXY のバイパスが勝つ
    p2, _ = resolve_proxy_for_url("http://localhost:11434/v1", "http://corp:3128", env=env)
    assert p2 is None
    # 公開HTTPSホストはスキームに応じた環境変数プロキシを使う
    p3, _ = resolve_proxy_for_url("https://api.example.com/v1", env=env)
    assert p3 == PROXY
    p4, _ = resolve_proxy_for_url("http://api.example.com/v1", env=env)
    assert p4 == "http://h:1"
    # 明示指定は(NO_PROXY 対象外なら)環境変数より優先
    p5, note5 = resolve_proxy_for_url("https://api.example.com/v1", "http://llm:9", env=env)
    assert p5 == "http://llm:9" and "明示" in note5


def test_brave_and_ddg_accept_proxy():
    import httpx

    from fermiscope.research.search.brave import BraveSearchProvider
    from fermiscope.research.search.duckduckgo import DuckDuckGoSearchProvider

    # transport 指定時は proxy を適用しない(モック互換)。構築が通ることを確認。
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    b = BraveSearchProvider(api_key="k", proxy=PROXY, transport=t)
    d = DuckDuckGoSearchProvider(proxy=PROXY, transport=t)
    assert b is not None and d is not None
