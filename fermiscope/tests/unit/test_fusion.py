"""証拠統合(重み付き分位点・クラスタ・外れ値・未解決)のテスト。"""

import pytest

from fermiscope.domain.enums import ParameterStatus, ValueBasis
from fermiscope.domain.models import EvidenceItem, ParameterEstimate
from fermiscope.estimation.fusion import fuse_evidence, weighted_median, weighted_quantile


def make_param(**kwargs) -> ParameterEstimate:
    defaults = dict(id="p1", name="テスト", unit="dimensionless")
    defaults.update(kwargs)
    return ParameterEstimate(**defaults)


def make_ev(value, score, cluster="", **kwargs) -> EvidenceItem:
    ev = EvidenceItem(
        url=kwargs.pop("url", f"https://src{value}.example.jp/"),
        parameter_id="p1",
        extracted_value=value,
        evidence_score=score,
        unit=kwargs.pop("unit", "dimensionless"),
        **kwargs,
    )
    ev.cluster_id = cluster or f"cluster_{ev.id}"
    return ev


def test_weighted_median_hand_computed():
    # 値 [1, 2, 10]、重み [1, 1, 1] → 中央値 2
    assert weighted_median([1, 2, 10], [1, 1, 1]) == pytest.approx(2.0)
    # 重みを10側に寄せると中央値が10側へ引かれる(補間型: 手計算値 8.545…)
    assert weighted_median([1, 2, 10], [1, 1, 10]) == pytest.approx(8.5454545, rel=1e-6)
    # 圧倒的な重みでは値そのものに到達する
    assert weighted_median([1, 2, 10], [0.01, 0.01, 100]) == pytest.approx(10.0, rel=0.01)


def test_weighted_quantile_bounds():
    values, weights = [1.0, 2.0, 3.0], [1.0, 1.0, 1.0]
    assert weighted_quantile(values, weights, 0.0) == 1.0
    assert weighted_quantile(values, weights, 1.0) == 3.0
    q50 = weighted_quantile(values, weights, 0.5)
    assert q50 == pytest.approx(2.0)


def test_weighted_quantile_validation():
    with pytest.raises(ValueError):
        weighted_quantile([], [], 0.5)
    with pytest.raises(ValueError):
        weighted_quantile([1], [1, 2], 0.5)
    with pytest.raises(ValueError):
        weighted_quantile([1], [-1], 0.5)
    with pytest.raises(ValueError):
        weighted_quantile([-1], [1], 0.5, log_space=True)


def test_log_space_median():
    # 対数空間の中央値は幾何的な補間になる
    v = weighted_quantile([1.0, 100.0], [1.0, 1.0], 0.5, log_space=True)
    assert v == pytest.approx(10.0, rel=0.01)


def test_no_evidence_becomes_unresolved(settings):
    param = fuse_evidence(make_param(), [], settings)
    assert param.status == ParameterStatus.UNRESOLVED
    assert param.value_basis == ValueBasis.UNRESOLVED
    assert param.central is None
    assert "捏造" in param.unresolved_reason


def test_low_score_evidence_excluded(settings):
    param = fuse_evidence(make_param(), [make_ev(5.0, score=5.0)], settings)
    assert param.status == ParameterStatus.UNRESOLVED


def test_single_evidence_gets_assumption_width(settings):
    param = fuse_evidence(make_param(), [make_ev(0.5, score=80)], settings)
    assert param.central == pytest.approx(0.5)
    assert param.low < 0.5 < param.high
    assert any("仮定" in a for a in param.assumptions)
    assert param.value_basis == ValueBasis.EVIDENCE


def test_reprint_cluster_counted_once(settings):
    """同じ一次資料の転載記事はまとめて1証拠として扱う(絶対条件9)。"""
    primary = make_ev(0.10, score=90, cluster="shared")
    reprint1 = make_ev(0.10, score=40, cluster="shared")
    reprint2 = make_ev(0.10, score=35, cluster="shared")
    other = make_ev(0.30, score=90)
    param = fuse_evidence(make_param(), [primary, reprint1, reprint2, other], settings)
    # 転載が3件でも1票なので、中央値は0.10と0.30の中間になる
    # (もし3票なら0.10へ張り付く)
    assert param.central > 0.12
    assert "転載クラスタ" in param.fusion_note


def test_incompatible_definition_not_averaged(settings):
    good = make_ev(0.10, score=90)
    bad = make_ev(0.99, score=85)
    bad.incompatible_reason = "定義が非互換"
    param = fuse_evidence(make_param(), [good, bad], settings)
    assert param.central == pytest.approx(0.10, rel=0.05)


def test_stale_data_excluded_when_recent_exists(settings):
    recent = make_ev(0.10, score=85, time_period="2024年")
    stale = make_ev(0.22, score=60, time_period="1996年")
    param = fuse_evidence(make_param(), [recent, stale], settings, reference_year=2026)
    assert param.central == pytest.approx(0.10, rel=0.2)
    assert "古いデータ" in param.fusion_note


def test_outlier_excluded_from_fusion(settings):
    items = [make_ev(v, score=70) for v in (1.0, 1.1, 0.9, 1.05, 1000.0)]
    param = fuse_evidence(make_param(), items, settings)
    assert param.central < 5.0
    assert "外れ値" in param.fusion_note


def test_percent_unit_bridged(settings):
    param = make_param(unit="piano/household")
    ev = make_ev(10.4, score=85, unit="percent")
    fused = fuse_evidence(param, [ev], settings)
    assert fused.central == pytest.approx(0.104)
    assert ev.normalized_value == pytest.approx(0.104)


def test_distribution_rationale_saved(settings):
    param = fuse_evidence(make_param(), [make_ev(0.5, score=80), make_ev(0.7, score=70)], settings)
    assert param.distribution_rationale
    assert param.distribution.value in ("lognormal", "triangular", "loguniform")


def test_value_change_history_recorded(settings):
    param = fuse_evidence(make_param(), [make_ev(0.5, score=80)], settings)
    assert any(h.field == "central" for h in param.history)
