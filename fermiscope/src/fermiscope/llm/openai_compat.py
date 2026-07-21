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
from fermiscope.llm.http_base import HttpLLMProvider, build_llm_http_client

logger = logging.getLogger(__name__)


def _body_snippet(resp: httpx.Response, limit: int = 160) -> str:
    """エラー応答本文の先頭を1行に整形して返す(診断用。長文・改行は畳む)。"""
    try:
        text = " ".join(resp.text.split())
    except Exception:  # noqa: BLE001 — 本文が読めなくても診断は続行
        return ""
    return text[:limit]


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
        # None=未指定(環境変数から解決)/ ""=明示的にキー無し(環境変数で補完しない)。
        # 接続先変更後にキー未設定なら、旧キーを新接続先へ送らないための区別。
        key = os.environ.get("LLM_API_KEY", "") if api_key is None else api_key
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
        # 接続先ごとのプロキシ解決(NO_PROXY 最優先)+ trust_env=False。
        # ローカルLLM(localhost 等)は社内プロキシを経由させない。
        self._client, self._connection_info = build_llm_http_client(
            self.api_base, headers, timeout_seconds, explicit_proxy=proxy, transport=transport
        )
        self.last_error = ""
        # response_format(JSONモード)非対応サーバへのフォールバック記憶
        self._json_mode_unsupported = False
        self.available = True

    async def close(self) -> None:
        await self._client.aclose()

    async def _raw_json_completion(self, system: str, user: str) -> str | None:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        }
        if not self._json_mode_unsupported:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(2):
            try:
                resp = await self._client.post(f"{self.api_base}/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                # キー・プロキシ資格情報は含めない
                self.last_error = f"接続エラー: {type(exc).__name__}"
                logger.warning("LLM API接続エラー: %s", type(exc).__name__)
                return None
            if resp.status_code == 429:
                self.last_error = "HTTP 429(レート制限)"
                logger.warning("LLM APIレート制限(429)")
                return None
            if resp.status_code == 400 and "response_format" in payload and attempt == 0:
                # ローカルLLMサーバ等は response_format 未対応で 400 を返すことがある。
                # 1回だけ外して再試行し、以後はこのプロバイダでは送らない。
                self._json_mode_unsupported = True
                payload.pop("response_format", None)
                logger.info("response_format 非対応の可能性(HTTP 400)。外して再試行します。")
                continue
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code}: {_body_snippet(resp)}"
                logger.warning("LLM APIエラー: HTTP %s", resp.status_code)
                return None
            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                self.last_error = ""
                return content
            except (KeyError, IndexError, ValueError):
                self.last_error = "応答の形式が不正です(choices[0].message.content がありません)"
                logger.warning("LLM API応答の形式が不正です")
                return None
        return None
