"""OpenAI 互換エンドポイントへの薄いラッパー。"""

from __future__ import annotations

import re
import threading

import httpx
from openai import OpenAI

from .config import Settings, get_settings

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """推論モデルの思考過程（<think>…</think>）を応答から除去する。

    チャットテンプレートが開始タグを消費し **閉じタグ `</think>` だけが残る** モデル
    （Qwen3/DeepSeek-R1 系で頻出）にも対応: 閉じタグが残っていれば、最後の閉じタグ
    以降だけを答えとして扱う。
    """
    if not text:
        return text
    out = _THINK_RE.sub("", text)
    low = out.lower()
    for tag in ("</think>", "</thinking>"):
        idx = low.rfind(tag)
        if idx != -1:
            out = out[idx + len(tag):]
            break
    return out.strip()

# configure() で接続情報が変わるたびに作り直す（reset_client で破棄）。
# BookRAG の並列抽出などマルチスレッドから呼ばれるため、初期化はロックで保護する。
_client: OpenAI | None = None
_embed_client: OpenAI | None = None
_lock = threading.Lock()


def build_http_client(s: Settings) -> httpx.Client:
    """設定に応じた httpx クライアントを作る（プロキシ on/off を反映）。

    - use_proxy=False: 環境変数のプロキシも無視して直結（trust_env=False）
    - use_proxy=True かつ proxy_url 指定: その URL を使用
    - use_proxy=True かつ proxy_url 空: 環境変数のプロキシ（HTTP(S)_PROXY）を使用
    """
    # read/write は request_timeout、接続は短め。固まるサーバでの無限待ちを防ぐ。
    timeout = httpx.Timeout(s.request_timeout, connect=min(10.0, s.request_timeout))
    if not s.use_proxy:
        return httpx.Client(trust_env=False, timeout=timeout)
    if s.proxy_url:
        return httpx.Client(proxy=s.proxy_url, timeout=timeout)
    return httpx.Client(trust_env=True, timeout=timeout)


def get_client() -> OpenAI:
    """設定済みの OpenAI 互換クライアント（チャット/補完用）を返す。"""
    global _client
    if _client is None:
        with _lock:
            if _client is None:  # double-checked（並列 build_graph からの同時初期化対策）
                s = get_settings()
                # timeout/max_retries を明示（SDK 既定は 600s × リトライで“ハング”に見えるため）。
                _client = OpenAI(
                    base_url=s.base_url, api_key=s.api_key, http_client=build_http_client(s),
                    timeout=s.request_timeout, max_retries=2,
                )
    return _client


def get_embed_client() -> OpenAI:
    """埋め込み用クライアントを返す。embed_base_url 指定時はそちらへ向ける。"""
    global _embed_client
    if _embed_client is None:
        with _lock:
            if _embed_client is None:
                s = get_settings()
                base = s.embed_base_url or s.base_url
                key = s.embed_api_key or s.api_key
                _embed_client = OpenAI(
                    base_url=base, api_key=key, http_client=build_http_client(s),
                    timeout=s.request_timeout, max_retries=2,
                )
    return _embed_client


def reset_client() -> None:
    """キャッシュ済みクライアントを破棄する（configure() から呼ばれる）。"""
    global _client, _embed_client
    with _lock:
        _client = None
        _embed_client = None


def complete(prompt: str, *, system: str | None = None, **kwargs) -> str:
    """単発のプロンプトに対する応答文字列を返す簡易ヘルパー（思考過程は除去）。"""
    s = get_settings()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = get_client().chat.completions.create(model=s.model, messages=messages, **kwargs)
    raw = (resp.choices[0].message.content or "") if resp.choices else ""
    return strip_think(raw)
