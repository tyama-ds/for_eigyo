"""DocumentFetcher(多形式・キャッシュ・robots)の統合テスト。"""

import httpx
import pytest

from fermiscope.domain.enums import DocumentType
from fermiscope.research.fetcher import DocumentFetcher, FetchError


@pytest.mark.asyncio
async def test_html_fetch_and_parse(settings, mock_fetcher):
    doc = await mock_fetcher.fetch("https://stats.example-gov.jp/kokusei/tokyo-households-2020.html")
    assert doc.doc_type == DocumentType.HTML
    assert "7,227,180" in doc.text
    assert doc.tables  # 表が抽出される
    assert doc.title
    assert doc.content_hash


@pytest.mark.asyncio
async def test_pdf_fetch_and_parse(settings, mock_fetcher):
    doc = await mock_fetcher.fetch("https://survey.example.or.jp/workload2024.pdf")
    assert doc.doc_type == DocumentType.PDF
    assert "1.5 tunings" in doc.text
    assert "200 days" in doc.text


@pytest.mark.asyncio
async def test_csv_fetch(settings, mock_fetcher):
    doc = await mock_fetcher.fetch("https://esri.example-gov.jp/shouhi/piano-ownership.csv")
    assert doc.doc_type == DocumentType.CSV
    assert "ピアノ,10.4" in doc.text


@pytest.mark.asyncio
async def test_fetch_cache(settings, mock_fetcher):
    doc1 = await mock_fetcher.fetch("https://esri.example-gov.jp/shouhi/piano-ownership.csv")
    doc2 = await mock_fetcher.fetch("https://esri.example-gov.jp/shouhi/piano-ownership.csv")
    assert doc1 is doc2  # キャッシュヒット


@pytest.mark.asyncio
async def test_404_raises(settings, mock_fetcher):
    with pytest.raises(FetchError, match="404"):
        await mock_fetcher.fetch("https://unknown.example.jp/missing.html")


@pytest.mark.asyncio
async def test_robots_disallow_respected(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        return httpx.Response(200, text="<html><body>secret</body></html>",
                              headers={"content-type": "text/html"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(FetchError, match="robots"):
        await fetcher.fetch("https://example.jp/private/page.html")
    # 許可されたパスは取得できる
    doc = await fetcher.fetch("https://example.jp/public/page.html")
    assert "secret" in doc.text


@pytest.mark.asyncio
async def test_redirect_followed_and_revalidated(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        if url == "https://example.jp/old":
            return httpx.Response(301, headers={"location": "https://example.jp/new"})
        if url == "https://example.jp/new":
            return httpx.Response(200, text="<html><body>moved</body></html>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(404)

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    doc = await fetcher.fetch("https://example.jp/old")
    assert doc.final_url == "https://example.jp/new"
    assert "moved" in doc.text


@pytest.mark.asyncio
async def test_user_agent_sent(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    await fetcher.fetch("https://example.jp/page")
    assert "FermiScopeBot" in seen["ua"]
