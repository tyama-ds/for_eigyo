from copy import deepcopy
from datetime import date
import json

import pytest

import app.frontiers as frontiers
from app.analytics import analyze


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 11)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(frontiers, "date", FixedDate)


TOPICS = [{"id": "t1", "label": "Materials"}, {"id": "t2", "label": "Devices"}]
OPTIONS = {"start_year": 2025, "end_year": 2026}


def paper(identifier, month=None, topic="t1", title="Evidence paper", **changes):
    value = {"id": identifier, "title": title, "abstract": "", "keywords": [], "authors": [],
             "year": int(month[:4]) if month else 2026, "topic_id": topic,
             "publication_date": month or "", "date_precision": "month" if month else "year",
             "date_source": "test", "citations": None, "citation_history": {}, "doi": "", "source": ""}
    if month and len(month) == 10:
        value["date_precision"] = "day"
    value.update(changes)
    return value


def group(prefix, count, month, topic):
    return [paper(f"{prefix}-{index}", month, topic) for index in range(count)]


def run(papers, keywords=None, **options):
    return frontiers.analyze_frontiers(papers, TOPICS, keywords or [], {**OPTIONS, **options})


def test_default_anchor_is_latest_complete_month_not_latest_paper():
    monthly = run([paper("old", "2025-06")])["monthly"]
    assert monthly["anchor_month"] == monthly["latest_complete_month"] == "2026-08"
    assert monthly["latest_data_month"] == "2025-06"
    assert monthly["date_lag_months"] == 14
    assert monthly["available"] is False
    assert monthly["reason"] == "no_dated_papers_in_window"
    assert monthly["stale"] is False


def test_historical_end_year_caps_anchor_and_reports_stale_scope():
    monthly = run([paper("old", "2025-06")], end_year=2025)["monthly"]
    assert monthly["anchor_month"] == "2025-12"
    assert monthly["stale"] is True
    assert monthly["months"][0]["month"] == "2025-01"
    assert monthly["months"][-1]["month"] == "2025-12"
    assert len(monthly["months"]) == 12


def test_recent_and_baseline_windows_are_equal_length_and_do_not_overlap():
    monthly = run([paper("a", "2026-08"), paper("b", "2026-04", "t2")])["monthly"]
    assert monthly["recent_months"] == ["2026-06", "2026-07", "2026-08"]
    assert monthly["baseline_months"] == ["2026-03", "2026-04", "2026-05"]
    assert not set(monthly["recent_months"]) & set(monthly["baseline_months"])


def test_date_missing_invalid_conflicting_and_future_values_are_not_allocated():
    papers = [paper("valid-month", "2026-08"), paper("valid-day", "2026-04-03", "t2"), paper("undated")]
    for index, value in enumerate(["2026-02-30", "2026-13", "2026-09-12", "2026", "2025-04"]):
        papers.append(paper(f"bad-{index}", publication_date=value, date_precision="day", year=2026))
    monthly = run(papers)["monthly"]
    assert monthly["dated_papers"] == 2
    assert monthly["undated_papers"] == 6
    assert monthly["invalid_dates"] == 5
    assert monthly["date_coverage"] == 25
    assert sum(row["count"] for row in monthly["months"]) == 2


@pytest.mark.parametrize("precision", ["year", "unknown"])
def test_coarse_declared_precision_does_not_invent_a_month(precision):
    monthly = run([paper("coarse", "2026-08-01", date_precision=precision)])["monthly"]
    assert monthly["dated_papers"] == 0
    assert monthly["date_coverage"] == 0
    assert monthly["reason"] == "missing_publication_dates"


def test_month_precision_ignores_accidental_day_and_current_month_is_excluded():
    monthly = run([paper("a", "2026-09-30", date_precision="month"), paper("b", "2026-04", "t2")])["monthly"]
    assert monthly["dated_papers"] == 2
    assert monthly["anchor_month"] == "2026-08"
    assert all(row["month"] != "2026-09" for row in monthly["months"])
    assert monthly["recent_total"] == 0


