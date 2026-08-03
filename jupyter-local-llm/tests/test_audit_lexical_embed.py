"""監査項目7〜9の回帰テスト: Entity埋め込みの128件バッチ・
英数トークンの完全一致・200文書サイレント打ち切りの撤廃。

外部サーバは使わない（MockEmbedding + モック抽出）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llmlab.indexmanager import _ascii_tokens, _lexical_score, _lexical_terms  # noqa: E402


def _inject():
    from llama_index.core import Settings as LI
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    LI.embed_model = MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None


# --------------------------------------------------------------------------
# 7. Entity 埋め込みの固定サイズバッチ
# --------------------------------------------------------------------------

def test_entity_embeddings_batched_at_128(monkeypatch):
    """300件超の Entity でも embed() 1回あたり最大128件・順序維持。"""
    _inject()
    import llmlab.bookindex as bx

    calls: list[list[str]] = []

    def fake_embed(texts):
        calls.append(list(texts))
        return np.ones((len(texts), 4), dtype=np.float32)

    monkeypatch.setattr(bx, "embed", fake_embed)
    # ノード5個 × 各64エンティティ = 320件（>= 300）
    monkeypatch.setattr(
        bx, "_extract_graph",
        lambda node, fail_fast=True: {
            "entities": [{"name": f"E{node.id}-{j}", "type": "T",
                          "description": "d"} for j in range(64)],
            "relations": []})
    monkeypatch.setattr(bx, "MAX_ENTITIES_PER_NODE", 64)

    bi = bx.BookIndex()
    root = bi.add_node(type="Section", content="", book="B", title="r", level=1)
    bi.roots.append(root.id)
    ids = []
    for i in range(5):
        n = bi.add_node(type="Text", book="B", parent=root.id,
                        content=f"ノード{i}の本文。" * 10)
        root.children.append(n.id)
        ids.append(n.id)
    bx.build_graph(bi, ids, max_workers=1, all_nodes=True)

    embed_calls = [c for c in calls if c]
    total = sum(len(c) for c in embed_calls)
    assert total >= 300, f"300件以上を埋め込む（実際 {total}）"
    assert all(len(c) <= bx.GRAPH_EMBED_BATCH_SIZE for c in embed_calls), \
        f"すべての embed() 呼び出しが {bx.GRAPH_EMBED_BATCH_SIZE} 件以下"
    assert len(embed_calls) >= 3, "複数バッチに分かれる"
    flat = [t for c in embed_calls for t in c]
    assert flat[0].startswith("E") and flat == sorted(
        flat, key=lambda s: (int(s.split("E")[1].split("-")[0]),
                             int(s.split("-")[1].split(" ")[0]))), \
        "Entity の順序と node 対応が維持される"


def test_embed_batched_helper_order():
    import llmlab.bookindex as bx

    calls = []
    orig = bx.embed
    try:
        def fake(texts):
            calls.append(len(texts))
            return np.arange(len(texts), dtype=np.float32).reshape(-1, 1)

        bx.embed = fake
        out = bx.embed_batched([f"t{i}" for i in range(300)], batch_size=128)
        assert calls == [128, 128, 44]
        assert out.shape == (300, 1)
        assert out[0, 0] == 0.0 and out[128, 0] == 0.0, "バッチ境界で順序が保たれる"
    finally:
        bx.embed = orig


# --------------------------------------------------------------------------
# 8. 英数トークンの完全一致（部分文字列一致にしない）
# --------------------------------------------------------------------------

def test_token_exact_match_not_substring():
    terms, _ = _lexical_terms("REG-4711 の内容")
    assert "reg-4711" in terms
    s_bad, strong_bad = _lexical_score("XREG-47110Z を参照する。", ["reg-4711"], [])
    assert s_bad == 0.0 and strong_bad is False, \
        "XREG-47110Z は REG-4711 の完全一致ではない"
    s_ok, strong_ok = _lexical_score("規程 REG-4711 号を参照する。", ["reg-4711"], [])
    assert s_ok > 0 and strong_ok is True, "トークン一致は強一致"


def test_token_match_normalizes_trailing_punct():
    assert "zx-900b" in _ascii_tokens("型番は ZX-900B. とする")
    s, strong = _lexical_score("type ZX-900B.", ["zx-900b"], [])
    assert strong, "文末ピリオドつきトークンも一致する"


def test_docs_with_exact_terms_token_semantics(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    src = tmp_path / "src"
    src.mkdir()
    im = IndexManager(storage_dir=tmp_path / "index")
    (src / "noise.txt").write_text("XREG-47110Z の説明。" * 15, encoding="utf-8")
    im.add_document(src / "noise.txt")
    (src / "hit.txt").write_text("規程 REG-4711 号。" * 15, encoding="utf-8")
    hit_id = im.add_document(src / "hit.txt")["doc_id"]
    found = im._docs_with_exact_terms(["reg-4711"])
    assert found == {hit_id}, "部分文字列一致の文書は救済対象にしない"


# --------------------------------------------------------------------------
# 9. 200文書のサイレント打ち切り撤廃（201件目以降も救済）
# --------------------------------------------------------------------------

def test_exact_term_rescue_beyond_200_docs(tmp_path):
    """205文書の最後の1件だけに完全一致語 → 自動選定で漏れない。"""
    _inject()
    from llmlab.indexmanager import IndexManager

    src = tmp_path / "src"
    src.mkdir()
    im = IndexManager(storage_dir=tmp_path / "index")
    for i in range(204):
        p = src / f"n{i:03d}.txt"
        p.write_text(f"一般文書{i:03d}の本文。", encoding="utf-8")
        im.add_document(p)
    needle = src / "needle.txt"
    needle.write_text("特殊規程 REG-99999 を定める。", encoding="utf-8")
    needle_id = im.add_document(needle)["doc_id"]
    assert len(im.documents()) == 205

    found = im._docs_with_exact_terms(["reg-99999"])
    assert needle_id in found, "201件目以降も既定で走査する（打ち切りなし）"
    assert im._docs_with_exact_terms(["reg-99999"], limit=10) == set(), \
        "limit は呼び出し側が明示した場合だけ適用"

    hits = im.search("REG-99999 の規定は？", document_top_n=3)
    assert any(h.doc_id == needle_id for h in hits), \
        "205文書でも完全一致文書が自動選定に入る"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
