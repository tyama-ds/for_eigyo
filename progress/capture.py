#!/usr/bin/env python3
"""アプリの画面を撮って、進捗ダッシュボードに「画面イメージ」「修正箇所イメージ」として登録する。

    # 画面イメージ（現在の画面）
    python progress/capture.py --slug mycel --url http://127.0.0.1:8795 --name main \
        --caption "ノートの閲覧画面"

    # 修正箇所イメージ（変更した部分を赤枠で囲む。--highlight は CSS セレクタ、複数可）
    python progress/capture.py --slug mycel --url http://127.0.0.1:8795 --name graph-zoom \
        --kind change --highlight "#graph-panel .zoom" --caption "グラフに拡大ボタンを追加" --ref "#104"

    # 操作してから撮る（--click / --fill / --wait は書いた順に実行）
    python progress/capture.py --slug tensorium --url http://127.0.0.1:8740 --name augment \
        --click "text=データ拡張" --wait 800 --kind change --highlight ".augment-panel"

    # claudecode のプロジェクト
    python progress/capture.py --repo-root ../claudecode --slug deep_research_tool --url http://127.0.0.1:8802 ...

画像は <リポジトリ>/progress/images/<slug>/<日付>-<name>.jpg に保存し、
<リポジトリ>/progress/projects/<slug>.json の images の先頭に追記する（--no-record で追記しない）。
撮った後は python progress/build_dashboard.py でダッシュボードを作り直す。

必要なもの: pip install playwright pillow（ブラウザは playwright install chromium、
または --browser / 環境変数 CAPTURE_BROWSER で Chrome / Chromium の実行ファイルを指定）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FALLBACK_BROWSERS = ["/opt/pw-browsers/chromium"]
HIGHLIGHT = (220, 38, 38)  # 赤枠


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slug", required=True, help="プロジェクトの slug（progress/projects/<slug>.json）")
    ap.add_argument("--url", help="撮る画面の URL（--image を使う場合は不要）")
    ap.add_argument("--image", type=Path, help="撮影済みの画像を使う（PNG / JPEG）")
    ap.add_argument("--name", required=True, help="ファイル名に使う短い英数字（例: main, graph-zoom）")
    ap.add_argument("--kind", choices=["gui", "change"], default="gui",
                    help="gui=画面イメージ / change=修正箇所イメージ（既定: gui）")
    ap.add_argument("--caption", required=True, help="画像の説明（1行）")
    ap.add_argument("--ref", default="", help="関連する PR など（例: #104）")
    ap.add_argument("--highlight", action="append", default=[], metavar="SELECTOR",
                    help="赤枠で囲む要素の CSS / Playwright セレクタ（複数可）")
    ap.add_argument("--box", action="append", default=[], metavar="X,Y,W,H",
                    help="赤枠の位置を画像に対する %% で指定（複数可）")
    ap.add_argument("--click", action="append", default=[], metavar="SELECTOR", help="撮る前にクリックする要素")
    ap.add_argument("--fill", action="append", default=[], metavar="SELECTOR=TEXT", help="撮る前に入力する")
    ap.add_argument("--wait", action="append", default=[], metavar="MS|SELECTOR",
                    help="待つ（ミリ秒、または表示を待つセレクタ）")
    ap.add_argument("--size", default="1280x800", help="画面サイズ（既定: 1280x800）")
    ap.add_argument("--dark", action="store_true", help="ダークモードで撮る")
    ap.add_argument("--repo-root", type=Path, default=ROOT, help="対象リポジトリ（既定: for_eigyo）")
    ap.add_argument("--browser", default=os.environ.get("CAPTURE_BROWSER", ""), help="ブラウザの実行ファイル")
    ap.add_argument("--quality", type=int, default=80, help="JPEG の品質（既定: 80）")
    ap.add_argument("--no-record", action="store_true", help="進捗ファイルに追記しない")
    ap.add_argument("--out", type=Path, help="保存先を指定する（既定: progress/images/<slug>/<日付>-<name>.jpg）")
    a = ap.parse_args(argv)
    if not a.url and not a.image:
        ap.error("--url か --image のどちらかが必要です")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", a.name):
        ap.error("--name は英数字・ハイフン・アンダースコアで指定してください")
    if a.kind == "change" and not (a.highlight or a.box):
        ap.error("--kind change では --highlight か --box で修正箇所を指定してください")
    return a


def ordered_actions(argv: list[str]) -> list[tuple[str, str]]:
    """--click / --fill / --wait を書かれた順に並べる。"""
    acts, it = [], iter(argv)
    for tok in it:
        for opt in ("--click", "--fill", "--wait"):
            if tok == opt:
                acts.append((opt[2:], next(it, "")))
            elif tok.startswith(opt + "="):
                acts.append((opt[2:], tok.split("=", 1)[1]))
    return acts


def launch(pw, browser_path: str):
    try:
        return pw.chromium.launch(executable_path=browser_path) if browser_path else pw.chromium.launch()
    except Exception:
        if browser_path:
            raise
        for path in FALLBACK_BROWSERS:
            if Path(path).exists():
                return pw.chromium.launch(executable_path=path)
        raise


def shoot(a, actions) -> tuple[bytes, list[tuple[float, float, float, float]]]:
    """画面を撮り、PNG と赤枠の位置（画像に対する比率 0〜1）を返す。"""
    from playwright.sync_api import sync_playwright

    w, h = (int(v) for v in a.size.lower().split("x"))
    with sync_playwright() as pw:
        browser = launch(pw, a.browser)
        page = browser.new_page(viewport={"width": w, "height": h}, color_scheme="dark" if a.dark else "light")
        page.goto(a.url, wait_until="networkidle", timeout=30000)
        for kind, arg in actions:
            if kind == "click":
                page.click(arg, timeout=10000)
            elif kind == "fill":
                sel, _, text = arg.partition("=")
                page.fill(sel, text, timeout=10000)
            elif kind == "wait":
                if arg.isdigit():
                    page.wait_for_timeout(int(arg))
                else:
                    page.wait_for_selector(arg, timeout=15000)
        page.wait_for_timeout(300)
        boxes = []
        for sel in a.highlight:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=10000)
            if len(a.highlight) == 1:  # 固定ヘッダーに隠れないよう画面の中央に寄せる
                loc.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
                page.wait_for_timeout(300)
            b = loc.bounding_box()
            if not b:
                browser.close()
                sys.exit(f"--highlight の要素が見つからないか、表示されていません: {sel}")
            boxes.append((b["x"] / w, b["y"] / h, b["width"] / w, b["height"] / h))
        png = page.screenshot()
        browser.close()
    return png, boxes


def draw_boxes(img, boxes):
    from PIL import ImageDraw

    img = img.convert("RGB")
    W, H = img.size
    d = ImageDraw.Draw(img)
    stroke = max(3, round(W / 400))
    pad = stroke * 2
    for x, y, bw, bh in boxes:
        x0, y0 = max(0, x * W - pad), max(0, y * H - pad)
        x1, y1 = min(W - 1, (x + bw) * W + pad), min(H - 1, (y + bh) * H + pad)
        d.rounded_rectangle([x0, y0, x1, y1], radius=stroke * 3, outline=HIGHLIGHT, width=stroke)
    return img


def record(a, rel: str, today: str) -> Path:
    pfile = a.repo_root / "progress" / "projects" / f"{a.slug}.json"
    p = json.loads(pfile.read_text(encoding="utf-8"))
    entry = {"file": rel, "kind": a.kind, "caption": a.caption, "date": today}
    if a.ref:
        entry["ref"] = a.ref
    images = [i for i in p.get("images", []) if i.get("file") != rel]
    p["images"] = [entry] + images
    pfile.write_text(json.dumps(p, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return pfile


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    a = parse_args(argv)
    from PIL import Image

    a.repo_root = a.repo_root.resolve()
    pfile = a.repo_root / "progress" / "projects" / f"{a.slug}.json"
    if not a.no_record and not pfile.exists():
        sys.exit(f"進捗ファイルがありません: {pfile}")
    today = dt.date.today().isoformat()

    if a.image:
        img = Image.open(a.image)
        boxes = []
    else:
        png, boxes = shoot(a, ordered_actions(argv))
        img = Image.open(io.BytesIO(png))
    for spec in a.box:
        x, y, bw, bh = (float(v) / 100 for v in spec.split(","))
        boxes.append((x, y, bw, bh))
    if boxes:
        img = draw_boxes(img, boxes)

    rel = f"images/{a.slug}/{today}-{a.name}.jpg"
    out = a.out or a.repo_root / "progress" / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, "JPEG", quality=a.quality, optimize=True, progressive=True)
    print(f"保存しました: {out}（{out.stat().st_size // 1024} KB）")
    if not a.no_record and not a.out:
        print(f"進捗ファイルに追記しました: {record(a, rel, today)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
