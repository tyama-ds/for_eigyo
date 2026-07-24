"""LLM profile / proxy policy / allowlist の解決サービス。"""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import LlmEndpointAllowlist, LlmProfile, ProxyConfig, RoleAssignment
from app.llm.client import ResolvedLlmProfile
from app.security.proxy import EffectiveProxyPolicy, policy_from_environment
from app.security.secrets import SecretStore

ROLES = ("research", "summarization", "normalization", "synthesis")


class ProfileNotConfiguredError(RuntimeError):
    pass


def load_allowlist(session: Session) -> set[tuple[str, int]]:
    rows = session.scalars(select(LlmEndpointAllowlist)).all()
    return {(r.host.lower(), r.port) for r in rows}


def endpoint_host_port(endpoint: str) -> tuple[str, int]:
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def resolve_profile(
    session: Session, settings: Settings, profile_id: str
) -> ResolvedLlmProfile:
    profile = session.get(LlmProfile, profile_id)
    if profile is None or not profile.enabled:
        raise ProfileNotConfiguredError(f"LLM profile {profile_id} が存在しないか無効です")
    api_key: str | None = None
    if profile.api_key_secret_id:
        api_key = SecretStore(session, settings).reveal(profile.api_key_secret_id)
    endpoint = profile.endpoint
    if not endpoint:
        if profile.provider == "openai":
            endpoint = settings.openai_base_url
        elif profile.provider == "anthropic":
            endpoint = settings.anthropic_base_url
        else:
            raise ProfileNotConfiguredError(
                f"LLM profile {profile.name} にendpointが設定されていません"
            )
    api = "anthropic" if profile.provider == "anthropic" else "openai-compatible"
    return ResolvedLlmProfile(
        profile_id=profile.id,
        name=profile.name,
        provider=profile.provider,
        api=api,
        endpoint=endpoint,
        model=profile.model,
        api_key=api_key,
        timeout_seconds=profile.timeout_seconds,
        max_concurrency=profile.max_concurrency,
    )


def resolve_role_profile(
    session: Session, settings: Settings, role: str
) -> ResolvedLlmProfile:
    """roleに割り当てられたprofileを解決する。未割り当てなら明示エラー (silent fallback禁止)。"""
    assignment = session.get(RoleAssignment, role)
    if assignment is None:
        raise ProfileNotConfiguredError(
            f"role '{role}' にLLM profileが割り当てられていません。"
            "Settings画面でprofileを作成し、roleへ割り当ててください。"
        )
    return resolve_profile(session, settings, assignment.profile_id)


def effective_proxy_policy(
    session: Session, settings: Settings, engine_id: str | None = None
) -> EffectiveProxyPolicy:
    """engine別override > global explicit > environment inherit > off。"""
    scopes = []
    if engine_id:
        scopes.append(engine_id)
    scopes.append("global")
    store: SecretStore | None = None

    for scope in scopes:
        cfg = session.get(ProxyConfig, scope)
        if cfg is None or cfg.mode == "off":
            if cfg is not None and cfg.mode == "off" and scope == (engine_id or "global"):
                # 明示的off (engine override) は上位を見ずに確定
                return EffectiveProxyPolicy(mode="off", source_scope=f"engine:{scope}" if engine_id and scope == engine_id else "off")
            continue
        if cfg.mode == "inherit":
            policy = policy_from_environment(cfg.ca_bundle_path or settings.proxy_ca_bundle)
            policy.no_proxy = list({*policy.no_proxy, *[str(x) for x in cfg.no_proxy]})
            policy.source_scope = f"engine:{scope}" if scope == engine_id else "global"
            return policy
        if cfg.mode == "explicit":
            if store is None:
                store = SecretStore(session, settings)

            def _reveal(secret_id: str | None) -> str | None:
                return store.reveal(secret_id) if secret_id else None

            return EffectiveProxyPolicy(
                mode="explicit",
                http_proxy=_reveal(cfg.http_proxy_secret_id),
                https_proxy=_reveal(cfg.https_proxy_secret_id),
                all_proxy=_reveal(cfg.all_proxy_secret_id),
                no_proxy=[str(x) for x in cfg.no_proxy],
                ca_bundle_path=cfg.ca_bundle_path,
                source_scope=f"engine:{scope}" if scope == engine_id else "global",
            )

    # DB設定なし: settings.proxy_mode (環境変数ブートストラップ) を参照
    if settings.proxy_mode == "inherit":
        return policy_from_environment(settings.proxy_ca_bundle)
    return EffectiveProxyPolicy(mode="off", source_scope="off")
