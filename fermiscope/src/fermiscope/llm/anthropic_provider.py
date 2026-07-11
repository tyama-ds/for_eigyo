"""Anthropic Messages API アダプタ。

公式仕様: POST {base}/v1/messages
- 認証ヘッダ: x-api-key、anthropic-version
- 応答本文: content[0].text

OpenAI互換と同じ補助タスク(構造化・抽出・批判・分解・説明文)を提供する。
プロキシ経由にも対応。APIキーはログに出さない。
"""

from __future__ import annotations

import logging
import os

import httpx

from fermiscope.llm.base import LLMProviderError
from fermiscope.llm.http_base import HttpLLMProvider

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(HttpLLMProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        api_base: str | None = None,
        timeout_seconds: float = 60.0,
        max_tokens: int = 1024,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "") or os.environ.get("LLM_MODEL", "")
        # プロキシ/ゲートウェイ経由の場合は api_base を差し替え可能
        self.api_base = (api_base or os.environ.get("ANTHROPIC_API_BASE", "") or _DEFAULT_BASE).rstrip("/")
        proxy = proxy or os.environ.get("LLM_PROXY") or None
        self.max_tokens = max_tokens
        if not key or not self.model:
            raise LLMProviderError(
                "Anthropic の APIキーとモデルID を設定してください"
                "(GUIの設定、または環境変数 ANTHROPIC_API_KEY / ANTHROPIC_MODEL)。"
            )
        headers = {
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        client_kwargs: dict = {"timeout": timeout_seconds, "headers": headers}
        if transport is not None:
            client_kwargs["transport"] = transport
        elif proxy:
            client_kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**client_kwargs)
        self.available = True

    async def close(self) -> None:
        await self._client.aclose()

    async def _raw_json_completion(self, system: str, user: str) -> str | None:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
            "system": system + "\n必ずJSONオブジェクトのみを出力してください(前後に地の文を付けない)。",
            "messages": [{"role": "user", "content": user}],
        }
        try:
            resp = await self._client.post(f"{self.api_base}/v1/messages", json=payload)
        except httpx.HTTPError as exc:
            logger.warning("Anthropic API接続エラー: %s", type(exc).__name__)  # キーは含めない
            return None
        if resp.status_code == 429:
            logger.warning("Anthropic APIレート制限(429)")
            return None
        if resp.status_code != 200:
            logger.warning("Anthropic APIエラー: HTTP %s", resp.status_code)
            return None
        try:
            data = resp.json()
            blocks = data.get("content", [])
            texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            return "".join(texts) or None
        except (KeyError, IndexError, ValueError, AttributeError):
            logger.warning("Anthropic API応答の形式が不正です")
            return None
