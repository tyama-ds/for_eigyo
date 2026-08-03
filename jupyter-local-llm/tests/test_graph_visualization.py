"""グラフ可視化 API（graph_data）とグラフ根拠の追跡IDの回帰テスト。

- GET /api/docs/graph-data の実体 IndexManager.graph_data():
  limit の必須適用とサーバ上限、center+hops、名前/種別フィルタ、
  dangling edge を返さない、origins（ページ/セクション/抜粋/元ファイル）解決、
  doc_id 検証、旧索引（統計なし）は null。
- _graph_chunks(): 根拠へ doc_id/book_node_id/chunk_id/section_id/section/
  page/source/entity_ids/kind を付与し、生成回答は graph_answer に分離する。
外部サーバは使わない。
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


def _im_with_graph_doc(tmp_path, *, n_entities=6, chain=True):
    """fast 文書を登録し、その doc_id の BookIndex（木+KG）を手組みで永続化する。

    エンティティ 0..n-1、関係はチェーン 0→1→2→…（center+hops の検証用）。
    """
    import llmlab.bookindex as bx
    from llmlab.bookindex import Entity
    from llmlab.indexmanager import IndexManager

    p = tmp_path / "src" / "doc.txt"
    p.parent.mkdir(parents=True)
    p.write_text("# 第1章 概要\n本文の説明。" * 20, encoding="utf-8")
    im = IndexManager(storage_dir=tmp_path / "index")
    meta = im.add_document(p, title="グラフ文書")
    doc_id = meta["doc_id"]

    bi = bx.BookIndex()
    root = bi.add_node(type="Section", content="", book="グラフ文書",
                       title="グラフ文書", level=0)
    bi.roots.append(root.id)
    sec = bi.add_node(type="Section", content="", book="グラフ文書",
                      title="第2章 製品概要", level=1, parent=root.id)
    root.children.append(sec.id)
    text_nodes = []
    for i in range(n_entities):
        n = bi.add_node(type="Text", book="グラフ文書", parent=sec.id,
                        content=f"エンティティ{i}に関する本文。会社Aが提供する。",
                        page=i + 1)
        sec.children.append(n.id)
        text_nodes.append(n.id)
    types = ["Organization", "Product", "Person"]
    for i in range(n_entities):
        bi.entities[i] = Entity(id=i, name=f"エンティティ{i}",
                                type=types[i % 3], description=f"説明{i}",
                                origin_nodes=[text_nodes[i]])
    bi._ent_seq = n_entities
    if chain:
        for i in range(n_entities - 1):
            bi.relations.append((i, i + 1, "提供する"))
    book_dir = tmp_path / "index" / "bookindex" / doc_id
    bi.persist(book_dir)
    return im, doc_id, text_nodes


def test_graph_data_basic_shape_and_origins(tmp_path):
    _inject()
    im, doc_id, text_nodes = _im_with_graph_doc(tmp_path)
    data = im.graph_data(doc_id)
    assert data["doc_id"] == doc_id
    assert data["stats"]["total_nodes"] == 6
    assert data["stats"]["total_edges"] == 5
    assert data["stats"]["returned_nodes"] == 6
    assert not data["stats"]["truncated"]
    assert set(data["types"]) == {"Organization", "Product", "Person"}
    n0 = next(n for n in data["nodes"] if n["id"] == 0)
    assert n0["label"] == "エンティティ0" and n0["type"] == "Organization"
    o = n0["origins"][0]
    assert o["chunk_id"] == f"g:{doc_id}:{o['book_node_id']}"
    assert o["section"] == "第2章 製品概要", "見出しパスを解決（ルート書名は除く）"
    assert o["section_id"], "安定 section_id"
    assert o["page"] == 1 and "本文" in o["snippet"]
    assert o["source"], "元ファイル（node.source が無ければ doc の source_path）"


def test_graph_data_no_dangling_edges_and_limit(tmp_path):
    _inject()
    im, doc_id, _ = _im_with_graph_doc(tmp_path)
    data = im.graph_data(doc_id, limit=3)
    ids = {n["id"] for n in data["nodes"]}
    assert len(ids) == 3, "limit を必ず適用"
    assert data["stats"]["truncated"] is True
    for e in data["edges"]:
        assert e["source"] in ids and e["target"] in ids, "dangling edge を返さない"


def test_graph_data_server_side_limit_cap(tmp_path):
    _inject()
    from llmlab.indexmanager import GRAPH_DATA_MAX_LIMIT

    im, doc_id, _ = _im_with_graph_doc(tmp_path)
    data = im.graph_data(doc_id, limit=10_000)
    assert data["stats"]["returned_nodes"] <= GRAPH_DATA_MAX_LIMIT


def test_graph_data_center_and_hops(tmp_path):
    _inject()
    im, doc_id, _ = _im_with_graph_doc(tmp_path)   # チェーン 0-1-2-3-4-5
    one = im.graph_data(doc_id, center_entity_id=0, hops=1)
    assert {n["id"] for n in one["nodes"]} == {0, 1}
    two = im.graph_data(doc_id, center_entity_id=0, hops=2)
    assert {n["id"] for n in two["nodes"]} == {0, 1, 2}


def test_graph_data_filters(tmp_path):
    _inject()
    im, doc_id, _ = _im_with_graph_doc(tmp_path)
    by_name = im.graph_data(doc_id, name_filter="エンティティ2")
    assert {n["id"] for n in by_name["nodes"]} == {2}
    by_type = im.graph_data(doc_id, type_filter="Product")
    assert all(n["type"] == "Product" for n in by_type["nodes"])
    assert by_type["nodes"], "種別フィルタで該当が返る"


def test_graph_data_validates_doc_id(tmp_path):
    _inject()
    im, doc_id, _ = _im_with_graph_doc(tmp_path)
    with pytest.raises(ValueError):
        im.graph_data("../etc/passwd")
    with pytest.raises(ValueError):
        im.graph_data("a/b")
    with pytest.raises(KeyError):
        im.graph_data("d0000000000000000")


def test_graph_data_unbuilt_graph_is_graceful(tmp_path):
    """graph 未構築（fast 文書）の graph-data はエラーにせず built=False。"""
    _inject()
    from llmlab.indexmanager import IndexManager

    p = tmp_path / "src" / "f.txt"
    p.parent.mkdir(parents=True)
    p.write_text("# 章\n本文。" * 20, encoding="utf-8")
    im = IndexManager(storage_dir=tmp_path / "index")
    doc_id = im.add_document(p)["doc_id"]
    data = im.graph_data(doc_id)
    assert data["nodes"] == [] and data["edges"] == []
    assert data["stats"]["built"] is False
    assert data["stats"]["coverage_ratio"] is None, "旧/未構築は null（後方互換）"


def test_graph_chunks_traceable_and_answer_separated(tmp_path, monkeypatch):
    """graph 検索の根拠に追跡ID一式が付き、生成回答はチャンクに混ざらない。"""
    _inject()
    import llmlab.bookrag as brmod

    im, doc_id, text_nodes = _im_with_graph_doc(tmp_path)
    # meta を graph 扱いに（use_graph 検索の分岐に入るように）
    meta = im._read(im.docs_dir, doc_id)
    meta.update(graph_index=True, graph_status="ready")
    im._write(im.docs_dir, doc_id, meta)

    evid = [brmod.Evidence(node_id=text_nodes[i], title="第2章 製品概要",
                           page=i + 1, s_graph=0.5, s_text=0.9 - i * 0.1,
                           snippet=f"根拠スニペット{i}") for i in range(6)]
    fake = brmod.BookAnswer(text="生成された回答です。", evidence=evid)
    monkeypatch.setattr(brmod.BookRAG, "query", lambda self, q: fake)

    hits = im.search("会社Aは何を提供する？", doc_ids=[doc_id],
                     use_graph=True, max_chunks_per_doc=4)
    h = hits[0]
    assert h.used_graph is True
    assert h.graph_answer == "生成された回答です。", "生成回答は分離される"
    assert len(h.chunks) == 4, "cap は根拠だけに適用（生成回答を数えない）"
    for c in h.chunks:
        assert c["kind"] == "graph_evidence"
        assert c["doc_id"] == doc_id
        assert c["chunk_id"] == f"g:{doc_id}:{c['book_node_id']}"
        assert c["section"] == "第2章 製品概要" and c["section_id"]
        assert c["entity_ids"], "根拠ノードに紐づくエンティティID"
        assert c["source"], "元ファイル"
        assert "[BookRAG回答]" not in c["text"], "回答文がチャンクに混ざらない"
    d = h.to_dict()
    assert d["graph_answer"] and all(x["kind"] == "graph_evidence"
                                     for x in d["chunks"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
