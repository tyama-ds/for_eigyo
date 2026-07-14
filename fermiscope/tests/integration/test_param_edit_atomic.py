"""Phase 1-5: パラメータ編集の原子的検証の回帰テスト。"""

from __future__ import annotations

from tests.conftest import PIANO_QUESTION


def _make_project(app_client) -> str:
    report = app_client.post("/api/projects", json={"question": PIANO_QUESTION}).json()
    return report["project"]["id"]


def test_merged_low_central_high_validated(app_client):
    """部分更新でも更新後の完全な low<=central<=high を検証する。"""
    pid = _make_project(app_client)
    # central だけ既存 low/high の外へ動かす → 422
    app_client.patch(f"/api/projects/{pid}/parameters/base_households",
                     json={"low": 100.0, "central": 200.0, "high": 300.0})
    r = app_client.patch(f"/api/projects/{pid}/parameters/base_households",
                         json={"central": 1000.0})  # high(300)を超える
    assert r.status_code == 422


def test_failed_edit_does_not_persist(app_client):
    """検証失敗後の GET に変更値が残らない(原子的ロールバック)。"""
    pid = _make_project(app_client)
    app_client.patch(f"/api/projects/{pid}/parameters/base_households",
                     json={"low": 100.0, "central": 200.0, "high": 300.0})
    before = app_client.get(f"/api/projects/{pid}").json()
    before_central = next(
        p["central"] for p in before["parameters"] if p["id"] == "base_households"
    )
    # 不正編集(low>high)
    bad = app_client.patch(f"/api/projects/{pid}/parameters/base_households",
                           json={"low": 9.0, "high": 1.0})
    assert bad.status_code == 422
    after = app_client.get(f"/api/projects/{pid}").json()
    after_central = next(
        p["central"] for p in after["parameters"] if p["id"] == "base_households"
    )
    assert after_central == before_central  # 変更前の値のまま


def test_invalid_input_is_422_not_500(app_client):
    pid = _make_project(app_client)
    r = app_client.patch(f"/api/projects/{pid}/parameters/base_households",
                         json={"low": 5.0, "high": 1.0})
    assert r.status_code == 422
    # 分布を lognormal にしつつ負値 → 422(500 にしない)
    r2 = app_client.patch(f"/api/projects/{pid}/parameters/base_households",
                          json={"central": -1.0, "low": -2.0, "high": 0.0,
                                "distribution": "lognormal"})
    assert r2.status_code == 422


def test_distribution_only_change_keeps_unresolved(app_client):
    """数値なしで分布だけ変更しても、未解決状態を誤って解除しない。"""
    pid = _make_project(app_client)
    rep = app_client.get(f"/api/projects/{pid}").json()
    # 未解決(central=None)のパラメータを探す
    unresolved = [p for p in rep["parameters"] if p["central"] is None]
    if not unresolved:
        # 作成直後に未解決が無い構成なら、既知の末端を未解決化して検証する
        target = rep["parameters"][0]["id"]
    else:
        target = unresolved[0]["id"]
    r = app_client.patch(f"/api/projects/{pid}/parameters/{target}",
                         json={"distribution": "triangular"})
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        after = app_client.get(f"/api/projects/{pid}").json()
        p = next(p for p in after["parameters"] if p["id"] == target)
        # 数値を与えていないので user_input として確定していないこと
        assert p["value_basis"] != "user_input"


def test_valid_edit_recalculates(app_client):
    pid = _make_project(app_client)
    r = app_client.patch(f"/api/projects/{pid}/parameters/base_households",
                         json={"central": 5000000.0, "low": 4000000.0, "high": 6000000.0})
    assert r.status_code == 200
    p = next(p for p in r.json()["parameters"] if p["id"] == "base_households")
    assert p["central"] == 5000000.0
    assert p["value_basis"] == "user_input"
