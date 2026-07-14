"""設定ローダ。

優先順位: 環境変数 > YAML設定ファイル > コード内デフォルト。
設定ファイルのハッシュを監査ログ用に保持する(再現性要件)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
# wheel 同梱リソース(hatch force-include で fermiscope/_bundled 以下に配置)
_BUNDLED = PACKAGE_ROOT / "_bundled"


def _resolve_resource_dir(name: str, env_key: str) -> Path:
    """設定・静的ファイルのディレクトリを解決する。

    優先順: 環境変数 > wheel同梱(_bundled) > 開発時のリポジトリ直下。
    非editable wheel でも起動できるよう、同梱リソースを最優先で探索する。
    """
    env_val = os.environ.get(env_key)
    if env_val:
        return Path(env_val)
    bundled = _BUNDLED / name
    if bundled.exists():
        return bundled
    return PROJECT_ROOT / name  # 開発(editable)時


DEFAULT_CONFIG_DIR = _resolve_resource_dir("config", "FERMISCOPE_CONFIG_DIR")
DEFAULT_WEB_DIR = _resolve_resource_dir("web", "FERMISCOPE_WEB_DIR")
DEFAULT_MOCK_CORPUS_DIR = PACKAGE_ROOT / "data" / "mock_corpus"


def default_data_dir() -> Path:
    """書き込み可能なデータ(DB・LLM設定)を置くディレクトリ。

    パッケージディレクトリには書き込まない。editable開発時はリポジトリ直下、
    インストール済みならユーザーデータディレクトリを使う。
    """
    env_val = os.environ.get("FERMISCOPE_DATA_DIR")
    if env_val:
        return Path(env_val)
    if (PROJECT_ROOT / "pyproject.toml").exists():
        return PROJECT_ROOT  # 開発時は従来どおりリポジトリ直下
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "fermiscope"


def require_resources(config_dir: Path, web_dir: Path) -> None:
    """必須の設定・静的ファイルが存在することを確認する。無ければ明示エラー。

    黙って空設定へフォールバックしない(要件 §7)。
    """
    missing: list[str] = []
    if not (config_dir / "estimation.yaml").exists():
        missing.append(f"設定ファイル {config_dir / 'estimation.yaml'}")
    if not (web_dir / "templates" / "index.html").exists():
        missing.append(f"テンプレート {web_dir / 'templates' / 'index.html'}")
    if not (web_dir / "static").is_dir():
        missing.append(f"静的ディレクトリ {web_dir / 'static'}")
    if missing:
        raise RuntimeError(
            "必須リソースが見つかりません: " + " / ".join(missing) +
            "。wheelが正しく同梱されていないか、FERMISCOPE_CONFIG_DIR / "
            "FERMISCOPE_WEB_DIR の指定を確認してください。"
        )


class DecompositionConfig(BaseModel):
    initial_max_leaves: int = 10
    max_leaves_after_expansion: int = 15
    max_depth: int = 3
    max_revisits_per_parameter: int = 2
    critique_severity_threshold: float = 0.60
    importance_threshold: float = 0.40


class ScenarioQuantiles(BaseModel):
    bear: float = 0.10
    base: float = 0.50
    bull: float = 0.90


class SimulationSettings(BaseModel):
    iterations: int = 20000
    default_seed: int = 20260710
    scenario_quantiles: ScenarioQuantiles = Field(default_factory=ScenarioQuantiles)
    extra_quantiles: list[float] = Field(default_factory=lambda: [0.05, 0.25, 0.75, 0.95])
    histogram_bins: int = 40


class ValidationSettings(BaseModel):
    central_ratio_warning: float = 3.0
    interval_overlap_warning: float = 0.10


class SearchSettings(BaseModel):
    max_searches_per_project: int = 40
    max_cost_per_project_usd: float = 1.0
    cost_per_search_usd: float = 0.005
    rate_limit_per_second: float = 1.0
    timeout_seconds: float = 15
    max_retries: int = 2
    retry_backoff_seconds: float = 2.0
    cache_ttl_hours: float = 168
    max_results_per_query: int = 6


class FetchSettings(BaseModel):
    timeout_seconds: float = 20
    max_response_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 5
    cache_ttl_hours: float = 720
    user_agent: str = "FermiScopeBot/0.1 (+local research tool)"
    allowed_content_types: list[str] = Field(
        default_factory=lambda: [
            "text/html",
            "application/xhtml+xml",
            "text/plain",
            "text/csv",
            "application/json",
            "application/pdf",
            # Office Open XML(docx / xlsx / pptx)
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ]
    )
    # Office(ZIPベース)文書の解凍後サイズ上限。ZIP爆弾対策。
    max_office_uncompressed_bytes: int = 60 * 1024 * 1024
    # 抽出テキストの最大文字数(プロンプトインジェクション面・メモリの抑制)
    max_extracted_chars: int = 400_000
    # --- Selenium ハイブリッド取得(任意) ---
    # httpx取得の本文が乏しい(JS描画必須)ページのみ、URL検証後にSeleniumでDOMを取得する。
    use_selenium_fallback: bool = False
    selenium_min_text_chars: int = 200          # httpx本文がこれ未満ならSeleniumを試す
    selenium_page_timeout_seconds: float = 20.0
    selenium_wait_seconds: float = 2.0          # 描画待ち
    selenium_driver_path: str = ""              # chromedriver。空ならPATH/Selenium Manager
    selenium_binary_path: str = ""              # Chromium/Chrome本体。空なら既定


class FusionSettings(BaseModel):
    low_quantile: float = 0.10
    high_quantile: float = 0.90
    outlier_iqr_multiplier: float = 3.0
    min_evidence_score: float = 20
    log_space_for_positive: bool = True


class AppSettings(BaseModel):
    name: str = "FermiScope"
    language: str = "ja"


class ScoringWeights(BaseModel):
    source_authority: float = 0.18
    primaryness: float = 0.14
    parameter_directness: float = 0.18
    methodology_transparency: float = 0.12
    geography_fit: float = 0.08
    population_fit: float = 0.07
    time_fit: float = 0.08
    recency: float = 0.05
    independence: float = 0.05
    reproducibility: float = 0.05


class ScoringPenalties(BaseModel):
    conflict_of_interest_penalty: float = 15.0
    unclear_definition_penalty: float = 10.0
    secondary_citation_penalty: float = 12.0
    stale_data_penalty: float = 10.0
    sample_bias_penalty: float = 10.0
    unverifiable_claim_penalty: float = 15.0


class ScoringTimeSettings(BaseModel):
    recency_half_life_years: float = 5.0
    stale_threshold_years: float = 8.0
    time_fit_tolerance_years: float = 2.0


class ClusteringSettings(BaseModel):
    title_similarity_threshold: float = 0.75


class ContradictionSettings(BaseModel):
    ratio_threshold: float = 2.0


class ScoringConfig(BaseModel):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    penalties: ScoringPenalties = Field(default_factory=ScoringPenalties)
    time: ScoringTimeSettings = Field(default_factory=ScoringTimeSettings)
    clustering: ClusteringSettings = Field(default_factory=ClusteringSettings)
    contradiction: ContradictionSettings = Field(default_factory=ContradictionSettings)


class SourceClassDef(BaseModel):
    base_authority: float
    label: str = ""
    description: str = ""


class DomainHint(BaseModel):
    suffixes: list[str]
    hint_class: str


class SourceClassConfig(BaseModel):
    classes: dict[str, SourceClassDef] = Field(default_factory=dict)
    domain_hints: list[DomainHint] = Field(default_factory=list)
    patent_rules: dict[str, Any] = Field(default_factory=dict)
    conflict_of_interest: dict[str, Any] = Field(default_factory=dict)


class Settings(BaseModel):
    """アプリケーション全体の設定。"""

    app: AppSettings = Field(default_factory=AppSettings)
    decomposition: DecompositionConfig = Field(default_factory=DecompositionConfig)
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    fusion: FusionSettings = Field(default_factory=FusionSettings)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    source_classes: SourceClassConfig = Field(default_factory=SourceClassConfig)

    config_dir: Path = DEFAULT_CONFIG_DIR
    web_dir: Path = DEFAULT_WEB_DIR
    mock_corpus_dir: Path = DEFAULT_MOCK_CORPUS_DIR
    database_url: str = Field(
        default_factory=lambda: f"sqlite:///{default_data_dir() / 'fermiscope.db'}"
    )

    # プロキシ設定(検索・文書取得・Selenium・LLM で共通利用)。HTTP と HTTPS を
    # 別々に保持し、1つに潰さない。all_proxy は両スキームの既定。no_proxy は
    # バイパス対象ホスト。いずれも認証情報を含み得るためログ・API応答には出さない。
    http_proxy: str = ""  # http:// 向けプロキシ(後方互換で既定プロキシも兼ねる)
    https_proxy: str = ""  # https:// 向けプロキシ
    all_proxy: str = ""  # スキーム別指定が無い場合の既定(SOCKS 等)
    no_proxy: str = ""  # バイパスするホスト(カンマ区切り。"*" は全バイパス)

    # 環境変数由来(値はログに出さない)
    search_provider: str = "mock"  # mock | brave | duckduckgo
    llm_provider: str = "noop"  # noop | mock | openai_compatible
    config_hash: str = ""

    def display_name(self) -> str:
        return self.app.name

    def effective_proxy(self, scheme: str) -> str:
        """スキーム(http/https)に対する実効プロキシURL。無ければ空文字。

        HTTP と HTTPS を潰さず、スキーム別指定 → all_proxy の順で解決する。
        http_proxy は後方互換のため、https 指定が無い場合の https 既定も兼ねる。
        """
        scheme = (scheme or "").lower()
        if scheme == "https":
            return self.https_proxy or self.http_proxy or self.all_proxy
        if scheme == "http":
            return self.http_proxy or self.all_proxy
        return self.all_proxy or self.https_proxy or self.http_proxy

    def any_proxy_configured(self) -> bool:
        return bool(self.http_proxy or self.https_proxy or self.all_proxy)

    def no_proxy_bypass(self, host: str) -> bool:
        """host が NO_PROXY によりプロキシ対象外(直接接続)か判定する。"""
        return _no_proxy_match(host, self.no_proxy)

    def proxy_for_url(self, url: str) -> str | None:
        """URL に適用すべきプロキシURL。NO_PROXY 対象や未設定なら None。"""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if self.no_proxy_bypass(host):
            return None
        proxy = self.effective_proxy(parsed.scheme)
        return proxy or None

    def httpx_mounts(self) -> dict[str, Any] | None:
        """httpx.AsyncClient(mounts=...) 用の scheme→transport 対応表を作る。

        HTTP と HTTPS で別プロキシを設定できるようにする。プロキシ未設定なら None。
        SOCKS 指定で socksio 未導入なら分かりやすい日本語エラーを送出する。
        """
        if not self.any_proxy_configured():
            return None
        import httpx

        mounts: dict[str, Any] = {}
        for scheme in ("http", "https"):
            proxy = self.effective_proxy(scheme)
            if not proxy:
                continue
            _require_socks_if_needed(proxy)
            mounts[f"{scheme}://"] = httpx.AsyncHTTPTransport(proxy=proxy)
        if not mounts:
            return None
        # NO_PROXY 対象は直接接続(プロキシ非経由)にする。より具体的な mount が
        # 優先されるため、ホスト単位の直接トランスポートを追加する。
        for entry in (e.strip() for e in self.no_proxy.split(",") if e.strip()):
            if entry == "*":
                # 全バイパス: プロキシ mount を無効化する
                return None
            base = entry.lstrip(".").rstrip(".")
            if not base:
                continue
            mounts[f"all://{base}"] = httpx.AsyncHTTPTransport()
            mounts[f"all://*.{base}"] = httpx.AsyncHTTPTransport()
        return mounts or None


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _int_env(env: dict[str, str], key: str, default: int) -> int:
    """整数の環境変数を安全に読む。不正値は警告扱いでデフォルトにフォールバックする
    (アプリ全体の起動失敗を避ける)。"""
    raw = env.get(key, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "環境変数 %s の値 %r は整数として解釈できません。既定値 %d を使用します。",
            key,
            raw,
            default,
        )
        return default


def _hash_configs(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        if p.exists():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _no_proxy_match(host: str, no_proxy: str) -> bool:
    """host が NO_PROXY リストに合致するか(直接接続すべきか)を判定する。

    - "*" は全ホストをバイパス。
    - 完全一致、サブドメイン境界一致(先頭ドット/ドメイン名)、IP完全一致に対応。
    - 大文字小文字を無視する。
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    entries = [e.strip().lower() for e in (no_proxy or "").split(",") if e.strip()]
    for entry in entries:
        if entry == "*":
            return True
        base = entry.lstrip(".").rstrip(".")
        if not base:
            continue
        if h == base or h.endswith("." + base):
            return True
    return False


