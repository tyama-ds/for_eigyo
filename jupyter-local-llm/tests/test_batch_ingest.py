"""取り込みのバッチ化（メモリ制限つき）の回帰テスト。

- 埋め込み・ベクトル挿入は ingest_batch_size 件以下のバッチで行う。
- 途中失敗で status=ready にしない／再実行で中途チャンクを二重登録しない。
- txt / md / PDF（疑似Reader）で動く。巨大テキストはセグメント分割して読む。
外部サーバ・巨大実ファイルは使わない（MockEmbedding + 合成テキスト + 疑似Reader）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _inject():
    from llama_index.core import Settings as LI
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    LI.embed_model = MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None


def _spy_insert(monkeypatch):
    """VectorStoreIndex.insert_nodes の呼び出しごとの件数を記録するスパイ。"""
    from llama_index.core.indices.vector_store import VectorStoreIndex

    calls: list[int] = []
    orig = VectorStoreIndex.insert_nodes

    def spy(self, nodes, **kw):
        calls.append(len(nodes))
        return orig(self, nodes, **kw)

    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", spy)
    return calls


def _long_text(n_units: int = 60) -> str:
    return "\n\n".join(f"# 第{i}節\nこれは第{i}節の本文で、規程の説明が続く。" * 14
                       for i in range(1, n_units + 1))


def test_txt_ingest_batches_bounded(tmp_path, monkeypatch):
    _inject()
    from llmlab.pagedrag import PagedRAG

    calls = _spy_insert(monkeypatch)
    p = tmp_path / "big.txt"
    p.write_text(_long_text(), encoding="utf-8")
    rag = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=8)
    rag.add_book(p)

    assert len(calls) >= 2, "複数バッチに分かれる"
    assert max(calls) <= 8, "1回のベクトル挿入は ingest_batch_size 件以下"
    doc = rag.document(rag.books()[0]["doc_id"])
    assert sum(calls) == len(doc["chunks"]), "全チャンクが欠けなく登録される"
    ids = [c["chunk_id"] for c in doc["chunks"]]
    assert len(ids) == len(set(ids)), "chunk_id の重複なし"


def test_md_ingest_works(tmp_path, monkeypatch):
    _inject()
    from llmlab.pagedrag import PagedRAG

    calls = _spy_insert(monkeypatch)
    p = tmp_path / "doc.md"
    p.write_text(_long_text(20), encoding="utf-8")
    rag = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=4)
    rag.add_book(p)
    assert max(calls) <= 4
    assert rag.books()[0]["chunks"] == sum(calls)


def test_fake_pdf_pages_batched(tmp_path, monkeypatch):
    """PDF 相当（ページ単位 Document）を疑似 Reader で模擬してバッチ処理を検証。

    注意: 実PDFの解析は SimpleDirectoryReader に委ねており、ここでは
    「ページ列 → チャンク → バッチ挿入」の経路だけを検証する（疑似テスト）。
    """
    _inject()
    from llama_index.core import Document

    from llmlab.pagedrag import PagedRAG

    calls = _spy_insert(monkeypatch)
    fake_pages = [Document(text=f"{i}ページ目の本文。" * 40,
                           metadata={"page_label": str(i)})
                  for i in range(1, 31)]
    monkeypatch.setattr(PagedRAG, "_load_source_documents",
                        lambda self, path: fake_pages)
    p = tmp_path / "manual.pdf"
    p.write_bytes(b"%PDF-1.4 fake")   # 内容ハッシュ用のダミー（読解はしない）
    rag = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=16)
    rag.add_book(p)

    assert max(calls) <= 16
    doc = rag.document(rag.books()[0]["doc_id"])
    pages = {c["page"] for c in doc["chunks"]}
    assert "1" in pages and "30" in pages, "全ページがチャンク化される"


def test_large_text_is_segmented(tmp_path):
    _inject()
    from llmlab.pagedrag import PagedRAG

    p = tmp_path / "huge.txt"
    p.write_text("あ" * 950_000, encoding="utf-8")   # ~1MB 弱
    rag = PagedRAG(storage_dir=tmp_path / "store")
    docs = rag._load_source_documents(p)
    assert len(docs) >= 4, "巨大テキストは1つの Document にしない"
    assert sum(len(d.text) for d in docs) == 950_000, "分割で本文を失わない"


def test_partial_failure_cleanup_no_duplicates(tmp_path, monkeypatch):
    """2バッチ目で失敗→再実行しても中途チャンクが二重登録されない。"""
    _inject()
    from llama_index.core.indices.vector_store import VectorStoreIndex

    from llmlab.pagedrag import PagedRAG

    p = tmp_path / "doc.txt"
    p.write_text(_long_text(40), encoding="utf-8")
    rag = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=8)

    orig = VectorStoreIndex.insert_nodes
    state = {"n": 0}

    def failing(self, nodes, **kw):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("embedding server down（模擬）")
        return orig(self, nodes, **kw)

    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", failing)
    with pytest.raises(RuntimeError):
        rag.add_book(p)
    doc_id = None
    partials = list((tmp_path / "store" / "documents").glob("*.ingest.jsonl"))
    assert partials, "中途失敗の挿入記録（partial）が残る"
    doc_id = partials[0].name.split(".")[0]

    # 再実行（今度は成功）
    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", orig)
    rag2 = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=8)
    rag2.add_book(p)
    assert not list((tmp_path / "store" / "documents").glob("*.ingest.jsonl")), \
        "完走したら中途記録は消える"
    doc = rag2.document(doc_id)
    ids = [c["chunk_id"] for c in doc["chunks"]]
    assert len(ids) == len(set(ids))
    # ベクトル索引側の実チャンク数も JSON と一致（浮遊ノードによる二重登録なし）
    nodes = rag2.retrieve_in_doc("規程", doc_id=doc_id, top_m=10_000)
    assert len(nodes) == len(ids), "索引内のチャンク数が JSON と一致（重複なし）"


def test_indexmanager_failure_not_ready_then_recovers(tmp_path, monkeypatch):
    """IndexManager 経由: 取り込み途中失敗は status=failed。再実行で復旧する。"""
    _inject()
    from llama_index.core.indices.vector_store import VectorStoreIndex

    from llmlab.indexmanager import IndexManager

    p = tmp_path / "src" / "doc.txt"
    p.parent.mkdir(parents=True)
    p.write_text(_long_text(40), encoding="utf-8")
    im = IndexManager(storage_dir=tmp_path / "index", ingest_batch_size=8)

    orig = VectorStoreIndex.insert_nodes
    state = {"n": 0}

    def failing(self, nodes, **kw):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("模擬失敗")
        return orig(self, nodes, **kw)

    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", failing)
    with pytest.raises(RuntimeError):
        im.add_document(p)
    docs = im.documents()
    assert docs and docs[0]["status"] == "failed", "途中失敗を ready にしない"
    assert docs[0]["vector_status"] == "failed"

    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", orig)
    meta = im.add_document(p)
    assert meta["status"] == "ready"
    hits = im.search("規程", doc_ids=[meta["doc_id"]])
    assert hits and hits[0].chunks


def test_ingest_progress_forwarded(tmp_path):
    """バッチ進捗が progress_to（ETA計測点）へ流れる。"""
    _inject()
    import llmlab.bookindex as bx
    from llmlab.pagedrag import PagedRAG

    p = tmp_path / "doc.txt"
    p.write_text(_long_text(40), encoding="utf-8")
    rag = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=8)
    events = []
    with bx.progress_to(lambda d, c, t: events.append((d, c, t))):
        rag.add_book(p)
    ingest = [e for e in events if "埋め込み" in e[0]]
    assert ingest, "取り込みバッチの進捗イベントが出る"
    assert [c for _d, c, _t in ingest] == list(range(len(ingest))), \
        "current が 0 から単調に進む"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