def test_unobserved_baseline_does_not_turn_recent_only_search_into_new_signal():
    monthly = run(group("recent", 5, "2026-07", "t1"), observed_months=["2026-06", "2026-07", "2026-08"])["monthly"]
    assert monthly["available"] is False
    assert monthly["reason"] == "unobserved_comparison_months"
    assert monthly["missing_observed_months"] == ["2026-03", "2026-04", "2026-05"]
    assert monthly["topics"][0]["recent_count"] == 5
    assert all(topic["status"] == "insufficient" for topic in monthly["topics"])
    assert all(topic["growth_pct"] is None and topic["share_change_pp"] is None for topic in monthly["topics"])
    assert next(row for row in monthly["months"] if row["month"] == "2026-04")["observed"] is False


def test_known_empty_observation_scope_is_not_treated_as_unknown_csv_scope():
    monthly = run(group("recent", 4, "2026-08", "t1") + group("base", 4, "2026-04", "t2"), observed_months=[])["monthly"]
    assert monthly["available"] is False
    assert monthly["observation_scope_known"] is True


def test_csv_unknown_collection_scope_has_explicit_warning_without_fabricating_scope():
    monthly = run(group("recent", 4, "2026-08", "t1") + group("base", 4, "2026-04", "t2"))["monthly"]
    assert monthly["available"] is True
    assert monthly["observation_scope_known"] is False
    assert any("取得対象月が不明" in item for item in monthly["warnings"])


def test_rising_topic_requires_both_count_growth_and_increasing_corpus_share():
    papers = group("a-base", 2, "2026-04", "t1") + group("a-now", 5, "2026-07", "t1")
    papers += group("b-base", 8, "2026-04", "t2") + group("b-now", 5, "2026-07", "t2")
    monthly = run(papers)["monthly"]
    topic = monthly["topics"][0]
    assert topic["recent_count"] == 5 and topic["baseline_count"] == 2
    assert topic["delta"] == 3
    assert topic["growth_pct"] == 150
    assert topic["share_change_pp"] == 30
    assert topic["status"] == "rising"


def test_whole_corpus_growth_alone_does_not_mark_every_topic_rising():
    papers = group("a-base", 3, "2026-04", "t1") + group("a-now", 6, "2026-07", "t1")
    papers += group("b-base", 3, "2026-04", "t2") + group("b-now", 6, "2026-07", "t2")
    topics = run(papers)["monthly"]["topics"]
    assert all(topic["share_change_pp"] == 0 for topic in topics)
    assert all(topic["status"] == "stable" for topic in topics)


def test_zero_topic_baseline_is_new_with_null_growth_not_infinite_growth():
    papers = group("new", 3, "2026-07", "t1") + group("other-base", 4, "2026-04", "t2") + group("other-now", 1, "2026-07", "t2")
    monthly = run(papers)["monthly"]
    assert monthly["topics"][0]["status"] == "new"
    assert monthly["topics"][0]["growth_pct"] is None
    assert monthly["topics"][0]["share_change_pp"] == 75
    assert monthly["topics"][1]["status"] == "declining"
    json.dumps(monthly, allow_nan=False)


def test_insufficient_comparison_period_disables_growth_metrics():
    monthly = run(group("a", 4, "2026-03", "t1") + group("b", 3, "2026-01", "t2"),
                  start_year=2026, anchor_month="2026-04")["monthly"]
    assert monthly["reason"] == "insufficient_period"
    assert monthly["available"] is False
    assert all(topic["status"] == "insufficient" and topic["growth_pct"] is None for topic in monthly["topics"])


def test_month_comparison_spans_calendar_year_boundary_correctly():
    monthly = run(group("a", 3, "2026-01", "t1") + group("b", 3, "2025-12", "t2"),
                  anchor_month="2026-01", window_months=1)["monthly"]
    assert monthly["baseline_months"] == ["2025-12"]
    assert monthly["recent_months"] == ["2026-01"]
    assert monthly["available"] is True


