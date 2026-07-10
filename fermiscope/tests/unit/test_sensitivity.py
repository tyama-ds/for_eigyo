"""感度分析のテスト。"""

import numpy as np
import pytest

from fermiscope.domain.enums import DistributionKind, IssueType
from fermiscope.domain.models import Critique, ModelCandidate, ParameterEstimate, SimulationConfig
from fermiscope.estimation.engine import run_monte_carlo
from fermiscope.formula.graph import build_graph
from fermiscope.sensitivity.engine import analyze_sensitivity, spearman_check


def setup_model():
    units = {"a": "item", "b": "dimensionless", "c": "dimensionless"}
    graph = build_graph("a * b / c", "item", units)
    params = {
        "a": ParameterEstimate(id="a", name="A", unit="item", central=100.0, low=95.0, high=105.0,
                               distribution=DistributionKind.LOGNORMAL),
        "b": ParameterEstimate(id="b", name="B", unit="dimensionless", central=0.5, low=0.25,
                               high=1.0, distribution=DistributionKind.LOGNORMAL),
        "c": ParameterEstimate(id="c", name="C", unit="dimensionless", central=2.0, low=1.0,
                               high=4.0, distribution=DistributionKind.LOGNORMAL),
    }
    model = ModelCandidate(name="m", formula=graph, parameter_ids=list(params))
    return model, params


def test_elasticity_signs(settings):
    model, params = setup_model()
    sim, _, _ = run_monte_carlo(model, params, SimulationConfig(iterations=3000, seed=7), settings)
    results = {r.parameter_id: r for r in analyze_sensitivity(model, params, sim, {})}
    assert results["a"].elasticity == pytest.approx(1.0, abs=0.05)
    assert results["b"].elasticity == pytest.approx(1.0, abs=0.05)
    assert results["c"].elasticity == pytest.approx(-1.0, abs=0.05)  # 分母は弾力性-1
    assert results["c"].direction == "decrease"
    assert results["b"].direction == "increase"


def test_oat_and_rank(settings):
    model, params = setup_model()
    sim, _, _ = run_monte_carlo(model, params, SimulationConfig(iterations=3000, seed=7), settings)
    results = analyze_sensitivity(model, params, sim, {})
    # aは幅が狭い → OATスパンが最小 → 寄与順位最下位
    by_rank = sorted(results, key=lambda r: r.contribution_rank)
    assert by_rank[-1].parameter_id == "a"
    assert all(r.expected_improvement for r in results)


def test_spearman_against_simple_impl(settings):
    rng = np.random.default_rng(0)
    x = rng.normal(size=500)
    y = 2 * x + rng.normal(scale=0.5, size=500)
    from scipy.stats import spearmanr

    assert spearman_check(x, y) == pytest.approx(spearmanr(x, y).statistic, abs=1e-9)


def test_importance_combines_critique_severity(settings):
    model, params = setup_model()
    sim, _, _ = run_monte_carlo(model, params, SimulationConfig(iterations=3000, seed=7), settings)
    critique = Critique(parameter_id="c", issue_type=IssueType.SINGLE_SOURCE,
                        claim="単一情報源", severity=0.8)
    with_crit = {r.parameter_id: r for r in
                 analyze_sensitivity(model, params, sim, {critique.id: critique})}
    no_crit = {r.parameter_id: r for r in analyze_sensitivity(model, params, sim, {})}
    # 批判があるパラメータの重要度は、批判なし時より高い
    assert with_crit["c"].importance >= no_crit["c"].importance
    assert with_crit["c"].critique_severity == 0.8
