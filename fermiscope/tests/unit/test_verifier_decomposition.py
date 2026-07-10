"""敵対的検証(決定論チェック)と再分解エンジンのテスト。"""

import pytest

from fermiscope.adversarial.verifier import analyze_counter_evidence, deterministic_checks
from fermiscope.decomposition.engine import (
    _rule_decompose,
    decide_decompositions,
    validate_decomposition,
)
from fermiscope.domain.enums import IssueType, SearchPurpose
from fermiscope.domain.models import (
    Critique,
    EvidenceItem,
    ModelCandidate,
    ParameterEstimate,
    SensitivityResult,
)
from fermiscope.formula.graph import build_graph
from fermiscope.llm import NoOpLLMProvider
from fermiscope.llm.schemas import DecompositionProposal, ParameterProposal


def param(**kwargs) -> ParameterEstimate:
    defaults = dict(id="p1", name="テスト", unit="dimensionless",
                    central=1.0, low=0.5, high=2.0, target_geography="東京都")
    defaults.update(kwargs)
    return ParameterEstimate(**defaults)


def ev(**kwargs) -> EvidenceItem:
    defaults = dict(url="https://x.example.jp/", parameter_id="p1",
                    extracted_value=1.0, evidence_score=70.0, accepted=True)
    e = EvidenceItem(**{**defaults, **kwargs})
    e.cluster_id = e.cluster_id or f"cluster_{e.id}"
    return e


def issue_types(critiques):
    return {c.issue_type for c in critiques}


def test_no_evidence_critique(settings):
    p = param(central=None)
    crits = deterministic_checks(p, {}, None, settings, 2026)
    assert issue_types(crits) == {IssueType.NO_EVIDENCE}
    assert crits[0].severity >= 0.8
    assert crits[0].check_detail  # 検査内容が記録される


def test_single_source_critique(settings):
    p = param(evidence_ids=["e1"])
    evidence = {"e1": ev(id="e1", evidence_score=40.0)}
    evidence["e1"].cluster_id = "c1"
    crits = deterministic_checks(p, evidence, None, settings, 2026)
    single = [c for c in crits if c.issue_type == IssueType.SINGLE_SOURCE]
    assert single and single[0].severity == 0.6  # 低スコア単一情報源は重大度0.6


def test_geography_mismatch_critique(settings):
    p = param(evidence_ids=["e1", "e2"])
    evidence = {
        "e1": ev(id="e1", geography="日本"),
        "e2": ev(id="e2", url="https://y.example.jp/", geography="日本"),
    }
    evidence["e1"].cluster_id, evidence["e2"].cluster_id = "c1", "c2"
    crits = deterministic_checks(p, evidence, None, settings, 2026)
    geo = [c for c in crits if c.issue_type == IssueType.GEOGRAPHY_MISMATCH]
    assert geo and "全国値" in geo[0].claim


def test_stale_extrapolation_critique(settings):
    p = param(evidence_ids=["e1"])
    evidence = {"e1": ev(id="e1", time_period="1996年")}
    crits = deterministic_checks(p, evidence, None, settings, 2026)
    assert IssueType.STALE_EXTRAPOLATION in issue_types(crits)


def test_wide_uncertainty_critique(settings):
    p = param(low=0.1, high=1.0, central=0.3, evidence_ids=["e1", "e2"])
    evidence = {
        "e1": ev(id="e1", methodology_summary="調査"),
        "e2": ev(id="e2", url="https://y.example.jp/", methodology_summary="調査"),
    }
    evidence["e1"].cluster_id, evidence["e2"].cluster_id = "c1", "c2"
    crits = deterministic_checks(p, evidence, None, settings, 2026)
    assert IssueType.WIDE_UNCERTAINTY in issue_types(crits)


def test_counter_evidence_scaled_by_quality(settings):
    p = param(central=0.104, evidence_ids=["e1"])
    weak_counter = ev(
        id="ce1", url="https://weak.example.jp/", extracted_value=80.0, unit="percent",
        evidence_score=20.0, search_purpose=SearchPurpose.COUNTER_EVIDENCE,
    )
    evidence = {"e1": ev(id="e1"), "ce1": weak_counter}
    crits = analyze_counter_evidence(p, evidence, settings)
    counter = [c for c in crits if c.issue_type == IssueType.COUNTER_EVIDENCE_EXISTS]
    assert counter
    # 弱い反証(スコア20)は重大度が抑制され、再分解閾値(0.6)未満
    assert counter[0].severity < 0.6
    assert counter[0].opposing_evidence_ids == ["ce1"]


def test_revision_detected(settings):
    p = param(evidence_ids=["e1"])
    correction = ev(
        id="ce1", url="https://gov.example-gov.jp/correction",
        title="統計の訂正について", extracted_value=None,
        search_purpose=SearchPurpose.CORRECTION,
    )
    evidence = {"e1": ev(id="e1"), "ce1": correction}
    crits = analyze_counter_evidence(p, evidence, settings)
    assert IssueType.RETRACTION_OR_REVISION in issue_types(crits)


