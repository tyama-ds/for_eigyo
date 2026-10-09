from copy import deepcopy
import os
import struct

import pytest

from app import report_figures as figures


def saved_result():
    return {"id": "saved-result", "summary": {"papers": 20_000},
        "timeline": [{"year": 2021, "papers": 8000}, {"year": 2022, "papers": 12000}],
        "topics": [{"id": "a", "label": "ステンレス鋼の熱処理", "count": 13000, "color": "#087f8c"},
                   {"id": "b", "label": "疲労と微細組織", "count": 7000, "color": "#6554b5"}],
        "map": {"nodes": [
            {"id": "p1", "topic_id": "a", "x": .1, "y": .2, "year": 2021, "publication_date": "2021-02", "date_precision": "month"},
            {"id": "p2", "topic_id": "a", "x": .2, "y": .3, "year": 2021, "publication_date": "2021-02-20", "date_precision": "day"},
            {"id": "p3", "topic_id": "a", "x": .6, "y": .7, "year": 2022, "publication_date": "2022-03", "date_precision": "month"},
            {"id": "p4", "topic_id": "b", "x": .9, "y": .1, "year": 2022, "date_precision": "year"}]}}


def capture(monkeypatch):
    saved = []
    def render(fig, title, caption, **kwargs):
        saved.append((fig, title, caption))
        return {"title": title, "caption": caption, "png": b"not-used", "width": 1500, "height": 900}
    monkeypatch.setattr(figures, "_render", render)
    return saved


def test_annual_uses_report_counts_and_preserves_missing_neighbor(monkeypatch):
    saved = capture(monkeypatch)
    report = {"focus": {"label": "分野 A"}, "neighbor": {"label": "分野 B"},
              "annual": [{"year": 2021, "focus_count": 7, "neighbor_count": None},
                         {"year": 2022, "focus_count": 12, "neighbor_count": 3}]}
    value = figures._annual(saved_result(), report, "field", {})
    assert [bar.get_height() for bar in saved[0][0].axes[0].patches] == [7, 12, 3]
    assert "欠測は 0 件に置き換えていません" in value["caption"]


def test_annual_report_unobserved_year_is_not_drawn_as_zero(monkeypatch):
    saved = capture(monkeypatch)
    report = {"annual_rows": [{"year": 2021, "count": 5, "observed": True},
                              {"year": 2022, "count": 0, "observed": False},
                              {"year": 2023, "count": 0, "observed": True}]}
    figures._annual({}, report, "annual", {})
    assert [bar.get_height() for bar in saved[0][0].axes[0].patches] == [5, 0]


def test_foresight_never_substitutes_original_corpus_counts_or_topics(monkeypatch):
    saved = capture(monkeypatch)
    report = {"candidates": [{"id": "new", "label": "追加探索の候補", "growth": {"series": [{"year": 2024, "count": 19}]}},
                              {"id": "other", "label": "別候補", "metrics": {"growth": {"series": [{"year": 2024, "count": 30}]}}}]}
    figures._annual(saved_result(), report, "foresight", {"candidate_id": "new"})
    assert [bar.get_height() for bar in saved[0][0].axes[0].patches] == [19]
    assert figures._topics(saved_result(), report, "foresight") is None
    assert figures._annual(saved_result(), {}, "foresight", {}) is None


def test_map_uses_original_coordinates_and_labels_display_scope_without_refitting(monkeypatch):
    from app import landscape, corpus_landscape, storage
    def forbidden(*args, **kwargs):
        raise AssertionError("exports must not rebuild or reread a whole corpus")
    monkeypatch.setattr(landscape, "build_landscape", forbidden)
    monkeypatch.setattr(corpus_landscape, "build_full_landscape", forbidden)
    monkeypatch.setattr(storage, "read", forbidden)
    saved = capture(monkeypatch)
    original = saved_result()
    before = deepcopy(original)
    result = figures.build_report_figures(original, {}, kind="result")
    assert original == before
    map_figure = next(item for item in saved if item[1].startswith("技術ランドスケープ"))
    offsets = [tuple(row) for collection in map_figure[0].axes[0].collections for row in collection.get_offsets()]
    assert set(offsets) == {(row["x"], row["y"]) for row in original["map"]["nodes"]}
    assert "20,000 件" in map_figure[2] and "4 点" in map_figure[2]
    assert "初期マップ" in map_figure[2]
    temporal = next(item for item in result if item["title"].startswith("時層"))
    assert "描画した点の算術平均" in temporal["caption"]


