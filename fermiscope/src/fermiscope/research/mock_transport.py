"""モックコーパスを配信する httpx トランスポート。

Mock検索モードとテストで使用し、実ネットワークへは一切出ない。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx


def build_mock_transport(corpus_dir: Path) -> httpx.MockTransport:
    corpus_dir = Path(corpus_dir)
    with (corpus_dir / "url_map.json").open(encoding="utf-8") as f:
        url_map: dict[str, dict[str, str]] = json.load(f)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404, text="not found")
        entry = url_map.get(url)
        if entry is None:
            return httpx.Response(404, text="not found")
        path = corpus_dir / "documents" / entry["file"]
        data = path.read_bytes()
        return httpx.Response(
            200,
            content=data,
            headers={"content-type": entry.get("content_type", "text/html")},
        )

    return httpx.MockTransport(handler)
