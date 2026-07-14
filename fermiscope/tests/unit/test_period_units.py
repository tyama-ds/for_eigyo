"""Phase 1-1: 期間・単位の全工程整合の回帰テスト。"""

from __future__ import annotations

from fermiscope.domain.enums import StockOrFlow
from fermiscope.formula.units import (
    period_to_unit,
    units_directly_comparable,
    units_same_dimension,
)
from fermiscope.models.generator import flow_target_unit
from fermiscope.question.parser import parse_question_rule_based

# ---- フロー/ストック判定(日付の部分一致で誤判定しない)----


def test_daily_email_is_daily_flow_not_annual():
    s = parse_question_rule_based("世界で毎日送信されるメールは何件か")
    assert s.stock_or_flow == StockOrFlow.FLOW
    assert s.time_period == "1日"
    assert flow_target_unit(s) == "event/day"
    assert "/year" not in flow_target_unit(s)


def test_daily_umbrella_is_item_per_day():
    s = parse_question_rule_based("日本で1日に廃棄される傘は何本か")
    assert s.stock_or_flow == StockOrFlow.FLOW
    assert flow_target_unit(s) == "item/day"


def test_year_in_question_is_stock_not_flow():
    s = parse_question_rule_based("2021年の日本の人口は何人か")
    assert s.stock_or_flow == StockOrFlow.STOCK
    assert flow_target_unit(s) == "person"


def test_full_date_is_stock_not_flow():
    s = parse_question_rule_based("2026年1月1日現在の東京都の人口は何人か")
    assert s.stock_or_flow == StockOrFlow.STOCK


def test_annual_market_size_still_flow():
    s = parse_question_rule_based("日本国内のペットボトル飲料の年間市場規模はいくらか")
    assert s.stock_or_flow == StockOrFlow.FLOW
    assert flow_target_unit(s) == "JPY/year"


def test_year_reference_without_flow_keyword_is_stock():
    s = parse_question_rule_based("2030年時点で必要なEV充電器は何台か")
    assert s.stock_or_flow == StockOrFlow.STOCK
    assert s.reference_date == "2030"


# ---- 期間→単位 ----


def test_period_to_unit_mapping():
    assert period_to_unit("1日") == "day"
    assert period_to_unit("1年間") == "year"
    assert period_to_unit("1か月") == "month"
    assert period_to_unit("") == ""


# ---- 検算: 日次 vs 年次は無換算で比較不可 ----


def test_day_and_year_units_not_directly_comparable():
    # 次元は同じ
    assert units_same_dimension("item/day", "item/year") is True
    # だが倍率が違うので直接比較不可(検算不成立の根拠)
    assert units_directly_comparable("item/day", "item/year") is False
    # 同一単位は比較可能
    assert units_directly_comparable("item/day", "item/day") is True
    assert units_directly_comparable("person", "person") is True
    # 次元が違えば当然不可
    assert units_directly_comparable("person", "item/year") is False


def test_validate_models_incompatible_units_not_compared():
    """日次モデルと年次モデルを換算なしで検算(数値比較)しない。"""
    from fermiscope.config import load_settings
    from fermiscope.domain.models import (
        FormulaGraph,
        FormulaNode,
        ModelCandidate,
        SimulationResult,
    )
    from fermiscope.validation.engine import validate_models

    def _model(unit: str) -> ModelCandidate:
        root = FormulaNode(kind="parameter", parameter_id="direct_value")
        graph = FormulaGraph(
            root=root, expression="direct_value", target_unit=unit,
            unit_check_passed=True, unit_check_detail="",
        )
        return ModelCandidate(name=f"model {unit}", formula=graph, parameter_ids=["direct_value"])

    primary = _model("item/day")
    check = _model("item/year")
    sim_p = SimulationResult(model_id=primary.id, iterations=1000, seed=1, median=100.0)
    sim_c = SimulationResult(model_id=check.id, iterations=1000, seed=1, median=36500.0)
    res = validate_models(
        primary, check, sim_p, sim_c, parameters={}, evidence={}, critiques={},
        settings=load_settings(env={}),
    )
    assert res.comparable is False
    assert res.agreement == "incompatible"
    assert res.central_ratio is None  # 数値比較していない
