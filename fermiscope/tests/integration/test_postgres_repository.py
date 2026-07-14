"""Section 1: PostgreSQL(psycopg)バックエンドの統合テスト。

`FERMISCOPE_TEST_POSTGRES_URL`(例: postgresql+psycopg://user:pass@host/db)が
設定されている場合のみ実行する。CI では postgres サービスコンテナに対して走る。
外部APIキーは不要(DB接続のみ)。
"""

from __future__ import annotations

import os

import pytest

from fermiscope.domain.enums import ParameterStatus
from fermiscope.domain.models import EstimateProject, ParameterEstimate, QuestionSpec
from fermiscope.persistence.repository import ProjectRepository

POSTGRES_URL = os.environ.get("FERMISCOPE_TEST_POSTGRES_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="FERMISCOPE_TEST_POSTGRES_URL 未設定"),
]


def _project() -> EstimateProject:
    proj = EstimateProject(
        question=QuestionSpec(original_question="東京都のコンビニは何店か", subject="コンビニ"),
        name="pg-test",
    )
    proj.parameters["stores"] = ParameterEstimate(
        id="stores", name="店舗数", unit="store", central=8000.0,
        status=ParameterStatus.ESTIMATED,
    )
    return proj


def test_postgres_save_and_load_roundtrip():
    repo = ProjectRepository(POSTGRES_URL)
    proj = _project()
    repo.save(proj)
    loaded = repo.load(proj.id)
    assert loaded is not None
    assert loaded.question.subject == "コンビニ"
    assert loaded.parameters["stores"].central == 8000.0


def test_postgres_list_and_delete():
    repo = ProjectRepository(POSTGRES_URL)
    proj = _project()
    repo.save(proj)
    ids = [p["id"] for p in repo.list_projects(limit=100)]
    assert proj.id in ids
    assert repo.delete(proj.id) is True
    assert repo.load(proj.id) is None
