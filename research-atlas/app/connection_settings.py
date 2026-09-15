"""Browser-owned connection preferences, held only in a request/task context.

There is deliberately no environment fallback and no persistence in this module.
Only fixed application endpoints or explicitly configured LLM URLs use the transport.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import ipaddress
import json
import re
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


HEADER_NAME = "X-Atlas-Connection"
MAX_HEADER_LENGTH = 16384
_INVALID = "ブラウザの接続設定が不正です。設定画面で入力内容を確認して保存し直してください。"


class _SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


def _model(value: str) -> str:
    value = value.strip()
    if value and not re.fullmatch(r"[\w][\w.:/\-]{0,159}", value, re.ASCII):
        raise ValueError("LLMモデル名の形式を確認してください。")
    return value


def _secret(value: SecretStr) -> SecretStr:
    raw = value.get_secret_value()
    if len(raw) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("認証情報の形式を確認してください。")
    return value


def _valid_hostname(host: str) -> bool:
    """Accept IP literals or DNS/IDNA names without performing DNS lookups."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        if ":" in host or re.fullmatch(r"[\d.]+", host, re.ASCII):
            return False
    try:
        name = host.removesuffix(".").encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return len(name) <= 253 and all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?", label, re.IGNORECASE | re.ASCII)
        for label in name.split("."))


def _url(value: str, *, allow_path: bool) -> str:
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        valid = bool(host) and parsed.scheme in {"http", "https"} and parsed.port != 0
        valid = valid and not (parsed.username is not None or parsed.password is not None or "?" in value or "#" in value)
        valid = valid and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value)
        # urlsplit alone accepts malformed authorities such as '[::1]suffix'
        # and 'server:'. Require an explicit port to contain valid digits.
        authority = r"\[[^\]]+\](?::[0-9]+)?" if parsed.netloc.startswith("[") else r"[^:]+(?::[0-9]+)?"
        valid = valid and bool(re.fullmatch(authority, parsed.netloc)) and _valid_hostname(host)
        if not allow_path:
            valid = valid and parsed.path in {"", "/"}
    except (ValueError, TypeError):
        valid = False
    if not valid:
        message = "LLMサーバーの有効なHTTP(S) URLを指定してください。IPアドレス・ホスト名を使用でき、URL内の認証情報・クエリ・フラグメントは使用できません。" if allow_path else "Proxyは認証情報・パス・クエリを含まないHTTP(S) URLを指定してください。"
        raise ValueError(message)
    return value


class OpenAISettings(_SettingsModel):
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    model: str = Field(default="", max_length=160)
    _valid_model = field_validator("model")(_model)
    _valid_secret = field_validator("api_key")(_secret)


class LocalSettings(_SettingsModel):
    backend: Literal["ollama", "openai_compatible"] = "ollama"
    url: str = Field(default="http://127.0.0.1:11434", max_length=2048)
    model: str = Field(default="", max_length=160)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    _valid_model = field_validator("model")(_model)
    _valid_secret = field_validator("api_key")(_secret)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _url(value, allow_path=True)


def _no_proxy_rule(value: str) -> tuple[object, int | None]:
    """Return a host/suffix, IP network, or wildcard plus optional port."""
    value = value.strip().lower()
    if value == "*":
        return "*", None
    try:
        return ipaddress.ip_network(value, strict=False), None
    except ValueError:
        pass
    port = None
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::(\d{1,5}))?", value)
        if not match:
            raise ValueError("Proxy除外先の形式を確認してください。")
        host, raw_port = match.groups()
        host = str(ipaddress.ip_address(host))
        port = int(raw_port) if raw_port else None
    else:
        if ":" in value:
            value, raw_port = value.rsplit(":", 1)
            if not raw_port.isascii() or not raw_port.isdigit():
                raise ValueError("Proxy除外先の形式を確認してください。")
            port = int(raw_port)
        host = value.removeprefix("*.").lstrip(".").rstrip(".")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.\-]{0,251}[a-z0-9])?", host) or ".." in host:
            raise ValueError("Proxy除外先の形式を確認してください。")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Proxy除外先のポートを確認してください。")
    return host, port


