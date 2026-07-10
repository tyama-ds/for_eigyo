"""文書間検索（doc_id 単位の多様化）の回帰テスト。

外部の LLM / 埋め込みサーバは使わない:
- doc_id 生成と文書単位集約は純関数として直接検証
- 索引が要る部分は LlamaIndex の MockEmbedding / MockLLM を注入して検証

実行:  pytest -q            もしくは   python tests/test_docid_multipaper.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llama_index.core.schema import NodeWithScore, TextNode  # noqa: E402

from llmlab.pagedrag import _DOC_ID_KEY, PagedRAG, make_doc_id  # noqa: E402


# --------------------------------------------------------------------------
# ヘルパ: 合成チャンク（NodeWithScore）
# --------------------------------------------------------------------------

def _chunk(doc_id: str, title: str, score: float, text: str = "x") -> NodeWithScore:
    return NodeWithScore(
        node=TextNode(text=text, metadata={_DOC_ID_KEY: doc_id, "title": title}),
        score=score,
    )


# --------------------------------------------------------------------------
# 1. doc_id が同名ファイル/同名タイトルでも衝突しない
# --------------------------------------------------------------------------

def _tmp_doc(name: str, text: str):
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="llmlab_docid_"))
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def test_doc_id_no_collision_same_name():
    # v0.6.0〜: doc_id は内容ハッシュ。同名ファイルでも内容が違えば別 ID。
    a = make_doc_id(_tmp_doc("report.txt", "2014年の内容"), "決算報告")
    b = make_doc_id(_tmp_doc("report.txt", "2015年の内容"), "決算報告")
    assert a != b, "同名ファイルでも内容が違えば別 doc_id になるべき"


def test_doc_id_same_content_same_id():
    # 内容が同じなら別フォルダ・別パスでも同じ ID（冪等な再取り込み）。
    a = make_doc_id(_tmp_doc("a.txt", "同一内容"))
    b = make_doc_id(_tmp_doc("b.txt", "同一内容"))
    assert a == b, "同一内容は場所やファイル名に関係なく同じ doc_id"


def test_doc_id_stable():
    p = _tmp_doc("report.txt", "本文")
    a1, a2 = make_doc_id(p, "決算報告"), make_doc_id(p, "決算報告")
    assert a1 == a2, "同じファイルなら安定した doc_id"
    assert a1.startswith("d") and len(a1) == 17  # "d" + sha256 先頭16桁


def test_doc_id_fallback_when_unreadable():
    # 読めないパスは絶対パスハッシュへフォールバック（旧形式・13桁）
    a = make_doc_id("/no/such/dir/report.pdf")
    assert a.startswith("d") and len(a) == 13


def test_doc_id_ignores_title():
    # 同じファイルは title に関係なく同じ doc_id（再取り込みの冪等性）。
    p = _tmp_doc("report.txt", "本文")
    assert make_doc_id(p) == make_doc_id(p, "A") == make_doc_id(p, "別タイトル")


# --------------------------------------------------------------------------
# 2. 文書単位集約: 1文書がチャンク top-k を独占しても複数文書を返す
# --------------------------------------------------------------------------

def test_aggregate_diversifies_when_one_doc_dominates():
    # doc A が高スコアのチャンクを 40 個、B/C は1個ずつ下位に。
    nodes = [_chunk("dA", "A", 0.99 - i * 0.01) for i in range(40)]
    nodes.append(_chunk("dB", "B", 0.55))
    nodes.append(_chunk("dC", "C", 0.50))

    ranked = PagedRAG._aggregate_by_doc(nodes, top_n=3, chunks_per_doc=4)
    ids = [r.doc_id for r in ranked]
    assert ids == ["dA", "dB", "dC"], f"3文書が多様に返るべき: {ids}"
    assert ranked[0].n_chunks == 40 and len(ranked[0].chunks) == 4  # 代表は上限まで
    assert ranked[0].score >= ranked[1].score >= ranked[2].score


def test_aggregate_top_n_caps():
    nodes = [_chunk(f"d{c}", c, s) for c, s in
             [("A", 0.9), ("B", 0.8), ("C", 0.7), ("D", 0.6)]]
    ranked = PagedRAG._aggregate_by_doc(nodes, top_n=2, chunks_per_doc=4)
    assert [r.doc_id for r in ranked] == ["dA", "dB"]


def test_aggregate_avg_mode():
    # A: 高スコア1件 + 低スコア多数 / B: 中スコアが揃う → avg では B が勝つことがある
    nodes = [_chunk("dA", "A", 0.99)] + [_chunk("dA", "A", 0.10) for _ in range(3)]
    nodes += [_chunk("dB", "B", 0.60) for _ in range(4)]
    by_max = PagedRAG._aggregate_by_doc(nodes, top_n=2, chunks_per_doc=4, agg="max")
    by_avg = PagedRAG._aggregate_by_doc(nodes, top_n=2, chunks_per_doc=4, agg="avg")
    assert by_max[0].doc_id == "dA"                 # max は A（0.99）
    assert by_avg[0].doc_id == "dB"                 # avg は B（0.60 平均 > A の 0.32）


def test_aggregate_fallback_to_title_without_doc_id():
    # 旧索引（doc_id 無し）は title でグルーピングされる
    n1 = NodeWithScore(node=TextNode(text="x", metadata={"title": "旧A"}), score=0.9)
    n2 = NodeWithScore(node=TextNode(text="y", metadata={"title": "旧B"}), score=0.8)
    ranked = PagedRAG._aggregate_by_doc([n1, n2], top_n=5, chunks_per_doc=2)
    assert {r.title for r in ranked} == {"旧A", "旧B"}


# --------------------------------------------------------------------------
# 3. rank_documents / locate が retriever をスタブしても多様化する
# --------------------------------------------------------------------------

def test_rank_documents_uses_retrieve(monkeypatch=None):
    rag = PagedRAG(storage_dir="/tmp/llmlab_test_rank")
    dominated = [_chunk("dA", "A", 0.9 - i * 0.01) for i in range(30)]
    dominated += [_chunk("dB", "B", 0.4), _chunk("dC", "C", 0.3)]
    rag._retrieve = lambda q, *, top_k, filters=None: dominated  # スタブ
    ranked = rag.rank_documents("q", candidate_chunk_k=50, top_n=3)
    assert [r.doc_id for r in ranked] == ["dA", "dB", "dC"]


def test_multipaper_locate_returns_multiple_docs():
    from llmlab.multipaper import MultiPaperRAG

    mp = MultiPaperRAG(storage_dir="/tmp/llmlab_test_mp", max_papers=4, chunks_per_doc=4)
    dominated = [_chunk("dA", "論文A", 0.9 - i * 0.01) for i in range(40)]
    dominated += [_chunk("dB", "論文B", 0.5), _chunk("dC", "論文C", 0.45)]
    mp._rag._retrieve = lambda q, *, top_k, filters=None: dominated  # サーバ不要
    titles = mp.locate("共通の話題は？")
    assert titles == ["論文A", "論文B", "論文C"], f"複数文書が返るべき: {titles}"


# --------------------------------------------------------------------------
# 4. 索引を使う統合テスト（MockEmbedding / MockLLM 注入）
# --------------------------------------------------------------------------

def _inject_mock_settings():
    from llama_index.core import Settings as LI
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    LI.embed_model = MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None  # 既に注入済み → 接続設定を要求しない


def test_document_json_and_no_dedup_collision(tmp_path=None):
    import tempfile

    _inject_mock_settings()
    work = Path(tempfile.mkdtemp(prefix="llmlab_docjson_"))
    # 同名ファイル "report.txt" を別フォルダに2つ用意（衝突テスト）
    (work / "2014").mkdir()
    (work / "2015").mkdir()
    f1 = work / "2014" / "report.txt"
    f2 = work / "2015" / "report.txt"
    f1.write_text("2014 年の売上は 100 億円。" * 20, encoding="utf-8")
    f2.write_text("2015 年の売上は 120 億円。" * 20, encoding="utf-8")

    rag = PagedRAG(storage_dir=str(work / "store"))
    rag.add_book(f1, title="決算報告")   # 同名タイトルにして衝突を誘発
    rag.add_book(f2, title="決算報告")

    books = rag.books()
    doc_ids = {b["doc_id"] for b in books}
    assert len(books) == 2, "同名ファイル/同名タイトルでも2件登録されるべき"
    assert len(doc_ids) == 2, "doc_id が衝突していない"

    # 文書ごと JSON が保存され、構造を持つ
    for did in doc_ids:
        doc = rag.document(did)
        assert doc is not None
        assert set(doc.keys()) >= {"doc_id", "title", "source", "path", "summary", "chunks"}
        assert doc["chunks"] and "chunk_id" in doc["chunks"][0] and "text" in doc["chunks"][0]
        assert doc["chunks"][0]["metadata"]["doc_id"] == did

    # 再取り込みは重複しない（doc_id 一致でスキップ）
    rag.add_book(f1, title="決算報告")
    assert len(rag.books()) == 2


def test_title_and_doc_id_filter_scopes_results():
    import tempfile

    _inject_mock_settings()
    work = Path(tempfile.mkdtemp(prefix="llmlab_filter_"))
    fa = work / "a.txt"
    fb = work / "b.txt"
    fa.write_text("アルパカに関する説明。" * 30, encoding="utf-8")
    fb.write_text("ラクダに関する説明。" * 30, encoding="utf-8")

    rag = PagedRAG(storage_dir=str(work / "store"), top_k=10)
    rag.add_book(fa, title="アルパカ本")
    rag.add_book(fb, title="ラクダ本")
    doc_a = next(b["doc_id"] for b in rag.books() if b["title"] == "アルパカ本")

    # title 指定: アルパカ本のチャンクだけが根拠になる
    ans = rag.query("説明して", title="アルパカ本")
    assert ans.sources, "title 絞り込みでも根拠が返るべき"
    assert all(s.title == "アルパカ本" for s in ans.sources)

    # doc_id 指定でも同様（予約語 doc_id ではなく専用キーで確実に効く）
    got = rag.retrieve_in_doc("説明して", doc_id=doc_a, top_m=10)
    assert got and all(n.node.metadata[_DOC_ID_KEY] == doc_a for n in got)


def test_doc_id_server_side_filter_returns_rows():
    """予約語トラップの回帰検知: サーバ側 MetadataFilter パスを直接叩く。

    _DOC_ID_KEY を予約語 "doc_id" に戻すと MetadataFilter が 0 件になり、この
    アサートが失敗する（クライアント側フォールバックを経由しないので確実に検知）。
    """
    import tempfile

    _inject_mock_settings()
    work = Path(tempfile.mkdtemp(prefix="llmlab_srvfilter_"))
    fa = work / "a.txt"
    fb = work / "b.txt"
    fa.write_text("アルパカの説明。" * 30, encoding="utf-8")
    fb.write_text("ラクダの説明。" * 30, encoding="utf-8")
    rag = PagedRAG(storage_dir=str(work / "store"))
    rag.add_book(fa, title="A")
    rag.add_book(fb, title="B")
    doc_a = next(b["doc_id"] for b in rag.books() if b["title"] == "A")

    filters = rag._build_filters(doc_id=doc_a)          # サーバ側フィルタのみ
    nodes = rag._retrieve("説明", top_k=50, filters=filters)
    assert nodes, "doc_id の MetadataFilter が 0 件（予約語トラップの回帰）"
    assert all(n.node.metadata[_DOC_ID_KEY] == doc_a for n in nodes)


def test_force_reingest_replaces_not_duplicates():
    """force=True は追記ではなく置換（旧チャンクを削除してから挿入）。"""
    import tempfile

    _inject_mock_settings()
    work = Path(tempfile.mkdtemp(prefix="llmlab_force_"))
    f = work / "doc.txt"
    f.write_text("段落。" * 200, encoding="utf-8")  # 複数チャンクに分かれる長さ
    rag = PagedRAG(storage_dir=str(work / "store"))
    rag.add_book(f, title="X")
    n1 = len(rag._get_or_create_index().docstore.docs)
    assert n1 >= 1

    rag.add_book(f, title="X", force=True)             # 再取り込み（置換）
    n2 = len(rag._get_or_create_index().docstore.docs)
    assert n2 == n1, f"force 再取り込みでチャンクが重複した: {n1} -> {n2}"
    assert len(rag.books()) == 1                        # カタログも1件のまま
    assert len(rag.document_ids()) == 1


def test_readd_different_title_is_idempotent():
    """同じファイルを title 違いで add しても二重登録しない（doc_id はパス由来）。"""
    import tempfile

    _inject_mock_settings()
    work = Path(tempfile.mkdtemp(prefix="llmlab_readd_"))
    f = work / "manual.txt"
    f.write_text("本文。" * 50, encoding="utf-8")
    rag = PagedRAG(storage_dir=str(work / "store"))
    rag.add_book(f, title="営業マニュアル")
    rag.add_book(f)                    # title 省略 → 旧実装では別 doc_id になり重複していた
    assert len(rag.books()) == 1, "同一ファイルは title に関係なく1件"
    assert len(rag.document_ids()) == 1


# --------------------------------------------------------------------------
# 直接実行（pytest なしでも動かせる）
# --------------------------------------------------------------------------

def _run_all():
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in fns:
        try:
            fn()
            print(f"[OK ] {name}")
        except Exception as e:  # noqa: BLE001
            import traceback

            failed.append(name)
            print(f"[FAIL] {name}: {e}")
            traceback.print_exc()
    print()
    if failed:
        print(f"FAILED: {failed}")
        return 1
    print(f"すべてのテストに合格しました（{len(fns)} 件）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
