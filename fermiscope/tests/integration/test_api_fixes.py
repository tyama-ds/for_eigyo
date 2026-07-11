"""レビュー指摘のAPI/オーケストレーション修正の回帰テスト。"""

from __future__ import annotations

import httpx
import pytest

from fermiscope.domain.models import SearchHit, SearchQuery
from fermiscope.research.fetcher import DocumentFetcher, FetchError
from fermiscope.research.search.base import SearchProvider, SearchProviderError
from fermiscope.research.search.service import SearchService

PIANO = "東京都内にはピアノ調律師が何人いるか"


class FlakyProvider(SearchProvider):
    name = "flaky"
    cost_per_search_usd = 0.0

    def __init__(self, fail_queries: set[str]):
        self.fail_queries = fail_queries
        self.calls: list[str] = []

    async def search(self, query, max_results=6, language="ja"):
        self.calls.append(query)
        if query in self.fail_queries:
            raise SearchProviderError("一時障害")
        return [SearchHit(url=f"https://ex.jp/{query}", title=query)]


@pytest.mark.asyncio
async def test_failed_query_can_be_retried_by_later_param(settings):
    settings.search.retry_backoff_seconds = 0.01
    settings.search.max_retries = 0
    provider = FlakyProvider(fail_queries=set())
    service = SearchService(provider, settings)

    # 1回目: 失敗させる
    provider.fail_queries = {"共通クエリ"}
    q1 = SearchQuery(query="共通クエリ")
    assert await service.run(q1) == []
    assert q1.error

    # 2回目: 同一クエリを別パラメータが実行 → 障害回復後は再試行される(汚染されない)
    provider.fail_queries = set()
    q2 = SearchQuery(query="共通クエリ")
    hits = await service.run(q2)
    assert len(hits) == 1
    assert not q2.deduplicated


@pytest.mark.asyncio
async def test_successful_query_is_deduped(settings):
    provider = FlakyProvider(fail_queries=set())
    service = SearchService(provider, settings)
    q1 = SearchQuery(query="同じ")
    q2 = SearchQuery(query="同じ")
    await service.run(q1)
    await service.run(q2)
    assert provider.calls.count("同じ") == 1
    assert q2.cache_hit is True


@pytest.mark.asyncio
async def test_budget_carries_across_runs(settings):
    provider = FlakyProvider(fail_queries=set())
    # 前実行で3回消費済みとして開始
    service = SearchService(provider, settings, max_searches=5, executed_count=3)
    await service.run(SearchQuery(query="a"))
    await service.run(SearchQuery(query="b"))
    from fermiscope.research.search.service import SearchBudgetExceeded

    with pytest.raises(SearchBudgetExceeded):
        await service.run(SearchQuery(query="c"))  # 3+2=5 到達
    assert service.session_searches == 2  # このサービスでの実行は2回


def test_project_budget_not_reset_on_rerun(app_client):
    report = app_client.post("/api/projects", json={"question": PIANO, "iterations": 4000}).json()
    pid = report["project"]["id"]
    assert app_client.post(f"/api/projects/{pid}/research/start?wait=true").json()["status"] == "done"
    # プロジェクト累積検索(searches_spent)が記録され、再実行に引き継がれる
    rep = app_client.get(f"/api/projects/{pid}").json()
    assert rep["run"]["searches_executed"] > 0
    st = app_client.get(f"/api/projects/{pid}/research/status").json()
    assert st["searches_executed"] > 0
    # 再実行しても全体は完走する(予算は累積されるが上限に達しない範囲)
    assert app_client.post(f"/api/projects/{pid}/research/start?wait=true").json()["status"] == "done"


def test_edit_blocked_via_running_guard(app_client, monkeypatch):
    report = app_client.post("/api/projects", json={"question": PIANO}).json()
    pid = report["project"]["id"]
    # run_manager.is_running を True に見せかけて編集拒否を確認
    monkeypatch.setattr(app_client.app.state.run_manager, "is_running", lambda p: True)
    r = app_client.patch(f"/api/projects/{pid}/parameters/base_households", json={"central": 1.0})
    assert r.status_code == 409
    r2 = app_client.post(f"/api/projects/{pid}/recalculate", json={})
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_empty_content_type_rejected(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"{}", headers={})  # Content-Typeなし

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(FetchError, match="Content-Type"):
        await fetcher.fetch("https://ex.jp/data.json")


@pytest.mark.asyncio
async def test_robots_rechecked_after_redirect(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            # 移動先ホストの robots は Disallow
            if "target.example" in url:
                return httpx.Response(200, text="User-agent: *\nDisallow: /")
            return httpx.Response(404)
        if "start.example" in url:
            return httpx.Response(302, headers={"location": "https://target.example/secret"})
        return httpx.Response(200, text="<html>secret</html>", headers={"content-type": "text/html"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(FetchError, match="robots"):
        await fetcher.fetch("https://start.example/page")


@pytest.mark.asyncio
async def test_giant_robots_does_not_exhaust_memory(settings):
    settings.fetch.max_response_bytes = 2000

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, content=b"A" * 5_000_000, headers={"content-type": "text/plain"})
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    # robots が巨大でも例外を握りつぶして許可扱い → 本体は取得できる
    doc = await fetcher.fetch("https://ex.jp/page")
    assert "ok" in doc.text
