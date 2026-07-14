"""Section 3: 数値入力の検証(NaN/Inf・相関・反復回数・有効範囲)の回帰テスト。"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError
from tests.conftest import PIANO_QUESTION

from fermiscope.domain.models import ParameterEstimate, SimulationConfig


def _make_project(app_client) -> str:
    report = app_client.post("/api/projects", json={"question": PIANO_QUESTION}).json()
    return report["project"]["id"]


# ---- モデルレベル(構築時の検証) ----


def test_simulation_config_rejects_zero_iterations():
    with pytest.raises(ValidationError):
        SimulationConfig(iterations=0)


def test_simulation_config_rejects_negative_iterations():
    with pytest.raises(ValidationError):
        SimulationConfig(iterations=-10)


def test_simulation_config_rejects_bad_correlation():
    with pytest.raises(ValidationError):
        SimulationConfig(correlations=[("a", "b", 1.5)])
    with pytest.raises(ValidationError):
        SimulationConfig(correlations=[("a", "b", -2.0)])


def test_parameter_rejects_nan_inf():
    with pytest.raises(ValidationError):
        ParameterEstimate(id="p", name="x", central=math.nan)
    with pytest.raises(ValidationError):
        ParameterEstimate(id="p", name="x", central=math.inf)


def test_parameter_rejects_out_of_range():
    # 割合パラメータ(0〜1)に 1.5 は範囲外
    with pytest.raises(ValidationError):
        ParameterEstimate(id="r", name="率", unit="dimensionless",
                          valid_min=0.0, valid_max=1.0, central=1.5)
    # 非負カウントに負値は範囲外
    with pytest.raises(ValidationError):
        ParameterEstimate(id="c", name="店舗数", unit="store",
                          valid_min=0.0, central=-5.0)


# ---- API レベル(422 を返す) ----


def test_create_rejects_out_of_range_iterations(app_client):
    r = app_client.post("/api/projects", json={"question": PIANO_QUESTION, "iterations": 10})
    assert r.status_code == 422
    r2 = app_client.post(
        "/api/projects", json={"question": PIANO_QUESTION, "iterations": 999999999}
    )
    assert r2.status_code == 422


def test_recalculate_rejects_bad_correlation(app_client):
    pid = _make_project(app_client)
    r = app_client.post(
        f"/api/projects/{pid}/recalculate",
        json={"correlations": [["base_households", "ownership_rate", 3.0]]},
    )
    assert r.status_code == 422


def test_recalculate_request_rejects_non_finite_override():
    """recalculate リクエストは NaN/Inf の custom_overrides を拒否する。

    標準 JSON は NaN/Inf を持てないため、リクエストモデルの検証で拒否することを
    確認する(誤った値が計算へ流入しない)。
    """
    from fermiscope.api.routes import RecalculateRequest

    with pytest.raises(ValidationError):
        RecalculateRequest(custom_overrides={"base_households": math.inf})
    with pytest.raises(ValidationError):
        RecalculateRequest(custom_overrides={"base_households": math.nan})


def test_parameter_range_metadata_present(app_client):
    """生成された割合パラメータに有効範囲メタが付与される。"""
    pid = _make_project(app_client)
    rep = app_client.get(f"/api/projects/{pid}").json()
    rate_params = [p for p in rep["parameters"] if p["id"] == "membership_rate"]
    assert rate_params, "membership_rate が見つからない"
    rp = rate_params[0]
    assert rp["valid_min"] == 0.0
    assert rp["valid_max"] == 1.0
    # 非負カウントは下限0・上限なし
    counts = [p for p in rep["parameters"] if p["id"] == "base_households"]
    assert counts and counts[0]["valid_min"] == 0.0 and counts[0]["valid_max"] is None


def test_parameter_update_out_of_range_is_422(app_client):
    """割合(0〜1)パラメータを 1 超へ更新すると 422。"""
    pid = _make_project(app_client)
    r = app_client.patch(
        f"/api/projects/{pid}/parameters/membership_rate",
        json={"central": 1.5, "low": 1.2, "high": 1.8},
    )
    assert r.status_code == 422
    # 非負カウントを負値へ更新しても 422
    r2 = app_client.patch(
        f"/api/projects/{pid}/parameters/base_households",
        json={"central": -1.0, "low": -2.0, "high": 0.0},
    )
    assert r2.status_code == 422
