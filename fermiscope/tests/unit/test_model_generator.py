"""問題類型テンプレート・モデル候補生成のテスト。"""

import pytest

from fermiscope.llm import MockLLMProvider, NoOpLLMProvider
from fermiscope.models.generator import generate_model_candidates
from fermiscope.question.parser import parse_question_rule_based


@pytest.mark.asyncio
async def test_piano_question_generates_demand_and_supply_models():
    spec = parse_question_rule_based("東京都内にはピアノ調律師が何人いるか")
    models, params, ai = await generate_model_candidates(spec, NoOpLLMProvider())
    keys = {m.template_key: m.role for m in models}
    assert keys.get("maintenance_demand") == "primary"
    assert keys.get("association_supply") == "check"
    assert ai is False
    # 全モデルの式が単位検査に合格
    for m in models:
        if m.role in ("primary", "check"):
            assert m.formula.unit_check_passed, m.formula.unit_check_detail
    # パラメータには検索語が付与されている
    primary = next(m for m in models if m.role == "primary")
    for pid in primary.formula.leaf_parameter_ids():
        assert params[pid].search_terms_ja


@pytest.mark.asyncio
async def test_scores_and_selection_reason():
    spec = parse_question_rule_based("東京都内にはピアノ調律師が何人いるか")
    models, _, _ = await generate_model_candidates(spec, NoOpLLMProvider())
    primary = next(m for m in models if m.role == "primary")
    assert primary.total_score == max(m.total_score for m in models)
    assert primary.selection_reason
    required_criteria = {
        "estimability", "unit_consistency", "explainability",
        "evidence_availability", "double_counting_risk", "dependency_risk", "independence",
    }
    assert required_criteria <= set(primary.scores.keys())


@pytest.mark.asyncio
async def test_generic_question_uses_population_ratio():
    spec = parse_question_rule_based("日本で1日に廃棄される傘は何本か")
    models, _, _ = await generate_model_candidates(spec, NoOpLLMProvider())
    keys = [m.template_key for m in models]
    assert "population_ratio" in keys


@pytest.mark.asyncio
async def test_llm_proposal_with_invalid_units_rejected():
    llm = MockLLMProvider(canned={"models": [
        {
            "name": "不正モデル",
            "expression": "a * b",
            "parameters": [
                {"id": "a", "name": "A", "unit": "person"},
                {"id": "b", "name": "B", "unit": "person"},  # person*person ≠ person
            ],
        },
    ]})
    spec = parse_question_rule_based("ピアノ調律師の平均年収はいくらか")
    spec.target_unit = "person"
    models, _, ai = await generate_model_candidates(spec, llm)
    llm_models = [m for m in models if m.proposed_by == "llm"]
    if llm_models:  # LLM候補が生成された場合、単位不合格は不採用になる
        assert all(m.role == "rejected" for m in llm_models)
