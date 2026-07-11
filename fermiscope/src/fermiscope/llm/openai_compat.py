"""OpenAI互換 Chat Completions API の汎用アダプタ。

OpenAI 本体・ローカルLLM(Ollama / LM Studio / vLLM の OpenAI 互換エンドポイント)・
各社ゲートウェイを1実装でカバーする。プロキシ経由の接続にも対応する。

設定は GUI(/api/settings/llm)またはコンストラクタ引数、環境変数の順で解決する。
- api_base: 例 https://api.openai.com/v1 、http://localhost:11434/v1 等
- api_key:  APIキー(ログには一切出力しない)
- model:    モデルID(コードに固定しない)
- proxy:    例 http://user:pass@proxy.example:8080

出力はJSONを要求し、Pydanticスキーマで検証する(基底クラス側)。
不正出力・タイムアウト・拒否・レート制限・空レスポンスはすべて None を返す。
"""

from __future__ import annotations

import logging
import os

import httpx

from fermiscope.llm.base import LLMProviderError
from fermiscope.llm.http_base import HttpLLMProvider

logger = logging.getLogger(__name__)


class OpenAICompatProvider(HttpLLMProvider):
    name = "openai_compatible"

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 60.0,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_base = (api_base or os.environ.get("LLM_API_BASE", "")).rstrip("/")
        key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "")
        proxy = proxy or os.environ.get("LLM_PROXY") or None
        if not self.api_base or not self.model:
            raise LLMProviderError(
                "LLM の接続先(api_base)とモデルID(model)を設定してください"
                "(GUIの設定、または環境変数 LLM_API_BASE / LLM_MODEL)。"
                "LLMなしで使う場合は provider=noop。"
            )
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
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
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        try:
            resp = await self._client.post(f"{self.api_base}/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            logger.warning("LLM API接続エラー: %s", type(exc).__name__)  # キーは含めない
            return None
        if resp.status_code == 429:
            logger.warning("LLM APIレート制限(429)")
            return None
        if resp.status_code != 200:
            logger.warning("LLM APIエラー: HTTP %s", resp.status_code)
            return None
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            logger.warning("LLM API応答の形式が不正です")
            return None
