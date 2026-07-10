"""分布・モンテカルロ・シナリオ計算のテスト。"""

import numpy as np
import pytest

from fermiscope.domain.enums import DistributionKind
from fermiscope.domain.models import ModelCandidate, ParameterEstimate, SimulationConfig
from fermiscope.estimation.distributions import DistributionError, build_ppf, sample_parameters
from fermiscope.estimation.engine import compute_scenarios, deterministic_evaluate, run_monte_carlo
from fermiscope.formula.graph import build_graph


def p(id, central, low, high, dist=DistributionKind.LOGNORMAL, unit="dimensionless"):
    return ParameterEstimate(
        id=id, name=id, unit=unit, central=central, low=low, high=high, distribution=dist
    )


def test_lognormal_ppf_matches_p10_p90():
    param = p("x", 10.0, 5.0, 20.0)
    ppf = build_ppf(param)
    assert float(ppf(np.array([0.5]))[0]) == pytest.approx(10.0, rel=0.01)
    assert float(ppf(np.array([0.1]))[0]) == pytest.approx(5.0, rel=0.05)
    assert float(ppf(np.array([0.9]))[0]) == pytest.approx(20.0, rel=0.05)


def test_loguniform_bounds():
    param = p("x", None, 1.0, 1000.0, DistributionKind.LOGUNIFORM)
    ppf = build_ppf(param)
    samples = ppf(np.linspace(0.001, 0.999, 100))
    assert samples.min() >= 1.0 and samples.max() <= 1000.0
    assert float(ppf(np.array([0.5]))[0]) == pytest.approx(np.sqrt(1000), rel=0.05)


def test_fixed_distribution():
    param = p("x", 42.0, None, None, DistributionKind.FIXED)
    ppf = build_ppf(param)
    assert np.all(ppf(np.array([0.01, 0.5, 0.99])) == 42.0)


def test_uniform_and_triangular_monotone():
    for dist in (DistributionKind.UNIFORM, DistributionKind.TRIANGULAR):
        param = p("x", 5.0, 1.0, 10.0, dist)
        ppf = build_ppf(param)
        values = ppf(np.linspace(0.01, 0.99, 50))
        assert np.all(np.diff(values) >= -1e-9)


def test_empirical_distribution():
    param = ParameterEstimate(
        id="x", name="x", unit="dimensionless",
        central=5.0, low=1.0, high=10.0,
        distribution=DistributionKind.EMPIRICAL,
        distribution_parameters={"p10": 1.0, "p50": 5.0, "p90": 10.0},
    )
    ppf = build_ppf(param)
    assert float(ppf(np.array([0.5]))[0]) == pytest.approx(5.0)


def test_invalid_distribution_params_raise():
    with pytest.raises(DistributionError):
        build_ppf(p("x", 1.0, -5.0, 10.0))  # lognormalに負のlow


def test_sampling_reproducible_with_seed():
    params = {"a": p("a", 10, 5, 20), "b": p("b", 2, 1, 4)}
    s1 = sample_parameters(params, 500, seed=123)
    s2 = sample_parameters(params, 500, seed=123)
    s3 = sample_parameters(params, 500, seed=456)
    assert np.array_equal(s1["a"], s2["a"])
    assert not np.array_equal(s1["a"], s3["a"])


def test_copula_correlation():
    params = {"a": p("a", 10, 5, 20), "b": p("b", 2, 1, 4)}
    samples = sample_parameters(params, 4000, seed=1, correlations=[("a", "b", 0.7)])
    from scipy.stats import spearmanr

    rho = spearmanr(samples["a"], samples["b"]).statistic
    assert 0.55 < rho < 0.85


def test_invalid_correlation_matrix_rejected():
    params = {"a": p("a", 10, 5, 20), "b": p("b", 2, 1, 4), "c": p("c", 3, 2, 5)}
    # 非正定値になる組合せ
    with pytest.raises(DistributionError):
        sample_parameters(
            params, 100, seed=1,
            correlations=[("a", "b", 0.99), ("b", "c", 0.99), ("a", "c", -0.99)],
        )


def make_model(settings):
    units = {"stock": "item", "rate": "event/(item*year)", "capacity": "event/(person*year)"}
    graph = build_graph("stock * rate / capacity", "person", units)
    params = {
        "stock": p("stock", 1000.0, 800.0, 1200.0, unit="item"),
        "rate": p("rate", 0.5, 0.3, 0.7, unit="event/(item*year)"),
        "capacity": p("capacity", 100.0, 50.0, 200.0, unit="event/(person*year)"),
    }
    model = ModelCandidate(name="test", formula=graph, parameter_ids=list(params))
    return model, params


def test_monte_carlo_reproducible(settings):
    model, params = make_model(settings)
    cfg = SimulationConfig(iterations=3000, seed=42)
    r1, _, _ = run_monte_carlo(model, params, cfg, settings)
    r2, _, _ = run_monte_carlo(model, params, cfg, settings)
    assert r1.median == r2.median
    assert r1.quantiles == r2.quantiles
    assert r1.seed == 42


def test_scenarios_ordered_and_extremes_direction_aware(settings):
    model, params = make_model(settings)
    cfg = SimulationConfig(iterations=4000, seed=42)
    sim, _, _ = run_monte_carlo(model, params, cfg, settings)
    scenarios = {s.kind: s for s in compute_scenarios(model, params, sim, settings)}
    assert scenarios["bear"].value < scenarios["base"].value < scenarios["bull"].value
    # capacityは分母 → 極端下限では capacity=high が使われる(方向考慮)
    assert scenarios["extreme_low"].parameter_overrides["capacity"] == 200.0
    assert scenarios["extreme_high"].parameter_overrides["capacity"] == 50.0
    assert scenarios["extreme_low"].value < scenarios["bear"].value
    assert scenarios["extreme_high"].value > scenarios["bull"].value


def test_custom_scenario_deterministic(settings):
    model, params = make_model(settings)
    cfg = SimulationConfig(iterations=2000, seed=42)
    sim, _, _ = run_monte_carlo(model, params, cfg, settings)
    scenarios = compute_scenarios(model, params, sim, settings, custom_overrides={"capacity": 100.0})
    custom = next(s for s in scenarios if s.kind == "custom")
    assert custom.value == pytest.approx(deterministic_evaluate(model, params, {"capacity": 100.0}))


def test_unresolved_parameter_blocks_estimation(settings):
    model, params = make_model(settings)
    params["rate"].central = None
    with pytest.raises(Exception, match="未解決"):
        deterministic_evaluate(model, params)
