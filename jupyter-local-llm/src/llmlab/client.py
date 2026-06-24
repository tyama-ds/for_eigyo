"""OpenAI 互換エンドポイントへの薄いラッパー。"""

from __future__ import annotations

import httpx
from openai import OpenAI

from .config import Settings, get_settings

# configure() で接続情報が変わるたびに作り直す（reset_client で破棄）。
_client: OpenAI | None = None
_embed_client: OpenAI | None = None


def build_http_client(s: Settings) -> httpx.Client:
    """設定に応じた httpx クライアントを作る（プロキシ on/off を反映）。

    - use_proxy=False: 環境変数のプロキシも無視して直結（trust_env=False）
    - use_proxy=True かつ proxy_url 指定: その URL を使用
    - use_proxy=True かつ proxy_url 空: 環境変数のプロキシ（HTTP(S)_PROXY）を使用
    """
    if not s.use_proxy:
        return httpx.Client(trust_env=False)
    if s.proxy_url:
        return httpx.Client(proxy=s.proxy_url)
    return httpx.Client(trust_env=True)


def get_client() -> OpenAI:
    """設定済みの OpenAI 互換クライアント（チャット/補完用）を返す。"""
    global _client
    if _client is None:
        s = get_settings()
        _client = OpenAI(base_url=s.base_url, api_key=s.api_key, http_client=build_http_client(s))
    return _client


def get_embed_client() -> OpenAI:
    """埋め込み用クライアントを返す。embed_base_url 指定時はそちらへ向ける。"""
    global _embed_client
    if _embed_client is None:
        s = get_settings()
        base = s.embed_base_url or s.base_url
        key = s.embed_api_key or s.api_key
        _embed_client = OpenAI(base_url=base, api_key=key, http_client=build_http_client(s))
    return _embed_client


def reset_client() -> None:
    """キャッシュ済みクライアントを破棄する（configure() から呼ばれる）。"""
    global _client, _embed_client
    _client = None
    _embed_client = None


def complete(prompt: str, *, system: str | None = None, **kwargs) -> str:
    """単発のプロンプトに対する応答文字列を返す簡易ヘルパー。"""
    s = get_settings()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = get_client().chat.completions.create(model=s.model, messages=messages, **kwargs)
    return resp.choices[0].message.content or ""
