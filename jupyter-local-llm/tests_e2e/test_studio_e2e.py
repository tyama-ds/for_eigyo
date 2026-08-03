"""Studio GUI のブラウザ E2E テスト（Playwright + モックLLM/埋め込み）。

実行方法（要 `pip install playwright` + Chromium）::

    python tests_e2e/test_studio_e2e.py
    # または
    pytest -q tests_e2e/test_studio_e2e.py

確認項目（仕様の GUI 必須確認項目）:
 1. 単一ファイルの明示タグが一覧に表示される
 2. 複数タグ選択時の対象件数表示と検索結果の文書数が一致する（AND 条件）
 3. フォルダ取り込み中に ETA（残り時間/計算中）が表示される
 4. グラフ未構築・構築済み・部分グラフ・失敗 の4状態を表示できる
 5. グラフのノードを選択すると根拠（セクション/ページ/抜粋/元ファイル）が出る
 6. 100ノード程度でも描画・操作できる
 7. 外部ネットワーク要求が発生しない（127.0.0.1 のみ）
 8. コンソール/ページエラーがない

外部 LLM サーバ・外部 CDN は使わない。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

PORT = 8931
FAILED: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILED.append(name)


def _setup_env(tmp: Path):
    """モック注入 + テスト用文書・グラフ状態の準備。"""
    from llama_index.core import Settings as LI
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    LI.embed_model = MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None
    import llmlab.config as cfg

    cfg.configure(base_url="http://127.0.0.1:1/v1", api_key="mock", model="mock",
                  embed_model="mock")

    root = tmp / "storage"
    docs = tmp / "docs"
    docs.mkdir(parents=True)
    for i in range(3):
        (docs / f"規程{i:02d}.txt").write_text(
            f"# 第1章\n規程{i:02d}の本文。時間外手当は125%。" * 15, encoding="utf-8")

    import json

    import llmlab.bookindex as bx
    from llmlab.bookindex import Entity
    from llmlab.indexmanager import IndexManager

    im = IndexManager(storage_dir=root / "index")

    # (a) グラフ未構築の fast 文書
    plain = tmp / "plain.txt"
    plain.write_text("# 章\n未構築文書の本文。" * 15, encoding="utf-8")
    im.add_document(plain, title="未構築文書")

    # (b) グラフ失敗状態の文書
    failp = tmp / "failed.txt"
    failp.write_text("# 章\n失敗文書の本文。" * 15, encoding="utf-8")
    fid = im.add_document(failp, title="失敗文書")["doc_id"]
    meta = im._read(im.docs_dir, fid)
    meta.update(index_mode="graph", graph_status="failed",
                graph_error="JSONDecodeError: モデルがJSONを返しません")
    im._write(im.docs_dir, fid, meta)

    # (c) 部分グラフの文書（100 エンティティのチェーン。手組みで永続化）
    def _graph_doc(title, fname, n_ent, *, complete):
        p = tmp / fname
        p.write_text(f"# 第1章 概要\n{title}の本文。" * 15, encoding="utf-8")
        did = im.add_document(p, title=title)["doc_id"]
        bi = bx.BookIndex()
        rt = bi.add_node(type="Section", content="", book=title, title=title, level=0)
        bi.roots.append(rt.id)
        sec = bi.add_node(type="Section", content="", book=title,
                          title="第2章 製品概要", level=1, parent=rt.id)
        rt.children.append(sec.id)
        types = ["Organization", "Product", "Person"]
        for i in range(n_ent):
            nd = bi.add_node(type="Text", book=title, parent=sec.id, page=i + 1,
                             content=f"エンティティ{i}の根拠となる本文抜粋。")
            sec.children.append(nd.id)
            bi.entities[i] = Entity(id=i, name=f"要素{i:03d}", type=types[i % 3],
                                    description=f"説明{i}", origin_nodes=[nd.id])
            if i:
                bi.relations.append((i - 1, i, "関連する"))
        bi._ent_seq = n_ent
        bdir = root / "index" / "bookindex" / did
        bi.persist(bdir)
        stats = {"eligible_nodes": n_ent * 2 if not complete else n_ent,
                 "sampled_nodes": n_ent, "processed_nodes": n_ent,
                 "graph_coverage_ratio": 0.5 if not complete else 1.0,
                 "graph_is_complete": complete,
                 "graph_max_nodes_used": n_ent, "resumed_from_checkpoint": False,
                 "extract_ok": n_ent - 2, "extract_empty": 1,
                 "extract_badjson": 1, "extract_error": 0}
        (bdir / "graph_stats.json").write_text(json.dumps(stats), encoding="utf-8")
        m = im._read(im.docs_dir, did)
        m.update(index_mode="graph", graph_status="ready", graph_index=True,
                 hierarchy_status="ready")
        for k, v in stats.items():
            m[k] = v
        im._write(im.docs_dir, did, m)
        return did

    _graph_doc("部分グラフ文書", "partial.txt", 12, complete=False)
    _graph_doc("大規模グラフ文書", "big.txt", 100, complete=True)

    # フォルダ取り込みの ETA を観測できるよう、1文書あたり最低 0.4 秒かける
    orig_add = IndexManager.add_document

    def slow_add(self, *a, **k):
        time.sleep(0.4)
        return orig_add(self, *a, **k)

    IndexManager.add_document = slow_add
    return root, docs


def run() -> int:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="studio_e2e_"))
    root, docs = _setup_env(tmp)

    from http.server import ThreadingHTTPServer

    import llmlab.app as appmod

    handler = type("H", (appmod._Handler,), {"root_dir": str(root)})
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"

    from playwright.sync_api import sync_playwright

    exe = "/opt/pw-browsers/chromium"
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(executable_path=exe) if Path(exe).exists() \
                else pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            print(f"SKIP: Chromium を起動できません（{e}）")
            return 0
        pg = b.new_page(viewport={"width": 1480, "height": 980})
        errors: list[str] = []
        ext_requests: list[str] = []
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console.error: {m.text}")
              if m.type == "error" else None)
        pg.on("request", lambda r: ext_requests.append(r.url)
              if not r.url.startswith(url) else None)

        pg.goto(url)
        pg.wait_for_load_state("networkidle")
        pg.fill("#root", str(root))
        pg.click('.vtab[data-v="docs"]')
        pg.wait_for_selector("#docsView.on")
        pg.wait_for_selector("#docRows tr")

        # --- 3. フォルダ取り込み中に ETA が表示される -----------------------
        pg.fill("#d_path", str(docs))
        pg.fill("#d_tags", "規程, 2024")
        pg.click("#btnDocAdd")
        stage_texts = []
        for _ in range(120):     # 最大 ~6 秒ポーリング
            t = pg.locator("#dstage").inner_text()
            if t:
                stage_texts.append(t)
            if "完了" in t or pg.locator("#dMsg.ok").count():
                break
            pg.wait_for_timeout(50)
        pg.wait_for_selector("#dMsg.ok", timeout=60000)
        check("フォルダ取り込み成功", "追加 3" in pg.locator("#dMsg").inner_text(),
              pg.locator("#dMsg").inner_text())
        check("取り込み中にETA表示",
              any(("残り" in t and "概算" in t) or "残り時間を計算中" in t
                  for t in stage_texts),
              " | ".join(stage_texts[-3:]))

        pg.wait_for_selector("#docRows tr td .modechip", timeout=15000)

        # --- 1. 単一ファイルの明示タグが表示される ---------------------------
        pg.fill("#d_path", str(docs.parent / "single.txt"))
        (docs.parent / "single.txt").write_text("# 章\n単独文書の本文。" * 15,
                                                encoding="utf-8")
        pg.fill("#d_tags", "単独タグ")
        pg.click("#btnDocAdd")
        pg.wait_for_selector("#dMsg.ok", timeout=60000)
        body = pg.locator("#docRows").inner_text()
        check("単一ファイルのタグ表示", "単独タグ" in body, body[:120])

        # --- 2. 複数タグ選択の件数とヒット数の一致（AND） --------------------
        pg.wait_for_selector(".scopechip.tag")
        pg.locator(".scopechip.tag", has_text="規程").first.click()
        pg.locator(".scopechip.tag", has_text="2024").first.click()
        hint = pg.locator("#s_hint").inner_text()
        import re as _re

        mnum = _re.search(r"絞り込み中: (\d+) 文書", hint)
        check("スコープ件数ヒント表示", bool(mnum), hint)
        n_hint = int(mnum.group(1)) if mnum else -1
        pg.select_option("#s_act", "search")
        pg.fill("#s_q", "時間外手当")
        pg.click("#btnDocSearch")
        pg.wait_for_selector("#docSearchRes .docgrp", timeout=60000)
        n_hit = pg.locator("#docSearchRes .docgrp").count()
        check("複数タグの件数と検索結果が一致", n_hit == n_hint == 3,
              f"hint={n_hint} hits={n_hit}")
        pg.locator(".scopechip.tag", has_text="規程").first.click()
        pg.locator(".scopechip.tag", has_text="2024").first.click()

        # --- 4. グラフ4状態の表示 -------------------------------------------
        def open_detail(title):
            row = pg.locator("#docRows tr", has_text=title).first
            row.locator('button[data-act="detail"]').click()
            pg.wait_for_selector("#docDetail .dtabs")

        def graph_tab():
            pg.locator('#docDetail .dtab[data-t="graph"]').click()
            pg.wait_for_timeout(400)

        open_detail("未構築文書")
        graph_tab()
        check("未構築: グラフを構築ボタン",
              pg.locator("#gBuildBtn", has_text="グラフを構築").count() >= 1)

        open_detail("失敗文書")
        graph_tab()
        gtxt = pg.locator("#dt_graph").inner_text()
        check("失敗: 通常RAG利用可+再開ボタン",
              "通常RAG検索はそのまま利用できます" in gtxt
              and pg.locator("#gBuildBtn", has_text="グラフ再開").count() >= 1, gtxt[:120])

        open_detail("部分グラフ文書")
        graph_tab()
        gtxt = pg.locator("#dt_graph").inner_text()
        check("部分グラフ: カバレッジ警告", "部分グラフ" in gtxt and "50%" in gtxt,
              gtxt[:160])
        check("抽出内訳の表示", "JSON不正" in gtxt, gtxt[:200])
        check("グラフSVG描画", pg.locator("#gsvgbox svg .gnode").count() >= 10)

        # --- 5. ノード選択で根拠表示 -----------------------------------------
        pg.locator("#gsvgbox .gnode").first.click()
        panel = pg.locator("#gdPanel").inner_text()
        check("ノード選択で根拠パネル",
              "根拠" in panel and "§" in panel and "本文抜粋" in panel, panel[:160])
        check("根拠に元ファイル", "↳" in panel)
        # エンティティ一覧/関係一覧タブ
        pg.locator('#docDetail .dtab[data-t="entities"]').click()
        check("エンティティ一覧タブ",
              pg.locator("#dt_entities .gtbl tbody tr").count() >= 10)
        pg.locator('#docDetail .dtab[data-t="relations"]').click()
        check("関係一覧タブ", pg.locator("#dt_relations .gtbl tbody tr").count() >= 5)

        # --- 6. 100ノードでも操作可能 ----------------------------------------
        open_detail("大規模グラフ文書")
        t0 = time.time()
        graph_tab()
        pg.wait_for_selector("#gsvgbox svg", timeout=20000)
        n_nodes = pg.locator("#gsvgbox .gnode").count()
        dt = time.time() - t0
        check("100ノード描画", n_nodes == 100, f"nodes={n_nodes} {dt:.1f}s")
        check("描画が10秒以内", dt < 10, f"{dt:.1f}s")
        pg.locator("#gsvgbox .gnode").nth(50).click()
        check("100ノードでも選択が効く",
              "要素" in pg.locator("#gdPanel").inner_text())
        # ズーム操作してもエラーにならない
        pg.locator("#gsvgbox").hover()
        pg.mouse.wheel(0, -240)
        pg.wait_for_timeout(200)

        # --- 7/8. 外部リクエストなし・コンソールエラーなし -------------------
        check("外部ネットワーク要求なし", not ext_requests, str(ext_requests[:3]))
        real = [e for e in errors if "favicon" not in e]
        check("コンソール/ページエラーなし", not real, str(real[:3]))
        b.close()
    httpd.shutdown()
    print("\n" + ("E2E ALL OK" if not FAILED else f"E2E FAILED: {FAILED}"))
    return 1 if FAILED else 0


def test_studio_gui_e2e():
    """pytest から呼ぶ場合のエントリポイント（playwright が無ければ skip）。"""
    import pytest

    pytest.importorskip("playwright")
    assert run() == 0, f"E2E 失敗: {FAILED}"


if __name__ == "__main__":
    sys.exit(run())
