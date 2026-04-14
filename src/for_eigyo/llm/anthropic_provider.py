"""Anthropic プロバイダ（Prompt Caching 対応）"""

from __future__ import annotations

import os
from typing import Any

from for_eigyo.llm.base import LLMProvider


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API を使ったテキスト生成（Prompt Caching 対応）"""

    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.3,
        max_tokens: int = 2000,
        use_cache: bool = True,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.use_cache = use_cache

        if not self.api_key:
            raise ValueError(
                "Anthropic API key not found. "
                "Set ANTHROPIC_API_KEY environment variable or pass api_key parameter."
            )

        try:
            from anthropic import Anthropic
            self.client = Anthropic(api_key=self.api_key)
        except ImportError:
            raise ImportError(
                "anthropic package not installed. "
                "Install with: pip install 'for-eigyo[llm]'"
            )

    def generate(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
        system_content = system or "あなたは営業支援AIアシスタントです。簡潔で実用的な回答をしてください。"

        # Prompt Caching: system prompt にキャッシュヒントを付与
        if self.use_cache:
            system_param = [
                {
                    "type": "text",
                    "text": system_content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system_content  # type: ignore[assignment]

        response = self.client.messages.create(
            model=kwargs.get("model", self.model),
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
            temperature=kwargs.get("temperature", self.temperature),
            system=system_param,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
