"""問い入力から結果生成までの統合テスト(MockSearchProvider + NoOpLLMProvider)。

固定フィクスチャ「東京都内のピアノ調律師数」を使用。外部ネットワーク不要。
"""

import asyncio

import pytest
from tests.conftest import run_piano_research

from fermiscope.config import load_settings
from fermiscope.domain.enums import DocumentType, RunStatus, SourceClass, ValueBasis
from fermiscope.llm import NoOpLLMProvider
from fermiscope.research.fetcher import DocumentFetcher
from fermiscope.research.mock_transport import build_mock_transport
from fermiscope.research.search.mock import MockSearchProvider


@pytest.fixture(scope="module")
def module_settings():
    s = load_settings()
    s.simulation.iterations = 4000
    return s


@pytest.fixture(scope="module")
def piano_project(module_settings):
    """調査パイプラインを1回だけ実行し、読み取り専用で全テストが共有する。"""
    search = MockSearchProvider(module_settings.mock_corpus_dir)
    fetcher = DocumentFetcher(
        module_settings,
        transport=build_mock_transport(module_settings.mock_corpus_dir),
        skip_dns=True,
    )
    return asyncio.run(
        run_piano_research(module_settings, NoOpLLMProvider(), search, fetcher)
    )


async def test_run_completes(piano_project):
    run = piano_project.current_run()
    assert run.status == RunStatus.DONE
    assert run.error == ""
    assert run.searches_executed > 0
    assert run.documents_fetched > 0
    assert run.evidence_found > 0


async def test_primary_parameters_resolved_with_evidence(piano_project):
    primary = piano_project.primary_model()
    for pid in primary.formula.leaf_parameter_ids():
        p = piano_project.parameters[pid]
        assert p.central is not None, pid
        assert p.low is not None and p.high is not None
        assert p.value_basis in (ValueBasis.EVIDENCE, ValueBasis.DERIVED)
        assert p.evidence_ids, f"{pid} に証拠がない"
        assert p.unit


async def test_scenarios_exist_and_ordered(piano_project):
    kinds = {s.kind: s for s in piano_project.scenarios}
    assert {"bear", "base", "bull"} <= set(kinds)
    assert kinds["bear"].value < kinds["base"].value < kinds["bull"].value
    # 桁の妥当性(数百〜数千人のオーダー)
    assert 100 < kinds["base"].value < 10000


async def test_monte_carlo_reproducible_seed_recorded(piano_project):
    sim = piano_project.simulation_results[0]
    assert sim.seed == piano_project.simulation_config.seed
    assert sim.iterations == piano_project.simulation_config.iterations
    assert sim.histogram_counts and sim.histogram_bin_edges


async def test_validation_consistent(piano_project):
    v = piano_project.validation
    assert v is not None
    assert v.central_ratio is not None and v.central_ratio < 3.0
    assert v.agreement in ("consistent", "moderate")


async def test_contradiction_reported_with_time_analysis(piano_project):
    cons = [c for c in piano_project.contradictions if c.parameter_id == "ownership_rate"]
    assert cons, "1996年と2024年の保有率の矛盾が検出されるはず"
    assert any("time" in c.analysis for c in cons)


async def test_reprints_clustered_not_multiplied(piano_project):
    own_ev = [e for e in piano_project.evidence.values()
              if e.parameter_id == "ownership_rate" and e.parent_source_id]
    assert own_ev, "転載記事の証拠が存在するはず"
    csv_ev = [e for e in piano_project.evidence.values()
              if e.parameter_id == "ownership_rate" and e.url.endswith(".csv")]
    assert csv_ev
    assert all(e.cluster_id == csv_ev[0].cluster_id for e in own_ev), "転載は一次資料と同一クラスタ"


async def test_pdf_and_csv_sources_used(piano_project):
    doc_types = {e.document_type for e in piano_project.evidence.values()}
    assert DocumentType.PDF in doc_types
    assert DocumentType.CSV in doc_types
    assert DocumentType.HTML in doc_types


