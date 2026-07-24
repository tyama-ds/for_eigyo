"""アプリケーション設定。

環境変数から読み込む。secretの実値はここに置かず、secret store
(app.security.secrets) と master key file を経由する。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DRO_", env_file=None, extra="ignore")

    # --- infra ---
    database_url: str = "postgresql+psycopg://dro:dro@localhost:5432/dro"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Path("/data")

    # --- secrets at rest ---
    # master keyはDB/リポジトリとは別のファイルまたはcontainer secretから読む
    master_key_file: Path = Path("/run/secrets/dro_master_key")
    # テスト・開発用: fileが無い場合に環境変数からの直接指定を許可
    master_key: str | None = None

    # --- server ---
    api_host: str = "0.0.0.0"
    api_port: int = 8800
    cors_origins: list[str] = []

    # --- orchestration ---
    global_max_concurrent_runs: int = 8
    default_engine_max_concurrency: int = 2
    run_poll_interval_seconds: float = 1.0
    run_default_timeout_seconds: int = 1800
    run_max_attempts: int = 3
    retry_backoff_base_seconds: float = 2.0
    retry_backoff_max_seconds: float = 60.0
    stuck_run_heartbeat_seconds: int = 120
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_reset_seconds: int = 300

    # --- artifact store ---
    artifact_inline_max_bytes: int = 262_144  # これ以下はPostgreSQLへinline保存
    artifact_quota_bytes: int = 10 * 1024 * 1024 * 1024  # DATA_DIR全体のquota
    artifact_retention_days: int = 30
    artifact_max_single_bytes: int = 512 * 1024 * 1024

    # --- LLM / search 既定 (環境変数によるブートストラップ設定) ---
    llm_provider: str = "local"  # local | openai | anthropic
    local_llm_api: str = "openai-compatible"
    local_llm_endpoint: str | None = None
    local_llm_api_key: str | None = None
    local_llm_model: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str | None = None
    anthropic_model: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"

    search_provider: str = "searxng"  # searxng | disabled
    searxng_endpoint: str = "http://searxng:8080"
    search_timeout_seconds: int = 20
    search_max_results: int = 10
    search_rate_limit_per_minute: int = 60

    # --- proxy (inherit mode が参照する環境変数は標準名を使う) ---
    proxy_mode: str = "off"  # off | inherit | explicit
    proxy_ca_bundle: str | None = None

    # --- runners ---
    runner_mock_url: str = "http://runner-mock:9001"
    runner_gptr_url: str | None = None
    runner_odr_url: str | None = None
    runner_shared_token: str | None = None

    # --- observability ---
    log_level: str = "INFO"
    otel_exporter_otlp_endpoint: str | None = None

    # --- retention / audit ---
    event_retention_days: int = 90
    audit_retention_days: int = 365


@lru_cache
def get_settings() -> Settings:
    return Settings()
