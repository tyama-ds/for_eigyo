"""取り込みの真のストリーム化（監査項目6）の回帰テスト。

- txt/md は Path.read_text() の全量読込をしない（64KBブロック読み→セグメント yield）
- チャンク本文は一時 JSONL へ逐次保存 → 最終 JSON をストリーム生成（tmp + atomic replace）
- 完了時に一時ファイルが残らない／失敗時に既存の完成済み JSON を上書きしない
- force 再構築の途中失敗後も、同一プロセスから永続済みの旧索引を検索できる
- PDF は1ページずつ yield（疑似 PdfReader で検証。実PDF解析は pypdf に委譲）
外部サーバは使わない。
"""

from __future__ import annotations

import json
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


def _big_text(n=40) -> str:
    return "\n\n".join(f"# 第{i}節\n" + f"第{i}節の本文が続く。" * 40
                       for i in range(1, n + 1))


def test_txt_ingest_without_read_text(tmp_path, monkeypatch):
    """source の Path.read_text() を例外化しても txt を取り込める。"""
    _inject()
    from llmlab.pagedrag import PagedRAG

    p = tmp_path / "big.txt"
    p.write_text(_big_text(), encoding="utf-8")
    rag = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=8)

    orig_read_text = Path.read_text

    def forbidden(self, *a, **k):
        if self == p:
            raise AssertionError("source を read_text() で全量読込してはならない")
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", forbidden)
    rag.add_book(p)
    doc = rag.document(rag.books()[0]["doc_id"])
    assert doc["chunks"], "read_text なしで取り込みが完走する"
    text_all = "".join(c["text"] for c in doc["chunks"])
    assert "第40節" in text_all, "末尾セグメントまで取り込まれる"


def test_segment_generator_preserves_content(tmp_path):
    """950,000字（改行あり/なし混在）を分割しても文字数・内容が一致する。"""
    _inject()
    from llmlab.pagedrag import PagedRAG

    body = ("x" * 500_000) + "\n" + ("あいう\n" * 100_000) + ("y" * 49_999)
    p = tmp_path / "mix.txt"
    p.write_text(body, encoding="utf-8")
    segs = list(PagedRAG._iter_text_segments(p))
    assert len(segs) >= 4
    assert "".join(segs) == body, "連結すると元の本文と完全一致（欠落・重複なし）"
    assert all(len(s) <= 200_000 + 1 for s in segs), "セグメント上限を超えない"


def test_jsonl_streamed_and_final_json_atomic(tmp_path):
    _inject()
    from llmlab.pagedrag import PagedRAG

    p = tmp_path / "doc.txt"
    p.write_text(_big_text(), encoding="utf-8")
    rag = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=8)
    rag.add_book(p)
    docs_dir = tmp_path / "store" / "documents"
    assert not list(docs_dir.glob("*.ingest.jsonl")), "完了後に一時JSONLが残らない"
    assert not list(docs_dir.glob("*.json.tmp")), "tmp が残らない"
    doc_id = rag.books()[0]["doc_id"]
    doc = json.loads((docs_dir / f"{doc_id}.json").read_text(encoding="utf-8"))
    ids = [c["chunk_id"] for c in doc["chunks"]]
    assert len(ids) == len(set(ids)) == rag.books()[0]["chunks"], \
        "最終JSONに全チャンクがユニークに入る"


def test_failure_does_not_overwrite_existing_final_json(tmp_path, monkeypatch):
    """force 再構築の途中失敗で、既存の完成済み文書 JSON を上書きしない。"""
    _inject()
    from llama_index.core.indices.vector_store import VectorStoreIndex

    from llmlab.pagedrag import PagedRAG

    p = tmp_path / "doc.txt"
    p.write_text(_big_text(), encoding="utf-8")
    rag = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=8)
    rag.add_book(p)
    doc_id = rag.books()[0]["doc_id"]
    final = tmp_path / "store" / "documents" / f"{doc_id}.json"
    before = final.read_text(encoding="utf-8")

    orig = VectorStoreIndex.insert_nodes
    state = {"n": 0}

    def failing(self, nodes, **kw):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("模擬失敗")
        return orig(self, nodes, **kw)

    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", failing)
    with pytest.raises(RuntimeError):
        rag.add_book(p, force=True)
    assert final.read_text(encoding="utf-8") == before, \
        "失敗時は既存の完成済み文書 JSON がそのまま"
    assert not list((tmp_path / "store" / "documents").glob("*.json.tmp"))


