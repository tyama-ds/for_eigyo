"""SearchProvider 抽象インターフェース。

検索エンジンの検索結果ページを無断スクレイピングせず、
正式な検索APIのみを利用する。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from fermiscope.domain.models import SearchHit


class SearchProviderError(RuntimeError):
    pass


class SearchProvider(ABC):
    """Web検索プロバイダの共通インターフェース。"""

    name: str = "abstract"
    cost_per_search_usd: float = 0.0

    @abstractmethod
    async def search(self, query: str, max_results: int = 6, language: str = "ja") -> list[SearchHit]:
        """クエリを実行し検索ヒットを返す。失敗時は SearchProviderError。"""

    async def close(self) -> None:  # noqa: B027
        """リソース解放(必要なプロバイダのみ実装)。"""
