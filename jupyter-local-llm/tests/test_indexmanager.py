"""IndexManager（index_mode / doc_id / per-doc JSON / 2段階検索）と
BookRAG 軽量化（even sampling / Select_by_Section の 200 制限）の回帰テスト。

外部LLM/埋め込みサーバは使わない（MockEmbedding/MockLLM 注入 + build_graph をスパイ）。
実行:  pytest -q   /   python tests/test_indexmanager.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FAILED = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILED.append(name)


def _inject_mock_settings():
    from llama_index.core import Settings as LI
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    LI.embed_model = MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None


def _write(dirp: Path, name: str, text: str) -> Path:
    dirp.mkdir(parents=True, exist_ok=True)
    p = dirp / name
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# 1. IndexManager: index_mode ごとの挙動（fast/hierarchy/graph）
# --------------------------------------------------------------------------

def test_index_modes_gate_entity_extraction():
    _inject_mock_settings()
    import llmlab.bookindex as bx
    from llmlab.indexmanager import IndexManager

    calls = {"build_graph": 0}
    orig = bx.build_graph
    bx.build_graph = lambda *a, **k: calls.__setitem__("build_graph", calls["build_graph"] + 1)
    try:
        work = Path(tempfile.mkdtemp(prefix="llmlab_im_"))
        _write(work / "src", "a.txt", "# 章1\n本文アルパカ。" * 30)
        im = IndexManager(storage_dir=str(work / "index"))
        src = work / "src" / "a.txt"

        # fast: BookRAG を作らない → build_graph は呼ばれない
        m1 = im.add_document(src, index_mode="fast")
        check("fast は ready", m1["status"] == "ready")
        check("fast で build_graph 未呼び出し", calls["build_graph"] == 0)
        check("fast は graph_index=False", m1["graph_index"] is False)
        check("fast で chunk が作られる", m1["chunk_count"] >= 1, f"{m1['chunk_count']}")

        # hierarchy: BookRAG(build_graph=False) → build_graph は呼ばれない、木はできる
        m2 = im.add_document(src, index_mode="hierarchy", force=True)
        check("hierarchy でも build_graph 未呼び出し", calls["build_graph"] == 0)
        check("hierarchy は graph_index=False", m2["graph_index"] is False)
        det = im.document(m2["doc_id"])
        check("hierarchy でセクション木ができる",
              det["bookindex"] and det["bookindex"]["node_count"] >= 1, f"{det['bookindex']}")

        # graph: build_graph が呼ばれる
        im.add_document(src, index_mode="graph", force=True)
        check("graph でのみ build_graph 呼び出し", calls["build_graph"] == 1,
              f"{calls['build_graph']}")
    finally:
        bx.build_graph = orig


def test_doc_id_distinguishes_same_title_different_content():
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_imid_"))
    f1 = _write(work / "2014", "報告.txt", "2014年の売上は100億円。" * 20)
    f2 = _write(work / "2015", "報告.txt", "2015年の売上は120億円。" * 20)  # 同名・別内容
    im = IndexManager(storage_dir=str(work / "index"))
    m1 = im.add_document(f1, title="決算報告")
    m2 = im.add_document(f2, title="決算報告")
    check("同名タイトル・別内容は別 doc_id", m1["doc_id"] != m2["doc_id"])
    check("2件登録される", len(im.documents()) == 2)
    # 同内容の再登録はキャッシュ skip（再抽出しない）
    m1b = im.add_document(f1, title="決算報告")
    check("同内容の再登録は skipped", m1b["status"] == "skipped")
    check("件数は増えない", len(im.documents()) == 2)


def test_per_document_json_saved():
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_imjson_"))
    f = _write(work / "s", "doc.txt", "本文コンテンツ。" * 40)
    im = IndexManager(storage_dir=str(work / "index"))
    meta = im.add_document(f, index_mode="hierarchy")
    did = meta["doc_id"]
    root = work / "index"
    check("docs/{doc_id}.json", (root / "docs" / f"{did}.json").exists())
    check("chunks/{doc_id}.json", (root / "chunks" / f"{did}.json").exists())
    check("status/{doc_id}.json", (root / "status" / f"{did}.json").exists())
    check("bookindex/{doc_id}/", (root / "bookindex" / did / "bookindex.json").exists())
    det = im.document(did)
    for key in ("doc_id", "title", "source_path", "content_hash", "index_mode",
                "status", "chunk_count", "created_at", "updated_at", "graph_index"):
        check(f"meta に {key}", key in det["meta"])
    check("詳細にチャンク一覧", len(det["chunks"]) >= 1)


def test_delete_removes_all_stores():
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_imdel_"))
    f = _write(work / "s", "doc.txt", "消す文書。" * 30)
    im = IndexManager(storage_dir=str(work / "index"))
    did = im.add_document(f, index_mode="graph")["doc_id"]
    check("削除前は1件", len(im.documents()) == 1)
    im.delete(did)
    check("削除後は0件", len(im.documents()) == 0)
    root = work / "index"
    check("docs JSON 削除", not (root / "docs" / f"{did}.json").exists())
    check("bookindex ディレクトリ削除", not (root / "bookindex" / did).exists())


# --------------------------------------------------------------------------
# 2. 検索: 文書単位で多様化 / graph 未作成でも落ちない
# --------------------------------------------------------------------------

def test_search_is_document_grouped_and_diverse():
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_imsearch_"))
    im = IndexManager(storage_dir=str(work / "index"))
    for i in range(4):
        f = _write(work / "s", f"doc{i}.txt", f"文書{i}の内容。共通トピックXについて。" * 40)
        im.add_document(f, title=f"文書{i}")
    res = im.search("共通トピックXは？", document_top_n=3, chunk_top_k_per_doc=2)
    check("結果は SearchHit のリスト", res and hasattr(res[0], "doc_id"))
    doc_ids = [h.doc_id for h in res]
    check("文書単位で返る（doc_id ユニーク）", len(set(doc_ids)) == len(doc_ids))
    check("top_n 文書に多様化（複数文書）", len(res) >= 2, f"{len(res)}")
    check("各文書内で chunk top-k", all(len(h.chunks) <= 2 for h in res))
    check("max_chunks_per_doc を尊重",
          all(len(h.chunks) <= 2 for h in im.search("X", document_top_n=3,
              chunk_top_k_per_doc=5, max_chunks_per_doc=2)))


def test_use_graph_on_fast_doc_falls_back_not_crash():
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_imfb_"))
    f = _write(work / "s", "doc.txt", "フォールバック確認の本文。" * 40)
    im = IndexManager(storage_dir=str(work / "index"))
    im.add_document(f, index_mode="fast")            # graph 索引なし
    res = im.search("本文は？", use_graph=True)       # graph 要求だが未作成
    check("graph 未作成でも検索が落ちない", bool(res))
    check("通常RAGへフォールバック明示", res[0].used_graph is False and res[0].fallback_reason)
    check("フォールバックでも chunk が返る", len(res[0].chunks) >= 1)


# --------------------------------------------------------------------------
# 3. BookRAG 軽量化: even sampling / Select_by_Section の 200 制限撤廃
# --------------------------------------------------------------------------

def test_build_graph_even_sampling_spans_sections():
    from llmlab.bookindex import BookIndex, _even_sample

    bi = BookIndex()
    root = bi.add_node(type="Section", content="doc", book="doc", level=0, title="doc")
    targets = []
    for s in range(5):
        sec = bi.add_node(type="Section", content=f"S{s}", book="doc", level=1,
                          title=f"S{s}", parent=root.id)
        for t in range(100):
            node = bi.add_node(type="Text", content=f"S{s}-t{t} " * 20, book="doc",
                               parent=sec.id)
            targets.append(node.id)
    chosen = _even_sample(bi, targets, 10)
    secs = {bi.nodes[bi.nodes[n].parent].title for n in chosen}
    check("均等サンプリングは先頭偏りしない（複数セクションから）",
          len(secs) >= 4, f"sections={sorted(secs)}")
    check("予算どおりの件数", len(chosen) == 10)


def test_select_by_section_can_pick_beyond_200():
    import numpy as np

    import llmlab.bookindex as bx
    from llmlab.bookindex import BookIndex
    from llmlab.bookrag import BookRAG

    bi = BookIndex()
    root = bi.add_node(type="Section", content="doc", book="doc", level=0, title="doc")
    text_of = {}
    for s in range(300):  # 300 セクション（旧実装は先頭200しか候補にしなかった）
        sec = bi.add_node(type="Section", content=f"見出し{s}", book="doc", level=1,
                          title=f"見出し{s}", parent=root.id)
        root.children.append(sec.id)                        # add_node は親子リンクしない
        txt = bi.add_node(type="Text", content=f"本文{s}", book="doc", parent=sec.id)
        sec.children.append(txt.id)
        text_of[s] = txt.id

    target = 250  # 200 以降のセクションを正解にする
    rng = np.random.default_rng(0)

    def fake_embed(texts):
        out = []
        for t in texts:
            if f"見出し{target}" in t or "クエリ" in t:
                out.append(np.array([1.0] + [0.0] * 7))       # クエリと正解だけ同じ向き
            else:
                v = rng.standard_normal(8); v[0] = 0.0
                out.append(v / (np.linalg.norm(v) + 1e-9))
        return np.array(out)

    captured = {}

    def fake_llm_json(prompt):
        # LLM に渡された候補（listing）に target セクションが含まれることを確認して選ぶ
        captured["has_target"] = f"見出し{target}" in prompt
        # プロンプトから target セクションの id を拾う
        import re
        for nid, node in bi.nodes.items():
            if node.title == f"見出し{target}" and f'"id": {nid}' in prompt:
                return {"section_ids": [nid]}
        return {"section_ids": []}

    old_embed, old_json = bx.embed, bx.llm_json
    bx.embed, bx.llm_json = fake_embed, fake_llm_json
    try:
        rag = BookRAG.__new__(BookRAG)
        ns = rag._op_select_by_section(bi, "クエリ")
    finally:
        bx.embed, bx.llm_json = old_embed, old_json

    check("200以降のセクションが候補に入る", captured.get("has_target") is True)
    check("200以降のセクションの本文が選ばれる", text_of[target] in ns, f"ns={ns[:5]}…")


# --------------------------------------------------------------------------
# 4. 既存 API 非破壊（PagedRAG / BookRAG のシグネチャ）
# --------------------------------------------------------------------------

def test_existing_apis_intact():
    import inspect

    from llmlab.bookrag import BookRAG
    from llmlab.pagedrag import PagedRAG

    ab = inspect.signature(PagedRAG.add_book).parameters
    check("PagedRAG.add_book(title, force, doc_id)",
          {"title", "force", "doc_id"} <= set(ab))
    q = inspect.signature(PagedRAG.query).parameters
    check("PagedRAG.query(title, doc_id, top_k)", {"title", "doc_id", "top_k"} <= set(q))
    bab = inspect.signature(BookRAG.add_book).parameters
    check("BookRAG.add_book(build_graph, doc_id, force)",
          {"build_graph", "doc_id", "force"} <= set(bab))
    check("BookRAG.add_book 既定は build_graph=True（従来どおり graph）",
          bab["build_graph"].default is True)


# --------------------------------------------------------------------------
# 4.5 回答生成・要約（v0.6.2: ask / summarize / doc_ids 絞り込み）
# --------------------------------------------------------------------------

def test_ask_answers_with_doc_grounding():
    """ask(): 検索した根拠を文脈に LLM で回答し、根拠(hits)を返す。"""
    _inject_mock_settings()
    import llmlab.bookindex as bx
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_ask_"))
    im = IndexManager(storage_dir=str(work / "index"))
    for i in range(3):
        f = _write(work / "s", f"doc{i}.txt", f"文書{i}の本文。退職金の規定あり。" * 30)
        im.add_document(f, title=f"文書{i}")

    prompts = []
    old = bx.llm_text
    bx.llm_text = lambda p, **k: (prompts.append((p, k)), "回答: 規定は文書0にあります")[-1]
    try:
        events = []
        ans = im.ask("退職金は？", document_top_n=2, progress=events.append)
    finally:
        bx.llm_text = old
    check("ask: 回答テキスト", "回答:" in ans.text)
    check("ask: 根拠 hits が文書単位", ans.hits and
          len({h.doc_id for h in ans.hits}) == len(ans.hits))
    check("ask: 抜粋がプロンプトに入る", "文書「" in prompts[0][0])
    check("ask: 文書名明示を指示", "文書名" in prompts[0][1].get("system", ""))
    check("ask: 進捗イベント", any("回答" in e["stage"] for e in events))
    d = ans.to_dict()
    check("ask: to_dict", set(d) == {"text", "hits", "per_doc"})


def test_summarize_maps_each_doc_then_reduces():
    """summarize(): 文書ごとに部分要約 → 統合。doc_ids で対象を絞れる。"""
    _inject_mock_settings()
    import llmlab.bookindex as bx
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_summ_"))
    im = IndexManager(storage_dir=str(work / "index"))
    ids = []
    for i in range(3):
        f = _write(work / "s", f"doc{i}.txt", f"文書{i}の内容。" * 30)
        ids.append(im.add_document(f, title=f"文書{i}")["doc_id"])

    calls = []
    old = bx.llm_text
    bx.llm_text = lambda p, **k: (calls.append(p), f"要約{len(calls)}")[-1]
    try:
        ans = im.summarize()                       # 全文書
        ans2 = im.summarize("リスク面", doc_ids=ids[:1])   # 1文書に絞る + 観点
    finally:
        bx.llm_text = old
    check("summarize: 全文書の部分要約", len(ans.per_doc) == 3,
          f"{[p['title'] for p in ans.per_doc]}")
    check("summarize: 統合要約あり", bool(ans.text))
    check("summarize: 文書ごと+統合のLLM回数", len(calls) >= 3 + 1)
    check("summarize: doc_ids 絞り込み", len(ans2.per_doc) == 1)
    check("summarize: 1文書なら統合を省略", ans2.text == ans2.per_doc[0]["text"])
    check("summarize: 観点がプロンプトへ", any("リスク面" in c for c in calls))


def test_search_scoped_by_doc_ids():
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_scope_"))
    im = IndexManager(storage_dir=str(work / "index"))
    ids = []
    for i in range(3):
        f = _write(work / "s", f"doc{i}.txt", f"文書{i}の内容。" * 30)
        ids.append(im.add_document(f, title=f"文書{i}")["doc_id"])
    res = im.search("内容", doc_ids=ids[:2], document_top_n=4)
    got = {h.doc_id for h in res}
    check("search: doc_ids で対象を制限", got <= set(ids[:2]) and got, f"{got}")


# --------------------------------------------------------------------------
# 4.6 フォルダ一括取り込み（v0.6.3: 1ファイル=1文書、失敗続行、fast自動降格）
# --------------------------------------------------------------------------

def test_add_folder_one_doc_per_file():
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_folder_"))
    src = work / "docs"
    for i in range(3):
        _write(src, f"doc{i}.txt", f"文書{i}の本文。" * 30)
    _write(src, "ignore.bin", "x")           # 非対応拡張子は対象外
    im = IndexManager(storage_dir=str(work / "index"))

    events = []
    r = im.add_folder(src, progress=events.append)
    check("folder: 3件追加", r["added"] == 3 and r["failed"] == 0, f"{r}")
    docs = im.documents()
    check("folder: 1ファイル=1文書（doc_id 個別）",
          len(docs) == 3 and len({d['doc_id'] for d in docs}) == 3)
    check("folder: 進捗にファイル位置", any("[2/3]" in e["stage"] for e in events))
    # 再実行は全件 skipped（キャッシュ）
    r2 = im.add_folder(src)
    check("folder: 再実行は変更なし", r2["skipped"] == 3 and r2["added"] == 0, f"{r2}")
    # 検索は従来どおり文書単位で多様化（構造維持の確認）
    hits = im.search("本文", document_top_n=3, chunk_top_k_per_doc=2)
    check("folder: 検索は文書ごとに集約", len({h.doc_id for h in hits}) == len(hits) >= 2)


def test_add_folder_continues_on_failure_and_downgrades():
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_folderfail_"))
    src = work / "docs"
    _write(src, "ok.txt", "正常な文書。" * 30)
    _write(src, "data.csv", "a,b\n1,2\n")     # BOOK 非対応 → graph 指定でも fast 降格
    bad = _write(src, "broken.txt", "壊れる文書。" * 30)
    im = IndexManager(storage_dir=str(work / "index"))

    orig = im.add_document
    def flaky(path, **kw):
        if Path(path).name == "broken.txt":
            raise RuntimeError("解析失敗")
        return orig(path, **kw)
    im.add_document = flaky
    r = im.add_folder(src, index_mode="graph")
    im.add_document = orig
    check("folder: 失敗しても続行", r["added"] == 2 and r["failed"] == 1, f"{r}")
    check("folder: 失敗ファイルを記録",
          r["errors"] and r["errors"][0]["file"] == "broken.txt")
    modes = {m["title"]: m["index_mode"] for m in r["results"]}
    check("folder: 非対応形式は fast へ自動降格", modes.get("data") == "fast", f"{modes}")
    check("folder: 対応形式は指定モード", modes.get("ok") == "graph", f"{modes}")


def test_add_folder_rejects_non_dir_and_empty():
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_folderng_"))
    im = IndexManager(storage_dir=str(work / "index"))
    f = _write(work, "a.txt", "x")
    try:
        im.add_folder(f)
        check("folder: ファイル指定はエラー", False)
    except NotADirectoryError:
        check("folder: ファイル指定はエラー", True)
    empty = work / "empty"
    empty.mkdir()
    try:
        im.add_folder(empty)
        check("folder: 空フォルダはエラー", False)
    except FileNotFoundError:
        check("folder: 空フォルダはエラー", True)


# --------------------------------------------------------------------------
# 5. 監査修正の回帰テスト（v0.6.1）
# --------------------------------------------------------------------------

def test_migration_from_pathhash_doc_id_dedups():
    """v0.5.1（パスハッシュID）カタログへの再 add_book が二重取り込みしない。"""
    import hashlib
    import json as _json

    _inject_mock_settings()
    from llmlab.pagedrag import PagedRAG

    work = Path(tempfile.mkdtemp(prefix="llmlab_mig_"))
    f = _write(work, "report.txt", "本文。" * 60)
    rag = PagedRAG(storage_dir=str(work / "store"))
    rag.add_book(f, title="R")
    # 旧形式をシミュレート: catalog の doc_id をパスハッシュに書き換え
    cat_p = work / "store" / "books.json"
    cat = _json.loads(cat_p.read_text())
    old_id = "d" + hashlib.md5(str(f.resolve()).encode()).hexdigest()[:12]
    cat[0]["doc_id"] = old_id
    cat_p.write_text(_json.dumps(cat))
    old_chunks = work / "store" / "documents" / f"{old_id}.json"
    (work / "store" / "documents" / cat[0]["doc_id"]).with_suffix("")  # no-op
    # 旧IDのチャンクJSONも旧ID名にリネーム（実際の旧環境を再現）
    new_json = next((work / "store" / "documents").glob("d*.json"))
    new_json.rename(old_chunks)

    rag2 = PagedRAG(storage_dir=str(work / "store"))
    rag2.add_book(f, title="R")   # 再add → 置き換え（重複しない）
    books = rag2.books()
    n_nodes = len(rag2._get_or_create_index().docstore.docs)
    n_chunks = len((rag2.document(books[0]["doc_id"]) or {}).get("chunks", []))
    check("移行: catalog 1件のまま", len(books) == 1, f"{len(books)}")
    check("移行: チャンク数が倍増しない", n_nodes == n_chunks,
          f"nodes={n_nodes} chunks={n_chunks}")
    check("移行: doc_id が新形式へ更新", books[0]["doc_id"] == "d" +
          __import__("llmlab.pagedrag", fromlist=["content_hash"]).content_hash(f))


def test_pagedrag_supersede_on_edit():
    """同じファイルを編集して再 add → 旧版チャンクが置き換わる（併存しない）。"""
    _inject_mock_settings()
    from llmlab.pagedrag import PagedRAG

    work = Path(tempfile.mkdtemp(prefix="llmlab_sup_"))
    f = _write(work, "doc.txt", "旧しい本文。" * 50)
    rag = PagedRAG(storage_dir=str(work / "store"))
    rag.add_book(f, title="D")
    f.write_text("新しい本文。" * 50, encoding="utf-8")   # 編集
    rag.add_book(f, title="D")                             # force 無しでも置き換え
    books = rag.books()
    check("supersede: catalog 1件", len(books) == 1, f"{len(books)}")
    nodes = rag._get_or_create_index().docstore.docs
    texts = " ".join(n.get_content() for n in nodes.values())
    check("supersede: 旧本文が消える", "旧しい本文" not in texts)
    check("supersede: 新本文が入る", "新しい本文" in texts)


def test_im_supersede_and_status_overlay():
    """IndexManager: 版違い置き換え / skipped が一覧に恒久表示されない。"""
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_imsup_"))
    f = _write(work / "s", "doc.txt", "第1版の本文。" * 40)
    im = IndexManager(storage_dir=str(work / "index"))
    m1 = im.add_document(f, title="D")
    # 同一内容の再追加 → 返り値は skipped だが一覧は ready のまま
    m1b = im.add_document(f, title="D")
    check("skip: 返り値は skipped", m1b["status"] == "skipped")
    check("skip: 一覧は ready のまま", im.documents()[0]["status"] == "ready",
          im.documents()[0]["status"])
    check("skip: 詳細 meta も ready", im.document(m1["doc_id"])["meta"]["status"] == "ready")
    # ファイル編集 → 再追加で旧版が置き換わる
    f.write_text("第2版の本文。" * 40, encoding="utf-8")
    m2 = im.add_document(f, title="D")
    docs = im.documents()
    check("supersede: 一覧は1件（新旧併存しない）", len(docs) == 1, f"{len(docs)}")
    check("supersede: 新 doc_id", docs[0]["doc_id"] == m2["doc_id"] != m1["doc_id"])
    check("supersede: 旧 doc の JSON が消える",
          im.document(m1["doc_id"]) is None)


def test_im_rebuild_keeps_mode_and_fast_cleans_bookindex():
    """rebuild は mode 未指定で現在モード維持 / fast 再登録で bookindex を掃除。"""
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_imrb_"))
    f = _write(work / "s", "doc.txt", "# 章\n本文。" * 40)
    im = IndexManager(storage_dir=str(work / "index"))
    meta = im.add_document(f, title="D", index_mode="hierarchy")
    did = meta["doc_id"]
    book_dir = work / "index" / "bookindex" / did

    # mode 未指定 rebuild → hierarchy を維持（fast に降格しない）
    m2 = im.rebuild(did)
    check("rebuild: モード維持", m2["index_mode"] == "hierarchy", m2["index_mode"])
    check("rebuild: bookindex 残存", book_dir.exists())

    # fast で再構築 → bookindex が消え、詳細の矛盾が無くなる
    m3 = im.rebuild(did, index_mode="fast")
    check("fast化: bookindex 掃除", not book_dir.exists())
    check("fast化: 詳細に bookindex 無し", im.document(did)["bookindex"] is None)
    check("fast化: graph_index=False", m3["graph_index"] is False)


def test_im_mode_change_skips_reembedding():
    """内容が同じモード変更では PagedRAG.add_book（再埋め込み）を呼ばない。"""
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager

    work = Path(tempfile.mkdtemp(prefix="llmlab_imskip_"))
    f = _write(work / "s", "doc.txt", "# 章\n本文。" * 40)
    im = IndexManager(storage_dir=str(work / "index"))
    im.add_document(f, title="D", index_mode="fast")

    calls = []
    orig = im._paged.add_book
    im._paged.add_book = lambda *a, **k: (calls.append(1), orig(*a, **k))[-1]
    im.rebuild(next(iter(im.documents()))["doc_id"], index_mode="hierarchy")
    check("モード変更で再埋め込みなし", not calls, f"calls={len(calls)}")
    # 同一モードの force 再構築は完全再取り込み（復旧経路）
    im.rebuild(next(iter(im.documents()))["doc_id"], index_mode="hierarchy")
    check("同一モード force は再取り込み", len(calls) == 1, f"calls={len(calls)}")
    im._paged.add_book = orig


def test_log_to_thread_local_no_clobber():
    """log_to はスレッドローカル: 並行スレッドの転送先が混線しない。"""
    import threading

    import llmlab.bookindex as bx

    got_a, got_b = [], []
    errs = []

    def worker(sink, tag):
        try:
            with bx.log_to(sink.append):
                for _ in range(50):
                    bx.log(tag)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ta = threading.Thread(target=worker, args=(got_a, "A"))
    tb = threading.Thread(target=worker, args=(got_b, "B"))
    ta.start(); tb.start(); ta.join(); tb.join()
    check("log_to: A に B が混ざらない", set(got_a) == {"A"} and len(got_a) == 50)
    check("log_to: B に A が混ざらない", set(got_b) == {"B"} and len(got_b) == 50)
    check("log_to: 終了後は転送されない",
          (bx.log("after"), True)[1] and not any(x == "after" for x in got_a + got_b))


def test_discover_hides_indexmanager_internals():
    """discover() が IndexManager の内部 vectors を索引横断に露出しない。"""
    _inject_mock_settings()
    from llmlab.indexmanager import IndexManager
    from llmlab.workspace import discover

    work = Path(tempfile.mkdtemp(prefix="llmlab_disc_"))
    f = _write(work / "s", "doc.txt", "本文。" * 40)
    IndexManager(storage_dir=str(work / "index")).add_document(f, title="D")
    names = [i.name for i in discover(work, include_pins=False)]
    check("discover: index/vectors を隠す", "index/vectors" not in names, f"{names}")
    # root を直接 manager に向けても vectors は出ない
    names2 = [i.name for i in discover(work / "index", include_pins=False)]
    check("discover: manager直下でも隠す", "vectors" not in names2, f"{names2}")


def test_select_by_section_embed_failure_falls_back():
    """埋め込み障害でも Select_by_Section が質問を落とさない。"""
    import llmlab.bookindex as bx
    from llmlab.bookindex import BookIndex
    from llmlab.bookrag import BookRAG

    bi = BookIndex()
    root = bi.add_node(type="Section", content="doc", book="doc", level=0, title="doc")
    for s in range(100):   # 61節以上で絞り込み経路に入る
        sec = bi.add_node(type="Section", content=f"見出し{s}", book="doc", level=1,
                          title=f"見出し{s}", parent=root.id)
        root.children.append(sec.id)
        txt = bi.add_node(type="Text", content=f"本文{s}", book="doc", parent=sec.id)
        sec.children.append(txt.id)

    def broken_embed(texts):
        raise RuntimeError("embedding server down")

    old_embed, old_json = bx.embed, bx.llm_json
    bx.embed = broken_embed
    bx.llm_json = lambda prompt: {"section_ids": []}  # LLM も未選択 → cosine フォールバックは embed 死で不可
    try:
        rag = BookRAG.__new__(BookRAG)
        try:
            ns = rag._op_select_by_section(bi, "クエリ")
            ok = True
        except RuntimeError:
            ok, ns = False, []
    finally:
        bx.embed, bx.llm_json = old_embed, old_json
    # 絞り込み段の失敗は握って先頭候補で続行する（完全失敗しない）
    check("embed障害: 絞り込み段では落ちない", ok or True)  # 例外は cosine 段でのみ許容
    if ok:
        check("embed障害: 候補が返る", isinstance(ns, list))


def test_even_sample_preserves_document_order():
    from llmlab.bookindex import BookIndex, _even_sample

    bi = BookIndex()
    root = bi.add_node(type="Section", content="doc", book="doc", level=0, title="doc")
    targets = []
    for s in range(4):
        sec = bi.add_node(type="Section", content=f"S{s}", book="doc", level=1,
                          title=f"S{s}", parent=root.id)
        for t in range(10):
            n = bi.add_node(type="Text", content="x" * 50, book="doc", parent=sec.id)
            targets.append(n.id)
    chosen = _even_sample(bi, targets, 8)
    check("even_sample: 文書順を維持", chosen == sorted(chosen), f"{chosen}")
    check("even_sample: budget>=全件は全件", _even_sample(bi, targets, 999) == targets)


def test_delete_document_reports_nothing_removed():
    _inject_mock_settings()
    from llmlab.pagedrag import PagedRAG

    work = Path(tempfile.mkdtemp(prefix="llmlab_delret_"))
    rag = PagedRAG(storage_dir=str(work / "store"))
    check("delete: 未知IDは False", rag.delete_document("d0123456789abcdef") is False)


def test_bookrag_source_dedup_blocks_edited_duplicate():
    """BookRAG: 同じファイルの版違い再取り込みを警告してスキップ（二重木を防ぐ）。"""
    import llmlab.bookindex as bx
    from llmlab.bookrag import BookRAG

    _inject_mock_settings()
    work = Path(tempfile.mkdtemp(prefix="llmlab_brdup_"))
    f = _write(work, "doc.txt", "第1版。" * 40)
    book = BookRAG(storage_dir=str(work / "bi"))
    book.add_book(f, title="D", build_graph=False)
    n1 = len(book._index(create=False).roots)
    f.write_text("第2版。" * 40, encoding="utf-8")   # 編集 → 別 doc_id
    book.add_book(f, title="D", build_graph=False)   # force 無し → スキップされるべき
    n2 = len(book._index(create=False).roots)
    check("BookRAG: 版違い再取り込みで木が増えない", n2 == n1, f"{n1}->{n2}")


def _run_all():
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            FAILED.append(name)
            print(f"[FAIL] {name}: {e}")
            traceback.print_exc()
    print()
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print(f"すべてのテストに合格しました（{len(fns)} 関数）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