def test_force_rebuild_failure_recovers_old_index_same_process(tmp_path, monkeypatch):
    """force 再構築中の失敗後、同一プロセスから永続済みの旧索引を検索できる。"""
    _inject()
    from llama_index.core.indices.vector_store import VectorStoreIndex

    from llmlab.pagedrag import PagedRAG

    p = tmp_path / "doc.txt"
    p.write_text(_big_text(), encoding="utf-8")
    rag = PagedRAG(storage_dir=tmp_path / "store", ingest_batch_size=8)
    rag.add_book(p)
    doc_id = rag.books()[0]["doc_id"]
    n_before = len(rag.retrieve_in_doc("本文", doc_id=doc_id, top_m=10_000))
    assert n_before > 8

    orig = VectorStoreIndex.insert_nodes
    state = {"n": 0}

    def failing(self, nodes, **kw):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("模擬失敗")
        return orig(self, nodes, **kw)

    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", failing)
    with pytest.raises(RuntimeError):
        rag.add_book(p, force=True)   # メモリ上では旧ノード削除済みで失敗
    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", orig)

    # self._index が破棄され、永続済みの旧索引を読み直して検索できる
    nodes = rag.retrieve_in_doc("本文", doc_id=doc_id, top_m=10_000)
    assert len(nodes) == n_before, "旧索引が同一プロセスで復旧する"


def test_pdf_pages_yielded_one_by_one(monkeypatch, tmp_path):
    """PDF ページを1ページずつ yield し page_label を保持する（疑似 PdfReader）。"""
    _inject()
    import pypdf

    from llmlab.pagedrag import PagedRAG

    class FakePage:
        def __init__(self, i):
            self.i = i

        def extract_text(self):
            if self.i == 2:
                raise RuntimeError("壊れたページ（模擬）")
            return f"{self.i}ページ目の本文"

    class FakeReader:
        def __init__(self, _path):
            self.pages = [FakePage(i + 1) for i in range(5)]

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    p = tmp_path / "m.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    gen = PagedRAG._iter_pdf_pages(p)
    import types

    assert isinstance(gen, types.GeneratorType), "全ページを list 化しない"
    docs = list(gen)
    assert [d.metadata["page_label"] for d in docs] == ["1", "2", "3", "4", "5"]
    assert docs[0].text == "1ページ目の本文"
    assert docs[1].text == "", "1ページの抽出失敗で全体を止めない"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def _minimal_pdf(texts) -> bytes:
    """テキスト入りの最小構造 PDF を手書きで生成する（外部ライブラリ不要）。"""
    objs = []
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(texts)))
    objs.append("1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj")
    objs.append(f"2 0 obj << /Type /Pages /Kids [{kids}] /Count {len(texts)} >> endobj")
    objs.append("3 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj")
    for i, t in enumerate(texts):
        stream = f"BT /F1 12 Tf 72 720 Td ({t}) Tj ET"
        objs.append(f"{4 + i * 2} 0 obj << /Type /Page /Parent 2 0 R "
                    "/MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + i * 2} 0 R >> endobj")
        objs.append(f"{5 + i * 2} 0 obj << /Length {len(stream)} >> stream\n"
                    f"{stream}\nendstream endobj")
    body = "%PDF-1.4\n"
    offsets = []
    for o in objs:
        offsets.append(len(body.encode("latin-1")))
        body += o + "\n"
    xref_pos = len(body.encode("latin-1"))
    n = len(objs) + 1
    body += f"xref\n0 {n}\n0000000000 65535 f \n"
    for off in offsets:
        body += f"{off:010d} 00000 n \n"
    body += f"trailer << /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"
    return body.encode("latin-1")


def test_real_pdf_ingest_end_to_end(tmp_path):
    """実PDF（手書きの最小構造・5ページ）を pypdf 経由で取り込み、
    page_label 保持とトークン検索まで通しで検証する（疑似Readerではない）。"""
    _inject()
    from llmlab.indexmanager import IndexManager

    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(_minimal_pdf([f"Page {i} body with CODE-{i}77"
                                  for i in range(1, 6)]))
    im = IndexManager(storage_dir=tmp_path / "index", ingest_batch_size=2)
    meta = im.add_document(pdf, title="実PDF")
    assert meta["status"] == "ready" and meta["chunk_count"] >= 1
    doc = im._paged.document(meta["doc_id"])
    pages = {c["page"] for c in doc["chunks"]}
    assert {"1", "5"} <= pages, "全ページが page_label つきでチャンク化される"
    hits = im.search("CODE-377", doc_ids=[meta["doc_id"]])
    assert "CODE-377" in hits[0].chunks[0]["text"], "実PDF本文をトークン検索で発見"
