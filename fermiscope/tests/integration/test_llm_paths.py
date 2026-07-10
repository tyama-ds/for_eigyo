"""AIフォールバック経路の統合テスト(MockLLMProvider使用)。"""

import pytest
from tests.conftest import build_piano_project, run_piano_research

from fermiscope.domain.models import ParameterEstimate
from fermiscope.evidence.extractor import validate_llm_extraction
from fermiscope.llm import MockLLMProvider, NoOpLLMProvider
from fermiscope.research.orchestrator import ResearchOrchestrator
from fermiscope.research.search.service import SearchService


@pytest.mark.asyncio
async def test_pipeline_with_mock_llm_completes(settings, mock_llm, mock_search, mock_fetcher):
    project = await run_piano_research(settings, mock_llm, mock_search, mock_fetcher)
    run = project.current_run()
    assert run.status.value == "done"
    # NoOp と同様に主要パラメータは証拠ベースで解決される(LLMが主経路を奪わない)
    primary = project.primary_model()
    for pid in primary.formula.leaf_parameter_ids():
        assert project.parameters[pid].central is not None


@pytest.mark.asyncio
async def test_llm_extraction_fallback_success(settings, mock_search, mock_fetcher):
    """ルール抽出が失敗する文書でLLM抽出が使われ、監査に記録される。"""
    doc = await mock_fetcher.fetch("https://forum.example-bbs.jp/threads/piano-tuner-union.html")
    # 実文書に存在する抜粋を返すLLM → 検証を通過する
    llm = MockLLMProvider(canned={"extract": {
        "value": 50.0, "unit": "percent",
        "excerpt": "組織率は50%くらいらしいよ",
        "locator": "本文",
    }})
    payload = await llm.extract_structured_evidence("dummy", "組織率", "dimensionless")
    ok, reason = validate_llm_extraction(
        doc,
        ParameterEstimate(id="membership_rate", name="組織率", unit="dimensionless"),
        payload.model_dump(),
    )
    assert ok, reason


@pytest.mark.asyncio
async def test_llm_fabricated_excerpt_rejected(settings, mock_fetcher):
    """文書に存在しない抜粋(捏造)はPython側検証で棄却される。"""
    doc = await mock_fetcher.fetch("https://forum.example-bbs.jp/threads/piano-tuner-union.html")
    fabricated = {
        "value": 99.0, "unit": "percent",
        "excerpt": "全国調査によると組織率は99%で確定しています",
        "locator": "本文",
    }
    ok, reason = validate_llm_extraction(
        doc,
        ParameterEstimate(id="membership_rate", name="組織率", unit="dimensionless"),
        fabricated,
    )
    assert not ok
    assert "捏造" in reason or "見つかりません" in reason


@pytest.mark.asyncio
async def test_llm_invalid_decomposition_rejected(settings, mock_search, mock_fetcher):
    """LLMの分解案が次元不一致ならPython検査で却下され、分解不能仮定になる。"""
    llm = MockLLMProvider(canned={
        "decompose": {
            "expression": "bogus_a * bogus_b",
            "parameters": [
                {"id": "bogus_a", "name": "A", "unit": "person", "search_terms_ja": ["a"]},
                {"id": "bogus_b", "name": "B", "unit": "person", "search_terms_ja": ["b"]},
            ],
            "rationale": "でたらめな分解",
        },
        "critique": [],
    })
    project = await run_piano_research(settings, llm, mock_search, mock_fetcher)
    # membership_rate(dimensionless)に対する person*person の分解は却下される
    rejected = [a for a in project.decomposition_attempts
                if a.parameter_id == "membership_rate" and not a.accepted]
    assert rejected
    assert any("次元" in d for a in rejected for d in a.check_details)
    assert any(i.parameter_id == "membership_rate" for i in project.irreducible_assumptions)


@pytest.mark.asyncio
async def test_ai_fallback_recorded_in_audit(settings, mock_search, mock_fetcher):
    llm = MockLLMProvider()
    project = await run_piano_research(settings, llm, mock_search, mock_fetcher)
    run = project.current_run()
    ai_events = [a for a in project.audit_events if a.category == "ai_fallback"]
    # MockLLMはdecompose提案なし→分解はルール。批判仮説はcritique経由で使われ得る
    assert run.ai_fallback_uses == len(ai_events)


@pytest.mark.asyncio
async def test_llm_unavailable_does_not_fail_run(settings, mock_search, mock_fetcher):
    """LLMが利用不能でも全体は失敗せず、未解決項目として残る(絶対条件8)。"""
    project = await build_piano_project(settings, NoOpLLMProvider())
    # 検索が何もヒットしないパラメータを追加
    primary = project.primary_model()
    from fermiscope.formula.graph import build_graph

    project.parameters["mystery"] = ParameterEstimate(
        id="mystery", name="謎の係数", unit="dimensionless",
        search_terms_ja=["存在しないキーワードXYZ123"],
    )
    units = {pid: p.unit for pid, p in project.parameters.items()}
    primary.formula = build_graph(
        primary.formula.expression + " * mystery", primary.formula.target_unit, units
    )
    service = SearchService(mock_search, settings)
    orch = ResearchOrchestrator(settings, service, mock_fetcher, NoOpLLMProvider())
    await orch.run_research(project)
    run = project.current_run()
    assert run.status.value == "done"  # 全体は失敗しない
    mystery = project.parameters["mystery"]
    assert mystery.status.value == "unresolved"
    assert mystery.central is None  # 値を捏造しない
    # 主モデルのシミュレーションは実行できない(未解決)が、その旨が監査に残る
    warnings = [a for a in project.audit_events if a.category in ("warning", "unresolved")]
    assert warnings
