"""MockSearchProvider — フィクスチャ駆動の検索プロバイダ。

APIキーなしで全体のデモとテストを動かすための実装。
`search_index.json` のエントリ({keywords, hits})に対して、
クエリにキーワードが部分一致したエントリのヒットを返す。
"""

from __future__ import annotations

import json
from pathlib import Path

from fermiscope.domain.models import SearchHit
from fermiscope.research.search.base import SearchProvider, SearchProviderError


class MockSearchProvider(SearchProvider):
    name = "mock"
    cost_per_search_usd = 0.0

    def __init__(self, corpus_dir: Path) -> None:
        self.corpus_dir = Path(corpus_dir)
        index_path = self.corpus_dir / "search_index.json"
        if not index_path.exists():
            raise SearchProviderError(f"モック検索インデックスがありません: {index_path}")
        with index_path.open(encoding="utf-8") as f:
            self._index: list[dict] = json.load(f)

    async def search(self, query: str, max_results: int = 6, language: str = "ja") -> list[SearchHit]:
        scored: list[tuple[int, dict]] = []
        for entry in self._index:
            keywords = entry.get("keywords", [])
            matches = sum(1 for k in keywords if k.lower() in query.lower())
            if matches > 0:
                for hit in entry.get("hits", []):
                    scored.append((matches, hit))
        # マッチ数降順で並べ、URL重複を除去
        scored.sort(key=lambda t: -t[0])
        seen: set[str] = set()
        results: list[SearchHit] = []
        for rank, (_, hit) in enumerate(scored):
            if hit["url"] in seen:
                continue
            seen.add(hit["url"])
            results.append(
                SearchHit(
                    url=hit["url"],
                    title=hit.get("title", ""),
                    snippet=hit.get("snippet", ""),
                    rank=rank + 1,
                    published_hint=hit.get("published_hint", ""),
                )
            )
            if len(results) >= max_results:
                break
        return results
