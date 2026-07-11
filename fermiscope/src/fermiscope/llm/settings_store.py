"""LLM 接続設定のランタイムストア。

GUI から LLM プロバイダ(なし/ローカル・OpenAI互換/Anthropic)と接続情報を
設定・切替できるようにする。APIキーはサーバ側のファイルにのみ保存し、
APIレスポンスでは「設定済みか(key_set)」のみを返す(キー本体は返さない)。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from fermiscope.llm.anthropic_provider import AnthropicProvider
from fermiscope.llm.base import LLMProvider, LLMProviderError
from fermiscope.llm.mock import MockLLMProvider
from fermiscope.llm.noop import NoOpLLMProvider
from fermiscope.llm.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

ProviderName = Literal["noop", "mock", "openai_compatible", "anthropic"]


class LLMRuntimeConfig(BaseModel):
    """GUI から編集できる LLM 接続設定。"""

    provider: ProviderName = "noop"
    api_base: str = ""
    model: str = ""
    api_key: str = ""  # サーバ内でのみ保持。API では返さない。
    proxy: str = ""
    timeout_seconds: float = 60.0

    def public_dict(self) -> dict:
        """APIへ返す安全な表現(キー本体は含めず有無のみ)。"""
        return {
            "provider": self.provider,
            "api_base": self.api_base,
            "model": self.model,
            "proxy": self.proxy,
            "timeout_seconds": self.timeout_seconds,
            "key_set": bool(self.api_key),
        }


def build_provider(config: LLMRuntimeConfig) -> LLMProvider:
    """設定から LLMProvider を構築する。失敗時は LLMProviderError。"""
    name = config.provider
    if name == "noop":
        return NoOpLLMProvider()
    if name == "mock":
        return MockLLMProvider()
    if name == "openai_compatible":
        return OpenAICompatProvider(
            api_base=config.api_base or None,
            api_key=config.api_key or None,
            model=config.model or None,
            timeout_seconds=config.timeout_seconds,
            proxy=config.proxy or None,
        )
    if name == "anthropic":
        return AnthropicProvider(
            api_key=config.api_key or None,
            model=config.model or None,
            api_base=config.api_base or None,
            timeout_seconds=config.timeout_seconds,
            proxy=config.proxy or None,
        )
    raise LLMProviderError(f"未知のLLMプロバイダです: {name}")


def _config_from_env(env: dict[str, str]) -> LLMRuntimeConfig:
    provider = (env.get("LLM_PROVIDER", "noop") or "noop").lower()
    if provider not in ("noop", "mock", "openai_compatible", "anthropic"):
        provider = "noop"
    if provider == "anthropic":
        return LLMRuntimeConfig(
            provider="anthropic",
            api_base=env.get("ANTHROPIC_API_BASE", ""),
            model=env.get("ANTHROPIC_MODEL", "") or env.get("LLM_MODEL", ""),
            api_key=env.get("ANTHROPIC_API_KEY", "") or env.get("LLM_API_KEY", ""),
            proxy=env.get("LLM_PROXY", ""),
        )
    return LLMRuntimeConfig(
        provider=provider,  # type: ignore[arg-type]
        api_base=env.get("LLM_API_BASE", ""),
        model=env.get("LLM_MODEL", ""),
        api_key=env.get("LLM_API_KEY", ""),
        proxy=env.get("LLM_PROXY", ""),
    )


class LLMSettingsStore:
    """LLM 設定の永続化と現行プロバイダの保持。"""

    def __init__(self, path: Path, env: dict[str, str] | None = None) -> None:
        self.path = Path(path)
        self._env = dict(os.environ) if env is None else env
        self.config = self._load()
        self._provider: LLMProvider | None = None

    def _load(self) -> LLMRuntimeConfig:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return LLMRuntimeConfig.model_validate(data)
            except (ValueError, OSError) as exc:
                logger.warning("LLM設定の読み込みに失敗: %s。環境変数から初期化します。", type(exc).__name__)
        return _config_from_env(self._env)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.config.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("LLM設定の保存に失敗: %s", type(exc).__name__)

    def get_provider(self) -> LLMProvider:
        if self._provider is None:
            try:
                self._provider = build_provider(self.config)
            except LLMProviderError as exc:
                # 設定不備でも起動は妨げない。noop へフォールバック。
                logger.warning("LLMプロバイダ構築に失敗、noopで継続: %s", exc)
                self._provider = NoOpLLMProvider()
        return self._provider

    async def update(self, patch: dict) -> LLMRuntimeConfig:
        """設定を部分更新し、プロバイダを作り直す。

        api_key は空文字の場合「変更なし」として既存キーを保持する
        (GUIがキーを再送しなくても切替できるように)。
        """
        current = self.config.model_dump()
        for k, v in patch.items():
            if k == "api_key" and (v is None or v == ""):
                continue  # 空キーは既存を維持
            if k in current and v is not None:
                current[k] = v
        new_config = LLMRuntimeConfig.model_validate(current)
        # 構築可能かを検証(不正なら例外を投げ、既存設定を維持)
        provider = build_provider(new_config)
        await self._close_provider()
        self.config = new_config
        self._provider = provider
        self._save()
        return new_config

    async def _close_provider(self) -> None:
        if self._provider is not None:
            with contextlib.suppress(Exception):
                await self._provider.close()
        self._provider = None

    async def test_connection(self) -> tuple[bool, str]:
        """現行プロバイダで軽い呼び出しを行い、疎通を確認する。"""
        provider = self.get_provider()
        if not provider.available:
            return False, "LLMは無効(provider=noop)です。"
        try:
            result = await provider.classify_question("テスト:東京都の人口は何人か")
        except Exception as exc:  # noqa: BLE001
            return False, f"接続に失敗しました: {type(exc).__name__}"
        if result is None:
            return False, "応答が得られませんでした(接続先・モデルID・APIキーを確認してください)。"
        return True, "接続に成功しました。"
