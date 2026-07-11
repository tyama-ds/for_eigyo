"""設定ローダ。

優先順位: 環境変数 > YAML設定ファイル > コード内デフォルト。
設定ファイルのハッシュを監査ログ用に保持する(再現性要件)。
"""

from __future__ import annotations

import hashlib
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_WEB_DIR = PROJECT_ROOT / "web"
DEFAULT_MOCK_CORPUS_DIR = PACKAGE_ROOT / "data" / "mock_corpus"


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
        ]
    )


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
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'fermiscope.db'}"

    # 環境変数由来(値はログに出さない)
    search_provider: str = "mock"  # mock | brave
    llm_provider: str = "noop"  # noop | mock | openai_compatible
    config_hash: str = ""

    def display_name(self) -> str:
        return self.app.name


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

    settings.config_hash = _hash_configs(
        [cdir / "estimation.yaml", cdir / "evidence_scoring.yaml", cdir / "source_classes.yaml"]
    )
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
