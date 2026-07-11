"""監査で検出したAPIレイヤ問題(H3: 実行中の編集ガード)への回帰テスト。"""

from __future__ import annotations

from tests.conftest import PIANO_QUESTION


def test_edit_endpoints_blocked_while_running(app_client):
    """調査実行中は update_question / select_models も 409 で拒否される。

    修正前は両者に _ensure_not_running ガードが無く、実行中に共有状態を
    破壊できた。
    """
    report = app_client.post("/api/projects", json={"question": PIANO_QUESTION}).json()
    pid = report["project"]["id"]
    models = report["models"]

    # 実行中状態を強制(RunManager.is_running を True に固定)
    app_client.app.state.run_manager.is_running = lambda project_id: True

    q = app_client.patch(f"/api/projects/{pid}/question", json={"reference_date": "2027"})
    assert q.status_code == 409

    sel = app_client.post(
        f"/api/projects/{pid}/models/select",
        json={"primary_id": models[0]["id"]},
    )
    assert sel.status_code == 409


def test_wait_run_is_registered_with_manager(app_client, monkeypatch):
    """?wait=true の調査も RunManager 経由で起動され、実行中ロックが機能する。

    修正前は wait=true が manager.start を経由せず、is_running() が False の
    ままとなり二重起動・実行中編集を防げなかった。
    """
    started: dict[str, bool] = {"called": False}
    real_start = app_client.app.state.run_manager.start

    def spy_start(project_id, coro):
        started["called"] = True
        return real_start(project_id, coro)

    app_client.app.state.run_manager.start = spy_start

    report = app_client.post(
        "/api/projects", json={"question": PIANO_QUESTION, "iterations": 2000}
    ).json()
    pid = report["project"]["id"]
    r = app_client.post(f"/api/projects/{pid}/research/start?wait=true")
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    assert started["called"], "wait=true の実行が RunManager.start を経由していない"
