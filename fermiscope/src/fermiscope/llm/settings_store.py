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
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from fermiscope.llm.anthropic_provider import AnthropicProvider
from fermiscope.llm.base import LLMProvider, LLMProviderError
from fermiscope.llm.mock import MockLLMProvider
from fermiscope.llm.noop import NoOpLLMProvider
from fermiscope.llm.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

ProviderName = Literal["noop", "mock", "openai_compatible", "anthropic"]


def _mask_proxy(proxy: str) -> str:
    """プロキシURLの認証情報(user:pass@)を伏せて返す。

    プロキシURLは `http://user:pass@host:port` の形式で資格情報を含み得るため、
    そのまま API レスポンスへ載せると平文で漏れる。userinfo 部分をマスクする。
    """
    if not proxy:
        return ""
    try:
        parsed = urlsplit(proxy)
    except ValueError:
        return "***"
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        netloc = f"***@{host}" + (f":{parsed.port}" if parsed.port else "")
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return proxy


def _validate_endpoint(url: str, field: str) -> None:
    """api_base / proxy の URL スキームを検査する(http/https のみ許可)。"""
    if not url:
        return
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError as exc:
        raise LLMProviderError(f"{field} を解釈できません: {exc}") from exc
    if scheme not in ("http", "https"):
        raise LLMProviderError(f"{field} は http/https のみ指定できます(指定: {scheme or '(なし)'})")


class LLMRuntimeConfig(BaseModel):
    """GUI から編集できる LLM 接続設定。"""

    provider: ProviderName = "noop"
    api_base: str = ""
    model: str = ""
    api_key: str = ""  # サーバ内でのみ保持。API では返さない。
    proxy: str = ""
    timeout_seconds: float = 60.0

    def public_dict(self) -> dict:
        """APIへ返す安全な表現(キー本体もプロキシ資格情報も含めない)。"""
        return {
            "provider": self.provider,
            "api_base": self.api_base,
            "model": self.model,
            "proxy": _mask_proxy(self.proxy),
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
    # api_key は config の値をそのまま渡す(環境変数解決は設定ロード時に一度だけ)。
    # 空文字は「キー無し」を意味し、プロバイダは環境変数で補完しない。
    if name == "openai_compatible":
        return OpenAICompatProvider(
            api_base=config.api_base or None,
            api_key=config.api_key,
            model=config.model or None,
            timeout_seconds=config.timeout_seconds,
            proxy=config.proxy or None,
        )
    if name == "anthropic":
        return AnthropicProvider(
            api_key=config.api_key,
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
    # 設定に保存するのは明示的な LLM_PROXY のみ。一般の HTTPS_PROXY/HTTP_PROXY は
    # 保存時に焼き込まず、プロバイダ構築時に接続先ごとに解決する(NO_PROXY を尊重し、
    # localhost のローカルLLMを社内プロキシへ流さないため)。
    proxy = env.get("LLM_PROXY", "")
    if provider == "anthropic":
        return LLMRuntimeConfig(
            provider="anthropic",
            api_base=env.get("ANTHROPIC_API_BASE", ""),
            model=env.get("ANTHROPIC_MODEL", "") or env.get("LLM_MODEL", ""),
            api_key=env.get("ANTHROPIC_API_KEY", "") or env.get("LLM_API_KEY", ""),
            proxy=proxy,
        )
    return LLMRuntimeConfig(
        provider=provider,  # type: ignore[arg-type]
        api_base=env.get("LLM_API_BASE", ""),
        model=env.get("LLM_MODEL", ""),
        api_key=env.get("LLM_API_KEY", ""),
        proxy=proxy,
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
            # APIキーを平文で保持するため、所有者のみ読み書き可(0600)で作成する。
            # 既存ファイルにも権限を再適用する。
            data = json.dumps(self.config.model_dump(), ensure_ascii=False, indent=2)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data.encode("utf-8"))
            finally:
                os.close(fd)
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
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
        (GUIがキーを再送しなくてもモデル変更等ができるように)。

        セキュリティ: 接続先(provider / api_base / proxy)が変わったのに新しい
        api_key が与えられない場合は、既存キーを新しい接続先へ流用しない(クリアする)。
        これがないと、攻撃者が api_base だけを自分のサーバへ差し替え、空キーで保存 →
        接続テストで保存済みキーを窃取する、といった横流しが成立してしまう。
        """
        current = self.config.model_dump()
        new_key = patch.get("api_key")
        has_new_key = isinstance(new_key, str) and new_key != ""
        # 明示的なキー削除(GUIの「キーを削除」)。空送信の「変更なし」とは区別する。
        clear_key = bool(patch.get("clear_api_key"))
        # GET応答でマスクした proxy(***@host)をGUIがそのまま送り返す場合は
        # 「変更なし」として扱い、マスク文字列で実プロキシを上書きしない。
        proxy_in = patch.get("proxy")
        proxy_is_mask = isinstance(proxy_in, str) and "***" in proxy_in

        def _is_change(field: str) -> bool:
            if field == "proxy" and proxy_is_mask:
                return False
            v = patch.get(field)
            return v is not None and v != current.get(field)

        endpoint_changed = _is_change("provider") or _is_change("api_base") or _is_change("proxy")

        for k, v in patch.items():
            if k == "api_key" and not has_new_key:
                continue  # 空キーは既存を維持(接続先が同じ場合のみ意味を持つ)
            if k == "proxy" and proxy_is_mask:
                continue  # マスク済みプロキシは変更なし
            if k in current and v is not None:
                current[k] = v

        # 接続先変更で新キーが無ければ、旧キーを新接続先へ流用しない。
        # 明示削除でも空にする。
        if clear_key or (endpoint_changed and not has_new_key):
            current["api_key"] = ""

        # 接続先URL(api_base / proxy)のスキームを検査(http/https のみ)
        _validate_endpoint(current.get("api_base", ""), "api_base")
        _validate_endpoint(current.get("proxy", ""), "proxy")

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
        """現行プロバイダで軽い呼び出しを行い、疎通を確認する。

        失敗時は原因(HTTPステータス・接続エラー種別)と接続経路(接続先・プロキシ
        適用状況)を返す。APIキー・プロキシ資格情報は含めない。
        """
        provider = self.get_provider()
        if not provider.available:
            return False, "LLMは無効(provider=noop)です。プロバイダを選択して保存してください。"

        info: dict = {}
        conn_info = getattr(provider, "connection_info", None)
        if callable(conn_info):
            info = conn_info() or {}
        route_parts = []
        if info.get("api_base"):
            route_parts.append(f"接続先: {info['api_base']}")
        if info.get("proxy"):
            route_parts.append(f"プロキシ: {info['proxy']}")
        if info.get("proxy_note"):
            route_parts.append(info["proxy_note"])
        route = "(" + " / ".join(route_parts) + ")" if route_parts else ""

        try:
            result = await provider.classify_question("テスト:東京都の人口は何人か")
        except Exception as exc:  # noqa: BLE001
            return False, f"接続に失敗しました: {type(exc).__name__} {route}"
        if result is None:
            last = getattr(provider, "last_error", "")
            reason = last or "応答が得られませんでした(接続先・モデルID・APIキーを確認してください)。"
            return False, f"{reason} {route}"
        return True, f"接続に成功しました。{route}"