def test_six_month_window_has_twelve_comparison_months():
    monthly = run([paper("a", "2026-07"), paper("b", "2025-10", "t2")], window_months=6)["monthly"]
    assert monthly["recent_months"] == ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    assert monthly["baseline_months"][0] == "2025-09"
    assert monthly["baseline_months"][-1] == "2026-02"
    assert len(monthly["months"]) == 12


def test_monthly_evidence_is_latest_and_limited_to_comparison_period():
    papers = [paper("z-newest", "2026-08-30"), paper("a-old-day", "2026-08-01")]
    papers += group("july", 6, "2026-07", "t1") + group("baseline", 3, "2026-04", "t2")
    papers += [paper("outside-period", "2025-01")]
    evidence = run(papers)["monthly"]["topics"][0]["evidence_ids"]
    assert len(evidence) == 5
    assert evidence[:2] == ["z-newest", "a-old-day"]
    assert "outside-period" not in evidence


@pytest.mark.parametrize("bad", [
    {"window_months": 2}, {"window_months": True}, {"anchor_month": "2026-09"},
    {"anchor_month": "2026-12"}, {"anchor_month": "2024-12"}, {"anchor_month": "2026-8"},
    {"anchor_month": "2026-00"}, {"observed_months": ["invalid"]}, {"observed_months": "2026-08"},
])
def test_invalid_monthly_options_are_actionable(bad):
    with pytest.raises(ValueError):
        run([paper("a", "2026-08")], **bad)


def concept_corpus(a=5, b=5, joint=0, neither=2, term_a="laser cooling", term_b="quantum dots"):
    papers = [paper(f"a-{i}", title=term_a, abstract=term_a + " " + term_a, keywords=[term_a]) for i in range(a)]
    papers += [paper(f"b-{i}", topic="t2", title=term_b, keywords=[term_b]) for i in range(b)]
    papers += [paper(f"joint-{i}", title=f"{term_a}; {term_b}") for i in range(joint)]
    papers += [paper(f"other-{i}", title="Unrelated evidence") for i in range(neither)]
    keywords = [{"term": term_a, "count": a + joint, "topic_id": "t1"}, {"term": term_b, "count": b + joint, "topic_id": "t2"}]
    return papers, keywords


def test_sparse_expected_counts_use_all_papers_and_each_paper_only_once():
    papers, keywords = concept_corpus()
    sparse = run(papers, keywords)["sparse"]
    assert sparse["corpus_papers"] == 12
    assert len(sparse["candidates"]) == 1
    candidate = sparse["candidates"][0]
    assert candidate["count_a"] == candidate["count_b"] == 5
    assert candidate["observed"] == 0
    assert candidate["expected"] == round(25 / 12, 3)
    assert candidate["ratio"] == 0
    assert candidate["score"] == round(100 * (25 / 12) / 10, 2)
    assert len(candidate["evidence_a"]) == len(candidate["evidence_b"]) == 3
    assert candidate["joint_evidence_ids"] == []


def test_low_expected_cooccurrence_does_not_generate_a_candidate():
    papers, keywords = concept_corpus(a=10, b=10, neither=480)
    sparse = run(papers, keywords)["sparse"]
    assert sparse["corpus_papers"] == 500
    assert sparse["candidates"] == []
    assert len(sparse["terms"]) == 2
    assert any("条件を満たす" in warning for warning in sparse["warnings"])


def test_sparse_cooccurrence_evidence_and_nonzero_ratio_are_correct():
    papers, keywords = concept_corpus(a=5, b=5, joint=1, neither=1)
    candidate = run(papers, keywords)["sparse"]["candidates"][0]
    assert candidate["observed"] == 1
    assert candidate["expected"] == 3
    assert candidate["ratio"] == round(1 / 3, 4)
    assert candidate["score"] == 20
    assert candidate["joint_evidence_ids"] == ["joint-0"]
    assert "joint-0" not in candidate["evidence_a"] + candidate["evidence_b"]


