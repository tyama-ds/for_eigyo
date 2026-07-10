"""Brave Search API アダプタ(実検索プロバイダ)。

公式仕様: https://api-dashboard.search.brave.com/app/documentation/web-search/get-started
- エンドポイント: GET https://api.search.brave.com/res/v1/web/search
- 認証: X-Subscription-Token ヘッダ(環境変数 BRAVE_API_KEY)

APIキーはログに出さない。SERPスクレイピングは行わない。
"""

from __future__ import annotations

import os

import httpx

from fermiscope.domain.models import SearchHit
from fermiscope.research.search.base import SearchProvider, SearchProviderError

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchProvider(SearchProvider):
    name = "brave"
    cost_per_search_usd = 0.005  # 概算(有償プランの目安)。設定で上書き可。

    def __init__(
        self,
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key = api_key or os.environ.get("BRAVE_API_KEY", "")
        if not key:
            raise SearchProviderError(
                "BRAVE_API_KEY が設定されていません。実検索には環境変数でAPIキーを指定するか、"
                "SEARCH_PROVIDER=mock を使用してください。"
            )
        self._api_key = key
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": key,
            },
        )

    async def search(self, query: str, max_results: int = 6, language: str = "ja") -> list[SearchHit]:
        params: dict[str, str | int] = {
            "q": query,
            "count": min(max_results, 20),
            "search_lang": "jp" if language == "ja" else language,
            "country": "JP" if language == "ja" else "US",
        }
        try:
            resp = await self._client.get(_ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            # 例外メッセージにAPIキーを含めない
            raise SearchProviderError(f"Brave Search APIへの接続に失敗しました: {type(exc).__name__}") from None
        if resp.status_code == 401:
            raise SearchProviderError("Brave Search API 認証エラー(APIキーを確認してください)")
        if resp.status_code == 429:
            raise SearchProviderError("Brave Search API レート制限(429)")
        if resp.status_code != 200:
            raise SearchProviderError(f"Brave Search API エラー: HTTP {resp.status_code}")
        data = resp.json()
        results = (data.get("web") or {}).get("results") or []
        hits: list[SearchHit] = []
        for rank, item in enumerate(results[:max_results], start=1):
            url = item.get("url", "")
            if not url:
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=item.get("title", ""),
                    snippet=item.get("description", ""),
                    rank=rank,
                    published_hint=item.get("age", "") or item.get("page_age", ""),
                )
            )
        return hits

    async def close(self) -> None:
        await self._client.aclose()
