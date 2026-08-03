"""進捗転送（bookindex.progress_to）と ETA（app.EtaTracker）の回帰テスト。

仕様:
- progress() は progress_to() のコンテキスト内で (desc, 0, total) → … → (desc, total, total)
  を必ず転送する（受信側がフェーズごとの ETA を計算するための計測点）。
- EtaTracker はフェーズ（stage）が切り替わったら計測をリセットし、
  フェーズ内の最初の1件が完了するまでは eta_sec=None を返す
  （UI は「残り時間を計算中…」を表示する）。
"""

import threading

import pytest

import llmlab.bookindex as bx
from llmlab.app import EtaTracker


def test_progress_forwards_0_to_total():
    """進捗イベントが 0..total まで欠けなく転送される。"""
    events = []
    with bx.progress_to(lambda d, c, t: events.append((d, c, t))):
        consumed = list(bx.progress(range(5), total=5, desc="抽出"))
    assert consumed == list(range(5))
    assert [c for _d, c, _t in events] == [0, 1, 2, 3, 4, 5]
    assert all(d == "抽出" and t == 5 for d, _c, t in events)


def test_progress_total_inferred_from_len():
    """total 未指定でも len() から推定して転送する。"""
    events = []
    with bx.progress_to(lambda d, c, t: events.append((d, c, t))):
        list(bx.progress(["a", "b", "c"], desc="x"))
    assert events[0] == ("x", 0, 3)
    assert events[-1] == ("x", 3, 3)


def test_progress_to_restores_and_is_thread_local():
    """コンテキストを抜けると転送されない。他スレッドにも影響しない。"""
    events = []
    with bx.progress_to(lambda d, c, t: events.append((d, c, t))):
        pass
    list(bx.progress(range(3), total=3, desc="外"))
    assert events == []

    other: list = []

    def _other_thread():
        list(bx.progress(range(2), total=2, desc="他"))

    with bx.progress_to(lambda d, c, t: events.append((d, c, t))):
        th = threading.Thread(target=_other_thread)
        th.start()
        th.join()
    assert events == []  # 他スレッドの progress() は転送されない
    assert other == []


def test_progress_callback_error_does_not_break_iteration():
    """転送コールバックが例外を投げても本処理は完走する。"""
    def _boom(_d, _c, _t):
        raise RuntimeError("boom")

    with bx.progress_to(_boom):
        assert list(bx.progress(range(4), total=4, desc="x")) == [0, 1, 2, 3]


def test_eta_none_until_first_unit_then_measured():
    """最初の1件完了までは eta_sec=None（=残り時間を計算中）。以降は実測レート。"""
    clock = iter([0.0, 10.0, 12.0])
    t = EtaTracker(clock=lambda: next(clock))

    e0 = t.annotate({"stage": "抽出", "current": 0, "total": 10})
    assert e0["eta_sec"] is None
    assert e0["estimated_total_sec"] is None

    e5 = t.annotate({"stage": "抽出", "current": 5, "total": 10})  # 5件/10秒 → 2秒/件
    assert e5["eta_sec"] == pytest.approx(10.0)
    assert e5["estimated_total_sec"] == pytest.approx(20.0)

    e10 = t.annotate({"stage": "抽出", "current": 10, "total": 10})
    assert e10["eta_sec"] == 0.0
    assert e10["estimated_total_sec"] == pytest.approx(12.0)


def test_eta_resets_on_phase_switch():
    """フェーズ（stage）が切り替わったら前フェーズのレートを引き継がない。"""
    clock = iter([0.0, 4.0, 100.0, 101.0])
    t = EtaTracker(clock=lambda: next(clock))
    t.annotate({"stage": "A", "current": 0, "total": 4})
    ea = t.annotate({"stage": "A", "current": 2, "total": 4})   # 2秒/件
    assert ea["eta_sec"] == pytest.approx(4.0)

    eb0 = t.annotate({"stage": "B", "current": 0, "total": 8})
    assert eb0["eta_sec"] is None                                # リセット → 計算中
    eb1 = t.annotate({"stage": "B", "current": 1, "total": 8})   # 1秒/件（Aとは別レート）
    assert eb1["eta_sec"] == pytest.approx(7.0)
    assert eb1["estimated_total_sec"] == pytest.approx(8.0)


def test_eta_passes_through_log_events():
    """current/total の無いイベントやログ行（total=1）は素通しする。"""
    t = EtaTracker(clock=lambda: 0.0)
    log_evt = {"stage": "セクション木を構築", "current": 0, "total": 1, "detail": ""}
    assert t.annotate(log_evt) == log_evt
    assert t.annotate({"stage": "x"}) == {"stage": "x"}
    # ログ行を挟んでも進行中フェーズの計測は壊れない
    clock = iter([0.0, 3.0])
    t2 = EtaTracker(clock=lambda: next(clock))
    t2.annotate({"stage": "抽出", "current": 0, "total": 6})
    t2.annotate({"stage": "自動セーフモード…", "current": 0, "total": 1})
    e = t2.annotate({"stage": "抽出", "current": 3, "total": 6})
    assert e["eta_sec"] == pytest.approx(3.0)


def test_forward_logs_context_forwards_progress():
    """IndexManager._forward_logs 経由で progress() の current/total が届く。"""
    from llmlab.indexmanager import IndexManager

    events = []
    with IndexManager._forward_logs(events.append):
        list(bx.progress(range(3), total=3, desc="抽出"))
        bx.log("ログも届く")
    prog = [e for e in events if e.get("stage") == "抽出"]
    assert [e["current"] for e in prog] == [0, 1, 2, 3]
    assert all(e["total"] == 3 for e in prog)
    assert any(e["stage"] == "ログも届く" for e in events)
