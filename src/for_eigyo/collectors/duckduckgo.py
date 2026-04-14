"""DuckDuckGo 検索コレクター"""

from __future__ import annotations

import logging
from typing import Any

from duckduckgo_search import DDGS

from for_eigyo.collectors.base import BaseCollector
from for_eigyo.storage.models import SearchResult

logger = logging.getLogger(__name__)


class DuckDuckGoCollector(BaseCollector):
    """DuckDuckGo を使った Web / ニュース検索"""

    name = "duckduckgo"

    def __init__(self, region: str = "jp-jp", safesearch: str = "moderate"):
        self.region = region
        self.safesearch = safesearch

    def search(
        self,
        query: str,
        *,
        max_results: int = 20,
        search_type: str = "text",
        **kwargs: Any,
    ) -> list[SearchResult]:
        """
        Parameters
        ----------
        query : 検索キーワード
        max_results : 取得件数上限
        search_type : "text" | "news"
        """
        logger.info("DuckDuckGo %s search: %s (max=%d)", search_type, query, max_results)

        results: list[SearchResult] = []
        try:
            with DDGS() as ddgs:
                if search_type == "news":
                    raw = list(
                        ddgs.news(
                            query,
                            region=self.region,
                            safesearch=self.safesearch,
                            max_results=max_results,
                        )
                    )
                else:
                    raw = list(
                        ddgs.text(
                            query,
                            region=self.region,
                            safesearch=self.safesearch,
                            max_results=max_results,
                        )
                    )

                for item in raw:
                    results.append(
                        SearchResult(
                            query=query,
                            title=item.get("title", ""),
                            url=item.get("href", item.get("url", "")),
                            snippet=item.get("body", item.get("snippet", "")),
                            source=f"duckduckgo_{search_type}",
                            raw_data=item,
                        )
                    )
        except Exception:
            logger.exception("DuckDuckGo search failed for query: %s", query)

        logger.info("DuckDuckGo: got %d results", len(results))
        return results

    def search_news(self, query: str, max_results: int = 20) -> list[SearchResult]:
        return self.search(query, max_results=max_results, search_type="news")
