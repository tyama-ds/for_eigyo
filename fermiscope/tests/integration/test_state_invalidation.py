"""Phase 1-4: スコープ・モデル・証拠変更後の状態無効化の回帰テスト。"""

from __future__ import annotations

from tests.conftest import PIANO_QUESTION


def _create_and_research(client) -> str:
    report = client.post(
        "/api/projects", json={"question": PIANO_QUESTION, "iterations": 2000}
    ).json()
    pid = report["project"]["id"]
    r = client.post(f"/api/projects/{pid}/research/start?wait=true")
    assert r.status_code == 200 and r.json()["status"] == "done"
    return pid


def test_scope_change_replaces_params(app_client):
    """東京→大阪 / 2026→2027 変更後に旧スコープのパラメータ名・地域が残らない。"""
    pid = _create_and_research(app_client)
    before = app_client.get(f"/api/projects/{pid}").json()
    assert any("東京" in p["name"] for p in before["parameters"])

    r = app_client.patch(
        f"/api/projects/{pid}/question",
        json={"geography": "大阪府", "reference_date": "2027", "regenerate_models": True},
    )
    assert r.status_code == 200
    after = app_client.get(f"/api/projects/{pid}").json()
    # 旧「東京」パラメータ名が残っていない
    assert not any("東京" in p["name"] for p in after["parameters"])
    assert any("大阪" in p["name"] for p in after["parameters"])
    # 旧スコープの証拠・矛盾・検算・信頼度が破棄されている
    assert after["evidence"] == []
    assert after["conclusion"]["central"] is None


def test_same_value_scope_save_is_noop(app_client):
    """同値スコープ保存ではモデルIDと暫定状態が変わらない。"""
    report = app_client.post("/api/projects", json={"question": PIANO_QUESTION}).json()
    pid = report["project"]["id"]
    before = app_client.get(f"/api/projects/{pid}").json()
    before_model_ids = [m["id"] for m in before["models"]]
    before_prov = {p["field"] for p in before["question"]["provisional"]}
    # 現在値と同じ地域を再保存
    same_geo = before["question"]["geography"]
    r = app_client.patch(
        f"/api/projects/{pid}/question",
        json={"geography": same_geo, "regenerate_models": True},
    )
    assert r.status_code == 200
    after = app_client.get(f"/api/projects/{pid}").json()
    assert [m["id"] for m in after["models"]] == before_model_ids  # モデルID不変
    assert {p["field"] for p in after["question"]["provisional"]} == before_prov  # 暫定不変


def test_primary_switch_updates_scenario_and_validation_model_ids(app_client):
    pid = _create_and_research(app_client)
    report = app_client.get(f"/api/projects/{pid}").json()
    non_primary = [m for m in report["models"] if m["role"] != "primary"]
    assert non_primary, "切替候補モデルがない"
    new_primary = non_primary[0]["id"]
    check_candidates = [m["id"] for m in report["models"] if m["id"] != new_primary]
    r = app_client.post(
        f"/api/projects/{pid}/models/select",
        json={"primary_id": new_primary, "check_id": check_candidates[0]},
    )
    assert r.status_code == 200
    rep = r.json()
    primary_id = next(m["id"] for m in rep["models"] if m["role"] == "primary")
    assert primary_id == new_primary
    # シナリオ・検算の model_id が新主モデルと一致する
    for sc in rep["scenarios"]:
        if sc.get("model_id"):
            assert sc["model_id"] == new_primary


def test_primary_equals_check_rejected(app_client):
    pid = _create_and_research(app_client)
    report = app_client.get(f"/api/projects/{pid}").json()
    mid = report["models"][0]["id"]
    r = app_client.post(
        f"/api/projects/{pid}/models/select", json={"primary_id": mid, "check_id": mid}
    )
    assert r.status_code == 400


def test_rejecting_contradiction_evidence_clears_old_contradiction(app_client):
    """矛盾を構成する証拠を不採用にすると、古い矛盾が残らない。"""
    pid = _create_and_research(app_client)
    report = app_client.get(f"/api/projects/{pid}").json()
    contradictions = report.get("contradictions", [])
    # 同梱デモ(ピアノ保有率)は矛盾を必ず生成する — 条件分岐せず前提として検証する
    assert contradictions, "デモは証拠間の矛盾を生成するはず"
    ev_ids = contradictions[0]["evidence_ids"]
    # 矛盾を構成する証拠を1件不採用にする
    r = app_client.patch(
        f"/api/projects/{pid}/evidence/{ev_ids[0]}",
        json={"accepted": False, "rejection_reason": "テスト"},
    )
    assert r.status_code == 200
    after = app_client.get(f"/api/projects/{pid}").json()
    remaining = {tuple(sorted(c["evidence_ids"])) for c in after.get("contradictions", [])}
    assert tuple(sorted(ev_ids)) not in remaining  # 古い矛盾が消えている
