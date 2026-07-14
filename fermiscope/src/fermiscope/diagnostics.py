"""診断情報の収集(health/readiness API と `fermiscope doctor` の共通実装)。

秘密(APIキー・プロキシ資格情報・DB資格情報)は一切含めない。プロキシは有無と
スキーム・ホストのみ、DBはスキームのみを出す。
"""

from __future__ import annotations

import platform
from typing import Any
from urllib.parse import urlparse

from fermiscope.config import Settings, _app_version, proxy_without_credentials


def _proxy_summary(settings: Settings) -> dict[str, Any]:
    """プロキシ設定の要約(資格情報を含めない)。"""

    def scheme_host(url: str) -> str:
        if not url:
            return ""
        sanitized = proxy_without_credentials(url)
        p = urlparse(sanitized)
        return f"{p.scheme}://{p.hostname}:{p.port}" if p.port else f"{p.scheme}://{p.hostname}"

    return {
        "http_proxy_set": bool(settings.http_proxy),
        "https_proxy_set": bool(settings.https_proxy),
        "all_proxy_set": bool(settings.all_proxy),
        # 資格情報を除いた scheme://host:port のみ(監査・診断用)
        "http_proxy": scheme_host(settings.http_proxy),
        "https_proxy": scheme_host(settings.https_proxy),
        "all_proxy": scheme_host(settings.all_proxy),
        "no_proxy": settings.no_proxy,  # ホスト名のみ(秘密ではない)
    }


def _db_scheme(url: str) -> str:
    """DB URL のスキームのみ(資格情報・ホストは出さない)。"""
    return (urlparse(url).scheme or "").split("+")[0] or "unknown"


def collect_diagnostics(
    settings: Settings,
    *,
    repo: Any | None = None,
    search_provider: Any | None = None,
    llm: Any | None = None,
) -> dict[str, Any]:
    """診断情報を収集する。ready(準備完了)判定を含む。秘密は含めない。"""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    # 必須リソース(設定・Web静的)
    config_ok = add(
        "config_dir", settings.config_dir.exists(), str(settings.config_dir)
    )
    web_ok = add("web_dir", settings.web_dir.exists(), str(settings.web_dir))

    # DB 接続性(repo があれば実際に接続する)
    db_ok = True
    if repo is not None:
        try:
            from sqlalchemy import text

            with repo.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            db_ok = add("database", True, _db_scheme(settings.database_url))
        except Exception as exc:  # noqa: BLE001 — 失敗理由を種別のみ記録
            db_ok = add("database", False, type(exc).__name__)

    # 検索プロバイダ・LLM(名称のみ)
    if search_provider is not None:
        add("search_provider", True, getattr(search_provider, "name", "?"))
    if llm is not None:
        add("llm_provider", True, getattr(llm, "name", "?"))

    ready = config_ok and web_ok and db_ok

    return {
        "app_version": _app_version(),
        "config_hash": settings.config_hash,
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "search_provider": settings.search_provider,
        "llm_provider": settings.llm_provider,
        "proxy": _proxy_summary(settings),
        "database_scheme": _db_scheme(settings.database_url),
        "resources": {"config_dir_ok": config_ok, "web_dir_ok": web_ok},
        "checks": checks,
        "ready": ready,
    }


def run_doctor(settings: Settings | None = None) -> int:
    """CLI `fermiscope doctor`: 診断を実行し人間可読に出力。ready なら 0 を返す。"""
    from fermiscope.config import get_settings
    from fermiscope.persistence.repository import ProjectRepository

    settings = settings or get_settings()
    repo = None
    try:
        repo = ProjectRepository(settings.database_url)
    except Exception:  # noqa: BLE001 — DB構築失敗も診断結果として扱う
        repo = None

    diag = collect_diagnostics(settings, repo=repo)
    print(f"FermiScope doctor — version {diag['app_version']}")
    print(f"  Python: {diag['python_version']} / {diag['platform']}")
    print(f"  検索プロバイダ: {diag['search_provider']}  LLM: {diag['llm_provider']}")
    print(f"  DB: {diag['database_scheme']}  config_hash: {diag['config_hash']}")
    px = diag["proxy"]
    print(
        f"  プロキシ: http={px['http_proxy'] or '-'} https={px['https_proxy'] or '-'} "
        f"all={px['all_proxy'] or '-'} no_proxy={px['no_proxy'] or '-'}"
    )
    print("  チェック:")
    for c in diag["checks"]:
        mark = "OK " if c["ok"] else "NG "
        print(f"    [{mark}] {c['name']}: {c['detail']}")
    print(f"  => ready: {'yes' if diag['ready'] else 'no'}")
    return 0 if diag["ready"] else 1
