"""必須の統合フロー: 作成→調査→編集→再計算→モデル変更→証拠変更→再調査→再起動。

各段で状態が混在せず、原子的に更新されることを検証する。モック検索のみで
外部接続なし。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.conftest import PIANO_QUESTION

from fermiscope.api.app import create_app
from fermiscope.config import load_settings
from fermiscope.llm import NoOpLLMProvider
from fermiscope.research.fetcher import DocumentFetcher
from fermiscope.research.mock_transport import build_mock_transport
from fermiscope.research.search.mock import MockSearchProvider


def _app(db_url: str, settings):
    settings.database_url = db_url
    return create_app(
        settings=settings,
        search_provider=MockSearchProvider(settings.mock_corpus_dir),
        llm=NoOpLLMProvider(),
        fetcher=DocumentFetcher(
            settings, transport=build_mock_transport(settings.mock_corpus_dir), skip_dns=True
        ),
    )


def test_full_lifecycle(tmp_path):
    db_url = f"sqlite:///{tmp_path}/flow.db"
    s1 = load_settings(env={})
    s1.http_proxy = s1.https_proxy = s1.all_proxy = ""
    s1.simulation.iterations = 2000

    with TestClient(_app(db_url, s1)) as client:
        # 1) 作成
        rep = client.post(
            "/api/projects", json={"question": PIANO_QUESTION, "iterations": 2000}
        ).json()
        pid = rep["project"]["id"]

        # 2) 調査
        r = client.post(f"/api/projects/{pid}/research/start?wait=true")
        assert r.status_code == 200 and r.json()["status"] == "done"
        after_research = client.get(f"/api/projects/{pid}").json()
        assert after_research["conclusion"]["central"] is not None
        n_ev = len(after_research["evidence"])
        assert n_ev > 0

        # 3) パラメータ編集(原子的・再計算される)
        r = client.patch(
            f"/api/projects/{pid}/parameters/base_households",
            json={"central": 7000000.0, "low": 6500000.0, "high": 7500000.0},
        )
        assert r.status_code == 200
        edited = next(p for p in r.json()["parameters"] if p["id"] == "base_households")
        assert edited["central"] == 7000000.0 and edited["value_basis"] == "user_input"

        # 4) 再計算
        assert client.post(f"/api/projects/{pid}/recalculate").status_code == 200

        # 5) モデル変更(主モデル切替)
        models = client.get(f"/api/projects/{pid}").json()["models"]
        non_primary = [m for m in models if m["role"] != "primary"]
        if non_primary:
            new_primary = non_primary[0]["id"]
            others = [m["id"] for m in models if m["id"] != new_primary]
            r = client.post(
                f"/api/projects/{pid}/models/select",
                json={"primary_id": new_primary, "check_id": others[0] if others else None},
            )
            assert r.status_code == 200
            assert next(m["id"] for m in r.json()["models"] if m["role"] == "primary") == new_primary

        # 6) 証拠変更(不採用 → 矛盾等が再構築される)
        ev = client.get(f"/api/projects/{pid}").json()["evidence"]
        if ev:
            r = client.patch(
                f"/api/projects/{pid}/evidence/{ev[0]['id']}",
                json={"accepted": False, "rejection_reason": "テスト"},
            )
            assert r.status_code == 200

        # 7) 再調査(重複しない)
        r = client.post(f"/api/projects/{pid}/research/start?wait=true")
        assert r.status_code == 200
        rerun = client.get(f"/api/projects/{pid}").json()
        keys = [
            (e["parameter_id"], e["url"], e.get("extracted_value")) for e in rerun["evidence"]
        ]
        assert len(keys) == len(set(keys)), "再調査で証拠が重複"
        central_before_restart = rerun["conclusion"]["central"]

    # 8) 再起動(別プロセス相当): 同じDBで復元でき、再計算も動く
    s2 = load_settings(env={})
    s2.http_proxy = s2.https_proxy = s2.all_proxy = ""
    with TestClient(_app(db_url, s2)) as client2:
        restored = client2.get(f"/api/projects/{pid}").json()
        assert restored["conclusion"]["central"] == central_before_restart
        assert client2.post(f"/api/projects/{pid}/recalculate").status_code == 200
