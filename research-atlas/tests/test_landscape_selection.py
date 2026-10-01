import json
from copy import deepcopy

import numpy as np
import pytest

from app import corpus_landscape, landscape, landscape_selection, large_storage, storage
from app.landscape_selection import SelectionContext, validate_options


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    yield
    corpus_landscape._load.cache_clear()


def saved(papers, vectors, *, shown=None):
    papers = [dict({"id": f"p{index:04d}", "title": "alloy structure research", "abstract": "A measured alloy result.",
                    "topic_id": "topic", "year": 2023, "publication_date": "2023-06", "date_precision": "month"}, **paper,
                   landscape_vector=vector) for index, (paper, vector) in enumerate(zip(papers, vectors))]
    shown = papers if shown is None else papers[:shown]
    ids = {paper["id"] for paper in shown}
    value = {"id": storage.new_id(), "papers": papers, "meta": {"landscape_representation": {
        "basis_scope": "full_corpus", "embedding": "nmf", "dimensions": 2}},
        "topics": [{"id": "topic", "label": "Alloys"}],
        "map": {"nodes": [{"id": p["id"], "topic_id": p["topic_id"], "x": .5, "y": .5} for p in shown],
                "projection_inputs": {"paper_ids": [p["id"] for p in shown],
                                      "vectors": [v for p, v in zip(papers, vectors) if p["id"] in ids],
                                      "source": "saved_analysis_representation", "embedding": "nmf"}}}
    storage.save("results", value)
    return value


@pytest.mark.parametrize("bad", [0, 21, True, "6", 1.5, None])
def test_invalid_count_rejected(bad):
    with pytest.raises(ValueError, match="1〜20"):
        validate_options(papers_per_period=bad)


def test_invalid_method_scope_and_abstract_flag_rejected():
    with pytest.raises(ValueError):
        validate_options(selection_method="random")
    with pytest.raises(ValueError):
        validate_options(abstract_only="false")
    with pytest.raises(ValueError):
        SelectionContext("unused", "other")


def test_centroid_uses_original_vectors_and_all_available_candidates():
    vectors = [[1., index / 100.] for index in range(30)]
    result = saved([{} for _ in vectors], vectors)
    original = deepcopy(result)
    context = SelectionContext(result["id"])
    ids, info = context.select("topic", "2023", "year", papers_per_period=20)
    center = np.mean(vectors, axis=0)
    expected = sorted(range(30), key=lambda i: (round(landscape._cosine(vectors[i], center), 12), f"p{i:04d}"))[:20]
    assert ids == [f"p{i:04d}" for i in expected]
    assert info["candidate_count"] == info["eligible_count"] == 30
    assert info["selected_count"] == 20
    assert info["selected_records"][0]["cosine_distance"] == pytest.approx(landscape._cosine(vectors[expected[0]], center))
    assert storage.read("results", result["id"]) == original
    json.dumps(info, allow_nan=False)


def test_abstract_filter_never_recomputes_the_center():
    vectors = [[1., 0.]] * 8 + [[.8, .6], [.8, -.6], [0., 1.]]
    result = saved([{"abstract": ""}] * 8 + [{}] * 3, vectors)
    context = SelectionContext(result["id"])
    _, before = context.select("topic", "2023", "year", papers_per_period=20)
    ids, after = context.select("topic", "2023", "year", papers_per_period=20, abstract_only=True)
    distances = {row["id"]: row["cosine_distance"] for row in before["selected_records"]}
    assert len(ids) == after["selected_count"] == 3
    assert after["candidate_count"] == 11
    assert after["excluded_abstract_count"] == 8
    assert after["valid_vector_count"] == 11
    for row in after["selected_records"]:
        assert row["cosine_distance"] == distances[row["id"]]


def test_diverse_uses_deterministic_bounded_mmr_not_a_pairwise_matrix(monkeypatch):
    vectors = [[1., 0.]] * 30 + [[2 ** -.5, 2 ** -.5]] * 30
    result = saved([{}] * 60, vectors)
    context = SelectionContext(result["id"])
    centroid, _ = context.select("topic", "2023", "year", papers_per_period=2)
    original_clip = np.clip
    shapes = []
    def guarded_clip(values, *args, **kwargs):
        shapes.append(np.asarray(values).shape)
        assert np.asarray(values).ndim <= 1, "Selection must not construct a pairwise similarity matrix"
        return original_clip(values, *args, **kwargs)
    monkeypatch.setattr(landscape_selection.np, "clip", guarded_clip)
    first = context.select("topic", "2023", "year", papers_per_period=2, selection_method="diverse")
    second = context.select("topic", "2023", "year", papers_per_period=2, selection_method="diverse")
    assert centroid == ["p0000", "p0001"]
    assert first[0] == ["p0000", "p0030"]
    assert first == second
    assert len(shapes) > 2
    assert first[1]["diversity_weight"] == .35


def test_citation_unknown_is_after_known_zero_and_counts_are_cumulative():
    result = saved([{"citations": None}, {"citations": 0}, {"citations": 19}, {"citations": 19},
                    {"citations": -1}, {"citations": True}], [[1., 0.]] * 6)
    ids, info = SelectionContext(result["id"]).select("topic", "2023", "year", selection_method="cited")
    assert ids == ["p0002", "p0003", "p0001", "p0000", "p0004", "p0005"]
    assert info["selected_records"][-1]["citations"] is None
    assert "累積被引用数" in info["warnings"][0]


