"""FastAPI アプリケーションファクトリ。"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fermiscope import __version__
from fermiscope.api.runs import RunManager
from fermiscope.config import Settings, default_data_dir, get_settings, require_resources
from fermiscope.llm.base import LLMProvider
from fermiscope.llm.settings_store import LLMSettingsStore
from fermiscope.persistence.repository import ProjectRepository
from fermiscope.research.fetcher import DocumentFetcher
from fermiscope.research.mock_transport import build_mock_transport
from fermiscope.research.search.base import SearchProvider
from fermiscope.research.search.brave import BraveSearchProvider
from fermiscope.research.search.duckduckgo import DuckDuckGoSearchProvider
from fermiscope.research.search.mock import MockSearchProvider


def _default_llm_settings_path() -> str:
    return str(default_data_dir() / "llm_settings.json")


def _build_search_provider(settings: Settings) -> SearchProvider:
    # 検索APIは https。NO_PROXY 対象でなければ https 用の実効プロキシを使う。
    proxy = (settings.effective_proxy("https") or None) if settings.any_proxy_configured() else None
    if settings.search_provider == "brave":
        return BraveSearchProvider(timeout_seconds=settings.search.timeout_seconds, proxy=proxy)
    if settings.search_provider == "duckduckgo":
        return DuckDuckGoSearchProvider(timeout_seconds=settings.search.timeout_seconds, proxy=proxy)
    return MockSearchProvider(settings.mock_corpus_dir)


def _build_fetcher(settings: Settings) -> DocumentFetcher:
    if settings.search_provider == "mock":
        # モックモード: フィクスチャ配信トランスポート、DNS検査は省略
        return DocumentFetcher(
            settings,
            transport=build_mock_transport(settings.mock_corpus_dir),
            skip_dns=True,
        )
    return DocumentFetcher(settings)


def create_app(
    settings: Settings | None = None,
    search_provider: SearchProvider | None = None,
    llm: LLMProvider | None = None,
    fetcher: DocumentFetcher | None = None,
    repo: ProjectRepository | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    # 必須の設定・静的ファイルが無ければ黙って空へフォールバックせず明示エラー
    require_resources(settings.config_dir, settings.web_dir)
    app = FastAPI(title=settings.display_name(), version=__version__)

    app.state.settings = settings
    app.state.search_provider = search_provider or _build_search_provider(settings)
    # LLMは実行時に GUI から切替できるよう settings store 経由で解決する。
    # テスト等で明示注入された場合はそれを固定利用する(store は None)。
    if llm is not None:
        app.state.llm = llm
        app.state.llm_store = None
    else:
        settings_path = Path(
            os.environ.get("FERMISCOPE_LLM_SETTINGS_PATH", _default_llm_settings_path())
        )
        app.state.llm_store = LLMSettingsStore(settings_path)
        app.state.llm = None  # current_llm() が store から取得する
    app.state.fetcher = fetcher or _build_fetcher(settings)
    app.state.repo = repo or ProjectRepository(settings.database_url)
    app.state.run_manager = RunManager()
    app.state.projects_cache = {}  # id -> EstimateProject(実行中の状態を共有)

    templates = Jinja2Templates(directory=str(settings.web_dir / "templates"))
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(settings.web_dir / "static")), name="static")

    # DNSリバインディング対策の Host 許可リスト。既定はループバックのみ。
    # LAN/外部公開時は FERMISCOPE_ALLOWED_HOSTS にホスト名を明示し、加えて
    # リバースプロキシで認証をかけること(READMEに明記)。
    allowed_hosts = {"127.0.0.1", "localhost", "::1", "testserver"}
    extra_hosts = os.environ.get("FERMISCOPE_ALLOWED_HOSTS", "")
    for h in extra_hosts.split(","):
        h = h.strip().lower()
        if h:
            allowed_hosts.add(h)
    app.state.allowed_hosts = allowed_hosts

    def _host_only(raw: str) -> str:
        raw = raw.strip().lower()
        if raw.startswith("["):  # IPv6 リテラル [::1]:8720
            return raw[1 : raw.find("]")] if "]" in raw else raw.strip("[]")
        return raw.rsplit(":", 1)[0] if ":" in raw else raw

    @app.middleware("http")
    async def host_guard(request: Request, call_next):
        host = _host_only(request.headers.get("host", ""))
        # Host 未指定(HTTP/1.0 等)は許容。指定があれば許可リストで検証する。
        if host and host not in app.state.allowed_hosts:
            return JSONResponse(
                status_code=400,
                content={"detail": "許可されないHostヘッダです(DNSリバインディング対策)。"},
            )
        return await call_next(request)

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        # 状態変更(POST/PATCH/PUT/DELETE)への CSRF 対策。ブラウザが付与する
        # Origin ヘッダを許可 Host リストで検証する(Origin 無し=非ブラウザは許容)。
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            origin = request.headers.get("origin", "")
            if origin:
                netloc = urlparse(origin).netloc or origin
                origin_host = _host_only(netloc)
                if origin_host not in app.state.allowed_hosts:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "許可されないOriginです(CSRF対策)。"},
                    )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # ローカルアプリ向けの基本的なブラウザ保護
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    from fermiscope.api.routes import router

    app.include_router(router)
    return app
