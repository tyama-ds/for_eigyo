"""SearchService(キャッシュ・重複排除・上限・リトライ)の統合テスト。"""

import pytest

from fermiscope.domain.models import SearchHit, SearchQuery
from fermiscope.research.search.base import SearchProvider, SearchProviderError
from fermiscope.research.search.service import SearchBudgetExceeded, SearchService


class CountingProvider(SearchProvider):
    name = "counting"
    cost_per_search_usd = 0.01

    def __init__(self, fail_times: int = 0):
        self.calls = 0
        self.fail_times = fail_times

    async def search(self, query, max_results=6, language="ja"):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise SearchProviderError("一時的な失敗")
        return [SearchHit(url=f"https://example.jp/{self.calls}", title=query)]


@pytest.mark.asyncio
async def test_cache_hit_for_same_query(settings):
    provider = CountingProvider()
    service = SearchService(provider, settings)
    q1 = SearchQuery(query="東京都 世帯数")
    q2 = SearchQuery(query="東京都 世帯数")
    await service.run(q1)
    await service.run(q2)
    assert provider.calls == 1
    assert q2.cache_hit is True
    assert service.cache_hits == 1


@pytest.mark.asyncio
async def test_search_count_budget(settings):
    provider = CountingProvider()
    service = SearchService(provider, settings, max_searches=2)
    await service.run(SearchQuery(query="q1"))
    await service.run(SearchQuery(query="q2"))
    with pytest.raises(SearchBudgetExceeded):
        await service.run(SearchQuery(query="q3"))


@pytest.mark.asyncio
async def test_cost_budget(settings):
    provider = CountingProvider()  # $0.01/検索
    service = SearchService(provider, settings, max_cost_usd=0.015)
    await service.run(SearchQuery(query="q1"))
    with pytest.raises(SearchBudgetExceeded, match="コスト"):
        await service.run(SearchQuery(query="q2"))


@pytest.mark.asyncio
async def test_retry_then_success(settings):
    settings.search.retry_backoff_seconds = 0.01
    provider = CountingProvider(fail_times=1)
    service = SearchService(provider, settings)
    q = SearchQuery(query="retry me")
    hits = await service.run(q)
    assert len(hits) == 1
    assert q.error == ""
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_persistent_failure_recorded_not_raised(settings):
    settings.search.retry_backoff_seconds = 0.01
    provider = CountingProvider(fail_times=99)
    service = SearchService(provider, settings)
    q = SearchQuery(query="always fails")
    hits = await service.run(q)
    assert hits == []
    assert "失敗" in q.error


@pytest.mark.asyncio
async def test_brave_adapter_contract(settings):
    """BraveアダプタのAPI契約テスト(MockTransportで公式レスポンス形式を再現)。"""
    import httpx

    from fermiscope.research.search.brave import BraveSearchProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "test-key"
        assert request.url.params["q"] == "ピアノ 保有率"
        return httpx.Response(200, json={
            "web": {"results": [
                {"url": "https://example.jp/a", "title": "A", "description": "desc", "age": "2024-01-01"},
                {"url": "https://example.jp/b", "title": "B", "description": "desc2"},
            ]}
        })

    provider = BraveSearchProvider(api_key="test-key", transport=httpx.MockTransport(handler))
    hits = await provider.search("ピアノ 保有率", max_results=5)
    assert len(hits) == 2
    assert hits[0].url == "https://example.jp/a"
    assert hits[0].rank == 1
    await provider.close()


@pytest.mark.asyncio
async def test_brave_error_does_not_leak_key(settings):
    import httpx

    from fermiscope.research.search.brave import BraveSearchProvider

    provider = BraveSearchProvider(
        api_key="secret-key-12345",
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
    )
    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("query")
    assert "secret-key-12345" not in str(exc_info.value)
    await provider.close()


def test_brave_requires_key(monkeypatch):
    from fermiscope.research.search.brave import BraveSearchProvider

    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(SearchProviderError, match="BRAVE_API_KEY"):
        BraveSearchProvider()
