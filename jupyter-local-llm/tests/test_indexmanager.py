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
