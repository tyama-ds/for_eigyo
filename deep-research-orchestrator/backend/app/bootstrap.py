"""起動時ブートストラップ — 冪等。

- 環境変数からのLLM profile / role割り当て / proxy設定の初期投入
  (既にDBへ設定がある場合は上書きしない — UI設定が優先)
- EngineConfigの初期投入 (mock / gpt-researcher / open_deep_research)
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    EngineConfig,
    LlmEndpointAllowlist,
    LlmProfile,
    RoleAssignment,
)
from app.llm.profiles import ROLES, endpoint_host_port
from app.security.secrets import SecretStore

logger = structlog.get_logger(__name__)

MOCK_ENGINES = [
    ("mock-fast", "Mock Fast"),
    ("mock-slow", "Mock Slow"),
    ("mock-fail", "Mock Fail"),
    ("mock-partial", "Mock Partial"),
    ("mock-timeout", "Mock Timeout"),
    ("mock-cancellable", "Mock Cancellable"),
]


def bootstrap(session: Session, settings: Settings) -> None:
    _seed_engines(session, settings)
    _seed_llm_profiles(session, settings)
    session.flush()


def _seed_engines(session: Session, settings: Settings) -> None:
    for engine_id, name in MOCK_ENGINES:
        if session.get(EngineConfig, engine_id) is None:
            session.add(
                EngineConfig(
                    engine_id=engine_id,
                    display_name=name,
                    runner_url=settings.runner_mock_url,
                    enabled=True,
                    availability="available",
                    max_concurrency=settings.default_engine_max_concurrency,
                )
            )
    real = [
        ("gpt-researcher", "GPT Researcher", settings.runner_gptr_url),
        ("open-deep-research", "Open Deep Research", settings.runner_odr_url),
    ]
    for engine_id, name, url in real:
        existing = session.get(EngineConfig, engine_id)
        if existing is None:
            session.add(
                EngineConfig(
                    engine_id=engine_id,
                    display_name=name,
                    runner_url=url or "",
                    enabled=url is not None,
                    availability="available" if url else "disabled",
                    unavailable_reason=(
                        None if url
                        else "Runner URLが未設定です (DRO_RUNNER_GPTR_URL / DRO_RUNNER_ODR_URL)"
                    ),
                    max_concurrency=1,
                )
            )
        elif url and not existing.runner_url:
            existing.runner_url = url
            existing.enabled = True
            existing.availability = "available"
            existing.unavailable_reason = None


def _seed_llm_profiles(session: Session, settings: Settings) -> None:
    """環境変数からの初期profile。DBに同名profileがあれば触らない。"""
    has_any = session.scalar(select(LlmProfile).limit(1)) is not None

    def _ensure_profile(
        name: str, provider: str, endpoint: str | None, model: str | None, api_key: str | None
    ) -> LlmProfile | None:
        if not model:
            return None
        existing = session.scalar(select(LlmProfile).where(LlmProfile.name == name))
        if existing is not None:
            return existing
        profile = LlmProfile(
            name=name,
            provider=provider,
            api="anthropic" if provider == "anthropic" else "openai-compatible",
            endpoint=endpoint,
            model=model,
        )
        session.add(profile)
        session.flush()
        if api_key:
            # secret名はprofile idベース (名前変更後の再bootstrapで他profileの
            # keyを上書きしないため)
            profile.api_key_secret_id = SecretStore(session, settings).put(
                f"llm-profile:{profile.id}", api_key
            )
        if endpoint:
            host, port = endpoint_host_port(endpoint)
            if not session.scalar(
                select(LlmEndpointAllowlist).where(
                    LlmEndpointAllowlist.host == host, LlmEndpointAllowlist.port == port
                )
            ):
                session.add(LlmEndpointAllowlist(host=host, port=port, note=f"env:{name}"))
        logger.info("bootstrap_llm_profile_created", name=name, provider=provider)
        return profile

    local = _ensure_profile(
        "env-local", "local", settings.local_llm_endpoint, settings.local_llm_model,
        settings.local_llm_api_key,
    )
    openai_p = _ensure_profile(
        "env-openai", "openai", settings.openai_base_url, settings.openai_model,
        settings.openai_api_key,
    )
    anthropic_p = _ensure_profile(
        "env-anthropic", "anthropic", settings.anthropic_base_url, settings.anthropic_model,
        settings.anthropic_api_key,
    )

    # role割り当て: 既存割り当てがなければLLM_PROVIDERで指定されたprofileを全roleへ
    if session.scalar(select(RoleAssignment).limit(1)) is not None:
        return
    chosen = {"local": local, "openai": openai_p, "anthropic": anthropic_p}.get(
        settings.llm_provider
    )
    if chosen is None:
        if not has_any:
            logger.info("bootstrap_no_llm_profile",
                        note="LLM未設定。mockエンジンのみ実行可能です")
        return
    for role in ROLES:
        session.add(RoleAssignment(role=role, profile_id=chosen.id))