def test_ratio_exactly_half_is_not_a_sparse_candidate():
    papers, keywords = concept_corpus(a=6, b=6, joint=2, neither=2)
    assert run(papers, keywords)["sparse"]["candidates"] == []


def test_english_matches_full_words_and_japanese_matches_embedded_phrase():
    papers, keywords = concept_corpus(term_a="heat", term_b="引張強度")
    for item in papers:
        if item["id"].startswith("b-"):
            item["title"] = "高い引張強度特性を解析"
            item["keywords"] = []
        if item["id"].startswith("other-"):
            item["title"] = "preheat wheat cheating"
    terms = {row["term"]: row["count"] for row in run(papers, keywords)["sparse"]["terms"]}
    assert terms == {"heat": 5, "引張強度": 5}


def test_phrase_does_not_appear_by_joining_the_end_of_title_and_start_of_abstract():
    papers, keywords = concept_corpus()
    papers += [paper(f"bridge-{i}", title="laser", abstract="cooling") for i in range(3)]
    keywords[0]["count"] = 8
    terms = {row["term"]: row["count"] for row in run(papers, keywords)["sparse"]["terms"]}
    assert terms["laser cooling"] == 5


def test_sparse_analysis_uses_more_than_the_four_hundred_displayed_map_papers():
    papers, keywords = concept_corpus(a=300, b=300, neither=0)
    sparse = run(papers, keywords)["sparse"]
    assert sparse["corpus_papers"] == 600
    candidate = sparse["candidates"][0]
    assert candidate["count_a"] == candidate["count_b"] == 300
    assert candidate["expected"] == 150


def test_sparse_terms_are_diverse_bounded_and_candidates_keep_referential_integrity():
    papers, keywords = [], []
    for topic in range(4):
        terms = [f"concept{topic} variant{index}" for index in range(6)]
        for index in range(60):
            papers.append(paper(f"{topic}-{index}", topic=f"topic-{topic}", title="; ".join(terms)))
        keywords += [{"term": term, "count": 60, "topic_id": f"topic-{topic}"} for term in terms]
    sparse = run(papers, keywords)["sparse"]
    assert len(sparse["terms"]) == 24
    assert len(sparse["candidates"]) == 12
    assert len({term["topic_id"] for term in sparse["terms"][:4]}) == 4
    ids = {paper["id"] for paper in papers}
    for candidate in sparse["candidates"]:
        assert set(candidate["evidence_a"] + candidate["evidence_b"] + candidate["joint_evidence_ids"]) <= ids
        assert candidate["topic_a"] != candidate["topic_b"]


def test_frontiers_are_immutable_deterministic_and_json_finite():
    papers, keywords = concept_corpus()
    before_papers, before_keywords = deepcopy(papers), deepcopy(keywords)
    original = run(papers, keywords)
    assert original == run(papers, keywords)
    assert papers == before_papers and keywords == before_keywords
    json.dumps(original, allow_nan=False)


def test_empty_corpus_has_honest_empty_results():
    result = run([])
    assert result["monthly"]["available"] is False
    assert result["monthly"]["date_coverage"] == 0
    assert result["sparse"]["candidates"] == []
    assert result["sparse"]["terms"] == []


def test_analytics_preserves_publication_precision_and_returns_frontiers():
    papers = group("base", 3, "2026-04", "t1") + group("now", 5, "2026-07", "t2")
    result = analyze(papers, {**OPTIONS, "n_topics": 1})
    assert result["frontiers"]["monthly"]["dated_papers"] == 8
    assert result["papers"][0]["publication_date"] == "2026-04"
    assert result["papers"][0]["date_precision"] == "month"
    assert result["papers"][0]["date_source"] == "test"


def test_analytics_normalizes_declared_year_precision_without_exposing_a_fake_month():
    result = analyze([paper("coarse", "2026-01-01", date_precision="year")], {**OPTIONS, "n_topics": 1})
    assert result["papers"][0]["publication_date"] == ""
    assert result["papers"][0]["date_precision"] == "year"
    assert result["frontiers"]["monthly"]["dated_papers"] == 0
