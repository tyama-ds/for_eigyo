"""Settings API — LLM profile / role割り当て / proxy / allowlist / 接続試験。

secretの平文は書き込み専用。応答にはmasked placeholderのみを返す。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import db_session, settings_dep
from app.api.schemas import (
    LlmProfileIn,
    LlmProfileView,
    ProxyConfigIn,
    ProxyConfigView,
)
from app.config import Settings
from app.db.models import (
    AuditLog,
    LlmEndpointAllowlist,
    LlmProfile,
    ProxyConfig,
    RoleAssignment,
)
from app.llm.client import test_connection
from app.llm.profiles import (
    ROLES,
    effective_proxy_policy,
    endpoint_host_port,
    load_allowlist,
    resolve_profile,
)
from app.security.secrets import SecretStore

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _mask(has_key: bool) -> str | None:
    return "••••••••" if has_key else None


def _profile_view(p: LlmProfile) -> LlmProfileView:
    return LlmProfileView(
        id=p.id,
        name=p.name,
        provider=p.provider,
        api=p.api,
        endpoint=p.endpoint,
        model=p.model,
        has_api_key=p.api_key_secret_id is not None,
        api_key_masked=_mask(p.api_key_secret_id is not None),
        timeout_seconds=p.timeout_seconds,
        max_concurrency=p.max_concurrency,
        enabled=p.enabled,
    )


def _validate_profile_endpoint(
    session: Session, provider: str, endpoint: str | None
) -> None:
    """Local LLM endpointの検証。privateはallowlist登録と同時に許可される。"""
    if endpoint is None:
        if provider == "local":
            raise HTTPException(status_code=400, detail="local providerにはendpointが必須です")
        return
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="endpointはhttp(s) URLで指定してください")


@router.get("/llm-profiles", response_model=list[LlmProfileView])
def list_profiles(session: Session = Depends(db_session)) -> list[LlmProfileView]:
    return [_profile_view(p) for p in session.scalars(select(LlmProfile))]


@router.post("/llm-profiles", response_model=LlmProfileView, status_code=201)
def create_profile(
    body: LlmProfileIn,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> LlmProfileView:
    _validate_profile_endpoint(session, body.provider, body.endpoint)
    if session.scalar(select(LlmProfile).where(LlmProfile.name == body.name)):
        raise HTTPException(status_code=409, detail=f"profile名が重複しています: {body.name}")
    api = "anthropic" if body.provider == "anthropic" else body.api
    profile = LlmProfile(
        name=body.name,
        provider=body.provider,
        api=api,
        endpoint=body.endpoint,
        model=body.model,
        timeout_seconds=body.timeout_seconds,
        max_concurrency=body.max_concurrency,
        enabled=body.enabled,
    )
    if body.api_key:
        store = SecretStore(session, settings)
        profile.api_key_secret_id = store.put(f"llm-profile:{body.name}", body.api_key)
    session.add(profile)
    session.flush()
    # 管理者登録endpointをallowlistへ登録 (private Local LLMの許可経路)
    if body.endpoint:
        host, port = endpoint_host_port(body.endpoint)
        exists = session.scalar(
            select(LlmEndpointAllowlist).where(
                LlmEndpointAllowlist.host == host, LlmEndpointAllowlist.port == port
            )
        )
        if not exists:
            session.add(
                LlmEndpointAllowlist(host=host, port=port, note=f"llm-profile:{profile.name}")
            )
    session.add(AuditLog(action="settings.llm_profile.create", target=profile.id,
                         detail={"name": body.name, "provider": body.provider}))
    return _profile_view(profile)


@router.put("/llm-profiles/{profile_id}", response_model=LlmProfileView)
def update_profile(
    profile_id: str,
    body: LlmProfileIn,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> LlmProfileView:
    profile = session.get(LlmProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    _validate_profile_endpoint(session, body.provider, body.endpoint)
    profile.name = body.name
    profile.provider = body.provider
    profile.api = "anthropic" if body.provider == "anthropic" else body.api
    profile.endpoint = body.endpoint
    profile.model = body.model
    profile.timeout_seconds = body.timeout_seconds
    profile.max_concurrency = body.max_concurrency
    profile.enabled = body.enabled
    if body.api_key:  # 空なら既存keyを維持 (再表示不可のため)
        store = SecretStore(session, settings)
        profile.api_key_secret_id = store.put(f"llm-profile:{profile.id}", body.api_key)
    if body.endpoint:
        host, port = endpoint_host_port(body.endpoint)
        exists = session.scalar(
            select(LlmEndpointAllowlist).where(
                LlmEndpointAllowlist.host == host, LlmEndpointAllowlist.port == port
            )
        )
        if not exists:
            session.add(
                LlmEndpointAllowlist(host=host, port=port, note=f"llm-profile:{profile.name}")
            )
    session.add(AuditLog(action="settings.llm_profile.update", target=profile.id))
    return _profile_view(profile)


@router.delete("/llm-profiles/{profile_id}", status_code=204, response_model=None)
def delete_profile(
    profile_id: str,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> None:
    profile = session.get(LlmProfile, profile_id)
    if profile is None:
        return
    for assignment in session.scalars(
        select(RoleAssignment).where(RoleAssignment.profile_id == profile_id)
    ):
        session.delete(assignment)
    if profile.api_key_secret_id:
        SecretStore(session, settings).delete(profile.api_key_secret_id)
    session.delete(profile)
    session.add(AuditLog(action="settings.llm_profile.delete", target=profile_id))


@router.post("/llm-profiles/{profile_id}/test")
def test_profile(
    profile_id: str,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, Any]:
    """接続試験: 到達性・認証・model有無・最小生成。

    注意: 有料provider (openai/anthropic) では最小request分の実課金が発生し得る。
    UIは実行前にその旨を表示する。
    """
    try:
        profile = resolve_profile(session, settings, profile_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    policy = effective_proxy_policy(session, settings)
    allowlist = load_allowlist(session)
    result = test_connection(profile, policy=policy, allowlist=allowlist)
    result["billing_note"] = (
        "有料providerでは接続試験でも最小限の課金が発生し得ます"
        if profile.provider in ("openai", "anthropic")
        else None
    )
    session.add(AuditLog(action="settings.llm_profile.test", target=profile_id,
                         detail={"ok": result.get("generation_ok")}))
    return result


# ---------- role割り当て ----------


class RoleAssignmentIn(BaseModel):
    assignments: dict[str, str | None]  # role -> profile_id (Noneで解除)


@router.get("/roles")
def get_roles(session: Session = Depends(db_session)) -> dict[str, str | None]:
    current = {r.role: r.profile_id for r in session.scalars(select(RoleAssignment))}
    return {role: current.get(role) for role in ROLES}


@router.put("/roles")
def put_roles(
    body: RoleAssignmentIn, session: Session = Depends(db_session)
) -> dict[str, str | None]:
    for role, profile_id in body.assignments.items():
        if role not in ROLES:
            raise HTTPException(status_code=400, detail=f"未知のroleです: {role}")
        existing = session.get(RoleAssignment, role)
        if profile_id is None:
            if existing is not None:
                session.delete(existing)
            continue
        if session.get(LlmProfile, profile_id) is None:
            raise HTTPException(status_code=400, detail=f"profileが存在しません: {profile_id}")
        if existing is None:
            session.add(RoleAssignment(role=role, profile_id=profile_id))
        else:
            existing.profile_id = profile_id
    session.add(AuditLog(action="settings.roles.update", detail=dict(body.assignments)))
    session.flush()
    return get_roles(session)


# ---------- proxy ----------


@router.get("/proxy", response_model=list[ProxyConfigView])
def get_proxy(session: Session = Depends(db_session)) -> list[ProxyConfigView]:
    rows = list(session.scalars(select(ProxyConfig)))
    return [
        ProxyConfigView(
            scope=r.scope,
            mode=r.mode,
            has_http_proxy=r.http_proxy_secret_id is not None,
            has_https_proxy=r.https_proxy_secret_id is not None,
            has_all_proxy=r.all_proxy_secret_id is not None,
            no_proxy=[str(x) for x in (r.no_proxy or [])],
            ca_bundle_path=r.ca_bundle_path,
        )
        for r in rows
    ]


@router.put("/proxy", response_model=ProxyConfigView)
def put_proxy(
    body: ProxyConfigIn,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> ProxyConfigView:
    cfg = session.get(ProxyConfig, body.scope)
    if cfg is None:
        cfg = ProxyConfig(scope=body.scope)
        session.add(cfg)
    cfg.mode = body.mode
    cfg.no_proxy = body.no_proxy
    cfg.ca_bundle_path = body.ca_bundle_path
    store = SecretStore(session, settings)
    if body.http_proxy is not None:
        cfg.http_proxy_secret_id = (
            store.put(f"proxy:{body.scope}:http", body.http_proxy) if body.http_proxy else None
        )
    if body.https_proxy is not None:
        cfg.https_proxy_secret_id = (
            store.put(f"proxy:{body.scope}:https", body.https_proxy) if body.https_proxy else None
        )
    if body.all_proxy is not None:
        cfg.all_proxy_secret_id = (
            store.put(f"proxy:{body.scope}:all", body.all_proxy) if body.all_proxy else None
        )
    session.add(AuditLog(action="settings.proxy.update", target=body.scope,
                         detail={"mode": body.mode}))
    session.flush()
    return ProxyConfigView(
        scope=cfg.scope,
        mode=cfg.mode,
        has_http_proxy=cfg.http_proxy_secret_id is not None,
        has_https_proxy=cfg.https_proxy_secret_id is not None,
        has_all_proxy=cfg.all_proxy_secret_id is not None,
        no_proxy=[str(x) for x in (cfg.no_proxy or [])],
        ca_bundle_path=cfg.ca_bundle_path,
    )


class ProxyTestIn(BaseModel):
    scope: str = "global"
    external_url: str = Field(default="https://example.com/")
    internal_url: str | None = None


@router.post("/proxy/test")
def test_proxy(
    body: ProxyTestIn,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, Any]:
    """Test proxy: 外部URLがproxy経由になり、internal/Local LLMがbypassされることを確認。"""
    policy = effective_proxy_policy(session, settings, engine_id=None)
    result: dict[str, Any] = {
        "mode": policy.mode,
        "source_scope": policy.source_scope,
        "external": None,
        "internal_bypassed": None,
        "error": None,
    }
    # 経路判定はDNS不要。実フェッチ時はbuild_clientのSSRFガードが毎request検証する。
    ext_proxy = policy.proxy_for_url(body.external_url)
    result["external"] = {
        "url": body.external_url,
        "via_proxy": ext_proxy is not None,
    }
    if body.internal_url:
        int_proxy = policy.proxy_for_url(body.internal_url)
        result["internal_bypassed"] = int_proxy is None
    if policy.mode != "off":
        from app.security.http_client import build_client

        try:
            with build_client(origin="untrusted", policy=policy, timeout=10.0) as client:
                resp = client.get(body.external_url)
                result["external"]["status_code"] = resp.status_code
        except Exception as e:  # noqa: BLE001
            from app.security.redaction import redact

            result["error"] = redact(str(e))
    return result


# ---------- allowlist (読み取りのみ公開。書き込みはprofile作成経由) ----------


@router.get("/llm-endpoint-allowlist")
def get_allowlist(session: Session = Depends(db_session)) -> list[dict[str, Any]]:
    return [
        {"id": r.id, "host": r.host, "port": r.port, "note": r.note}
        for r in session.scalars(select(LlmEndpointAllowlist))
    ]


@router.delete("/llm-endpoint-allowlist/{entry_id}", status_code=204, response_model=None)
def delete_allowlist_entry(entry_id: str, session: Session = Depends(db_session)) -> None:
    row = session.get(LlmEndpointAllowlist, entry_id)
    if row is not None:
        session.delete(row)
        session.add(AuditLog(action="settings.allowlist.delete",
                             target=f"{row.host}:{row.port}"))


# ---------- search (env由来の現在値の表示) ----------


@router.get("/search")
def get_search(settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    return {
        "provider": settings.search_provider,
        "endpoint": settings.searxng_endpoint if settings.search_provider == "searxng" else None,
        "timeout_seconds": settings.search_timeout_seconds,
        "max_results": settings.search_max_results,
        "note": "検索providerは環境変数 (SEARCH_PROVIDER / SEARXNG_ENDPOINT) で設定します。"
        "有料検索APIはMVPでは利用できません。",
    }
