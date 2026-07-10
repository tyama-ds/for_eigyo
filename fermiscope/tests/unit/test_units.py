"""単位整合性検査のテスト。"""

import pytest

from fermiscope.formula.parser import parse_expression
from fermiscope.formula.units import (
    check_graph_units,
    convert_value,
    normalize_unit,
    translate_unit_ja,
)


def test_fermi_style_unit_check_passes():
    tree = parse_expression("households * ownership * frequency / capacity")
    units = {
        "households": "household",
        "ownership": "piano/household",
        "frequency": "tuning/(piano*year)",
        "capacity": "tuning/(person*year)",
    }
    result = check_graph_units(tree, units, "人")
    assert result.passed, result.detail


def test_unit_mismatch_detected():
    tree = parse_expression("households * ownership")
    units = {"households": "household", "ownership": "piano/household"}
    result = check_graph_units(tree, units, "人")
    assert not result.passed
    assert "不整合" in result.detail


def test_addition_requires_same_dimension():
    tree = parse_expression("a + b")
    result = check_graph_units(tree, {"a": "person", "b": "household"}, "person")
    assert not result.passed


def test_flow_unit_with_time_dimension():
    tree = parse_expression("population * per_capita")
    units = {"population": "person", "per_capita": "item/(person*year)"}
    result = check_graph_units(tree, units, "item/year")
    assert result.passed, result.detail
    # ストック単位(item)とは一致しない
    result2 = check_graph_units(tree, units, "item")
    assert not result2.passed


def test_japanese_unit_translation():
    assert translate_unit_ja("人") == "person"
    assert translate_unit_ja("世帯") == "household"
    assert translate_unit_ja("%") == "percent"
    assert normalize_unit("人") == "person"


def test_convert_value_deterministic():
    assert convert_value(50, "%", "dimensionless") == pytest.approx(0.5)
    assert convert_value(2, "km", "m") == pytest.approx(2000)


def test_unknown_unit_reported():
    tree = parse_expression("a")
    result = check_graph_units(tree, {"a": "!!invalid unit!!"}, "person")
    assert not result.passed
    assert "解釈できません" in result.detail
