"""Section 4: 問い・期間・単位の構造化の回帰テスト。"""

from __future__ import annotations

from fermiscope.question.parser import (
    _parse_period,
    is_known_target_unit,
    parse_question_rule_based,
)

# ---- subject 抽出 ----


def test_subject_strips_geography_and_particle():
    assert parse_question_rule_based("東京都のコンビニは何店か").subject == "コンビニ"


def test_subject_strips_period_and_geography():
    s = parse_question_rule_based("日本で1日に廃棄される傘は何本か")
    assert s.subject == "廃棄される傘"
    assert s.geography == "日本"


def test_subject_piano_unchanged():
    assert parse_question_rule_based("東京都内にはピアノ調律師が何人いるか").subject == "ピアノ調律師"


# ---- 期間の構造化: 毎日(レート) vs 7日間(合計) ----


def test_period_daily_rate():
    assert _parse_period("毎日") == ("1日", 1.0, "day", True)
    assert _parse_period("1日あたり") == ("1日", 1.0, "day", True)


def test_period_seven_day_total_not_rounded_to_one():
    label, qty, unit, is_rate = _parse_period("7日間の合計")
    assert qty == 7.0  # 7日を1日に丸めない
    assert unit == "day"
    assert is_rate is False
    assert label == "7日間"


def test_period_week_and_hour_supported():
    assert _parse_period("1週間で")[2] == "week"
    assert _parse_period("2時間で")[2] == "hour"
    assert _parse_period("毎月") == ("1か月", 1.0, "month", True)
    assert _parse_period("年間") == ("1年間", 1.0, "year", True)


def test_date_year_not_treated_as_period():
    # 「2021年のコンビニ」は年号であり期間ではない
    assert _parse_period("2021年のコンビニ") is None


def test_spec_period_fields_populated():
    s = parse_question_rule_based("日本で7日間に廃棄される傘は何本か")
    assert s.period_quantity == 7.0
    assert s.period_unit == "day"
    assert s.period_is_rate is False
    assert s.time_period == "7日間"


# ---- 正準単位と表示助数詞の分離 ----


def test_canonical_unit_separate_from_display_counter():
    s = parse_question_rule_based("日本で年間に売れる傘は何本か")
    assert s.target_unit == "item"  # 正準単位
    assert s.target_unit_display == "本"  # 表示助数詞


def test_person_counter():
    s = parse_question_rule_based("東京都にピアノ調律師は何人いるか")
    assert s.target_unit == "person"
    assert s.target_unit_display == "人"


# ---- 未知単位 ----


def test_known_target_unit():
    assert is_known_target_unit("人")
    assert is_known_target_unit("台")
    assert is_known_target_unit("store")
    assert is_known_target_unit("")  # 未指定は許容
    assert not is_known_target_unit("グワイヤー")  # 未知
