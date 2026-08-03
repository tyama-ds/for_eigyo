"""ハイブリッド検索（ベクトル候補 ∪ 独立字句候補 → 正規化再ランク）の回帰テスト。

従来は「ベクトル上位候補の再ソート」だったため、完全一致の規程番号・金額・
型番がベクトル候補の外にあると救済できなかった。修正後は字句検索が独立の
候補系統として union される。

検証方法: retrieve_in_doc をラップして目的チャンクを **意図的にベクトル候補から
除外** し、それでも字句系統の union により最終結果へ入ることを確認する。
外部サーバは使わない（MockEmbedding / モンキーパッチのみ）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llmlab.indexmanager import IndexManager, _lexical_score, _lexical_terms  # noqa: E402


def _inject():
    from llama_index.core import Settings as LI
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    LI.embed_model = MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None


FILLERS = [f"# 第{i}節\n" + f"休暇の一般的な説明パラグラフその{i}。制度の背景説明。" * 30
           for i in range(1, 12)]


def _doc_with_needle(tmp_path, needle_text):
    """通常チャンク多数 + 目的の1文（needle）を含む文書を作る。"""
    body = "\n\n".join(FILLERS) + f"\n\n# 附則\n{needle_text}\n" + "補足の説明。" * 20
    p = tmp_path / "src" / "規程.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    im = IndexManager(storage_dir=tmp_path / "index")
    meta = im.add_document(p, title="規程")
    assert meta["chunk_count"] >= 6, "needle 以外にも十分なチャンクがある"
    return im, meta["doc_id"]


def _exclude_from_vector(monkeypatch, marker: str):
    """retrieve_in_doc から marker を含むチャンクを除外（=ベクトル候補外を模擬）。"""
    from llmlab.pagedrag import PagedRAG

    orig = PagedRAG.retrieve_in_doc

    def wrapped(self, question, *, doc_id, top_m):
        nodes = orig(self, question, doc_id=doc_id, top_m=1000)
        return [n for n in nodes
                if marker not in n.node.get_content()][:top_m]

    monkeypatch.setattr(PagedRAG, "retrieve_in_doc", wrapped)


@pytest.mark.parametrize("kind,needle,marker,question", [
    ("規程番号", "詳細は規程 REG-4711 号を参照すること。", "REG-4711",
     "REG-4711 の内容は？"),
    ("金額", "支給額は 123456 円とする。", "123456", "支給額 123456 円の根拠は？"),
    ("型番", "対象機器の型番は ZX-900B とする。", "ZX-900B", "ZX-900B の対象は？"),
    ("日本語固有語", "アルパカ手当は月額五万とする。", "アルパカ手当",
     "アルパカ手当について教えて"),
])
def test_exact_match_rescued_from_outside_vector_candidates(
        tmp_path, monkeypatch, kind, needle, marker, question):
    _inject()
    im, did = _doc_with_needle(tmp_path, needle)
    _exclude_from_vector(monkeypatch, marker)

    # サニティ: ベクトル候補には marker が入っていない
    vec = im._paged.retrieve_in_doc(question, doc_id=did, top_m=8)
    assert all(marker not in n.node.get_content() for n in vec)

    hits = im.search(question, doc_ids=[did], chunk_top_k_per_doc=3)
    texts = [c["text"] for c in hits[0].chunks]
    assert any(marker in t for t in texts), \
        f"{kind}: ベクトル候補外の完全一致チャンクが字句 union で救済される"
    top = hits[0].chunks[0]
    assert marker in top["text"], f"{kind}: 強一致は最上位に来る"
    assert top.get("chunk_id"), "救済チャンクにも chunk_id が付く"


def test_lexical_terms_japanese_ngram():
    ascii_terms, gram_seqs = _lexical_terms("REG-4711 とアルパカ手当の規定")
    assert "reg-4711" in ascii_terms, "英数トークン（規程番号）"
    flat = {g for seq in gram_seqs for g in seq}
    assert "アル" in flat and "手当" in flat, "日本語は文字2-gramで対応"
    s_hit, strong = _lexical_score("アルパカ手当は5万円", [],
                                   [["アル", "ルパ", "パカ", "カ手", "手当"]])
    assert s_hit > 0.9 and strong, "連続一致する日本語固有語は強一致"
    # 「について」程度の助詞連結（連続3-gram）では強一致にならない
    _s, strong_particle = _lexical_score("これについて述べる", [],
                                         [["につ", "つい", "いて", "て教", "教え"]])
    assert not strong_particle
    s_miss, strong2 = _lexical_score("無関係の本文", [], [["アル", "ルパ"]])
    assert s_miss == 0.0 and not strong2


def test_candidate_k_settings_are_separate(tmp_path, monkeypatch):
    """vector/lexical の候補件数を別々に制御できる。"""
    _inject()
    im, did = _doc_with_needle(tmp_path, "対象型番は ZX-900B とする。")
    from llmlab.pagedrag import PagedRAG

    seen = {}
    orig = PagedRAG.retrieve_in_doc

    def spy(self, question, *, doc_id, top_m):
        seen["vector_top_m"] = top_m
        return orig(self, question, doc_id=doc_id, top_m=top_m)

    monkeypatch.setattr(PagedRAG, "retrieve_in_doc", spy)
    cands = im._doc_chunk_candidates("ZX-900B", did, 2, vector_k=5, lexical_k=1)
    assert seen["vector_top_m"] == 5, "vector_candidate_k_per_doc が効く"
    lex = im._lexical_candidates("ZX-900B", did, 1)
    assert len(lex) == 1, "lexical_candidate_k_per_doc が効く"
    assert cands, "候補が返る"


def test_doc_level_rescue_by_exact_term(tmp_path, monkeypatch):
    """自動文書選定でも、完全一致を含む文書がベクトル候補外なら補完される。"""
    _inject()
    im = IndexManager(storage_dir=tmp_path / "index")
    src = tmp_path / "src"
    src.mkdir()
    for i in range(4):
        p = src / f"noise{i}.txt"
        p.write_text(f"# 一般\n一般的な文書{i}の説明。" * 20, encoding="utf-8")
        im.add_document(p)
    needle = src / "needle.txt"
    needle.write_text("# 附則\n特殊な規程 REG-9999 を定める。" * 10, encoding="utf-8")
    needle_id = im.add_document(needle)["doc_id"]

    # 文書候補選定（rank_documents）から needle 文書を意図的に除外する
    from llmlab.pagedrag import PagedRAG

    orig = PagedRAG.rank_documents

    def wrapped(self, question, **kw):
        return [r for r in orig(self, question, **kw) if r.doc_id != needle_id]

    monkeypatch.setattr(PagedRAG, "rank_documents", wrapped)
    hits = im.search("REG-9999 の規定は？", document_top_n=3)
    assert any(h.doc_id == needle_id for h in hits), \
        "英数トークン完全一致の文書が文書選定でも救済される"


def test_search_accepts_new_kwargs(tmp_path):
    """search/ask が新しい設定値を受け付ける（後方互換の確認込み）。"""
    _inject()
    im, did = _doc_with_needle(tmp_path, "REG-1 を定める。")
    hits = im.search("規程", doc_ids=[did],
                     vector_candidate_k_per_doc=4, lexical_candidate_k_per_doc=2)
    assert hits and hits[0].chunks
    hits2 = im.search("規程", doc_ids=[did])   # 旧呼び出しもそのまま動く
    assert hits2 and hits2[0].chunks


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