def test_recent_preserves_date_precision_and_does_not_invent_january():
    papers = [{"publication_date": "2023", "date_precision": "year"},
              {"publication_date": "2023-10", "date_precision": "month"},
              {"publication_date": "2023-10-21", "date_precision": "day"},
              {"publication_date": "2023-10-31", "date_precision": "month"},
              {"publication_date": "2022-12-31", "date_precision": "day"},
              {"publication_date": "2023-02-31", "date_precision": "day"}]
    result = saved(papers, [[1., 0.]] * 6)
    ids, info = SelectionContext(result["id"]).select("topic", "2023", "year", selection_method="recent")
    assert ids == ["p0002", "p0001", "p0003", "p0000", "p0004", "p0005"]
    rows = {p["id"]: p for p in info["selected_records"]}
    assert rows["p0000"]["publication_date"] == "2023"
    assert rows["p0003"]["publication_date"] == "2023-10"
    assert rows["p0004"]["publication_date"] == "2023"  # Conflicting year is unavailable.
    assert rows["p0005"]["publication_date"] == "2023"  # Invalid day is unavailable.


def test_period_and_topic_membership_exclude_outside_candidates():
    result = saved([{}, {"publication_date": "2023", "date_precision": "year"},
                    {"publication_date": "2023-09"}, {"topic_id": "another"},
                    {"year": 2024, "publication_date": "2024-06"}], [[1., 0.]] * 5)
    context = SelectionContext(result["id"])
    assert context.select("topic", "2023-06", "month")[0] == ["p0000"]
    assert context.select("topic", "2023-Q2", "quarter")[0] == ["p0000"]
    assert context.select("topic", "2023", "year")[0] == ["p0000", "p0001", "p0002"]
    assert context.select("unknown", "2023", "year")[1]["candidate_count"] == 0
    with pytest.raises(ValueError):
        context.select("topic", "2023", "decade")


def test_zero_and_nonfinite_vectors_excluded_only_for_vector_selection(monkeypatch):
    result = saved([{"citations": 1}, {"citations": 9}, {"citations": 7}, {"citations": 2}], [[1., 0.]] * 4)
    vectors = np.array([[1., 0.], [0., 0.], [np.nan, 1.], [np.inf, 1.]])
    monkeypatch.setattr(landscape, "_representation", lambda *a: (vectors, "nmf", "fixture", []))
    context = SelectionContext(result["id"])
    ids, info = context.select("topic", "2023", "year")
    assert ids == ["p0000"]
    assert info["excluded_vector_count"] == 3
    ids, info = context.select("topic", "2023", "year", selection_method="cited")
    assert ids == ["p0001", "p0002", "p0003", "p0000"]
    assert all(row["cosine_distance"] is None for row in info["selected_records"][:3])
    json.dumps(info, allow_nan=False)


def test_cancelling_center_has_no_cosine_evidence_but_metadata_still_usable():
    result = saved([{}, {}], [[1., 0.], [-1., 0.]])
    context = SelectionContext(result["id"])
    ids, info = context.select("topic", "2023", "year")
    assert ids == []
    assert info["valid_vector_count"] == 2
    assert info["excluded_vector_count"] == 2
    ids, info = context.select("topic", "2023", "year", selection_method="recent")
    assert len(ids) == 2
    assert all(row["cosine_distance"] is None for row in info["selected_records"])


def test_full_corpus_selects_hidden_papers_and_reads_missingness_once(monkeypatch):
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 100)
    papers = [{"citations": None if i == 499 else i, "abstract": "" if i == 498 else "Measured alloy behavior."}
              for i in range(500)]
    result = saved(papers, [[1., 0.]] * 500, shown=400)
    corpus_landscape._load(corpus_landscape._revision(result["id"]))
    original = large_storage.iter_papers
    calls = []
    def once(*args, **kwargs):
        calls.append(1)
        yield from original(*args, **kwargs)
    monkeypatch.setattr(large_storage, "iter_papers", once)
    context = SelectionContext(result["id"], "full")
    ids, info = context.select("topic", "2023", "year", papers_per_period=20, selection_method="cited", abstract_only=True)
    assert ids[0] == "p0497"
    assert info["candidate_count"] == 500
    assert info["eligible_count"] == 499
    assert info["excluded_abstract_count"] == 1
    assert info["selected_count"] == 20
    context.select("topic", "2024", "year", selection_method="cited")
    assert calls == [1]
    assert all("abstract" not in record for record in context.records)
    sample_ids, sample = SelectionContext(result["id"], "sample").select("topic", "2023", "year", selection_method="cited")
    assert sample_ids[0] == "p0399"
    assert sample["candidate_count"] == 400


def test_sample_never_selects_beyond_original_map_limit():
    result = saved([{"citations": i} for i in range(410)], [[1., 0.]] * 410)
    # Keep a correct saved basis for the map's first 400 rows.
    result["map"]["projection_inputs"]["paper_ids"] = result["map"]["projection_inputs"]["paper_ids"][:400]
    result["map"]["projection_inputs"]["vectors"] = result["map"]["projection_inputs"]["vectors"][:400]
    storage.save("results", result)
    ids, info = SelectionContext(result["id"]).select("topic", "2023", "year", selection_method="cited")
    assert ids[0] == "p0399"
    assert info["candidate_count"] == 400


def test_annual_period_membership_cached_without_rewalking_records(monkeypatch):
    result = saved([{}, {"year": 2024, "publication_date": "2024-06"}], [[1., 0.]] * 2)
    context = SelectionContext(result["id"])
    assert context.select("topic", "2023", "year")[0] == ["p0000"]
    original = landscape._period_number
    calls = []
    def guarded(record, interval):
        calls.append((record["id"], interval))
        return original(record, interval)
    monkeypatch.setattr(landscape, "_period_number", guarded)
    assert context.select("topic", "2024", "year")[0] == ["p0001"]
    # Only selected-record date labels need date parsing on subsequent chapters.
    assert calls == [("p0001", "year"), ("p0001", "month")]