# ---- 再分解 ----

def test_rule_decompose_annual_capacity():
    p = param(unit="tuning/(person*year)", name="年間処理能力")
    proposal = _rule_decompose(p)
    assert proposal is not None
    assert "_daily" in proposal.expression and "_working_days" in proposal.expression


def test_validate_decomposition_dimension_mismatch_rejected(settings):
    p = param(unit="tuning/(person*year)")
    proposal = DecompositionProposal(
        expression="x1 * x2",
        parameters=[
            ParameterProposal(id="x1", name="A", unit="person", search_terms_ja=["a"]),
            ParameterProposal(id="x2", name="B", unit="person", search_terms_ja=["b"]),
        ],
    )
    checks, details = validate_decomposition(p, proposal, {}, settings, 5)
    assert checks["dimension_match"] is False
    assert any("次元" in d for d in details)


def test_validate_decomposition_cycle_rejected(settings):
    p = param(id="p1", unit="dimensionless")
    proposal = DecompositionProposal(
        expression="p1 * x2",
        parameters=[
            ParameterProposal(id="p1", name="自己参照", unit="dimensionless", search_terms_ja=["a"]),
            ParameterProposal(id="x2", name="B", unit="dimensionless", search_terms_ja=["b"]),
        ],
    )
    checks, _ = validate_decomposition(p, proposal, {"p1": p}, settings, 5)
    assert checks["no_cycle"] is False


def test_validate_decomposition_leaf_limit(settings):
    p = param(unit="dimensionless")
    proposal = DecompositionProposal(
        expression="x1 * x2",
        parameters=[
            ParameterProposal(id="x1", name="A", unit="dimensionless", search_terms_ja=["a"]),
            ParameterProposal(id="x2", name="B", unit="dimensionless", search_terms_ja=["b"]),
        ],
    )
    checks, _ = validate_decomposition(
        p, proposal, {}, settings,
        current_leaf_count=settings.decomposition.max_leaves_after_expansion,
    )
    assert checks["within_leaf_limit"] is False


@pytest.mark.asyncio
async def test_decide_decomposition_thresholds(settings):
    """重大な批判+高重要度のパラメータのみ分解され、他は対象外。"""
    units = {"cap": "tuning/(person*year)", "n": "item"}
    graph = build_graph("n * cap", "tuning/year", units)
    params = {
        "cap": param(id="cap", unit="tuning/(person*year)", central=300.0, low=100.0, high=900.0),
        "n": param(id="n", unit="item", central=10.0, low=9.0, high=11.0),
    }
    model = ModelCandidate(name="m", formula=graph, parameter_ids=["cap", "n"])
    critiques = {
        "c1": Critique(id="c1", parameter_id="cap", issue_type=IssueType.SINGLE_SOURCE,
                       claim="単一情報源", severity=0.7),
        "c2": Critique(id="c2", parameter_id="n", issue_type=IssueType.SINGLE_SOURCE,
                       claim="単一情報源", severity=0.7),
    }
    sens = [
        SensitivityResult(parameter_id="cap", importance=0.9, model_id=model.id),
        SensitivityResult(parameter_id="n", importance=0.05, model_id=model.id),  # 閾値未満
    ]
    attempts, irreducibles, _ = await decide_decompositions(
        model, params, critiques, sens, settings, NoOpLLMProvider()
    )
    attempted_ids = {a.parameter_id for a in attempts}
    assert "cap" in attempted_ids
    assert "n" not in attempted_ids  # 重要度が低いため分解しない
    cap_attempt = next(a for a in attempts if a.parameter_id == "cap")
    assert cap_attempt.accepted  # ルールテンプレートで分解成功
    assert set(model.formula.leaf_parameter_ids()) == {"n", "cap_daily", "cap_working_days"}
    assert params["cap"].decomposition_status.value == "decomposed"


@pytest.mark.asyncio
async def test_irreducible_when_no_template(settings):
    units = {"r": "dimensionless", "n": "item"}
    graph = build_graph("n * r", "item", units)
    params = {
        "r": param(id="r", unit="dimensionless", central=0.5, low=0.1, high=0.9),
        "n": param(id="n", unit="item", central=10.0, low=9.0, high=11.0),
    }
    model = ModelCandidate(name="m", formula=graph, parameter_ids=["r", "n"])
    critiques = {
        "c1": Critique(id="c1", parameter_id="r", issue_type=IssueType.SINGLE_SOURCE,
                       claim="単一情報源", severity=0.8),
    }
    sens = [SensitivityResult(parameter_id="r", importance=0.9, model_id=model.id)]
    attempts, irreducibles, _ = await decide_decompositions(
        model, params, critiques, sens, settings, NoOpLLMProvider()
    )
    assert irreducibles and irreducibles[0].parameter_id == "r"
    irr = irreducibles[0]
    assert irr.user_editable_value
    assert irr.what_new_evidence_would_resolve_it
    assert params["r"].decomposition_status.value == "irreducible"
