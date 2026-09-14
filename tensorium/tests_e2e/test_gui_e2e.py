"""Tensorium GUI のブラウザ E2E テスト（Playwright + 同梱サンプルデータ、外部ネットワーク不要）。

    python tests_e2e/test_gui_e2e.py [--shots DIR] [--headed]   # 終了コード 0=OK / 1=失敗 / 2=skip
    pytest -q tests_e2e/test_gui_e2e.py                          # Chromium 不在なら skip

確認項目:
 1. 初期表示（サンプル一覧・環境ピル）とコンソール/ページエラーがない
 2. サンプル CSV 読み込み → 行数・列カード・プレビューが表示される
 3. タスク設定 → 分割件数と目的変数の分布チャートが出る
 4. モデル選択 → ファミリー / ハイパーパラメータフォームが描画される
 5. 学習（torch があれば scratch を短時間、無ければベースライン）→ 完了ステータス
 6. 評価 → 指標タイルと混同行列 / 散布図
 7. 予測 → 手入力 1 件、ファイル一括（実測との比較）
 8. 履歴 → 一覧に実行が出る / 環境 → タイル表示
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []
LOGS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    line = f"[{'OK ' if cond else 'FAIL'}] {name} {detail}".rstrip()
    print(line)
    LOGS.append(line)
    if not cond:
        FAILED.append(name)


def _chromium_kwargs() -> dict:
    """同梱 Chromium を探す（PLAYWRIGHT_BROWSERS_PATH 配下、または既定）。"""
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    for pat in (f"{base}/chromium*/chrome-linux*/chrome", f"{base}/chromium*/chrome-linux*/headless_shell",
                f"{base}/chromium", "/opt/pw-browsers/chromium*/chrome-linux*/chrome"):
        for cand in sorted(glob.glob(pat), reverse=True):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return {"executable_path": cand}
    return {}


def run(shots: str | None = None, headed: bool = False, fmt: str = "png") -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright が未インストール（pip install playwright）")
        return 2

    # サーバは実運用と同じく別プロセスで起動（保存先はテスト用の一時フォルダ）
    import subprocess

    tmp = Path(tempfile.mkdtemp(prefix="tensorium-e2e-"))
    env = dict(os.environ, TENSORIUM_DATA_DIR=str(tmp / "data"), TENSORIUM_CONFIG_FILE=str(tmp / "config.json"),
               HF_HUB_OFFLINE="1", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", "0"], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    url = None
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if "http://127.0.0.1:" in line:
            url = "http://" + line.split("http://", 1)[1].split()[0]
            break
    if not url:
        proc.kill()
        print("FAIL: サーバが起動しません")
        return 1
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()   # ログを読み捨てる
    have_torch = False
    try:
        import torch  # noqa: F401
        have_torch = True
    except ImportError:
        pass
    errors: list[str] = []
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)

    def shot(page, name):
        if shots:
            page.screenshot(path=str(Path(shots) / f"{name}.{fmt}"), full_page=False,
                            **({"type": "jpeg", "quality": 82} if fmt == "jpg" else {}))

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=not headed, **_chromium_kwargs())
            except Exception as e:  # noqa: BLE001
                print(f"SKIP: Chromium を起動できません: {e}")
                return 2
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            ext_requests: list[str] = []
            page.on("request", lambda r: ext_requests.append(r.url) if not r.url.startswith(url) else None)

            # 1. 初期表示
            page.goto(url)
            page.wait_for_selector("#samples .sample")
            check("サンプル一覧が表示される", page.locator("#samples .sample").count() >= 2)
            page.wait_for_function("document.querySelector('#pill-env span:last-child').textContent !== '環境を確認中…'", timeout=30000)
            check("環境ピルが更新される", "確認中" not in page.locator("#pill-env").inner_text())
            shot(page, "01-data-empty")

            # 2. サンプル読み込み
            page.locator("#samples .sample[data-file='reviews_ja.csv']").click()
            page.wait_for_selector("#data-loaded:not(.hidden)")
            page.wait_for_function("document.querySelectorAll('#cols .col').length >= 6")
            check("行数タイル 600", "600" in page.locator("#ds-tiles").inner_text())
            check("列カード 6 枚", page.locator("#cols .col").count() == 6)
            check("プレビュー行", page.locator("#prev-tbl tbody tr").count() == 50)
            check("目的変数が自動選択", page.locator("#cols .col.is-target").count() == 1)
            shot(page, "02-data")

            # 3. タスク
            page.locator("[data-go='task']").first.click()
            page.wait_for_selector("#panel-task.active")
            page.wait_for_function("document.querySelector('#t-split').textContent.includes('学習')", timeout=15000)
            check("分割件数が表示される", "検証" in page.locator("#t-split").inner_text())
            check("タスクが分類", "active" in page.locator("#t-task button[data-v='classification']").get_attribute("class"))
            page.locator("#t-val").fill("20")
            page.locator("#t-val").dispatch_event("input")
            page.wait_for_function("document.querySelector('#t-val-v').textContent === '20%'")
            check("スライダー変更が反映", True)
            shot(page, "03-task")

            # 4. モデル
            page.locator("[data-go='model']").first.click()
            page.wait_for_selector("#panel-model.active")
            page.wait_for_selector("#fams .fam")
            check("ファミリーカード 5 枚", page.locator("#fams .fam").count() == 5)
            fam = "scratch" if have_torch else "baseline"
            page.locator(f"#fams .fam[data-fam='{fam}']").click()
            page.wait_for_function(f"document.querySelector('#fams .fam.on').dataset.fam === '{fam}'")
            if have_torch:
                page.wait_for_selector("[data-hp='epochs']")
                page.locator("[data-hp='epochs']").fill("3")
                page.locator("[data-hp='epochs']").dispatch_event("input")
                page.locator("[data-hp='d_model']").fill("32")
                page.locator("[data-hp='d_model']").dispatch_event("input")
                check("ハイパーパラメータフォーム", page.locator("[data-hp]").count() >= 5)
            page.locator("#m-name").fill("E2E テスト実行")
            check("学習ボタンが有効", not page.locator("#m-train").is_disabled())
            shot(page, "04-model")

            # 5. 学習
            page.locator("#m-train").click()
            page.wait_for_selector("#panel-train.active")
            page.wait_for_function("['完了','失敗','中止'].some(s => document.querySelector('#j-status').textContent.includes(s))", timeout=240000)
            status = page.locator("#j-status").inner_text()
            check("学習が完了", "完了" in status, status)
            check("ログが出ている", page.locator("#j-log div").count() >= 3)
            if have_torch:
                check("損失チャートが描画される", page.evaluate("(() => { const c = document.querySelector('#j-loss'); return c.width > 0 && c.height > 0; })()"))
            shot(page, "05-train")

            # 6. 評価
            page.locator("#j-eval").click()
            page.wait_for_selector("#panel-eval.active")
            page.wait_for_selector("#e-body .metric")
            check("指標タイル", "Accuracy" in page.locator("#e-body").inner_text())
            check("混同行列キャンバス", page.locator("#e-cm").count() == 1)
            page.locator("#e-split button[data-v='test']").click()
            page.wait_for_function("document.querySelector('#e-body').textContent.includes('テストデータ')", timeout=15000)
            check("テスト分割に切替", "テストデータ" in page.locator("#e-body").inner_text())
            shot(page, "06-eval")

            # 7. 予測
            page.locator("[data-go='predict']").first.click()
            page.wait_for_selector("#panel-predict.active")
            page.wait_for_selector("#p-form [data-col]")
            page.locator("#p-form textarea[data-col='レビュー本文']").fill("掃除機を購入。期待以上でした。買ってよかった。")
            page.locator("#p-form [data-col='価格']").fill("12000")
            page.locator("#p-form [data-col='購入回数']").fill("2")
            page.locator("#p-form [data-col='カテゴリ']").fill("家電")
            page.locator("#p-one").click()
            page.wait_for_selector("#p-one-res .result-hero", timeout=60000)
            check("手入力予測の結果", page.locator("#p-one-res .result-hero").inner_text().strip() != "")
            page.locator("#p-file").set_input_files(str(ROOT / "sample_data" / "reviews_ja.csv"))
            page.wait_for_selector("#p-res-card:not(.hidden)", timeout=120000)
            check("一括予測の結果表", page.locator("#p-tbl tbody tr").count() == 200)
            check("実測との比較", "正解率" in page.locator("#p-res-metric").inner_text())
            shot(page, "07-predict")

            # 8. 履歴 / 環境
            page.locator(".side-link[data-step='runs']").click()
            page.wait_for_selector("#panel-runs.active")
            page.wait_for_selector("#r-tbl tbody tr[data-id]")
            check("履歴に実行がある", page.locator("#r-tbl tbody tr[data-id]").count() >= 1)
            shot(page, "08-runs")
            page.locator(".side-link[data-step='env']").click()
            page.wait_for_selector("#env-tiles .tile")
            check("環境タイル", page.locator("#env-tiles .tile").count() >= 4)
            shot(page, "09-env")
            # ライトテーマ
            page.locator("#theme-toggle").click()
            check("ライトテーマ切替", page.evaluate("document.documentElement.dataset.theme") == "light")
            page.locator(".side-link[data-step='runs']").click()
            page.wait_for_timeout(500)                       # パネルのフェードイン完了を待つ
            shot(page, "10-light")

            check("外部ネットワーク要求がない", not ext_requests, "; ".join(ext_requests[:3]))
            check("コンソール / ページエラーがない", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILED:
        print(f"E2E FAILED: {FAILED}")
        return 1
    print("E2E ALL OK")
    return 0


def test_gui_e2e():
    import pytest
    code = run()
    if code == 2:
        pytest.skip("Playwright / Chromium が利用できません")
    assert code == 0, FAILED


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", help="スクリーンショット保存先")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--fmt", default="png", choices=["png", "jpg"], help="スクリーンショット形式")
    a = ap.parse_args()
    sys.exit(run(a.shots, a.headed, a.fmt))
