"""LLMクライアント — OpenAI互換API / Anthropic Messages API。

SDKではなくhttpx直叩きにすることで、proxy policy・SSRF allowlist・redactionを
一元適用する。provider間の自動fallbackは行わない (spec要件)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.security.http_client import build_client
from app.security.proxy import EffectiveProxyPolicy
from app.security.redaction import redact, redactor


class LlmError(RuntimeError):
    pass


@dataclass
class LlmUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class LlmResponse:
    text: str
    usage: LlmUsage
    model: str
    raw: dict[str, Any]


@dataclass
class ResolvedLlmProfile:
    """secret解決済みのLLM profile。平文keyはこのオブジェクト内のみで保持し、
    ログ・イベント・DBへ書かない。"""

    profile_id: str
    name: str
    provider: str  # local | openai | anthropic
    api: str  # openai-compatible | anthropic
    endpoint: str
    model: str
    api_key: str | None
    timeout_seconds: int = 120
    max_concurrency: int = 2

    def __post_init__(self) -> None:
        redactor.register(self.api_key)


def _client_for(
    profile: ResolvedLlmProfile,
    policy: EffectiveProxyPolicy | None,
    allowlist: set[tuple[str, int]] | None,
) -> httpx.Client:
    # local profileはadmin allowlist経由でprivate endpoint許可。
    # openai/anthropicはpublic endpointなのでuntrusted相当の検証でも通るが、
    # 管理者設定由来なのでorigin=adminで扱う。
    return build_client(
        origin="admin",
        policy=policy,
        allowlist=allowlist,
        timeout=float(profile.timeout_seconds),
    )


def chat_completion(
    profile: ResolvedLlmProfile,
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    policy: EffectiveProxyPolicy | None = None,
    allowlist: set[tuple[str, int]] | None = None,
) -> LlmResponse:
    """1回のchat補完。providerに応じてAPI形式を切り替える。fallbackしない。"""
    if profile.api == "anthropic":
        return _anthropic_messages(
            profile, messages, max_tokens=max_tokens, temperature=temperature,
            policy=policy, allowlist=allowlist,
        )
    return _openai_chat(
        profile, messages, max_tokens=max_tokens, temperature=temperature,
        policy=policy, allowlist=allowlist,
    )


def _openai_chat(
    profile: ResolvedLlmProfile,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
    policy: EffectiveProxyPolicy | None,
    allowlist: set[tuple[str, int]] | None,
) -> LlmResponse:
    url = profile.endpoint.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if profile.api_key:
        headers["Authorization"] = f"Bearer {profile.api_key}"
    body = {
        "model": profile.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    with _client_for(profile, policy, allowlist) as client:
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise LlmError(redact(f"LLM接続エラー ({profile.name}): {e}")) from e
    if resp.status_code >= 400:
        raise LlmError(
            redact(f"LLMエラー ({profile.name}, HTTP {resp.status_code}): {resp.text[:500]}")
        )
    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as e:
        raise LlmError(f"LLM応答の形式が不正です ({profile.name})") from e
    usage_raw = data.get("usage") or {}
    usage = LlmUsage(
        prompt_tokens=usage_raw.get("prompt_tokens"),
        completion_tokens=usage_raw.get("completion_tokens"),
        total_tokens=usage_raw.get("total_tokens"),
    )
    return LlmResponse(text=text, usage=usage, model=data.get("model", profile.model), raw=data)


def _anthropic_messages(
    profile: ResolvedLlmProfile,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
    policy: EffectiveProxyPolicy | None,
    allowlist: set[tuple[str, int]] | None,
) -> LlmResponse:
    url = profile.endpoint.rstrip("/") + "/v1/messages"
    if not profile.api_key:
        raise LlmError(f"Anthropic profile {profile.name} にAPI keyが設定されていません")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": profile.api_key,
        "anthropic-version": "2023-06-01",
    }
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    chat_messages = [m for m in messages if m["role"] != "system"]
    body: dict[str, Any] = {
        "model": profile.model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": chat_messages,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    with _client_for(profile, policy, allowlist) as client:
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise LlmError(redact(f"LLM接続エラー ({profile.name}): {e}")) from e
    if resp.status_code >= 400:
        raise LlmError(
            redact(f"LLMエラー ({profile.name}, HTTP {resp.status_code}): {resp.text[:500]}")
        )
    data = resp.json()
    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    usage_raw = data.get("usage") or {}
    input_tokens = usage_raw.get("input_tokens")
    output_tokens = usage_raw.get("output_tokens")
    total = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    usage = LlmUsage(
        prompt_tokens=input_tokens, completion_tokens=output_tokens, total_tokens=total
    )
    return LlmResponse(text=text, usage=usage, model=data.get("model", profile.model), raw=data)


def test_connection(
    profile: ResolvedLlmProfile,
    *,
    policy: EffectiveProxyPolicy | None = None,
    allowlist: set[tuple[str, int]] | None = None,
) -> dict[str, Any]:
    """接続試験: 到達性 → 認証 → model有無 → 最小生成。

    実課金が発生し得るため、最小request (max_tokens=8) のみ送る。
    """
    result: dict[str, Any] = {
        "reachable": False,
        "authenticated": False,
        "model_available": None,
        "generation_ok": False,
        "error": None,
    }
    try:
        if profile.api == "openai-compatible":
            url = profile.endpoint.rstrip("/") + "/models"
            headers = {}
            if profile.api_key:
                headers["Authorization"] = f"Bearer {profile.api_key}"
            with _client_for(profile, policy, allowlist) as client:
                resp = client.get(url, headers=headers)
            result["reachable"] = True
            if resp.status_code in (401, 403):
                result["error"] = "認証に失敗しました"
                return result
            result["authenticated"] = True
            if resp.status_code == 200:
                try:
                    ids = [m.get("id") for m in resp.json().get("data", [])]
                    result["model_available"] = profile.model in ids if ids else None
                except Exception:
                    result["model_available"] = None
        else:
            result["reachable"] = True  # Anthropicは/messagesで確認
        llm_resp = chat_completion(
            profile,
            [{"role": "user", "content": "reply with: ok"}],
            max_tokens=8,
            temperature=0.0,
            policy=policy,
            allowlist=allowlist,
        )
        result["reachable"] = True
        result["authenticated"] = True
        result["generation_ok"] = bool(llm_resp.text.strip())
        if result["model_available"] is None:
            result["model_available"] = True
    except LlmError as e:
        result["error"] = redact(str(e))
    except httpx.HTTPError as e:
        result["error"] = redact(f"接続エラー: {e}")
    except Exception as e:  # SSRF block等
        result["error"] = redact(str(e))
    return result
