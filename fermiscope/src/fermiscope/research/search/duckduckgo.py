"""DuckDuckGo 検索アダプタ(APIキー不要)。

Brave のような APIキーが無い環境向けのフォールバック検索プロバイダ。
DuckDuckGo の HTML エンドポイント(https://html.duckduckgo.com/html/)を
取得し、結果リンク・タイトル・スニペットを BeautifulSoup で抽出する。

- APIキー不要
- 返すのは検索ヒット(URL/タイトル/抜粋)のみ。各ページの取得は
  DocumentFetcher(SSRFガード等つき)が担当する。
- bot 対策で一時的にブロックされることがある(その場合は SearchProviderError)。
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import httpx

from fermiscope.domain.models import SearchHit
from fermiscope.research.search.base import SearchProvider, SearchProviderError

_ENDPOINT = "https://html.duckduckgo.com/html/"
# 通常のブラウザ相当の UA(空 UA は弾かれることがある)
_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0"


def _unwrap_ddg_url(href: str) -> str:
    """DuckDuckGo のリダイレクトリンク(//duckduckgo.com/l/?uddg=...)を実URLへ展開する。"""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


class DuckDuckGoSearchProvider(SearchProvider):
    name = "duckduckgo"
    cost_per_search_usd = 0.0  # APIキー不要・無料

    def __init__(
        self,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
    ) -> None:
        client_kwargs: dict = {
            "timeout": timeout_seconds,
            "transport": transport,
            "headers": {
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ja,en;q=0.7",
            },
            "follow_redirects": True,
        }
        if proxy and transport is None:
            client_kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**client_kwargs)

    async def search(self, query: str, max_results: int = 6, language: str = "ja") -> list[SearchHit]:
        params = {
            "q": query,
            "kl": "jp-jp" if language == "ja" else "us-en",
        }
        try:
            resp = await self._client.post(_ENDPOINT, data=params)
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                f"DuckDuckGo への接続に失敗しました: {type(exc).__name__}"
            ) from None
        if resp.status_code == 202 or resp.status_code == 429:
            raise SearchProviderError("DuckDuckGo にレート制限/ボット判定されました。時間をおいて再試行してください。")
        if resp.status_code != 200:
            raise SearchProviderError(f"DuckDuckGo エラー: HTTP {resp.status_code}")
        return self._parse(resp.text, max_results)

    def _parse(self, html: str, max_results: int) -> list[SearchHit]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for result in soup.select("div.result, div.web-result"):
            link = result.select_one("a.result__a")
            if link is None:
                continue
            url = _unwrap_ddg_url(str(link.get("href") or ""))
            if not url or not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            snippet_el = result.select_one(".result__snippet")
            hits.append(
                SearchHit(
                    url=url,
                    title=link.get_text(" ", strip=True),
                    snippet=snippet_el.get_text(" ", strip=True) if snippet_el else "",
                    rank=len(hits) + 1,
                )
            )
            if len(hits) >= max_results:
                break
        return hits

    async def close(self) -> None:
        await self._client.aclose()
