"""共有フィクスチャ。すべて外部ネットワークなしで動作する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fermiscope.config import Settings, load_settings
from fermiscope.domain.models import EstimateProject, SimulationConfig
from fermiscope.llm import MockLLMProvider, NoOpLLMProvider
from fermiscope.models.generator import generate_model_candidates
from fermiscope.question.parser import parse_question
from fermiscope.research.fetcher import DocumentFetcher
from fermiscope.research.mock_transport import build_mock_transport
from fermiscope.research.orchestrator import ResearchOrchestrator
from fermiscope.research.search.mock import MockSearchProvider
from fermiscope.research.search.service import SearchService

PIANO_QUESTION = "東京都内にはピアノ調律師が何人いるか"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = load_settings()
    s.database_url = f"sqlite:///{tmp_path}/test.db"
    s.simulation.iterations = 4000  # テスト高速化(それでも統計的に安定)
    s.http_proxy = ""  # ホスト環境の *_PROXY を持ち込まず、テストを決定論的にする
    return s


@pytest.fixture()
def settings_brave(tmp_path: Path) -> Settings:
    """実検索(brave)設定。モック用擬似信頼ドメインは注入されない。"""
    s = load_settings(env={"SEARCH_PROVIDER": "brave"})
    s.database_url = f"sqlite:///{tmp_path}/test_brave.db"
    s.http_proxy = ""
    return s


@pytest.fixture()
def mock_search(settings: Settings) -> MockSearchProvider:
    return MockSearchProvider(settings.mock_corpus_dir)


@pytest.fixture()
def mock_fetcher(settings: Settings) -> DocumentFetcher:
    return DocumentFetcher(
        settings,
        transport=build_mock_transport(settings.mock_corpus_dir),
        skip_dns=True,
    )


@pytest.fixture()
def noop_llm() -> NoOpLLMProvider:
    return NoOpLLMProvider()


@pytest.fixture()
def mock_llm() -> MockLLMProvider:
    return MockLLMProvider()


async def build_piano_project(settings: Settings, llm) -> EstimateProject:
    spec, _ = await parse_question(PIANO_QUESTION, llm)
    models, params, _ = await generate_model_candidates(spec, llm)
    project = EstimateProject(question=spec, name="テスト: ピアノ調律師")
    project.models = models
    project.parameters = params
    project.simulation_config = SimulationConfig(
        iterations=settings.simulation.iterations, seed=settings.simulation.default_seed
    )
    return project


async def run_piano_research(
    settings: Settings,
    llm,
    mock_search: MockSearchProvider,
    mock_fetcher: DocumentFetcher,
) -> EstimateProject:
    project = await build_piano_project(settings, llm)
    service = SearchService(mock_search, settings)
    orch = ResearchOrchestrator(settings, service, mock_fetcher, llm)
    await orch.run_research(project)
    return project


@pytest.fixture()
def app_client(settings: Settings, mock_search, mock_fetcher, noop_llm):
    """FastAPI TestClient(モック検索+NoOp LLM)。"""
    from fastapi.testclient import TestClient

    from fermiscope.api.app import create_app

    app = create_app(
        settings=settings,
        search_provider=mock_search,
        llm=noop_llm,
        fetcher=mock_fetcher,
    )
    with TestClient(app) as client:
        yield client