class ProxySettings(_SettingsModel):
    enabled: bool = False
    url: str = Field(default="", max_length=2048)
    username: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    password: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    no_proxy: str = Field(default="localhost,127.0.0.1,::1", max_length=2048)
    _valid_secrets = field_validator("username", "password")(_secret)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _url(value, allow_path=False) if value.strip() else ""

    @field_validator("no_proxy")
    @classmethod
    def valid_no_proxy(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        for part in parts:
            _no_proxy_rule(part)
        return ",".join(parts)

    @model_validator(mode="after")
    def valid_enabled(self):
        if self.enabled and not self.url:
            raise ValueError("Proxyを有効にする場合はURLを入力してください。")
        if self.password.get_secret_value() and not self.username.get_secret_value():
            raise ValueError("Proxyパスワードを指定する場合はユーザー名も入力してください。")
        return self


class ConnectionSettings(_SettingsModel):
    version: Literal[1] = 1
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    local: LocalSettings = Field(default_factory=LocalSettings)
    proxy: ProxySettings = Field(default_factory=ProxySettings)

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("接続設定のバージョンを確認してください。")
        return value


_CURRENT: ContextVar[ConnectionSettings] = ContextVar("atlas_connection_settings", default=ConnectionSettings())


def current_settings() -> ConnectionSettings:
    return _CURRENT.get()


@contextmanager
def settings_context(settings: ConnectionSettings):
    if not isinstance(settings, ConnectionSettings):
        raise ValueError(_INVALID)
    token = _CURRENT.set(settings)
    try:
        yield settings
    finally:
        _CURRENT.reset(token)


def parse_header(value: str | None) -> ConnectionSettings:
    if value is None:
        return ConnectionSettings()
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_HEADER_LENGTH:
            raise ValueError()
        raw = base64.b64decode(value, validate=True).decode("utf-8")
        data = json.loads(raw)
        return ConnectionSettings.model_validate(data)
    except Exception:
        # Never return pydantic input values or a malformed credential/header.
        raise ValueError(_INVALID) from None


def bypass_proxy(target_url: str, no_proxy: str | None = None) -> bool:
    parsed = urlsplit(target_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    rules = current_settings().proxy.no_proxy if no_proxy is None else no_proxy
    for raw in rules.split(","):
        if not raw.strip():
            continue
        rule, rule_port = _no_proxy_rule(raw)
        if rule_port is not None and port != rule_port:
            continue
        if rule == "*":
            return True
        if isinstance(rule, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            try:
                if ipaddress.ip_address(host) in rule:
                    return True
            except ValueError:
                pass
        elif host == rule or host.endswith("." + rule):
            return True
    return False


def proxy_for_url(target_url: str) -> httpx.Proxy | None:
    proxy = current_settings().proxy
    if not proxy.enabled or bypass_proxy(target_url, proxy.no_proxy):
        return None
    username, password = proxy.username.get_secret_value(), proxy.password.get_secret_value()
    return httpx.Proxy(proxy.url, auth=(username, password) if username else None)


def http_client(target_url: str, *, local: bool = False, **kwargs) -> httpx.Client:
    """Use explicit browser routing; OS proxy and certificate env vars are ignored."""
    kwargs.update(trust_env=False, follow_redirects=False, proxy=None if local else proxy_for_url(target_url))
    return httpx.Client(**kwargs)


def local_headers() -> dict[str, str]:
    key = current_settings().local.api_key.get_secret_value().strip()
    return {"Authorization": "Bearer " + key} if key else {}


@contextmanager
def openai_client(*, timeout: float, max_retries: int = 0):
    """Construct the SDK with request settings, clearing its extra env defaults.

The SDK merges OPENAI_CUSTOM_HEADERS after its auth headers. Reset that internal
map before any request so old .env values cannot override browser credentials.
No process environment is changed, which preserves concurrent request isolation.
"""
    from openai import OpenAI
    with http_client("https://api.openai.com/v1", timeout=timeout) as transport, OpenAI(
            api_key=current_settings().openai.api_key.get_secret_value().strip(),
            base_url="https://api.openai.com/v1", organization="", project="",
            http_client=transport, timeout=timeout, max_retries=max_retries) as client:
        client.organization = None
        client.project = None
        client.admin_api_key = None
        client.webhook_secret = None
        client._custom_headers = {}
        yield client


def connection_status() -> dict:
    value = current_settings()
    return {"storage": "browser", "openai": {"configured": bool(value.openai.api_key.get_secret_value().strip() and value.openai.model), "model": value.openai.model or None},
            "local": {"backend": value.local.backend, "model": value.local.model or None, "authenticated": bool(value.local.api_key.get_secret_value())},
            "proxy": {"enabled": value.proxy.enabled, "configured": bool(value.proxy.url)}}


def test_connection(target: str) -> dict:
    """Read-only checks, no papers or generation requests and no secret echo."""
    value = current_settings()
    if target == "local":
        from .field_llm import local_status
        status = local_status()
        ok = status["available"] and (not value.local.model or bool(status["default_model"]))
        return {"ok": ok, "target": target,
                "message": "ローカルLLMのモデル一覧を取得できました。" if ok else status["error"]}
    if target == "openai":
        if not value.openai.api_key.get_secret_value().strip() or not value.openai.model:
            return {"ok": False, "target": target, "message": "ブラウザの接続設定でOpenAI APIキーとモデル名を入力してください。"}
        url = "https://api.openai.com/v1/models/" + quote(value.openai.model, safe="")
        headers = {"Authorization": "Bearer " + value.openai.api_key.get_secret_value().strip()}
    elif target == "proxy":
        if not value.proxy.enabled:
            return {"ok": False, "target": target, "message": "Proxyは無効です。設定を有効にして保存してください。"}
        url, headers = "https://api.crossref.org/works?rows=0", {"User-Agent": "ResearchAtlas/1.0"}
    else:
        raise ValueError("接続確認の対象が不正です。")
    try:
        with http_client(url, timeout=httpx.Timeout(12, connect=5), headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
        message = "OpenAIの認証と指定モデルの情報取得を確認しました。生成処理の対応状況は別途確認が必要です。" if target == "openai" else ("Proxy除外ルールに従った直接接続でCrossrefに到達できました。" if bypass_proxy(url) else "設定されたProxy経由でCrossrefに到達できました。")
        return {"ok": True, "target": target, "message": message}
    except Exception:
        return {"ok": False, "target": target, "message": "接続を確認できませんでした。認証情報・モデル名・Proxy・ネットワーク設定を確認してください。"}