def proxy_without_credentials(proxy: str) -> str:
    """プロキシURLから認証情報(user:pass@)を取り除く。

    プロセス引数やログへ渡す際に資格情報を漏らさないために使う。
    """
    if not proxy:
        return ""
    parsed = urlparse(proxy)
    if parsed.hostname is None:
        return proxy
    netloc = parsed.hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def _require_socks_if_needed(proxy: str) -> None:
    """SOCKS プロキシ指定なのに socksio 未導入なら日本語で明示エラーを送出する。"""
    scheme = (urlparse(proxy).scheme or "").lower()
    if scheme.startswith("socks"):
        try:
            import socksio  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "SOCKSプロキシが指定されましたが socks 拡張が未導入です。"
                "`pip install \"fermiscope[socks]\"`(または httpx[socks])を導入してください。"
            ) from exc


# モック検索時のみ追加する擬似信頼ドメイン(実検索設定からは分離する)
_MOCK_DOMAIN_HINTS = [
    ("example-gov.jp", "S"),
    ("example-org.jp", "B"),
]


def _effective_config_hash(settings: Settings, config_files: list[Path]) -> str:
    """秘密値を除いた実効設定から再現性ハッシュを生成する。

    設定ファイルの内容に加え、検索・LLMプロバイダ、反復数・シード、検索/コスト上限、
    主要機能フラグ(Selenium)を含める。APIキー・プロキシ資格情報等の秘密は含めない。
    """
    h = hashlib.sha256()
    for p in sorted(config_files):
        if p.exists():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    effective = {
        "app_version": _app_version(),
        "search_provider": settings.search_provider,
        "llm_provider": settings.llm_provider,
        "iterations": settings.simulation.iterations,
        "seed": settings.simulation.default_seed,
        "max_searches": settings.search.max_searches_per_project,
        "max_cost_usd": settings.search.max_cost_per_project_usd,
        "use_selenium": settings.fetch.use_selenium_fallback,
        "proxy_set": settings.any_proxy_configured(),  # 有無のみ(値は秘密)
    }
    h.update(json.dumps(effective, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:16]


def _app_version() -> str:
    """アプリのバージョン(+可能ならビルド/コミットID)。"""
    from fermiscope import __version__

    build = os.environ.get("FERMISCOPE_BUILD_ID") or os.environ.get("GIT_COMMIT")
    return f"{__version__}+{build}" if build else __version__


def load_settings(config_dir: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    """YAML設定と環境変数から Settings を構築する。"""
    env = dict(os.environ) if env is None else env
    cdir = config_dir or Path(env.get("FERMISCOPE_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))

    est = _load_yaml(cdir / "estimation.yaml")
    scoring = _load_yaml(cdir / "evidence_scoring.yaml")
    source_classes = _load_yaml(cdir / "source_classes.yaml")

    settings = Settings(
        app=AppSettings(**est.get("app", {})),
        decomposition=DecompositionConfig(**est.get("decomposition", {})),
        simulation=SimulationSettings(**est.get("simulation", {})),
        validation=ValidationSettings(**est.get("validation", {})),
        search=SearchSettings(**est.get("search", {})),
        fetch=FetchSettings(**est.get("fetch", {})),
        fusion=FusionSettings(**est.get("fusion", {})),
        scoring=ScoringConfig(**scoring) if scoring else ScoringConfig(),
        source_classes=SourceClassConfig(**source_classes) if source_classes else SourceClassConfig(),
        config_dir=cdir,
        http_proxy=est.get("http_proxy", ""),
    )

    # 環境変数による上書き
    if env.get("FERMISCOPE_APP_NAME"):
        settings.app.name = env["FERMISCOPE_APP_NAME"]
    if env.get("FERMISCOPE_DATABASE_URL"):
        settings.database_url = env["FERMISCOPE_DATABASE_URL"]
    if env.get("SEARCH_PROVIDER"):
        settings.search_provider = env["SEARCH_PROVIDER"].lower()
    if env.get("LLM_PROVIDER"):
        settings.llm_provider = env["LLM_PROVIDER"].lower()
    if env.get("FERMISCOPE_MC_ITERATIONS"):
        settings.simulation.iterations = _int_env(
            env, "FERMISCOPE_MC_ITERATIONS", settings.simulation.iterations
        )
    if env.get("FERMISCOPE_MAX_SEARCHES"):
        settings.search.max_searches_per_project = _int_env(
            env, "FERMISCOPE_MAX_SEARCHES", settings.search.max_searches_per_project
        )
    if env.get("FERMISCOPE_WEB_DIR"):
        settings.web_dir = Path(env["FERMISCOPE_WEB_DIR"])
    # プロキシ: HTTP と HTTPS を潰さず個別に保持する。標準変数は大文字・小文字の
    # 両方を受け付け(小文字を優先)、専用変数 FERMISCOPE_* を最優先する。
    def _proxy_env(*names: str) -> str:
        for n in names:
            val = env.get(n)
            if val:
                return val
        return ""

    # env 指定があれば上書き、無ければ YAML 由来の値(settings.http_proxy)を保つ。
    settings.http_proxy = (
        _proxy_env("FERMISCOPE_HTTP_PROXY", "http_proxy", "HTTP_PROXY") or settings.http_proxy
    )
    settings.https_proxy = _proxy_env("FERMISCOPE_HTTPS_PROXY", "https_proxy", "HTTPS_PROXY")
    settings.all_proxy = _proxy_env("FERMISCOPE_ALL_PROXY", "all_proxy", "ALL_PROXY")
    settings.no_proxy = _proxy_env("FERMISCOPE_NO_PROXY", "no_proxy", "NO_PROXY")
    # Selenium ハイブリッド取得(任意)
    if env.get("FERMISCOPE_USE_SELENIUM", "").lower() in ("1", "true", "yes"):
        settings.fetch.use_selenium_fallback = True
    if env.get("FERMISCOPE_SELENIUM_DRIVER"):
        settings.fetch.selenium_driver_path = env["FERMISCOPE_SELENIUM_DRIVER"]
    if env.get("FERMISCOPE_SELENIUM_BINARY"):
        settings.fetch.selenium_binary_path = env["FERMISCOPE_SELENIUM_BINARY"]

    # モック検索時のみ擬似信頼ドメインを追加する(実検索では実ドメインのみ)
    if settings.search_provider == "mock":
        settings.source_classes.domain_hints = [
            *settings.source_classes.domain_hints,
            *(DomainHint(suffixes=[d], hint_class=c) for d, c in _MOCK_DOMAIN_HINTS),
        ]

    settings.config_hash = _effective_config_hash(
        settings,
        [cdir / "estimation.yaml", cdir / "evidence_scoring.yaml", cdir / "source_classes.yaml"],
    )
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
