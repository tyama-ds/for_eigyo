"""コードレビュー指摘への回帰テスト。"""

from __future__ import annotations

import numpy as np
import pytest

from fermiscope.config import load_settings
from fermiscope.domain.enums import DocumentType, ParameterStatus, ValueBasis
from fermiscope.domain.models import EvidenceItem, ParameterEstimate
from fermiscope.estimation.distributions import build_ppf
from fermiscope.estimation.fusion import fuse_evidence
from fermiscope.evidence.clustering import cluster_evidence
from fermiscope.evidence.dates import parse_year
from fermiscope.evidence.extractor import validate_llm_extraction
from fermiscope.evidence.ranker import rank_evidence
from fermiscope.formula.graph import FormulaEvalError, evaluate_node
from fermiscope.formula.parser import parse_expression
from fermiscope.reporting.export import _csv_safe
from fermiscope.research.fetcher import FetchedDocument
from fermiscope.security.sanitizer import sanitize_html, strip_html_to_text
from fermiscope.security.url_guard import UrlGuardError, validate_url


def _settings():
    return load_settings()


def _ev(**kw):
    e = EvidenceItem(url=kw.pop("url", "https://x.example.jp/"), parameter_id="p1", **kw)
    e.cluster_id = e.cluster_id or f"cluster_{e.id}"
    return e


def _param(**kw):
    d = dict(id="p1", name="テスト", unit="dimensionless")
    d.update(kw)
    return ParameterEstimate(**d)


# --- #1 LLM抽出の値が文書に紐づく ---

def _doc(text):
    return FetchedDocument(
        url="https://real.example.jp/", final_url="https://real.example.jp/",
        content_type="text/html", doc_type=DocumentType.HTML, status_code=200, text=text,
    )


def test_llm_value_must_appear_in_excerpt():
    doc = _doc("ピアノの普及率は10.4%と報告されている。")
    p = _param(unit="dimensionless")
    # 実在する抜粋を写しつつ、抜粋に無い値を捏造 → 棄却
    ok, reason = validate_llm_extraction(
        doc, p, {"value": 55.0, "unit": "percent", "excerpt": "ピアノの普及率は10.4%と報告されている"}
    )
    assert not ok and "捏造" in reason
    # 抜粋に現れる値なら通る
    ok2, _ = validate_llm_extraction(
        doc, p, {"value": 10.4, "unit": "percent", "excerpt": "ピアノの普及率は10.4%と報告されている"}
    )
    assert ok2


# --- #2 未解決化で古い値を残さない ---

def test_unresolved_clears_stale_value():
    s = _settings()
    p = _param(unit="dimensionless")
    p.central, p.low, p.high = 500.0, 400.0, 600.0
    p.value_basis = ValueBasis.EVIDENCE
    fuse_evidence(p, [], s)  # 証拠なし
    assert p.status == ParameterStatus.UNRESOLVED
    assert p.central is None and p.low is None and p.high is None
    assert p.confidence is None


# --- #3 独立情報源スコア ---

def test_unique_source_scores_independent():
    s = _settings()
    ev = _ev(publisher="総務省統計局", extracted_value=1.0, exact_definition="定義",
             methodology_summary="全数", geography="東京都", time_period="2025年")
    cluster_evidence([ev], s)  # 単独クラスタ
    rank_evidence(ev, _param(target_geography="東京都"), s, reference_year=2026)
    assert ev.subscores["independence"] == 85.0  # 独立扱い(30に固定されない)


def test_reprint_scores_non_independent():
    s = _settings()
    prim = _ev(url="https://gov.example-gov.jp/a.csv", content_hash="h1", extracted_value=1.0)
    rep = _ev(url="https://news.example.jp/b", content_hash="h1", extracted_value=1.0)
    cluster_evidence([prim, rep], s)
    rank_evidence(rep, _param(), s, reference_year=2026)
    assert rep.subscores["independence"] == 30.0


# --- ranker: 時点は time_period 優先 ---

def test_year_prefers_time_period_over_revision():
    s = _settings()
    ev = _ev(publisher="総務省統計局", extracted_value=1.0,
             time_period="2010年度", revision_date="2025-03")
    rank_evidence(ev, _param(), s, reference_year=2026)
    assert "stale_data_penalty" in ev.penalties_applied  # 2010年基準で古いと判定


# --- #5 / #8 式評価のガード ---

def test_power_zero_negative_guarded():
    tree = parse_expression("a ** -1")
    with pytest.raises(FormulaEvalError):
        evaluate_node(tree, {"a": 0.0})


def test_power_negative_base_fractional_guarded():
    tree = parse_expression("a ** 2")  # 整数指数はOK
    assert evaluate_node(tree, {"a": -3.0}) == pytest.approx(9.0)


def test_power_array_yields_nan_not_complex():
    # 指数は定数のみ許可。base配列×定数指数で、負底×小数指数がNaN化されることを検証
    tree = parse_expression("a ** 0.5")
    r = evaluate_node(tree, {"a": np.array([4.0, -4.0])})
    assert r[0] == pytest.approx(2.0) and np.isnan(r[1])


# --- #9 単一負値の範囲 ---

def test_single_negative_value_valid_range():
    s = _settings()
    p = _param(unit="dimensionless")
    ev = _ev(extracted_value=-0.5, evidence_score=80.0, unit="dimensionless")
    fuse_evidence(p, [ev], s)
    assert p.low is not None and p.high is not None and p.low <= p.central <= p.high
    # 分布が構築できる(low<=high)
    build_ppf(p)


# --- #10 config int env ---

def test_bad_int_env_falls_back(monkeypatch):
    monkeypatch.setenv("FERMISCOPE_MC_ITERATIONS", "20k")
    s = load_settings()  # 例外を投げない
    assert s.simulation.iterations > 0


# --- url_guard 混在16進 ---

@pytest.mark.parametrize("url", [
    "http://0x7f.0.0.1/", "http://0x7f000001/", "http://2130706433/", "http://127.1/",
])
def test_numeric_host_variants_rejected(url):
    with pytest.raises(UrlGuardError):
        validate_url(url, skip_dns=True)


def test_normal_hostname_with_hex_labels_allowed():
    # cafe.com のような正当なドメインは拒否しない
    assert validate_url("https://cafe.com/", skip_dns=True)


# --- sanitizer 未閉鎖タグ ---

def test_unclosed_style_does_not_drop_content():
    html = "<p>重要な値は10.4%</p><style>.x{color:red}"  # style未閉鎖
    text = strip_html_to_text(html)
    assert "10.4%" in text
    assert "color:red" not in text


def test_display_sanitize_removes_script_keeps_allowed():
    out = sanitize_html('<p onclick="x()">安全<b>強調</b></p><script>bad()</script>')
    assert "onclick" not in out and "script" not in out
    assert "<b>強調</b>" in out


# --- CSV インジェクション ---

@pytest.mark.parametrize("cell", ["=HYPERLINK(1)", "+1+2", "-2+3", "@SUM(A1)"])
def test_csv_formula_prefixed(cell):
    assert _csv_safe(cell).startswith("'")


def test_csv_normal_untouched():
    assert _csv_safe("東京都") == "東京都"
    assert _csv_safe(123) == 123


# --- parse_year(既存整合) ---

def test_parse_year_still_ok():
    assert parse_year("2010年度") == 2010
