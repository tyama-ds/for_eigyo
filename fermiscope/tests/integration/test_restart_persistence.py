"""再起動(DBからの再読込)で状態が保持されることの統合テスト。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from fermiscope.api.app import create_app
from fermiscope.config import load_settings
from fermiscope.llm import NoOpLLMProvider
from fermiscope.research.fetcher import DocumentFetcher
from fermiscope.research.mock_transport import build_mock_transport
from fermiscope.research.search.mock import MockSearchProvider
from tests.conftest import PIANO_QUESTION


def _make_app(db_url: str, settings):
    settings.database_url = db_url
    return create_app(
        settings=settings,
        search_provider=MockSearchProvider(settings.mock_corpus_dir),
        llm=NoOpLLMProvider(),
        fetcher=DocumentFetcher(
            settings, transport=build_mock_transport(settings.mock_corpus_dir), skip_dns=True
        ),
    )


def test_create_research_then_restart_persists(tmp_path):
    db_url = f"sqlite:///{tmp_path}/persist.db"
    s1 = load_settings(env={})
    s1.simulation.iterations = 2000
    with TestClient(_make_app(db_url, s1)) as client:
        rep = client.post(
            "/api/projects", json={"question": PIANO_QUESTION, "iterations": 2000}
        ).json()
        pid = rep["project"]["id"]
        assert client.post(f"/api/projects/{pid}/research/start?wait=true").status_code == 200
        before = client.get(f"/api/projects/{pid}").json()
        assert before["conclusion"]["central"] is not None

    # 別プロセス相当: 同じDBで新しいアプリを構築し、状態が復元されること
    s2 = load_settings(env={})
    with TestClient(_make_app(db_url, s2)) as client2:
        after = client2.get(f"/api/projects/{pid}").json()
        assert after["conclusion"]["central"] == before["conclusion"]["central"]
        assert after["question"]["subject"] == before["question"]["subject"]
        # 再計算がロード後も動く
        assert client2.post(f"/api/projects/{pid}/recalculate").status_code == 200
