"""日付解析・単位ブリッジ・設定・エクスポートの小粒テスト。"""

import pytest

from fermiscope.config import load_settings
from fermiscope.evidence.dates import parse_year
from fermiscope.evidence.normalize import expected_units_for, normalize_value


def test_parse_year():
    assert parse_year("2020年10月1日") == 2020
    assert parse_year("令和2年") == 2020
    assert parse_year("平成8年3月") == 1996
    assert parse_year("2024-04-15") == 2024
    assert parse_year("昭和60年") == 1985
    assert parse_year("不明") is None
    assert parse_year("") is None


def test_normalize_value_exact_conversion():
    v, note = normalize_value(50.0, "percent", "dimensionless")
    assert v == pytest.approx(0.5)
    assert note == ""  # Pintの厳密変換は無仮定


def test_normalize_value_percent_bridge():
    v, note = normalize_value(10.4, "percent", "piano/household")
    assert v == pytest.approx(0.104)
    assert "解釈" in note  # 仮定が注記される


def test_normalize_value_count_bridge():
    v, note = normalize_value(300.0, "event", "tuning/(person*year)")
    assert v == 300.0
    assert note


def test_normalize_value_impossible():
    v, _ = normalize_value(5.0, "JPY", "person")
    assert v is None


def test_expected_units():
    assert "percent" in expected_units_for("dimensionless")
    assert expected_units_for("person") == {"person"}
    assert "event" in expected_units_for("tuning/(person*year)")
    assert "household" in expected_units_for("household")


def test_settings_load_and_hash():
    s = load_settings()
    assert s.display_name() == "FermiScope"
    assert len(s.config_hash) == 16
    assert s.scoring.weights.source_authority == pytest.approx(0.18)
    assert s.decomposition.max_depth >= 3


def test_app_name_env_override(monkeypatch):
    monkeypatch.setenv("FERMISCOPE_APP_NAME", "MyEstimator")
    s = load_settings()
    assert s.display_name() == "MyEstimator"
