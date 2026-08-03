"""グラフ構築のカバレッジ/完全性メタデータの回帰テスト。

安全モード（max_nodes サンプリング）で一部ノードしか Entity 抽出していない場合、
graph_status=ready でも「部分グラフ」であることが分かる統計を
build_graph → BookRAG(graph_stats.json) → IndexManager(docメタ) に伝搬する。
外部サーバは使わない（抽出・埋め込みはモック）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
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


def _mock_graph(monkeypatch, *, status="ok"):
    """_extract_graph と embed をモックする。status で結果種別を制御。"""
    import llmlab.bookindex as bx

    def extract(node, fail_fast=True):
        if status == "badjson":
            return None
        if status == "empty":
            return {"entities": [], "relations": []}
        return {"entities": [{"name": f"E{node.id}", "type": "T",
                              "description": "d"}], "relations": []}

    monkeypatch.setattr(bx, "_extract_graph", extract)
    monkeypatch.setattr(bx, "embed",
                        lambda texts: np.ones((len(texts), 4), dtype=np.float32))


def _mini_bi(n_text=10):
    import llmlab.bookindex as bx

    bi = bx.BookIndex()
    root = bi.add_node(type="Section", content="", book="B", title="r", level=1)
    bi.roots.append(root.id)
    ids = []
    for i in range(n_text):
        n = bi.add_node(type="Text", book="B", parent=root.id,
                        content=f"ノード{i}の本文。" * 10)
        root.children.append(n.id)
        ids.append(n.id)
    return bi, ids


def test_build_graph_reports_partial_coverage(tmp_path, monkeypatch):
    _inject()
    import llmlab.bookindex as bx

    _mock_graph(monkeypatch)
    bi, ids = _mini_bi(10)
    stats = bx.build_graph(bi, ids, max_workers=1, max_nodes=4)
    assert stats["eligible_nodes"] == 10
    assert stats["sampled_nodes"] == 4
    assert stats["processed_nodes"] == 4
    assert stats["graph_coverage_ratio"] == pytest.approx(0.4)
    assert stats["graph_is_complete"] is False
    assert stats["graph_max_nodes_used"] == 4
    assert stats["extract_ok"] == 4


def test_build_graph_all_nodes_is_complete(tmp_path, monkeypatch):
    _inject()
    import llmlab.bookindex as bx

    _mock_graph(monkeypatch)
    bi, ids = _mini_bi(10)
    stats = bx.build_graph(bi, ids, max_workers=1, max_nodes=4, all_nodes=True)
    assert stats["processed_nodes"] == 10
    assert stats["graph_coverage_ratio"] == pytest.approx(1.0)
    assert stats["graph_is_complete"] is True


def test_failure_counts_reported(tmp_path, monkeypatch):
    _inject()
    import llmlab.bookindex as bx

    _mock_graph(monkeypatch, status="badjson")
    bi, ids = _mini_bi(4)
    stats = bx.build_graph(bi, ids, max_workers=1, all_nodes=True)
    assert stats["extract_badjson"] == 4, "JSON不正の件数を統計で確認できる"
    assert stats["extract_ok"] == 0
    assert stats["graph_is_complete"] is True, "処理はした（結果が不正なだけ）"


def test_indexmanager_meta_gets_coverage(tmp_path, monkeypatch):
    """IndexManager 経由: graph 取り込みで doc メタに部分グラフ情報が載る。"""
    _inject()
    _mock_graph(monkeypatch)
    from llmlab.indexmanager import IndexManager

    src = tmp_path / "src"
    src.mkdir()
    body = "\n\n".join(f"# 第{i}章\n本文{i}の説明。" * 8 for i in range(1, 13))
    p = src / "long.txt"
    p.write_text(body, encoding="utf-8")

    im = IndexManager(storage_dir=tmp_path / "index")
    meta = im.add_document(p, index_mode="graph",
                           graph_settings={"graph_max_nodes": 3,
                                           "graph_chunk_chars": 200})
    assert meta["graph_status"] == "ready"
    assert meta["graph_is_complete"] is False, "サンプリングしたら部分グラフと分かる"
    assert meta["eligible_nodes"] > meta["processed_nodes"] == 3
    assert 0 < meta["graph_coverage_ratio"] < 1
    assert meta["graph_max_nodes_used"] == 3
    # graph_stats.json も残っている（graph-data API が読む）
    assert (tmp_path / "index" / "bookindex" / meta["doc_id"]
            / "graph_stats.json").exists()


def test_all_nodes_setting_reaches_bookrag(tmp_path, monkeypatch):
    """graph_all_nodes=True が BookRAG.add_book(all_nodes=True) まで届く。"""
    _inject()
    import llmlab.bookrag as brmod
    from llmlab.indexmanager import IndexManager

    captured = {}

    class FakeBookRAG:
        def __init__(self, **kw):
            captured.update(kw)

        def add_book(self, *a, **kw):
            captured["add_book"] = kw

        def has_graph(self):
            return True

    monkeypatch.setattr(brmod, "BookRAG", FakeBookRAG)
    p = tmp_path / "d.txt"
    p.write_text("# 章\n本文。" * 30, encoding="utf-8")
    im = IndexManager(storage_dir=tmp_path / "index")
    im.add_document(p, index_mode="graph",
                    graph_settings={"graph_all_nodes": True})
    assert captured["add_book"]["all_nodes"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
