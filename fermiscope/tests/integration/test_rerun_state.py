"""Section 3: 再実行時の状態管理(run_id・重複防止・履歴分離)の回帰テスト。"""

from __future__ import annotations

from tests.conftest import PIANO_QUESTION

from fermiscope.api.runs import RunManager


def _create_and_research(client) -> str:
    report = client.post(
        "/api/projects", json={"question": PIANO_QUESTION, "iterations": 2000}
    ).json()
    pid = report["project"]["id"]
    r = client.post(f"/api/projects/{pid}/research/start?wait=true")
    assert r.status_code == 200
    return pid


def test_double_research_no_duplicate_evidence(app_client):
    """同一プロジェクトを2回調査しても証拠・矛盾・批判が重複しない。"""
    pid = _create_and_research(app_client)
    first = app_client.get(f"/api/projects/{pid}").json()
    n_ev1 = len(first["evidence"])
    assert n_ev1 > 0

    # 2回目の調査
    r = app_client.post(f"/api/projects/{pid}/research/start?wait=true")
    assert r.status_code == 200
    second = app_client.get(f"/api/projects/{pid}").json()

    # 証拠が2倍などに増殖していない(累積予算のため同数以下になり得る)
    assert len(second["evidence"]) <= n_ev1
    # 同一事実(パラメータ・URL・抽出値)の重複が生成されていない
    keys = [
        (e["parameter_id"], e["url"], e.get("extracted_value"))
        for e in second["evidence"]
    ]
    assert len(keys) == len(set(keys)), "再実行で同一証拠が重複している"
    # 矛盾も重複しない
    con_keys = [
        tuple(sorted(c["evidence_ids"])) for c in second.get("contradictions", [])
    ]
    assert len(con_keys) == len(set(con_keys)), "再実行で矛盾が重複している"
    # パラメータの evidence_ids は現存する証拠のみを指す(スタレ参照なし)
    live_ids = {e["id"] for e in second["evidence"]}
    for p in second["parameters"]:
        for eid in p["evidence_ids"]:
            assert eid in live_ids


def test_two_runs_have_distinct_run_ids(app_client):
    """2回の run はそれぞれ別の run_id を持つ。"""
    pid = _create_and_research(app_client)
    st1 = app_client.get(f"/api/projects/{pid}/research/status").json()
    run_id_1 = st1["run_id"]
    assert run_id_1
    app_client.post(f"/api/projects/{pid}/research/start?wait=true")
    st2 = app_client.get(f"/api/projects/{pid}/research/status").json()
    assert st2["run_id"] and st2["run_id"] != run_id_1


def test_run_manager_does_not_replay_old_done_to_new_run():
    """新しい run の最初のイベントで前 run の done を履歴から破棄する。"""
    mgr = RunManager()
    # run A: 完了(done)まで
    mgr.emit("p1", "stage", "開始", {"run_id": "run_A"})
    mgr.emit("p1", "done", "完了", {"run_id": "run_A"})
    # run A の購読者は done を受け取れる
    q_a = mgr.subscribe("p1")
    replayed_a = [q_a.get_nowait()["type"] for _ in range(q_a.qsize())]
    assert "done" in replayed_a

    # run B の最初のイベント → 履歴がクリアされ、run A の done は残らない
    mgr.emit("p1", "stage", "再開", {"run_id": "run_B"})
    q_b = mgr.subscribe("p1")
    replayed_b = [q_b.get_nowait() for _ in range(q_b.qsize())]
    types_b = [e["type"] for e in replayed_b]
    assert "done" not in types_b
    assert all(e["run_id"] == "run_B" for e in replayed_b)
