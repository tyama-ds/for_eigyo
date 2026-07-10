"""問い正規化のテスト。"""

import pytest

from fermiscope.domain.enums import StockOrFlow
from fermiscope.llm import MockLLMProvider, NoOpLLMProvider
from fermiscope.question.parser import parse_question, parse_question_rule_based


def test_piano_tuner_question():
    spec = parse_question_rule_based("東京都内にはピアノ調律師が何人いるか")
    assert spec.geography == "東京都"
    assert spec.target_unit == "person"
    assert spec.stock_or_flow == StockOrFlow.STOCK
    assert "ピアノ調律師" in spec.subject
    # 基準時点は暫定
    assert any(p.field == "reference_date" for p in spec.provisional)


def test_flow_question():
    spec = parse_question_rule_based("日本で1日に廃棄される傘は何本か")
    assert spec.stock_or_flow == StockOrFlow.FLOW
    assert spec.time_period == "1日"
    assert spec.target_unit == "item"
    assert spec.geography == "日本"


def test_market_size_question():
    spec = parse_question_rule_based("日本国内のペットボトル飲料の年間市場規模はいくらか")
    assert spec.target_unit == "JPY"
    assert spec.stock_or_flow == StockOrFlow.FLOW
    assert spec.geography == "日本"


def test_no_geography_becomes_provisional_default():
    spec = parse_question_rule_based("ピアノ調律師は何人いるか")
    assert spec.geography == "日本"
    assert any(p.field == "geography" for p in spec.provisional)


def test_year_extraction():
    spec = parse_question_rule_based("2030年時点で必要なEV充電器は何台か")
    assert spec.reference_date == "2030"


def test_hints_override_detection():
    spec = parse_question_rule_based(
        "ピアノ調律師の数", geography_hint="大阪府", target_unit_hint="人",
        reference_date_hint="2025",
    )
    assert spec.geography == "大阪府"
    assert spec.reference_date == "2025"
    assert spec.target_unit == "person"


@pytest.mark.asyncio
async def test_llm_assist_only_when_ambiguous():
    llm = MockLLMProvider(canned={"classify": {
        "subject": "コンビニ店舗", "geography": "日本", "target_unit": "store",
        "stock_or_flow": "stock",
    }})
    # 曖昧な問い(暫定項目2つ以上)→ LLM補助が使われる
    spec, ai = await parse_question("コンビニどれくらい?", llm)
    assert ai is True
    assert spec.parsed_by == "llm"
    # 明確な問い → LLMは使われない
    spec2, ai2 = await parse_question("東京都内にはピアノ調律師が何人いるか", llm)
    assert ai2 is False
    assert spec2.parsed_by == "rule"


@pytest.mark.asyncio
async def test_noop_llm_keeps_provisional():
    spec, ai = await parse_question("コンビニどれくらい?", NoOpLLMProvider())
    assert ai is False
    assert len(spec.provisional) >= 2  # 暫定のまま(捏造しない)
