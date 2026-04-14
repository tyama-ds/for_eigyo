"""LLM プロバイダ基底クラスとファクトリ"""

from __future__ import annotations

import abc
import os
from typing import Any


class LLMProvider(abc.ABC):
    """LLM プロバイダの共通インターフェース"""

    name: str = "base"

    @abc.abstractmethod
    def generate(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
        """テキスト生成"""

    def summarize(self, text: str, *, max_length: int = 500) -> str:
        """テキスト要約"""
        prompt = (
            f"以下のテキストを{max_length}文字以内で要約してください。"
            f"営業活動に有用な情報を中心にまとめてください。\n\n{text}"
        )
        return self.generate(prompt)

    def extract_structured(self, text: str, schema_description: str) -> str:
        """非構造データからの構造化抽出"""
        prompt = (
            f"以下のテキストから、指定されたスキーマに基づいて情報を抽出し、"
            f"JSON形式で出力してください。\n\n"
            f"スキーマ:\n{schema_description}\n\n"
            f"テキスト:\n{text}"
        )
        return self.generate(prompt, system="JSONのみを出力してください。")

    def classify(self, text: str, categories: list[str]) -> str:
        """テキスト分類"""
        cats = ", ".join(categories)
        prompt = (
            f"以下のテキストを次のカテゴリのいずれかに分類してください: {cats}\n\n"
            f"テキスト:\n{text}\n\n"
            f"カテゴリ名のみを回答してください。"
        )
        return self.generate(prompt)

    def generate_sales_draft(
        self,
        company_info: str,
        purpose: str = "初回アプローチ",
    ) -> str:
        """営業メール/トークスクリプトのドラフト生成"""
        prompt = (
            f"以下の企業情報を踏まえて、{purpose}用の営業メールのドラフトを作成してください。\n\n"
            f"企業情報:\n{company_info}\n\n"
            f"丁寧かつ簡潔に、相手企業の特徴に触れながら価値提案を含めてください。"
        )
        return self.generate(prompt)

    def report(self, analysis_results: str) -> str:
        """分析結果を自然言語レポートに変換"""
        prompt = (
            f"以下の分析結果を、営業担当者向けの分かりやすいレポートにまとめてください。\n\n"
            f"分析結果:\n{analysis_results}"
        )
        return self.generate(prompt)


def get_provider(
    provider_name: str = "openai",
    **kwargs: Any,
) -> LLMProvider:
    """
    LLM プロバイダのファクトリ

    Parameters
    ----------
    provider_name : "openai" | "anthropic"
    """
    if provider_name == "openai":
        from for_eigyo.llm.openai_provider import OpenAIProvider
        return OpenAIProvider(**kwargs)
    elif provider_name == "anthropic":
        from for_eigyo.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(**kwargs)
    else:
        raise ValueError(f"Unknown LLM provider: {provider_name}")


def is_llm_available(provider_name: str = "openai") -> bool:
    """指定プロバイダのAPIキーが設定されているかチェック"""
    key_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    env_key = key_map.get(provider_name)
    if not env_key:
        return False
    return bool(os.environ.get(env_key))