@pytest.mark.parametrize("record", [
    {"year": 2021, "date_precision": "year"},
    {"year": 2021, "publication_date": "2021-01-01", "date_precision": "year"},
    {"year": 2021, "publication_date": "2022-03", "date_precision": "month"},
    {"year": 2021, "publication_date": "2021-02-31", "date_precision": "day"},
])
def test_monthly_layers_do_not_invent_months_or_accept_invalid_dates(record):
    assert figures._period(record, "month") == (None, None)


def test_monthly_layer_excludes_unknown_months_and_retains_actual_time_spacing(monkeypatch):
    saved = capture(monkeypatch)
    original = saved_result()
    result = figures.build_report_figures(original, {}, kind="result", options={"interval": "month"})
    temporal = next(row for row in result if row["title"].startswith("時層"))
    assert "日付精度が不足する 1 点" in temporal["caption"]
    ax = next(fig.axes[0] for fig, title, _ in saved if title.startswith("時層"))
    ticks = list(ax.get_zticks())
    assert ticks == [0, 13]  # February 2021 to March 2022, not consecutive slots.


def test_saved_snapshot_uses_its_centers_and_other_projection_report_stays_separate(monkeypatch):
    saved = capture(monkeypatch)
    original = saved_result()
    snapshot = {"result_id": original["id"], "projection_id": "one", "map": original["map"], "topics": original["topics"],
        "meta": {"interval": "year", "scope": "full", "corpus_papers": 20000},
        "centroids": [{"topic_id": "a", "period_id": "2021", "x": .13, "y": .25},
                      {"topic_id": "a", "period_id": "2022", "x": .55, "y": .65}]}
    report = {"projection_id": "two", "scope": "full", "topic": {"id": "a"}, "movement": {
        "from_period": "2021", "to_period": "2022", "from": {"x": 100, "y": 200}, "to": {"x": 300, "y": 400}}}
    result = figures.build_report_figures(original, report, kind="movement", options={"landscape": snapshot})
    temporal = next(row for row in result if row["title"].startswith("時層"))
    exact = next(row for row in result if row["title"].startswith("レポートに保存"))
    assert "保存済み重心" in temporal["caption"]
    assert "異なる投影" in exact["caption"]
    fig = next(fig for fig, title, _ in saved if title.startswith("レポートに保存"))
    offsets = {tuple(row) for collection in fig.axes[0].collections for row in collection.get_offsets()}
    assert offsets == {(100, 200), (300, 400)}


def test_foreign_snapshot_is_ignored_and_invalid_coordinates_are_omitted():
    original = saved_result()
    original["map"]["nodes"] += [{"x": float("nan"), "y": 1}, {"x": True, "y": 1}]
    snapshot, saved = figures._snapshot(original, {}, {"landscape": {"result_id": "other", "map": {"nodes": []}}})
    assert not saved
    points, count = figures._points(snapshot, {"max_points": 2})
    assert count == 6 and len(points) == 2 and all(row["id"] in {"p1", "p2", "p3", "p4"} for row in points)


def test_citation_uses_saved_edges_only_and_skips_absent_reference_data(monkeypatch):
    saved = capture(monkeypatch)
    flow = {"interval": "year", "nodes": [
        {"id": "new", "period_id": "2022", "x": .7}, {"id": "old", "period_id": "2021", "x": .2}],
        "edges": [{"source_id": "new", "target_id": "old"}, {"source_id": "new", "target_id": "missing"}],
        "coverage": {"valid_edges": 9}}
    value = figures._citation(flow)
    assert "2 点・1 辺" in value["caption"] and "9 辺" in value["caption"]
    annotations = [item for item in saved[0][0].axes[0].texts if getattr(item, "arrow_patch", None)]
    assert len(annotations) == 1 and annotations[0].xy == (2021, .2)
    assert figures._citation({**flow, "edges": []}) is None


def test_no_saved_data_returns_omission_notes_instead_of_example_figures():
    notes = []
    assert figures.build_report_figures({}, {}, kind="result", options={"notes": notes}) == []
    assert len(notes) == 6 and all("省略" in note for note in notes)


def test_real_png_render_has_japanese_labels_and_known_dimensions():
    try:
        figures._font_path(os.getenv("ATLAS_PDF_FONT", ""))
    except ValueError:
        pytest.skip("Japanese font unavailable in test environment")
    original = saved_result()
    original["meta"] = {"is_demo": True}
    rendered = figures.build_report_figures(original, {}, kind="result")
    assert len(rendered) == 4
    for value in rendered:
        assert value["png"].startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", value["png"][16:24]) == (value["width"], value["height"]) == (1500, 900)
        assert 10_000 < len(value["png"]) < 2_000_000
        assert value["caption"].startswith("合成データ")
