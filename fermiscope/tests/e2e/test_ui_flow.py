"""UIフローのE2Eテスト(TestClientベース、ブラウザ不要)。

実ブラウザでのテストは test_browser.py(pytest -m e2e、Playwright必要)。
"""

PIANO_QUESTION = "東京都内にはピアノ調律師が何人いるか"


def test_index_page_accessibility_basics(app_client):
    html = app_client.get("/").text
    # 入力欄にlabelが付与されている
    assert '<label for="question">' in html
    assert '<label for="geography">' in html
    assert '<label for="research_mode">' in html
    # 調査モードの選択肢
    assert "高速" in html and "標準" in html and "慎重" in html


def test_project_page_has_all_sections(app_client):
    report = app_client.post("/api/projects", json={"question": PIANO_QUESTION}).json()
    html = app_client.get(f"/projects/{report['project']['id']}").text
    for section in ("結果", "スコープ", "モデル", "調査状況", "推定式", "図表",
                    "パラメータ", "証拠一覧", "監査ログ", "エクスポート"):
        assert section in html
    assert 'role="tablist"' in html  # キーボード操作可能なタブ
    assert "aria-selected" in html


def test_full_ui_backend_flow(app_client):
    """新規作成 → 調査 → 結果表示 → 編集 → 再計算 → エクスポート(UIが叩くAPI列)。"""
    # 1. 新規プロジェクト作成(index.jsのsubmitと同じペイロード)
    report = app_client.post("/api/projects", json={
        "question": PIANO_QUESTION,
        "geography": "", "reference_date": "", "target_unit": "",
        "known_facts": [], "research_mode": "standard",
    }).json()
    pid = report["project"]["id"]

    # 2. 調査実行
    assert app_client.post(
        f"/api/projects/{pid}/research/start?wait=true"
    ).json()["status"] == "done"

    # 3. SSEイベント履歴が再生される(project.jsのEventSource相当)
    with app_client.stream("GET", f"/api/projects/{pid}/events") as stream:
        collected = []
        for line in stream.iter_lines():
            if line.startswith("data: "):
                collected.append(line)
            if len(collected) >= 3:
                break
    assert any("stage" in c or "hello" in c for c in collected)

    # 4. レポートに結果ダッシュボードの中身が揃っている
    rep = app_client.get(f"/api/projects/{pid}").json()
    assert rep["conclusion"]["central_display"] != "—"
    assert rep["conclusion"]["confidence"] is not None
    primary = next(m for m in rep["models"] if m["role"] == "primary")
    assert primary["formula_tree"]["children"]  # 式ツリー(formula.jsが描画)
    sim = rep["simulation"]["results"][0]
    assert sim["histogram_counts"]  # ヒストグラム(charts.jsが描画)
    assert rep["sensitivity"]  # トルネードチャート用

    # 5. パラメータ編集 → 再計算(project.jsの保存ボタン相当)
    before = rep["conclusion"]["central"]
    rep2 = app_client.patch(f"/api/projects/{pid}/parameters/ownership_rate", json={
        "central": 0.2, "low": 0.15, "high": 0.25, "note": "UIテスト",
    }).json()
    after = rep2["conclusion"]["central"]
    assert after > before * 1.4  # 保有率2倍 → 結果もほぼ2倍

    # 6. エクスポートリンク
    for fmt in ("json", "csv", "html", "md"):
        assert app_client.get(f"/api/projects/{pid}/export/{fmt}").status_code == 200


def test_error_message_user_friendly(app_client):
    r = app_client.get("/api/projects/prj_missing")
    assert r.status_code == 404
    assert "見つかりません" in r.json()["detail"]  # 技術用語でなく利用者向け説明
