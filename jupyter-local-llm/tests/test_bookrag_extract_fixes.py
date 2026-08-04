"""BookRAG 抽出不全・低速化修正（v0.9.2）の回帰テスト。

1. _extract_graph: 切り詰め JSON（entities キー欠落）→ None（badjson として再試行）、
   {"entities": [], ...} → 従来どおり empty、正常 → ok。max_tokens は 1600/2200。
2. resolve_workers: None=自動セーフモード（50超は1並列）、明示 int は 50 ノード超でも尊重。
3. app.py: _graph_settings_from が graph_max_workers を通す、履歴プレビューの strip_think。
外部サーバは使わない（LLM 呼び出し・埋め込みはモック）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from queue import Queue

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


def _node():
    import llmlab.bookindex as bx

    return bx.TreeNode(id=1, type="Text",
                       content="日本製鉄の粗鋼生産量は3500万トンである。", book="B")


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


def _mock_embed(monkeypatch):
    import llmlab.bookindex as bx

    monkeypatch.setattr(bx, "embed",
                        lambda texts: np.ones((len(texts), 4), dtype=np.float32))


# ---- 1. _extract_graph の切り詰め対策 --------------------------------------


def test_truncated_json_inner_dict_returns_none(monkeypatch):
    """切り詰められた JSON から内側のエンティティ dict だけが拾われた場合は None。

    旧実装は result.get("entities", []) が [] になり「empty」に誤分類され、
    チェックポイントに完了として記録されて再抽出されなかった。
    """
    import llmlab.bookindex as bx

    # parse_json_answer が「最後に完結した JSON」として内側 dict を返した状況
    monkeypatch.setattr(bx, "llm_json",
                        lambda *a, **k: {"name": "日本製鉄", "type": "Organization"})
    assert bx._extract_graph(_node()) is None


def test_toplevel_array_returns_none(monkeypatch):
    """トップレベルが配列などスキーマ逸脱も empty ではなく None（badjson）。"""
    import llmlab.bookindex as bx

    monkeypatch.setattr(bx, "llm_json", lambda *a, **k: [{"name": "日本製鉄"}])
    assert bx._extract_graph(_node()) is None


def test_legit_zero_entities_stays_empty(monkeypatch):
    """entities キーがあり空 = 正当な「エンティティ0」は従来どおり empty 扱い。"""
    import llmlab.bookindex as bx

    monkeypatch.setattr(bx, "llm_json",
                        lambda *a, **k: {"entities": [], "relations": []})
    assert bx._extract_graph(_node()) == {"entities": [], "relations": []}


def test_normal_extraction_ok(monkeypatch):
    import llmlab.bookindex as bx

    monkeypatch.setattr(bx, "llm_json", lambda *a, **k: {
        "entities": [{"name": "日本製鉄", "type": "Organization",
                      "description": "鉄鋼メーカー"}],
        "relations": [{"source": "日本製鉄", "target": "粗鋼",
                       "type": "Produces"}],
    })
    data = bx._extract_graph(_node())
    assert data is not None
    assert len(data["entities"]) == 1 and len(data["relations"]) == 1


def test_extract_max_tokens_enlarged(monkeypatch):
    """日本語の満額出力（概算 900〜1300 tok）が収まる上限になっている。"""
    import llmlab.bookindex as bx

    seen: dict = {}

    def fake(prompt, **kw):
        seen.update(kw)
        return {"entities": [], "relations": []}

    monkeypatch.setattr(bx, "llm_json", fake)
    bx._extract_graph(_node(), fail_fast=True)
    assert seen["max_tokens"] == 1600
    bx._extract_graph(_node(), fail_fast=False)
    assert seen["max_tokens"] == 2200


def test_build_graph_counts_truncation_as_badjson(tmp_path, monkeypatch):
    """切り詰め応答は build_graph の統計で badjson になり、empty に紛れない。

    チェックポイントにも badjson で記録される（完走後はファイル自体が削除される
    ため、保存内容は _save_checkpoint のスパイで確認する）。
    """
    _inject()
    import llmlab.bookindex as bx

    _mock_embed(monkeypatch)
    monkeypatch.setattr(bx, "llm_json",
                        lambda *a, **k: {"name": "内側だけ", "type": "T"})
    saved: list[dict] = []
    orig_save = bx._save_checkpoint

    def spy(path, ckpt, bi):
        saved.append(json.loads(json.dumps(ckpt)))
        orig_save(path, ckpt, bi)

    monkeypatch.setattr(bx, "_save_checkpoint", spy)
    bi, ids = _mini_bi(4)
    ck = tmp_path / "graph_progress.json"
    stats = bx.build_graph(bi, ids, max_workers=1, all_nodes=True,
                           checkpoint_path=ck)
    assert stats["extract_badjson"] == 4
    assert stats["extract_empty"] == 0
    assert saved and all(v["status"] == "badjson"
                         for v in saved[-1]["nodes"].values()), \
        "badjson は完了（ok/empty）として記録されない"


def test_badjson_checkpoint_entries_are_retried(tmp_path, monkeypatch):
    """チェックポイントに badjson で残ったノードは再実行で再抽出される。

    （ok/empty は完了扱いでスキップされるのと対照的に。）
    """
    _inject()
    import llmlab.bookindex as bx

    _mock_embed(monkeypatch)
    bi, ids = _mini_bi(4)
    ck = tmp_path / "graph_progress.json"
    ck.write_text(json.dumps({
        "version": bx._CHECKPOINT_VERSION,
        "signature": bx._graph_signature(bi),
        "nodes": {str(i): {"status": "badjson", "entities": [],
                           "relations": [], "error": None} for i in ids},
    }), encoding="utf-8")
    calls: list[int] = []

    def extract(node, fail_fast=True):
        calls.append(node.id)
        return {"entities": [{"name": f"E{node.id}", "type": "T",
                              "description": "d"}], "relations": []}

    monkeypatch.setattr(bx, "_extract_graph", extract)
    stats = bx.build_graph(bi, ids, max_workers=1, all_nodes=True,
                           checkpoint_path=ck)
    assert sorted(set(calls)) == sorted(ids), "badjson の4ノードすべて再抽出"
    assert stats["extract_ok"] == 4


def test_versionless_old_checkpoint_is_discarded(tmp_path, monkeypatch):
    """version の無い旧チェックポイントは破棄され、全ノードが再抽出される。

    旧版（v0.9.1 以前）は切り詰め応答を「空=完了」と誤記録しており、
    再開時にスキップされ続けるため、形式バージョンで一括無効化する。
    """
    _inject()
    import llmlab.bookindex as bx

    _mock_embed(monkeypatch)
    bi, ids = _mini_bi(4)
    ck = tmp_path / "graph_progress.json"
    ck.write_text(json.dumps({  # 旧形式: version キーが無い + 全ノード「空=完了」
        "signature": bx._graph_signature(bi),
        "nodes": {str(i): {"status": "empty", "entities": [],
                           "relations": [], "error": None} for i in ids},
    }), encoding="utf-8")
    calls: list[int] = []

    def extract(node, fail_fast=True):
        calls.append(node.id)
        return {"entities": [{"name": f"E{node.id}", "type": "T",
                              "description": "d"}], "relations": []}

    monkeypatch.setattr(bx, "_extract_graph", extract)
    msgs: list[str] = []
    with bx.log_to(msgs.append):
        stats = bx.build_graph(bi, ids, max_workers=1, all_nodes=True,
                               checkpoint_path=ck)
    assert sorted(set(calls)) == sorted(ids), "旧形式は破棄され全ノード再抽出"
    assert stats["extract_ok"] == 4
    assert any("旧形式" in m for m in msgs)


# ---- 2. 並列数の決定ロジック（resolve_workers） ----------------------------


def test_resolve_workers_auto_safe_mode():
    import llmlab.bookindex as bx

    assert bx.resolve_workers(None, 50) == 2
    assert bx.resolve_workers(None, 51) == 1


def test_resolve_workers_explicit_is_respected():
    """明示指定は 50 ノード超でもそのまま使われる（旧版は 1 に強制していた）。"""
    import llmlab.bookindex as bx

    assert bx.resolve_workers(8, 1000) == 8
    assert bx.resolve_workers(1, 10) == 1
    assert bx.resolve_workers(0, 10) == 1  # 不正値は 1 に切り上げ


def test_resolve_workers_high_value_logs_caution():
    import llmlab.bookindex as bx

    msgs: list[str] = []
    with bx.log_to(msgs.append):
        assert bx.resolve_workers(3, 100) == 3
    assert any("同時処理耐性" in m for m in msgs)
    msgs.clear()
    with bx.log_to(msgs.append):
        assert bx.resolve_workers(2, 100) == 2
    assert not any("同時処理耐性" in m for m in msgs)


def test_build_graph_explicit_workers_over_50_nodes(tmp_path, monkeypatch):
    """60 ノードでも max_workers=8 明示なら進捗表示が「8並列」になる。"""
    _inject()
    import llmlab.bookindex as bx

    _mock_embed(monkeypatch)
    monkeypatch.setattr(
        bx, "_extract_graph",
        lambda node, fail_fast=True: {
            "entities": [{"name": f"E{node.id}", "type": "T",
                          "description": "d"}], "relations": []})
    bi, ids = _mini_bi(60)
    descs: list[str] = []
    with bx.progress_to(lambda d, c, t: descs.append(d)):
        stats = bx.build_graph(bi, ids, max_workers=8, all_nodes=True)
    assert stats["processed_nodes"] == 60
    assert any("8並列" in d for d in descs), \
        "明示指定した並列数が抽出フェーズにそのまま反映される"


def test_build_graph_default_is_auto_safe(tmp_path, monkeypatch):
    """未指定（None）なら 50 ノード超で自動的に 1 並列へ落ちる。"""
    _inject()
    import llmlab.bookindex as bx

    _mock_embed(monkeypatch)
    monkeypatch.setattr(
        bx, "_extract_graph",
        lambda node, fail_fast=True: {"entities": [], "relations": []})
    bi, ids = _mini_bi(60)
    descs: list[str] = []
    msgs: list[str] = []
    with bx.log_to(msgs.append), bx.progress_to(lambda d, c, t: descs.append(d)):
        bx.build_graph(bi, ids, all_nodes=True)
    assert any("1並列" in d for d in descs)
    assert any("自動セーフモード: 並列数を 1" in m for m in msgs)


def test_bookrag_default_max_workers_is_none():
    import inspect

    from llmlab.bookrag import BookRAG

    sig = inspect.signature(BookRAG.__init__)
    assert sig.parameters["max_workers"].default is None


# ---- 3. GUI/サーバ連携（並列数の通過・履歴プレビュー） ----------------------


def test_graph_settings_from_passes_max_workers():
    from llmlab.app import _graph_settings_from

    out = _graph_settings_from({"graph_settings": {
        "graph_max_workers": 5, "graph_max_nodes": 100,
        "graph_chunk_chars": 1200, "er_use_llm": False}})
    assert out["graph_max_workers"] == 5


def test_history_preview_strips_think(monkeypatch):
    """推論系モデルの <think> が履歴プレビューを埋めない（本文から始まる）。"""
    import llmlab.app as app
    import llmlab.workspace as wsmod

    class _R:
        text = "<think>とても長い推論の過程……</think>回答本文です。"
        partials: list = []

    class _FakeMultiRAG:
        def __init__(self, *a, **k):
            pass

        def ask(self, q):
            return _R()

    monkeypatch.setattr(wsmod, "MultiRAG", _FakeMultiRAG)
    records: list[dict] = []
    monkeypatch.setattr(app, "_history_append", records.append)
    app._tasks["t-preview-test"] = Queue()
    try:
        app._run_task("t-preview-test", {"action": "ask", "question": "q",
                                         "indexes": ["idx"]})
    finally:
        app._tasks.pop("t-preview-test", None)
    assert records, "履歴が記録される"
    assert records[0]["preview"].startswith("回答本文")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
