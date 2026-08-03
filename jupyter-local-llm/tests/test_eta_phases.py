"""ETA の phase_id 分離の回帰テスト。

従来は stage 文字列が変わるたびに計測がリセットされ、フォルダ取り込みや
Map 処理（stage にファイル名・文書名を含む）で ETA が最後まで出なかった。
修正後はリセット判定に固定の phase_id を使い、名前は detail に入る。
外部サーバは使わない（MockLLM/MockEmbedding）。
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llmlab.app import EtaTracker, _emitter  # noqa: E402


def _inject():
    from llama_index.core import Settings as LI
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    LI.embed_model = MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None


def test_folder_ingest_eta_survives_changing_filenames(tmp_path):
    """3ファイル取り込み: 2件目以降の eta_sec が None ではない。"""
    _inject()
    from llmlab.indexmanager import IndexManager

    src = tmp_path / "docs"
    src.mkdir()
    for i in range(3):
        (src / f"規程{i:02d}.txt").write_text(f"# 章\n文書{i}の本文。" * 15,
                                              encoding="utf-8")
    im = IndexManager(storage_dir=tmp_path / "index")
    events = []
    im.add_folder(src, progress=events.append)

    ingest = [e for e in events if e.get("phase_id") == "folder_ingest"]
    assert ingest, "フォルダ取り込みは固定 phase_id で流れる"
    assert all(e["stage"] == "文書を取り込み" for e in ingest), \
        "stage は固定（ファイル名は detail へ）"
    assert any("規程01" in e.get("detail", "") for e in ingest), \
        "ファイル名は detail に入る"
    assert all(e.get("unit") == "documents" for e in ingest)

    # 実イベント列を EtaTracker（決定的な時計）へ通す
    clock = itertools.count(0.0, 1.0)
    tracker = EtaTracker(clock=lambda: next(clock))
    annotated = [tracker.annotate(e) for e in ingest]
    later = [a for a in annotated if a.get("current", 0) >= 1]
    assert later, "2件目以降のイベントがある"
    assert all(a.get("eta_sec") is not None for a in later), \
        "文書名が変わっても同一 phase なら ETA が維持される"


def test_eta_resets_only_on_phase_change():
    """graph など別 phase へ移ったときだけリセットされる。"""
    clock = itertools.count(0.0, 1.0)
    t = EtaTracker(clock=lambda: next(clock))
    # folder_ingest: detail（ファイル名）が変わっても計測は継続
    t.annotate({"phase_id": "folder_ingest", "stage": "文書を取り込み",
                "current": 0, "total": 5, "detail": "a.pdf"})
    e2 = t.annotate({"phase_id": "folder_ingest", "stage": "文書を取り込み",
                     "current": 2, "total": 5, "detail": "c.pdf"})
    assert e2["eta_sec"] is not None
    # graph phase へ切替 → リセット（最初の1単位までは計算中）
    g1 = t.annotate({"phase_id": "graph_extract", "stage": "抽出: エンティティ/関係",
                     "current": 0, "total": 10})
    assert g1["eta_sec"] is None, "phase 切替時のみリセット"
    g2 = t.annotate({"phase_id": "graph_extract", "stage": "抽出: エンティティ/関係",
                     "current": 5, "total": 10})
    assert g2["eta_sec"] is not None


def test_eta_state_not_shared_between_tasks():
    """複数タスク（emit）間で ETA 状態が共有されない。"""
    from queue import Queue

    q1, q2 = Queue(), Queue()
    e1, e2 = _emitter(q1), _emitter(q2)
    evt = {"phase_id": "map_answer", "stage": "文書別に回答",
           "current": 3, "total": 10}
    e1(dict(evt))
    a1 = q1.get_nowait()
    e2(dict(evt))       # 別タスクの最初のイベント
    a2 = q2.get_nowait()
    # タスク1で進捗が進んでいても、タスク2は自分の計測を最初からやり直す
    assert a2.get("eta_sec") is None or a2["eta_sec"] != a1.get("eta_sec") or \
        (a1.get("eta_sec") is None and a2.get("eta_sec") is None)
    # 同一 phase の初回観測（done=0）はどちらも「計算中」
    assert a1.get("eta_sec") is None and a2.get("eta_sec") is None


def test_map_and_reduce_use_distinct_phases(tmp_path, monkeypatch):
    """ask() の Map と 中間統合/最終統合が別 phase として流れる。"""
    _inject()
    import llmlab.bookindex as bx
    from llmlab.indexmanager import IndexManager

    monkeypatch.setattr(bx, "llm_text", lambda prompt, **kw: "回答")
    src = tmp_path / "docs"
    src.mkdir()
    im = IndexManager(storage_dir=tmp_path / "index")
    ids = []
    for i in range(13):
        p = src / f"d{i:02d}.txt"
        p.write_text(f"# 章\n文書{i}の本文。" * 15, encoding="utf-8")
        ids.append(im.add_document(p, title=f"文書{i:02d}")["doc_id"])

    events = []
    im.ask("比較して", doc_ids=ids, progress=events.append)
    phases = {e.get("phase_id") for e in events}
    assert "map_answer" in phases
    assert any(str(p).startswith("reduce_l") for p in phases), "中間統合 phase"
    assert "reduce_final" in phases
    maps = [e for e in events if e.get("phase_id") == "map_answer"]
    assert all(e["stage"] == "文書別に回答" for e in maps), "stage は固定"
    assert any(e.get("detail") for e in maps), "文書名は detail"
    assert [e["current"] for e in maps] == list(range(13)) + [13]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
