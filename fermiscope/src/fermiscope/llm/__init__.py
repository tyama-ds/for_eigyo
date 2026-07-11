"""LLMプロバイダ抽象化(補助機能専用)。

LLMは曖昧さの解釈・候補生成・非構造化抽出の補助にのみ使い、
検索・計算・証拠保存・最終値確定には使用しない。
"""

from fermiscope.llm.anthropic_provider import AnthropicProvider
from fermiscope.llm.base import LLMProvider, LLMProviderError
from fermiscope.llm.mock import MockLLMProvider
from fermiscope.llm.noop import NoOpLLMProvider
from fermiscope.llm.openai_compat import OpenAICompatProvider


def create_llm_provider(provider_name: str) -> LLMProvider:
    """環境変数 LLM_PROVIDER の値からプロバイダを構築する。"""
    name = (provider_name or "noop").lower()
    if name == "noop":
        return NoOpLLMProvider()
    if name == "mock":
        return MockLLMProvider()
    if name == "openai_compatible":
        return OpenAICompatProvider()
    if name == "anthropic":
        return AnthropicProvider()
    raise LLMProviderError(f"未知のLLMプロバイダです: {provider_name}")


__all__ = [
    "AnthropicProvider",
    "LLMProvider",
    "LLMProviderError",
    "MockLLMProvider",
    "NoOpLLMProvider",
    "OpenAICompatProvider",
    "create_llm_provider",
]
