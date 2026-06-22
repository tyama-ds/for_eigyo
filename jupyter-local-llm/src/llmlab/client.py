"""OpenAI 互換エンドポイントへの薄いラッパー。"""

from __future__ import annotations

from openai import OpenAI

from .config import get_settings

# configure() で接続情報が変わるたびに作り直す（reset_client で破棄）。
_client: OpenAI | None = None


def get_client() -> OpenAI:
    """設定済みの OpenAI 互換クライアントを返す。"""
    global _client
    if _client is None:
        s = get_settings()
        _client = OpenAI(base_url=s.base_url, api_key=s.api_key)
    return _client


def reset_client() -> None:
    """キャッシュ済みクライアントを破棄する（configure() から呼ばれる）。"""
    global _client
    _client = None


def complete(prompt: str, *, system: str | None = None, **kwargs) -> str:
    """単発のプロンプトに対する応答文字列を返す簡易ヘルパー。"""
    s = get_settings()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = get_client().chat.completions.create(model=s.model, messages=messages, **kwargs)
    return resp.choices[0].message.content or ""
