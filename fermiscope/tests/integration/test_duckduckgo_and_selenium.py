"""DuckDuckGo検索(キー不要)と Selenium ハイブリッド取得のテスト。

いずれも外部ネットワーク・実ブラウザ不要(httpxはMockTransport、Seleniumは注入レンダラ)。
"""

from __future__ import annotations

import httpx
import pytest

from fermiscope.research.fetcher import DocumentFetcher
from fermiscope.research.search.duckduckgo import DuckDuckGoSearchProvider, _unwrap_ddg_url

# DuckDuckGo HTML エンドポイントの応答を模した最小HTML
_DDG_HTML = """
<html><body>
  <div class="result web-result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.stat.go.jp%2Fdata%2Ftokyo.html&rut=abc">
      東京都の世帯数（総務省統計局）
    </a>
    <a class="result__snippet">東京都の世帯数は約741万世帯。</a>
  </div>
  <div class="result web-result">
    <a class="result__a" href="https://example.com/piano">ピアノ調律師の人数</a>
    <div class="result__snippet">調律師は全国で約XXX人。</div>
  </div>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.stat.go.jp%2Fdata%2Ftokyo.html">重複（同一URL）</a>
  </div>
</body></html>
"""


def _ddg(html: str, status: int = 200) -> DuckDuckGoSearchProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=html, headers={"content-type": "text/html"})

    return DuckDuckGoSearchProvider(transport=httpx.MockTransport(handler))


def test_unwrap_ddg_redirect_url():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.stat.go.jp%2Fx&rut=z"
    assert _unwrap_ddg_url(wrapped) == "https://www.stat.go.jp/x"
    # 直リンクはそのまま
    assert _unwrap_ddg_url("https://example.com/a") == "https://example.com/a"


@pytest.mark.asyncio
async def test_duckduckgo_parses_and_dedups():
    provider = _ddg(_DDG_HTML)
    try:
        hits = await provider.search("東京都 ピアノ 調律師", max_results=6)
    finally:
        await provider.close()
    urls = [h.url for h in hits]
    assert "https://www.stat.go.jp/data/tokyo.html" in urls
    assert "https://example.com/piano" in urls
    assert len(urls) == len(set(urls))  # 重複URLは1件に
    assert all(h.url.startswith("https://") for h in hits)
    assert hits[0].title and hits[0].rank == 1
    assert provider.cost_per_search_usd == 0.0


@pytest.mark.asyncio
async def test_duckduckgo_rate_limit_raises():
    from fermiscope.research.search.base import SearchProviderError

    provider = _ddg("", status=429)
    with pytest.raises(SearchProviderError):
        await provider.search("q")
    await provider.close()


# ---------- Selenium ハイブリッド ----------

_JS_ONLY_HTML = "<html><body><div id='app'></div><noscript>JSが必要です</noscript></body></html>"
_RENDERED_HTML = "<html><body><p>調律師は東京都に約884人います。出典: 総務省。</p></body></html>"


def _html_fetcher(settings, body: str, renderer=None) -> DocumentFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return DocumentFetcher(
        settings,
        transport=httpx.MockTransport(handler),
        skip_dns=True,
        selenium_renderer=renderer,
    )


@pytest.mark.asyncio
async def test_selenium_fallback_used_when_text_is_sparse(settings):
    calls: list[str] = []

    def fake_render(url: str) -> str:
        calls.append(url)
        return _RENDERED_HTML

    fetcher = _html_fetcher(settings, _JS_ONLY_HTML, renderer=fake_render)
    doc = await fetcher.fetch("https://spa.example.jp/app")
    assert calls == ["https://spa.example.jp/app"]  # 本文が乏しいのでSeleniumが呼ばれる
    assert "884" in doc.text  # レンダリング後DOMから再抽出


@pytest.mark.asyncio
async def test_selenium_not_used_when_text_is_sufficient(settings):
    calls: list[str] = []

    def fake_render(url: str) -> str:
        calls.append(url)
        return _RENDERED_HTML

    rich = "<html><body><p>" + ("十分な本文。" * 60) + "</p></body></html>"
    fetcher = _html_fetcher(settings, rich, renderer=fake_render)
    doc = await fetcher.fetch("https://static.example.jp/page")
    assert calls == []  # httpxの本文で足りるのでSeleniumは呼ばれない
    assert "十分な本文" in doc.text


@pytest.mark.asyncio
async def test_selenium_render_error_keeps_httpx_result(settings):
    def boom(url: str) -> str:
        raise RuntimeError("driver crashed")

    fetcher = _html_fetcher(settings, _JS_ONLY_HTML, renderer=boom)
    doc = await fetcher.fetch("https://spa.example.jp/app")
    # レンダリング失敗でも例外を投げず、httpx取得の結果で継続する
    assert doc.doc_type.value == "html"