async def test_decomposition_of_capacity(piano_project):
    accepted = [a for a in piano_project.decomposition_attempts if a.accepted]
    assert any(a.parameter_id == "provider_capacity" for a in accepted)
    cap = piano_project.parameters["provider_capacity"]
    assert cap.decomposition_status.value == "decomposed"
    assert cap.value_basis == ValueBasis.DERIVED
    # 分解後の子パラメータはPDF証拠から推定される
    daily = piano_project.parameters["provider_capacity_daily"]
    assert daily.central == pytest.approx(1.5)
    days = piano_project.parameters["provider_capacity_working_days"]
    assert days.central == pytest.approx(200.0)


async def test_irreducible_assumption_reported(piano_project):
    irr = {i.parameter_id for i in piano_project.irreducible_assumptions}
    assert "membership_rate" in irr
    item = next(i for i in piano_project.irreducible_assumptions
                if i.parameter_id == "membership_rate")
    assert item.user_editable_value
    assert item.reason


async def test_every_parameter_has_adversarial_verification(piano_project):
    for model in (piano_project.primary_model(), piano_project.check_model()):
        for pid in model.formula.leaf_parameter_ids():
            p = piano_project.parameters[pid]
            assert p.verification_note, f"{pid} に敵対的検証の実施記録がない"
            crits = [c for c in piano_project.critiques.values() if c.parameter_id == pid]
            for c in crits:
                # 批判には検査内容または根拠(URL)が必ず紐づく
                assert c.check_detail or c.supporting_evidence_ids or c.opposing_evidence_ids
    # 少なくとも一部のパラメータには実際に指摘が存在する
    assert piano_project.critiques


async def test_geography_mismatch_critique_for_nationwide_evidence(piano_project):
    geo_crits = [c for c in piano_project.critiques.values()
                 if c.issue_type.value == "geography_mismatch"]
    assert geo_crits


async def test_source_classes_assigned(piano_project):
    classes = {e.source_class for e in piano_project.evidence.values()}
    assert SourceClass.S in classes  # 政府統計
    assert SourceClass.E in classes or SourceClass.D in classes  # 掲示板・ブログ


async def test_audit_log_reproducibility_info(piano_project):
    categories = {a.category for a in piano_project.audit_events}
    assert "run_start" in categories
    start = next(a for a in piano_project.audit_events if a.category == "run_start")
    assert "seed" in start.data
    assert "config_hash" in start.data
    assert "app_version" in start.data
    assert "search" in categories
    assert "fetch" in categories
    assert "value_change" in categories
    assert "decomposition" in categories


async def test_evidence_traceability(piano_project):
    """すべての証拠が URL・取得日・根拠箇所を持つ(絶対条件2・4)。"""
    for e in piano_project.evidence.values():
        assert e.url.startswith("https://")
        assert e.retrieval_date is not None
        if e.extracted_value is not None:
            assert e.short_supporting_excerpt, e.url
            assert e.evidence_score is not None
            assert e.scoring_reasons


async def test_confidence_and_caveats(piano_project):
    assert piano_project.overall_confidence is not None
    assert 0 < piano_project.overall_confidence < 1
    assert piano_project.confidence_reasons
    assert piano_project.key_caveats  # 矛盾・分解不能仮定が注意点に載る


async def test_search_budget_respected(piano_project, module_settings):
    run = piano_project.current_run()
    assert run.searches_executed <= module_settings.search.max_searches_per_project


async def test_sensitivity_for_both_models(piano_project):
    primary = piano_project.primary_model()
    check = piano_project.check_model()
    model_ids = {s.model_id for s in piano_project.sensitivity_results}
    assert primary.id in model_ids and check.id in model_ids
    for s in piano_project.sensitivity_results:
        assert s.contribution_rank >= 1
