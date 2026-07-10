"""Web APIの統合テスト(TestClient、外部ネットワークなし)。"""


PIANO_QUESTION = "東京都内にはピアノ調律師が何人いるか"


def create_and_research(client) -> dict:
    report = client.post("/api/projects", json={
        "question": PIANO_QUESTION, "iterations": 4000,
    }).json()
    pid = report["project"]["id"]
    r = client.post(f"/api/projects/{pid}/research/start?wait=true")
    assert r.status_code == 200 and r.json()["status"] == "done"
    return client.get(f"/api/projects/{pid}").json()


def test_full_api_flow(app_client):
    report = create_and_research(app_client)
    pid = report["project"]["id"]

    # 結論・シナリオ・感度・検算が揃っている
    assert report["conclusion"]["central"] is not None
    assert len([s for s in report["scenarios"] if s["kind"] in ("bear", "base", "bull")]) == 3
    assert report["sensitivity"]
    assert report["validation"]["central_ratio"] is not None
    assert report["run"]["seed"] == report["simulation"]["config"]["seed"]

    # 出典URL・発行日・取得日が追跡可能
    for e in report["evidence"]:
        assert e["url"].startswith("https://")
        assert e["retrieval_date"]

    # ステータス取得
    status = app_client.get(f"/api/projects/{pid}/research/status").json()
    assert status["status"] == "done"
    assert status["searches_executed"] > 0

    # パラメータ編集 → ローカル再計算(検索数は増えない)
    before_searches = status["searches_executed"]
    r = app_client.patch(f"/api/projects/{pid}/parameters/membership_rate",
                         json={"central": 0.4, "low": 0.3, "high": 0.55, "note": "手動調整"})
    assert r.status_code == 200
    rep2 = r.json()
    p = next(x for x in rep2["parameters"] if x["id"] == "membership_rate")
    assert p["central"] == 0.4
    assert p["value_basis"] == "user_input"
    assert p["user_overridden"] is True
    assert any(hh["actor"] == "user" for hh in p["history"])  # 変更履歴
    status2 = app_client.get(f"/api/projects/{pid}/research/status").json()
    assert status2["searches_executed"] == before_searches  # 再検索なし

    # シード変更つき再計算
    r = app_client.post(f"/api/projects/{pid}/recalculate",
                        json={"seed": 777, "custom_overrides": {"membership_rate": 0.5}})
    assert r.status_code == 200
    rep3 = r.json()
    assert rep3["simulation"]["config"]["seed"] == 777
    custom = [s for s in rep3["scenarios"] if s["kind"] == "custom"]
    assert custom and custom[0]["value"] is not None

    # 証拠の不採用 → 再統合
    ev = next(e for e in rep3["evidence"]
              if e["parameter_id"] == "base_households" and e["extracted_value"])
    r = app_client.patch(f"/api/projects/{pid}/evidence/{ev['id']}",
                         json={"accepted": False, "rejection_reason": "検証用"})
    assert r.status_code == 200
    rep4 = r.json()
    updated_ev = next(e for e in rep4["evidence"] if e["id"] == ev["id"])
    assert updated_ev["accepted"] is False
    assert updated_ev["rejection_reason"] == "検証用"

    # 再検証
    assert app_client.post(f"/api/projects/{pid}/reverify").status_code == 200

    # エクスポート4形式
    for fmt, marker in (("json", b"conclusion"), ("csv", b"parameters.csv"),
                        ("html", b"<!DOCTYPE html>"), ("md", b"# ")):
        r = app_client.get(f"/api/projects/{pid}/export/{fmt}")
        assert r.status_code == 200
        assert marker in r.content

    # 一覧
    projects = app_client.get("/api/projects").json()
    assert any(p["id"] == pid for p in projects)


def test_scope_update_regenerates_models(app_client):
    report = app_client.post("/api/projects", json={"question": PIANO_QUESTION}).json()
    pid = report["project"]["id"]
    r = app_client.patch(f"/api/projects/{pid}/question", json={
        "reference_date": "2027", "regenerate_models": True,
    })
    assert r.status_code == 200
    rep = r.json()
    assert rep["question"]["reference_date"] == "2027"
    # ユーザーが修正した項目は暫定フラグが外れる
    assert all(p["field"] != "reference_date" for p in rep["question"]["provisional"])


def test_model_selection(app_client):
    report = app_client.post("/api/projects", json={"question": PIANO_QUESTION}).json()
    pid = report["project"]["id"]
    check = next(m for m in report["models"] if m["role"] == "check")
    primary = next(m for m in report["models"] if m["role"] == "primary")
    r = app_client.post(f"/api/projects/{pid}/models/select",
                        json={"primary_id": check["id"], "check_id": primary["id"]})
    assert r.status_code == 200
    roles = {m["id"]: m["role"] for m in r.json()["models"]}
    assert roles[check["id"]] == "primary"
    assert roles[primary["id"]] == "check"


def test_error_handling(app_client):
    assert app_client.get("/api/projects/prj_nonexistent").status_code == 404
    assert app_client.post("/api/projects", json={"question": ""}).status_code == 422
    report = app_client.post("/api/projects", json={"question": PIANO_QUESTION}).json()
    pid = report["project"]["id"]
    assert app_client.patch(f"/api/projects/{pid}/parameters/nope",
                            json={"central": 1.0}).status_code == 404
    assert app_client.patch(f"/api/projects/{pid}/parameters/base_households",
                            json={"low": 10.0, "high": 5.0}).status_code == 400
    assert app_client.post(f"/api/projects/{pid}/research/cancel").status_code == 409
    assert app_client.get(f"/api/projects/{pid}/export/bogus").status_code == 400


def test_zero_search_results_leaves_unresolved(app_client):
    """検索結果ゼロの問い: 値を捏造せず未解決として返す。"""
    report = app_client.post("/api/projects", json={
        "question": "アンドロメダ銀河のコーヒーショップは何店あるか",
    }).json()
    pid = report["project"]["id"]
    r = app_client.post(f"/api/projects/{pid}/research/start?wait=true")
    assert r.status_code == 200
    assert r.json()["status"] == "done"  # 全体は失敗しない
    rep = app_client.get(f"/api/projects/{pid}").json()
    assert rep["conclusion"]["central"] is None  # 捏造しない
    primary_params = [p for p in rep["parameters"] if p["status"] == "unresolved"]
    assert primary_params
    for p in primary_params:
        assert p["central"] is None
        assert p["unresolved_reason"]


def test_config_endpoint(app_client):
    config = app_client.get("/api/config").json()
    assert config["app_name"] == "FermiScope"
    assert config["search_provider"] == "mock"
    assert config["llm_provider"] == "noop"
    assert config["llm_available"] is False


def test_pages_render(app_client):
    r = app_client.get("/")
    assert r.status_code == 200
    assert "問い" in r.text
    assert "FermiScope" in r.text
    report = app_client.post("/api/projects", json={"question": PIANO_QUESTION}).json()
    r = app_client.get(f"/projects/{report['project']['id']}")
    assert r.status_code == 200
    assert "調査を開始" in r.text
    # CSPヘッダ
    assert "default-src 'self'" in r.headers.get("content-security-policy", "")
