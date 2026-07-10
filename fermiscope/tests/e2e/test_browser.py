"""実ブラウザでのE2Eテスト(Playwright)。

実行方法:
    pip install -e ".[e2e]"
    python -m playwright install chromium   # 既にブラウザがある環境では不要
    pytest -m e2e tests/e2e/test_browser.py

Playwright/ブラウザが無い環境では自動的にスキップされる。
"""

import contextlib
import multiprocessing
import os
import time

import pytest

pytestmark = pytest.mark.e2e

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="Playwright未インストールのためブラウザE2Eをスキップ"
)

PORT = 8779


def _run_server():
    os.environ["SEARCH_PROVIDER"] = "mock"
    os.environ["LLM_PROVIDER"] = "noop"
    os.environ["FERMISCOPE_DATABASE_URL"] = "sqlite:///:memory:"
    os.environ["FERMISCOPE_MC_ITERATIONS"] = "4000"
    import uvicorn

    from fermiscope.api.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="error")


@pytest.fixture(scope="module")
def server():
    proc = multiprocessing.get_context("spawn").Process(target=_run_server, daemon=True)
    proc.start()
    import httpx

    for _ in range(60):
        with contextlib.suppress(Exception):
            if httpx.get(f"http://127.0.0.1:{PORT}/api/config", timeout=1).status_code == 200:
                break
        time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip("テストサーバーが起動しませんでした")
    yield f"http://127.0.0.1:{PORT}"
    proc.terminate()


@pytest.fixture(scope="module")
def page(server):
    from playwright.sync_api import sync_playwright

    # Playwright標準配置 → 環境変数 CHROMIUM_PATH → 一般的な配置場所の順で探す
    candidates = [None, os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]
    with sync_playwright() as pw:
        browser = None
        last_error = None
        for exe in candidates:
            if exe is not None and not os.path.exists(exe):
                continue
            try:
                browser = pw.chromium.launch(executable_path=exe)
                break
            except Exception as exc:
                last_error = exc
        if browser is None:
            pytest.skip(f"Chromiumを起動できません: {last_error}")
        page = browser.new_page()
        yield page
        browser.close()


def test_create_project_and_view_results(server, page):
    page.goto(server)
    assert "FermiScope" in page.title()
    page.fill("#question", "東京都内にはピアノ調律師が何人いるか")
    page.click("#start-btn")
    # プロジェクトページへ遷移し、自動で調査が始まる
    page.wait_for_url("**/projects/**", timeout=15000)
    # 調査完了 → 結論カードに数値が表示される(最大90秒)
    page.wait_for_selector(".conclusion-card .big", timeout=90000)
    big = page.text_content(".conclusion-card .big")
    assert "人" in big

    # 図表タブにSVGチャートが描画される
    page.click('.tab[data-tab="charts"]')
    page.wait_for_selector("#chart-scenarios svg", timeout=5000)
    assert page.locator("#chart-histogram svg").count() == 1
    assert page.locator("#chart-tornado svg").count() == 1

    # パラメータタブで編集フォームが存在する
    page.click('.tab[data-tab="params"]')
    page.wait_for_selector("#param-list .card", timeout=5000)

    # 証拠一覧にクラスとスコアが表示される
    page.click('.tab[data-tab="evidence"]')
    page.wait_for_selector("#evidence-list table", timeout=5000)
