"""Collector 基底クラス"""

from __future__ import annotations

import abc
from typing import Any

from for_eigyo.storage.models import SearchResult, Company


class BaseCollector(abc.ABC):
    """全コレクターの基底"""

    name: str = "base"

    @abc.abstractmethod
    def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        """キーワード検索を実行し SearchResult のリストを返す"""

    def to_companies(self, results: list[SearchResult]) -> list[Company]:
        """SearchResult から Company を簡易生成（サブクラスでオーバーライド可）"""
        companies: list[Company] = []
        seen: set[str] = set()
        for r in results:
            if r.title not in seen:
                seen.add(r.title)
                companies.append(
                    Company(
                        name=r.title,
                        website=r.url,
                        description=r.snippet,
                        source=self.name,
                    )
                )
        return companies
