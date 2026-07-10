"""SearchService — キャッシュ・レート制限・回数/コスト上限・重複排除・リトライ。

プロバイダ非依存の横断機能をここに集約する。
"""

from __future__ import annotations

import asyncio
import time

from fermiscope.config import Settings
from fermiscope.domain.models import SearchHit, SearchQuery, utcnow
from fermiscope.research.search.base import SearchProvider, SearchProviderError


class SearchBudgetExceeded(RuntimeError):
    """検索回数またはコスト上限に到達。"""


class _RateLimiter:
    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self._interval:
                await asyncio.sleep(self._interval - delta)
            self._last = time.monotonic()


class SearchService:
    """1プロジェクト分の検索実行を管理する。"""

    def __init__(
        self,
        provider: SearchProvider,
        settings: Settings,
        max_searches: int | None = None,
        max_cost_usd: float | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.max_searches = max_searches or settings.search.max_searches_per_project
        self.max_cost_usd = max_cost_usd if max_cost_usd is not None else settings.search.max_cost_per_project_usd
        self.executed_count = 0
        self.cache_hits = 0
        self.total_cost_usd = 0.0
        self._cache: dict[str, tuple[float, list[SearchHit]]] = {}
        self._seen_queries: set[str] = set()
        # レート制限は実プロバイダのみ(モックはローカル完結でAPI負荷がない)
        rate = 0.0 if provider.name == "mock" else settings.search.rate_limit_per_second
        self._rate = _RateLimiter(rate)

    def _cache_key(self, query: str, language: str) -> str:
        return f"{language}::{query.strip().lower()}"

    async def run(self, sq: SearchQuery) -> list[SearchHit]:
        """SearchQuery を実行し、ヒットを返す。SearchQuery に実行情報を記録する。"""
        key = self._cache_key(sq.query, sq.language)
        sq.provider = self.provider.name

        # 同一クエリの重複排除
        if key in self._seen_queries and key not in self._cache:
            sq.deduplicated = True
            sq.error = "同一クエリが既に失敗しているためスキップしました"
            return []
        # キャッシュ
        cached = self._cache.get(key)
        if cached is not None:
            ts, hits = cached
            ttl = self.settings.search.cache_ttl_hours * 3600
            if time.time() - ts < ttl:
                self.cache_hits += 1
                sq.cache_hit = True
                sq.executed_at = utcnow()
                sq.results_count = len(hits)
                if key in self._seen_queries:
                    sq.deduplicated = True
                self._seen_queries.add(key)
                return [h.model_copy(update={"query_id": sq.id}) for h in hits]

        # 上限検査
        if self.executed_count >= self.max_searches:
            raise SearchBudgetExceeded(
                f"検索回数上限({self.max_searches}回)に達しました。"
            )
        cost = self.provider.cost_per_search_usd or self.settings.search.cost_per_search_usd
        if self.provider.name == "mock":
            cost = 0.0
        if self.total_cost_usd + cost > self.max_cost_usd:
            raise SearchBudgetExceeded(
                f"検索コスト上限(${self.max_cost_usd:.2f})に達しました。"
            )

        self._seen_queries.add(key)
        await self._rate.wait()

        last_error: Exception | None = None
        for attempt in range(self.settings.search.max_retries + 1):
            try:
                hits = await asyncio.wait_for(
                    self.provider.search(
                        sq.query,
                        max_results=self.settings.search.max_results_per_query,
                        language=sq.language,
                    ),
                    timeout=self.settings.search.timeout_seconds,
                )
                self.executed_count += 1
                self.total_cost_usd += cost
                sq.executed_at = utcnow()
                sq.results_count = len(hits)
                sq.estimated_cost_usd = cost
                for h in hits:
                    h.query_id = sq.id
                self._cache[key] = (time.time(), hits)
                return hits
            except TimeoutError as exc:
                last_error = exc
            except SearchProviderError as exc:
                last_error = exc
            if attempt < self.settings.search.max_retries:
                await asyncio.sleep(self.settings.search.retry_backoff_seconds * (2**attempt))

        self.executed_count += 1
        sq.executed_at = utcnow()
        sq.error = f"検索に失敗しました: {type(last_error).__name__}: {last_error}"
        return []
