"""階層 Map-Reduce（予算管理・追跡・失敗継続）の回帰テスト。

- Map 出力に max_tokens、部分回答に文字上限、Reduce 入力に予算。
- 予算超過時は 4〜8 文書単位の中間統合 → さらに統合（13/50/100 件でも
  単一の巨大プロンプトを作らない）。
- 文書を黙って落とさず、answered / no_info / failed を記録して追跡できる。
外部サーバは使わない（llm_text を記録用モックに差し替え）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llmlab.indexmanager import (  # noqa: E402
    CONTEXT_CHAR_BUDGET,
    MAP_MAX_TOKENS,
    PARTIAL_CHAR_CAP,
    REDUCE_GROUP_SIZE,
    IndexManager,
)


def _inject():
    from llama_index.core import Settings as LI
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    LI.embed_model = MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None


def _record_llm(monkeypatch, reply="部分回答です。", fail_titles=(),
                noinfo_titles=()):
    """llm_text を記録モックへ。fail_titles を含むプロンプトは例外を投げる。"""
    import llmlab.bookindex as bx

    calls = []

    def fake(prompt, **kw):
        calls.append({"prompt": prompt, "kw": kw})
        for t in fail_titles:
            if f"文書「{t}」" in prompt:
                raise RuntimeError("LLM失敗（模擬）")
        for t in noinfo_titles:
            if f"文書「{t}」" in prompt:
                return "該当情報なし"
        return reply

    monkeypatch.setattr(bx, "llm_text", fake)
    return calls


def _make_docs(tmp_path, n):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    im = IndexManager(storage_dir=tmp_path / "index")
    ids = []
    for i in range(1, n + 1):
        p = src / f"doc{i:02d}.txt"
        p.write_text(f"# 第{i}章\n文書{i:02d}の本文 TERM{i:02d}。" * 12,
                     encoding="utf-8")
        ids.append(im.add_document(p, title=f"文書{i:02d}")["doc_id"])
    return im, ids


# --------------------------------------------------------------------------
# _hierarchical_reduce 単体（50/100 件の階層化・予算・巨大プロンプト禁止）
# --------------------------------------------------------------------------

def _reduce_with_fakes(monkeypatch, n_partials, text_len=1200):
    _inject()
    calls = _record_llm(monkeypatch, reply="統合しました。")
    im = IndexManager.__new__(IndexManager)   # ストレージ不要（メソッド単体）
    partials = [{"label": f"文書{i:03d}", "text": "あ" * text_len}
                for i in range(n_partials)]
    text, trace = im._hierarchical_reduce("依頼: 比較して", partials)
    return text, trace, calls


def test_reduce_50_docs_is_multi_stage(monkeypatch, tmp_path):
    text, trace, calls = _reduce_with_fakes(monkeypatch, 50)
    assert text == "統合しました。"
    assert trace["levels"] >= 2, "50件は中間統合を挟む（単発Reduceにしない）"
    assert len(trace["level0_groups"]) == -(-50 // REDUCE_GROUP_SIZE)
    assert sorted(i for g in trace["level0_groups"] for i in g) == list(range(50)), \
        "全部分結果がいずれかのグループに属す（黙って落とさない）"
    for c in calls:
        assert len(c["prompt"]) <= CONTEXT_CHAR_BUDGET + 2000, \
            "どの Reduce プロンプトも予算内（巨大プロンプト禁止）"


def test_reduce_100_docs_within_budget(monkeypatch, tmp_path):
    _text, trace, calls = _reduce_with_fakes(monkeypatch, 100, text_len=3000)
    assert trace["levels"] >= 2
    for c in calls:
        assert len(c["prompt"]) <= CONTEXT_CHAR_BUDGET + 2000
    # 部分回答は PARTIAL_CHAR_CAP に丸めてから入る
    assert all("あ" * (PARTIAL_CHAR_CAP + 1) not in c["prompt"] for c in calls)


def test_reduce_failure_keeps_group_content(monkeypatch, tmp_path):
    """中間統合が失敗してもグループの文書を黙って落とさない。"""
    _inject()
    import llmlab.bookindex as bx

    state = {"n": 0}

    def flaky(prompt, **kw):
        state["n"] += 1
        if state["n"] == 1:   # 最初の中間統合だけ失敗
            raise RuntimeError("boom")
        return "統合しました。"

    monkeypatch.setattr(bx, "llm_text", flaky)
    im = IndexManager.__new__(IndexManager)
    partials = [{"label": f"文書{i}", "text": f"内容{i}"} for i in range(13)]
    text, trace = im._hierarchical_reduce("依頼: 比較", partials)
    assert text == "統合しました。"
    assert trace["levels"] >= 2


# --------------------------------------------------------------------------
# ask() 統合（13文書・max_tokens・状態記録・追跡）
# --------------------------------------------------------------------------

def test_ask_13_docs_hierarchical_and_tracked(tmp_path, monkeypatch):
    _inject()
    im, ids = _make_docs(tmp_path, 13)
    calls = _record_llm(monkeypatch)
    r = im.ask("全文書を比較してください", doc_ids=ids)

    per_doc = {p["doc_id"]: p for p in r.per_doc}
    assert set(per_doc) == set(ids), "13文書すべてが Map 対象"
    assert all(p["status"] == "answered" for p in r.per_doc)
    assert all("group" in p for p in r.per_doc), "最終回答から文書別結果へ追跡できる"
    assert r.reduce_info["levels"] >= 2, "13件は 6件グループ→中間統合→最終統合"

    map_calls = [c for c in calls if "### 文書「" in c["prompt"]]
    assert len(map_calls) == 13
    for c in map_calls:
        assert c["prompt"].count("### 文書「") == 1, "Map は1文書ずつ"
        assert c["kw"].get("max_tokens") == MAP_MAX_TOKENS, "Map に max_tokens"
    reduce_calls = [c for c in calls if c not in map_calls]
    for c in reduce_calls:
        assert c["kw"].get("max_tokens"), "Reduce にも出力上限"
        assert len(c["prompt"]) <= CONTEXT_CHAR_BUDGET + 2000


def test_ask_one_doc_failure_does_not_stop(tmp_path, monkeypatch):
    _inject()
    im, ids = _make_docs(tmp_path, 7)
    _record_llm(monkeypatch, fail_titles=("文書03",), noinfo_titles=("文書05",))
    r = im.ask("比較してください", doc_ids=ids)
    st = {p["title"]: p["status"] for p in r.per_doc}
    assert st["文書03"] == "failed", "失敗は記録される（全体は停止しない）"
    assert st["文書05"] == "no_info", "該当情報なしも記録される"
    assert sum(1 for s in st.values() if s == "answered") == 5
    assert r.text, "最終回答は生成される"


def test_summarize_uses_hierarchical_reduce(tmp_path, monkeypatch):
    _inject()
    im, ids = _make_docs(tmp_path, 14)
    calls = _record_llm(monkeypatch, reply="要約です。")
    r = im.summarize()
    assert len(r.per_doc) == 14
    assert all(p["status"] == "answered" for p in r.per_doc)
    assert r.reduce_info.get("levels", 1) >= 2, "summarize も階層 Reduce"
    assert all("group" in p for p in r.per_doc)
    for c in calls:
        assert len(c["prompt"]) <= CONTEXT_CHAR_BUDGET + 2000


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
