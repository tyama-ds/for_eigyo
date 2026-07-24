"""SearXNG検索をopen_deep_researchへ提供する同梱FastMCPサーバー。

open_deep_research (search_api="none") は mcp_config 経由で
`{url}/mcp` (streamable HTTP) に接続し、`searxng_web_search` ツールを使う。

環境変数 (odr_engine.build_mcp_env が設定する):
- SEARXNG_ENDPOINT   : SearXNGベースURL (必須)
- SEARXNG_TIMEOUT    : 検索タイムアウト秒 (既定20)
- SEARXNG_MAX_RESULTS: クエリあたり最大結果数 (既定10)
- MCP_HOST / MCP_PORT: このサーバーのlisten先 (既定 127.0.0.1:8765)

検証済み: mcp==1.28.1 の FastMCP(host=..., port=..., stateless_http=True) と
mcp.run(transport="streamable-http") (パスは /mcp)。
"""

from __future__ import annotations

import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odr_engine import format_searx_results  # noqa: E402

MAX_QUERIES_PER_CALL = 5

mcp = FastMCP(
    "dro-searxng-search",
    instructions="Internal SearXNG web search for Deep Research Orchestrator.",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8765")),
    stateless_http=True,
)


@mcp.tool()
async def searxng_web_search(queries: list[str]) -> str:
    """Search the web via the internal SearXNG metasearch instance.

    Args:
        queries: One or more search queries (natural language or keywords).

    Returns:
        A formatted text block per query with numbered results:
        title, URL and snippet. Use the URLs to cite sources.
    """
    endpoint = os.environ["SEARXNG_ENDPOINT"].rstrip("/")
    timeout = float(os.environ.get("SEARXNG_TIMEOUT", "20"))
    max_results = int(os.environ.get("SEARXNG_MAX_RESULTS", "10"))

    blocks: list[str] = []
    # SearXNGは内部サービスのためproxyを経由しない (trust_env=False)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        for query in list(queries)[:MAX_QUERIES_PER_CALL]:
            if not isinstance(query, str) or not query.strip():
                continue
            try:
                response = await client.get(
                    f"{endpoint}/search",
                    params={"q": query, "format": "json"},
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                blocks.append(format_searx_results(query, response.json(), max_results))
            except httpx.HTTPError as e:
                blocks.append(f'Search results for "{query}":\n\n(search failed: {e})')
    if not blocks:
        return "(no valid queries)"
    return "\n\n---\n\n".join(blocks)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
